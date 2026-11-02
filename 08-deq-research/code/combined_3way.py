#!/usr/bin/env python3
"""
三者结合验证：4 臂消融实验

用户框架:
  方案三 异构架构 = 大 Transformer（吸收知识）+ 小 Equilibrium 模块（迭代寻优）
  方案一 MoE     = 稀疏专家，降低带宽
  方案二 数值阻尼 = Anderson 加速 + 阻尼系数，保证求解器稳定

四臂:
  A. Baseline:        Transformer only
  B. +DEQ:            Transformer(2层) + Equilibrium 模块（方案三）
  C. +MoE:            Transformer(3层) 但 FFN 换成 MoE（方案一）
  D. +ALL:            Transformer(2层) + MoE + Equilibrium(Anderson+damping)（三+一+二）

控制:
  同数据/同词表/同 batch/同步数/同优化器
  参数量尽量接近（报告实际差异）
  多指标: PPL / 时间 / 显存 / 求解器迭代数 / 收敛性
"""
import os, math, json, time, argparse
import torch, torch.nn as nn, torch.nn.functional as F
from collections import Counter

DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
print(f"device: {DEVICE}")
if DEVICE == 'cuda': print(f"  GPU: {torch.cuda.get_device_name(0)}")

# ─────────── 词表/数据 ───────────
class WordVocab:
    def __init__(self, text, max_size=20000):
        c = Counter(text.split())
        self.itos = ['<unk>','<eos>'] + [w for w,_ in c.most_common(max_size)]
        self.stoi = {w:i for i,w in enumerate(self.itos)}
    def __len__(self): return len(self.itos)
    def encode(self,t): return [self.stoi.get(w,0) for w in t.split()] + [1]

def make_batches(data, seq, bs):
    n=(len(data)-1)//(seq*bs)
    d=data[:n*seq*bs].view(bs,-1).t().contiguous()
    return [(d[i:i+seq].t().contiguous(), d[i+1:i+1+seq].t().contiguous())
            for i in range(0,d.shape[0]-seq,seq)]

# ─────────── 方案一: MoE FFN ───────────
class MoEFFN(nn.Module):
    """稀疏专家 FFN: N 个专家, top-k 路由"""
    def __init__(self, d_model, d_inner, n_experts=4, top_k=1):
        super().__init__()
        self.n_experts, self.top_k = n_experts, top_k
        self.router = nn.Linear(d_model, n_experts, bias=False)
        self.experts = nn.ModuleList([
            nn.Sequential(nn.Linear(d_model, d_inner), nn.GELU(), nn.Linear(d_inner, d_model))
            for _ in range(n_experts)
        ])
    def forward(self, x):
        B, L, D = x.shape
        logits = self.router(x)                    # (B,L,n_exp)
        weights, indices = logits.topk(self.top_k, dim=-1)   # (B,L,k)
        weights = F.softmax(weights, dim=-1)
        out = torch.zeros_like(x)
        for k in range(self.top_k):
            idx = indices[..., k]                   # (B,L)
            w = weights[..., k].unsqueeze(-1)       # (B,L,1)
            for e in range(self.n_experts):
                mask = (idx == e)
                if mask.any():
                    # 收集该专家处理的 token
                    sel = x[mask]                   # (N_tok, D)
                    y = self.experts[e](sel)
                    # 放回
                    out[mask] += w[mask] * y
        return out

class MoELayer(nn.Module):
    """Transformer 层 + MoE FFN"""
    def __init__(self, d_model, n_head, d_inner, n_experts=4, top_k=1):
        super().__init__()
        self.ln1 = nn.LayerNorm(d_model)
        self.attn = nn.MultiheadAttention(d_model, n_head, batch_first=True)
        self.ln2 = nn.LayerNorm(d_model)
        self.moe = MoEFFN(d_model, d_inner, n_experts, top_k)
    def forward(self, x, mask=None):
        h = self.ln1(x)
        a, _ = self.attn(h, h, h, attn_mask=mask)
        x = x + a
        x = x + self.moe(self.ln2(x))
        return x

class MoETransformerLM(nn.Module):
    def __init__(self, vocab_size, d_model=256, n_head=8, n_layer=3, d_inner=1024,
                 seq_len=128, n_experts=4, top_k=1):
        super().__init__()
        self.emb = nn.Embedding(vocab_size, d_model)
        self.pos = nn.Embedding(seq_len, d_model)
        self.layers = nn.ModuleList([MoELayer(d_model, n_head, d_inner, n_experts, top_k)
                                      for _ in range(n_layer)])
        self.ln = nn.LayerNorm(d_model)
        self.head = nn.Linear(d_model, vocab_size)
        self.n_routed = 0
        self.n_total = 0
    def forward(self, x, y):
        B,L = x.shape
        pos = torch.arange(L,device=x.device).unsqueeze(0).expand(B,-1)
        h = self.emb(x)+self.pos(pos)
        mask = torch.triu(torch.ones(L,L,device=x.device),diagonal=1).bool()
        for layer in self.layers:
            h = layer(h, mask)
        logits = self.head(self.ln(h))
        return F.cross_entropy(logits.reshape(-1,logits.size(-1)), y.reshape(-1))

# ─────────── 方案三+二: Equilibrium 模块（Anderson + damping）───────────
def deq_f(z, inj, W, b):
    return torch.tanh(z @ W + inj + b)

def anderson_solve(f, z0, tol=1e-3, max_iter=20, m=3, beta=0.5):
    """Anderson 加速 + 阻尼 beta（方案二）"""
    z = z0.clone()
    Z, F_ = [], []
    for it in range(max_iter):
        fz = f(z)
        r = fz - z
        rnorm = r.abs().max().item()
        if rnorm < tol: return fz, it+1, rnorm
        if len(Z) >= m: Z.pop(0); F_.pop(0)
        Z.append(z); F_.append(r)
        if len(Z) < 2:
            z = (1-beta)*z + beta*fz              # 阻尼
            continue
        try:
            dZ = torch.stack([Z[i+1]-Z[i] for i in range(len(Z)-1)])
            dF = torch.stack([F_[i+1]-F_[i] for i in range(len(Z)-1)])
            A = dF.reshape(len(dF), -1).T
            bvec = r.reshape(-1,1)
            gamma = torch.linalg.lstsq(A, bvec).solution
            dz = dZ.reshape(len(dZ),-1).T @ gamma
            z = fz - dz.reshape(z.shape)
        except Exception:
            z = (1-beta)*z + beta*fz
    return z, max_iter, rnorm

class EquilibriumBlock(nn.Module):
    """DEQ 均衡模块（方案三）+ Anderson/damping（方案二）"""
    def __init__(self, d_model, d_eq=None, max_iter=15, tol=1e-3, use_anderson=True,
                 damping=0.5, use_spec_norm=True):
        super().__init__()
        d_eq = d_eq or d_model
        self.inj = nn.Linear(d_model, d_eq)
        self.W_hat = nn.Parameter(torch.randn(d_eq, d_eq) * (1.0/d_eq**0.5))
        self.b = nn.Parameter(torch.zeros(d_eq))
        self.out = nn.Linear(d_eq, d_model)
        self.max_iter, self.tol = max_iter, tol
        self.use_anderson, self.damping, self.use_spec_norm = use_anderson, damping, use_spec_norm
        self.last_iters, self.last_resid = 0, 0.0
    def get_W(self):
        if not self.use_spec_norm: return self.W_hat
        with torch.no_grad():
            v = self.W_hat[0].detach().clone(); v = v/(v.norm()+1e-12)
            for _ in range(2):
                u = self.W_hat @ v; u = u/(u.norm()+1e-12)
                v = self.W_hat.T @ u; v = v/(v.norm()+1e-12)
            sigma = (self.W_hat @ v).norm().item()
        scale = min(1.0, 0.9/(sigma+1e-12))
        return self.W_hat * scale
    def forward(self, x):
        B,L,D = x.shape
        # ★ 关键：求解器用 no_grad（不建图，省显存），不动点处单步重建图
        # 这是 standard DEQ 做法；图只覆盖 1 步，不是 max_iter 步
        with torch.no_grad():
            inj_det = self.inj(x).detach().reshape(-1, self.inj.out_features)
            W_det = self.get_W().detach()
            f = lambda z: deq_f(z, inj_det, W_det, self.b.detach())
            z0 = torch.zeros_like(inj_det)
            if self.use_anderson:
                z, iters, resid = anderson_solve(f, z0, self.tol, self.max_iter, m=3, beta=self.damping)
            else:
                z = z0
                for i in range(self.max_iter):
                    z_new = (1-self.damping)*z + self.damping*f(z)
                    iters = i+1
                    resid = (z_new-z).abs().max().item()
                    z = z_new
                    if resid < self.tol: break
        self.last_iters, self.last_resid = iters, resid
        # 不动点处重建单步图（梯度正确且省显存）
        inj = self.inj(x).reshape(-1, self.inj.out_features)
        W = self.get_W()
        z_g = deq_f(z.detach(), inj, W, self.b)
        return x + self.out(z_g.reshape(B,L,-1))

class HeterogeneousLM(nn.Module):
    """方案三: 大 Transformer（吸收知识）+ 小 Equilibrium（迭代寻优）
       方案一: 可选 MoE FFN
       方案二: Equilibrium 的 Anderson + damping
    """
    def __init__(self, vocab_size, d_model=256, n_head=8, n_layer=2, d_inner=1024,
                 seq_len=128, use_deq=True, use_moe=False, n_experts=4, top_k=1,
                 deq_max_iter=15, use_anderson=True, damping=0.5):
        super().__init__()
        self.emb = nn.Embedding(vocab_size, d_model)
        self.pos = nn.Embedding(seq_len, d_model)
        if use_moe:
            self.layers = nn.ModuleList([MoELayer(d_model, n_head, d_inner, n_experts, top_k)
                                          for _ in range(n_layer)])
        else:
            layer = nn.TransformerEncoderLayer(d_model, n_head, d_inner, dropout=0.1, batch_first=True)
            self.layers = nn.ModuleList([layer for _ in range(n_layer)])
        self.use_moe = use_moe
        self.use_deq = use_deq
        if use_deq:
            self.eq = EquilibriumBlock(d_model, max_iter=deq_max_iter, use_anderson=use_anderson, damping=damping)
        self.ln = nn.LayerNorm(d_model)
        self.head = nn.Linear(d_model, vocab_size)
        self.last_iters = 0
    def forward(self, x, y):
        B,L = x.shape
        pos = torch.arange(L,device=x.device).unsqueeze(0).expand(B,-1)
        h = self.emb(x)+self.pos(pos)
        mask = torch.triu(torch.ones(L,L,device=x.device),diagonal=1).bool()
        for layer in self.layers:
            if self.use_moe: h = layer(h, mask)
            else: h = layer(h, src_mask=mask)
        if self.use_deq:
            h = self.eq(h)
            self.last_iters = self.eq.last_iters
        logits = self.head(self.ln(h))
        return F.cross_entropy(logits.reshape(-1,logits.size(-1)), y.reshape(-1))

# ─────────── 训练/评估 ───────────
@torch.no_grad()
def evaluate(m, bs, n=30):
    m.eval(); tot=0.0; c=0
    for x,y in bs[:n]:
        x,y=x.to(DEVICE),y.to(DEVICE)
        tot+=m(x,y).item(); c+=1
    m.train(); return math.exp(tot/max(c,1))

def train_arm(name, model, tr, va, steps, lr=3e-4):
    print(f"\n{'='*72}\n{name}\n{'='*72}")
    np_=sum(p.numel() for p in model.parameters())
    print(f"  参数量: {np_/1e6:.2f}M")
    opt=torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    sch=torch.optim.lr_scheduler.CosineAnnealingLR(opt, steps)
    model.train()
    if DEVICE=='cuda': torch.cuda.reset_peak_memory_stats(); torch.cuda.synchronize()
    t0=time.time(); losses=[]; iters_log=[]
    for step in range(steps):
        x,y=tr[step%len(tr)]; x,y=x.to(DEVICE),y.to(DEVICE)
        loss=model(x,y)
        opt.zero_grad(); loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(),1.0)
        opt.step(); sch.step(); losses.append(loss.item())
        if hasattr(model,'last_iters') and model.last_iters>0: iters_log.append(model.last_iters)
        if (step+1)%max(1,steps//5)==0:
            k=max(1,steps//5); el=time.time()-t0
            it = f", it={model.last_iters}" if hasattr(model,'last_iters') and model.last_iters else ""
            print(f"    {step+1}/{steps}: loss={sum(losses[-k:])/len(losses[-k:]):.3f} ({el:.0f}s){it}")
    tt=time.time()-t0
    if DEVICE=='cuda': torch.cuda.synchronize(); peak=torch.cuda.max_memory_allocated()
    else: peak=0
    ppl=evaluate(model,va)
    avg_it = sum(iters_log)/len(iters_log) if iters_log else 0
    print(f"  ★ PPL={ppl:.2f}, 时间={tt:.0f}s, 显存={peak/1e9:.2f}GB, 平均迭代={avg_it:.1f}")
    return {'name':name,'params':np_,'ppl':ppl,'time':tt,'mem_gb':peak/1e9,'avg_iters':avg_it}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--data', default=os.path.expanduser('~/deq-lab/data/deq-lm'))
    ap.add_argument('--steps',type=int,default=2000)
    ap.add_argument('--batch',type=int,default=16)
    ap.add_argument('--seq',type=int,default=128)
    ap.add_argument('--d_model',type=int,default=256)
    ap.add_argument('--d_inner',type=int,default=1024)
    ap.add_argument('--out',default=os.path.expanduser('~/deq-lab/train/results_3way.json'))
    a=ap.parse_args()

    tr_text=open(os.path.join(a.data,'train.txt'),encoding='utf-8',errors='ignore').read()
    vocab=WordVocab(tr_text[:2_000_000])
    print(f"\n词表: {len(vocab):,}")
    tr=make_batches(torch.tensor(vocab.encode(tr_text),dtype=torch.long),a.seq,a.batch)
    va_text=open(os.path.join(a.data,'valid.txt'),encoding='utf-8',errors='ignore').read()
    va=make_batches(torch.tensor(vocab.encode(va_text),dtype=torch.long),a.seq,a.batch)
    print(f"训练批: {len(tr)}, 验证批: {len(va)}")

    C = dict(vocab_size=len(vocab), d_model=a.d_model, n_head=8, d_inner=a.d_inner, seq_len=a.seq)
    R=[]

    # A. Baseline: Transformer 3 层
    torch.manual_seed(0)
    m = HeterogeneousLM(n_layer=3, use_deq=False, use_moe=False, **C).to(DEVICE)
    R.append(train_arm("A. Baseline (Transformer-3层)", m, tr, va, a.steps))
    del m; torch.cuda.empty_cache()

    # B. +DEQ（方案三 + 方案二阻尼）
    torch.manual_seed(0)
    m = HeterogeneousLM(n_layer=2, use_deq=True, use_moe=False,
                        deq_max_iter=15, use_anderson=True, damping=0.5, **C).to(DEVICE)
    R.append(train_arm("B. +Equilibrium (方案三+二)", m, tr, va, a.steps))
    del m; torch.cuda.empty_cache()

    # C. +MoE（方案一）
    torch.manual_seed(0)
    m = HeterogeneousLM(n_layer=3, use_deq=False, use_moe=True, n_experts=4, top_k=1, **C).to(DEVICE)
    R.append(train_arm("C. +MoE (方案一)", m, tr, va, a.steps))
    del m; torch.cuda.empty_cache()

    # D. 三者结合
    torch.manual_seed(0)
    m = HeterogeneousLM(n_layer=2, use_deq=True, use_moe=True, n_experts=4, top_k=1,
                        deq_max_iter=15, use_anderson=True, damping=0.5, **C).to(DEVICE)
    R.append(train_arm("D. 三者结合 (三+一+二)", m, tr, va, a.steps))

    print(f"\n{'='*72}\n汇总（{a.steps} 步, batch {a.batch}, seq {a.seq}）\n{'='*72}")
    print(f"  {'模型':<28} {'参数':>9} {'PPL':>9} {'时间':>8} {'显存':>9} {'迭代':>7}")
    print(f"  {'-'*28} {'-'*9} {'-'*9} {'-'*8} {'-'*9} {'-'*7}")
    for r in R:
        print(f"  {r['name']:<28} {r['params']/1e6:>8.2f}M {r['ppl']:>9.2f} {r['time']:>7.0f}s {r['mem_gb']:>8.2f}GB {r['avg_iters']:>6.1f}")
    base=R[0]
    print()
    for r in R[1:]:
        print(f"  {r['name']}: PPL {((r['ppl']-base['ppl'])/base['ppl']*100):+.1f}%  "
              f"时间 {r['time']/base['time']:.2f}x  显存 {r['mem_gb']/base['mem_gb']:.2f}x")
    print()
    print(f"  ★ 三者结合 vs 各自单独:")
    print(f"     三者结合 (D): {R[3]['ppl']:.2f}")
    print(f"     方案三单独 (B): {R[1]['ppl']:.2f}")
    print(f"     方案一单独 (C): {R[2]['ppl']:.2f}")
    print(f"     基线 (A):      {R[0]['ppl']:.2f}")
    json.dump(R, open(a.out,'w'), indent=2)
    print(f"\n结果: {a.out}")

if __name__=='__main__': main()
