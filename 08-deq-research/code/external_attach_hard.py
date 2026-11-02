#!/usr/bin/env python3
"""
外挂 DEQ 验证 v2：用真正需要迭代的困难任务

任务设计（按"需要多少步"排序）:
  T1. 多步模加（10 个数 mod 7）      — 需要累积，但可并行
  T2. 模幂（a^b mod 7）              — 需要 log(b) 步迭代
  T3. 大数加法（4 位数，检验进位传播）— 串行依赖
  T4. 嵌套括号（((a+b)*c-d) mod 7）   — 语法树深度

为什么这些任务更合适:
  - 前面 mod 7 的两数加法，Qwen 的 hidden state 已线性可分（100%）
  - 这些任务需要模型真正"算"，不是"查表"

控制:
  base 完全冻结，只训探针
  三臂同参数量级: 线性(6K) / MLP(1.6M) / DEQ(1.6M)
  多 seed 取均值
"""
import torch, torch.nn as nn, torch.nn.functional as F
import random, json, time

DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
print(f"device: {DEVICE}")

# ─────────────────────── 任务生成 ───────────────────────
def gen_T1(n, mod=7, k=10, seed=0):
    """多步模加: (a1+...+ak) mod 7"""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        nums = [rng.randint(0, mod-1) for _ in range(k)]
        out.append((" + ".join(map(str, nums)) + " = ", sum(nums) % mod))
    return out

def gen_T2(n, mod=7, seed=0):
    """模幂: a^b mod 7"""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        a, b = rng.randint(2, 6), rng.randint(2, 6)
        out.append((f"{a} ^ {b} mod 7 = ", pow(a, b, mod)))
    return out

def gen_T3(n, seed=0):
    """4 位数加法（标签=模 7 的余数，避免超大类别）"""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        a = rng.randint(1000, 9999)
        b = rng.randint(1000, 9999)
        out.append((f"{a} + {b} = ", (a+b) % 7))
    return out

def gen_T4(n, mod=7, seed=0):
    """嵌套括号: ((a+b)*c-d) mod 7"""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        a, b, c, dd = [rng.randint(0, 6) for _ in range(4)]
        out.append((f"(({a} + {b}) * {c} - {dd}) mod 7 = ", ((a+b)*c-dd) % mod))
    return out

# ─────────────────────── DEQ 层 ───────────────────────
def deq_f(z, x, W, b, U):
    return torch.tanh(z @ W + x @ U + b)

class DEQFn(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, W, b, U, tol, max_iter):
        z = torch.zeros(x.size(0), W.size(0), device=x.device, dtype=x.dtype)
        iters = 0
        for i in range(max_iter):
            z_new = deq_f(z, x, W, b, U)
            iters = i+1
            if (z_new - z).abs().max() < tol:
                z = z_new; break
            z = z_new
        ctx.save_for_backward(z, x, W, b, U)
        ctx.tol, ctx.max_iter = tol, max_iter
        return z
    @staticmethod
    def backward(ctx, grad_z):
        z, x, W, b, U = ctx.saved_tensors
        def fT(v):
            # J^T v = W @ ((1-z²) ⊙ v)
            # 推导：J_{ij} = (1-z_i²) W_{j,i}
            #      (J^T v)_j = Σ_i J_{ij} v_i = Σ_i W_{j,i} [(1-z_i²) v_i]
            #                = (W @ ((1-z²)⊙v))_j
            return grad_z + ((1 - z**2) * v) @ W.T
        v = grad_z.clone()
        for _ in range(ctx.max_iter):
            v_new = fT(v)
            if (v_new - v).abs().max() < ctx.tol: v = v_new; break
            v = v_new
        dz = (1 - z**2) * v
        return None, z.T @ dz, dz.sum(0), x.T @ dz, None, None

class DEQProbe(nn.Module):
    def __init__(self, d_in, d_eq, n_cls, tol=1e-3, max_iter=40):
        super().__init__()
        self.U = nn.Parameter(torch.randn(d_in, d_eq) * (1.0/d_in**0.5))
        self.W = nn.Parameter(torch.randn(d_eq, d_eq) * (0.5/d_eq**0.5))  # 收缩因子
        self.b = nn.Parameter(torch.zeros(d_eq))
        self.head = nn.Linear(d_eq, n_cls)
        self.tol, self.max_iter = tol, max_iter
    def forward(self, x):
        z = DEQFn.apply(x, self.W, self.b, self.U, self.tol, self.max_iter)
        return self.head(z)

class MLPProbe(nn.Module):
    def __init__(self, d_in, d_hid, n_cls):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(d_in, d_hid), nn.GELU(),
            nn.Linear(d_hid, d_hid), nn.GELU(),
            nn.Linear(d_hid, n_cls))
    def forward(self, x): return self.net(x)

class LinearProbe(nn.Module):
    def __init__(self, d_in, n_cls):
        super().__init__()
        self.fc = nn.Linear(d_in, n_cls)
    def forward(self, x): return self.fc(x)

@torch.no_grad()
def extract_hidden(model, tok, prompts, batch_size=64):
    """分批提取最后层 hidden state（避免 lm_head 的 OOM）"""
    all_h = []
    for i in range(0, len(prompts), batch_size):
        chunk = prompts[i:i+batch_size]
        enc = tok(chunk, return_tensors='pt', padding=True, truncation=True, max_length=128)
        enc = {k: v.to(DEVICE) for k, v in enc.items()}
        # num_logits_to_keep=1 让 lm_head 只算最后一个 token → 省 50x 显存
        out = model(**enc, output_hidden_states=True, num_logits_to_keep=1)
        lh = out.hidden_states[-1]
        mask = enc['attention_mask']
        idx = mask.sum(1) - 1
        all_h.append(lh[torch.arange(lh.size(0)), idx].float().cpu())
    return torch.cat(all_h, 0).to(DEVICE)

def train_eval(probe, h_tr, y_tr, h_te, y_te, steps=1200, lr=1e-3):
    opt = torch.optim.Adam(probe.parameters(), lr=lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, steps)
    n = len(y_tr)
    for step in range(steps):
        idx = torch.randint(0, n, (64,))
        logits = probe(h_tr[idx])
        loss = F.cross_entropy(logits, y_tr[idx])
        opt.zero_grad(); loss.backward()
        torch.nn.utils.clip_grad_norm_(probe.parameters(), 1.0)
        opt.step(); sched.step()
    with torch.no_grad():
        acc = (probe(h_te).argmax(1) == y_te).float().mean().item()
    return acc

def run_task(name, gen_fn, model, tok, seeds=(0,1,2)):
    print(f"\n{'='*70}\n任务: {name}\n{'='*70}")
    train = gen_fn(3000, seed=1)
    test = gen_fn(600, seed=2)
    prompts_tr = [p for p, _ in train]
    prompts_te = [p for p, _ in test]
    y_tr = torch.tensor([y for _, y in train], device=DEVICE)
    y_te = torch.tensor([y for _, y in test], device=DEVICE)

    print(f"  训练 {len(train)}, 测试 {len(test)}, 类别 7")
    t0 = time.time()
    h_tr = extract_hidden(model, tok, prompts_tr)
    h_te = extract_hidden(model, tok, prompts_te)
    mu, sd = h_tr.mean(0, keepdim=True), h_tr.std(0, keepdim=True) + 1e-6
    h_tr, h_te = (h_tr-mu)/sd, (h_te-mu)/sd
    D = h_tr.shape[1]
    print(f"  hidden: {h_tr.shape} ({time.time()-t0:.1f}s)")

    res = {}
    for arm, mk in [("linear", lambda: LinearProbe(D,7)),
                    ("mlp",    lambda: MLPProbe(D,896,7)),
                    ("deq",    lambda: DEQProbe(D,896,7))]:
        accs = []
        for s in seeds:
            torch.manual_seed(s)
            p = mk().to(DEVICE)
            accs.append(train_eval(p, h_tr, y_tr, h_te, y_te))
        n_p = sum(x.numel() for x in mk().parameters())
        mean = sum(accs)/len(accs)
        std = (sum((a-mean)**2 for a in accs)/len(accs))**0.5
        print(f"  {arm:8s}: {mean:.3f} ± {std:.3f}  ({', '.join(f'{a:.3f}' for a in accs)})  params={n_p:,}")
        res[arm] = {'acc': mean, 'std': std, 'accs': accs, 'params': n_p}

    res['random_baseline'] = 1/7
    return res

def main():
    from transformers import AutoModelForCausalLM, AutoTokenizer
    MPATH = '/home/assion/deepseek-xdna-hardware-analysis/models/qwen2.5-0.5b-instruct'
    tok = AutoTokenizer.from_pretrained(MPATH)
    model = AutoModelForCausalLM.from_pretrained(MPATH, dtype=torch.float32).to(DEVICE).eval()
    for p in model.parameters(): p.requires_grad = False
    print(f"模型: {sum(p.numel() for p in model.parameters())/1e6:.1f}M（冻结）")

    all_res = {}
    for name, fn in [
        ("T1 多步模加(10项 mod 7)", lambda n, seed: gen_T1(n, seed=seed)),
        ("T2 模幂(a^b mod 7)",      lambda n, seed: gen_T2(n, seed=seed)),
        ("T3 四位数加法(mod 7)",     lambda n, seed: gen_T3(n, seed=seed)),
        ("T4 嵌套括号(mod 7)",       lambda n, seed: gen_T4(n, seed=seed)),
    ]:
        all_res[name] = run_task(name, fn, model, tok)

    print(f"\n{'='*70}\n汇总\n{'='*70}")
    print(f"  {'任务':<26} {'linear':>8} {'mlp':>8} {'deq':>8} {'deq-mlp':>9}")
    print(f"  {'-'*26} {'-'*8} {'-'*8} {'-'*8} {'-'*9}")
    for name, r in all_res.items():
        d_mlp = (r['deq']['acc'] - r['mlp']['acc'])*100
        print(f"  {name:<26} {r['linear']['acc']:>8.3f} {r['mlp']['acc']:>8.3f} {r['deq']['acc']:>8.3f} {d_mlp:>+8.1f}pp")

    import copy
    out = {}
    for k, r in all_res.items():
        out[k] = {}
        for a, v in r.items():
            if isinstance(v, dict):
                out[k][a] = {kk: vv for kk, vv in v.items() if kk != 'accs'}
            else:
                out[k][a] = v
    json.dump(out, open('/home/assion/deq-lab/attach/results_hard.json','w'), indent=2)
    print(f"\n结果: ~/deq-lab/attach/results_hard.json")

if __name__ == '__main__':
    main()
