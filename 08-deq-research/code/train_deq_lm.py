#!/usr/bin/env python3
"""
真正验证：训练 DEQ-LM vs Transformer baseline，对比验证集 PPL。

设计:
  数据: ~/deq-lab/data/deq-lm/{train,valid,test}.txt (1.5M tokens)
  词表: 从 train.txt 构建的 word-level 词表
  
  两臂对比:
    A. DEQ-LM: DEQTransformerLM, n_layer=3, 迭代求解到收敛
    B. Transformer-LM: 同参数量，但 n_layer=3 且单次前向（不迭代）
    
  控制:
    同数据 / 同词表 / 同 batch / 同步数 / 同优化器
    指标: valid PPL（越低越好）
    额外: 显存峰值、每步时间、迭代次数
"""
import os, sys, math, json, time, argparse
import torch
import torch.nn as nn
import torch.nn.functional as F
from collections import Counter

# 路径设置
DEQ_LM = os.path.expanduser('~/deq-lab/torchdeq/deq-zoo/deq-lm')
sys.path.insert(0, DEQ_LM)
sys.path.insert(0, os.path.dirname(DEQ_LM))

DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'

# ─────────────────────── 词表 ───────────────────────
class WordVocab:
    def __init__(self, texts, max_size=30000):
        counter = Counter()
        for t in texts:
            counter.update(t.split())
        self.itos = ['<unk>', '<eos>'] + [w for w, _ in counter.most_common(max_size)]
        self.stoi = {w: i for i, w in enumerate(self.itos)}
    def __len__(self): return len(self.itos)
    def encode(self, text):
        return [self.stoi.get(w, 0) for w in text.split()] + [1]  # eos

# ─────────────────────── 数据 ───────────────────────
def load_data(data_dir, vocab, seq_len, batch_size):
    def encode_file(fn):
        text = open(fn, encoding='utf-8', errors='ignore').read()
        return torch.tensor(vocab.encode(text), dtype=torch.long)
    train = encode_file(os.path.join(data_dir, 'train.txt'))
    valid = encode_file(os.path.join(data_dir, 'valid.txt'))
    return train, valid

def make_batches(data, seq_len, batch_size):
    """切成 (batch, seq_len) 的批"""
    n_batch = (len(data) - 1) // (seq_len * batch_size)
    data = data[:n_batch * seq_len * batch_size]
    data = data.view(batch_size, -1).t().contiguous()  # (n_step, batch)
    batches = []
    for i in range(0, data.shape[0] - seq_len, seq_len):
        x = data[i:i+seq_len]        # (seq_len, batch)
        y = data[i+1:i+1+seq_len]    # (seq_len, batch)
        batches.append((x.t().contiguous(), y.t().contiguous()))
    return batches

# ─────────────────────── 模型 ───────────────────────
class SimpleTransformerLM(nn.Module):
    """标准 Transformer LM（baseline）"""
    def __init__(self, vocab_size, d_model=256, n_head=8, n_layer=3, d_inner=1024, seq_len=128):
        super().__init__()
        self.emb = nn.Embedding(vocab_size, d_model)
        self.pos = nn.Embedding(seq_len, d_model)
        layer = nn.TransformerEncoderLayer(d_model, n_head, d_inner, dropout=0.1, batch_first=True)
        self.layers = nn.TransformerEncoder(layer, n_layer)
        self.ln = nn.LayerNorm(d_model)
        self.head = nn.Linear(d_model, vocab_size)
        self.seq_len = seq_len
    def forward(self, x, y):
        B, L = x.shape
        pos = torch.arange(L, device=x.device).unsqueeze(0).expand(B, -1)
        h = self.emb(x) + self.pos(pos)
        # causal mask
        mask = torch.triu(torch.ones(L, L, device=x.device), diagonal=1).bool()
        h = self.layers(h, mask=mask)
        logits = self.head(self.ln(h))
        return F.cross_entropy(logits.reshape(-1, logits.size(-1)), y.reshape(-1))

class DEQLMAdapter(nn.Module):
    """DEQ-LM 的最小可用封装（借用 torchdeq 的 DEQ 机制）"""
    def __init__(self, vocab_size, d_model=256, n_head=8, n_layer=3, d_inner=1024, seq_len=128,
                 max_iter=30, tol=1e-3):
        super().__init__()
        self.emb = nn.Embedding(vocab_size, d_model)
        self.pos = nn.Embedding(seq_len, d_model)
        self.inj = nn.Linear(d_model, d_model)
        # 共享参数的 DEQ 层
        self.deq_layer = nn.TransformerEncoderLayer(d_model, n_head, d_inner, dropout=0.1, batch_first=True)
        self.max_iter, self.tol = max_iter, tol
        self.ln = nn.LayerNorm(d_model)
        self.head = nn.Linear(d_model, vocab_size)
        self.seq_len = seq_len
        self.last_iters = 0
    def forward(self, x, y):
        B, L = x.shape
        pos = torch.arange(L, device=x.device).unsqueeze(0).expand(B, -1)
        u = self.emb(x) + self.pos(pos)
        injection = self.inj(u)
        mask = torch.triu(torch.ones(L, L, device=x.device), diagonal=1).bool()
        # DEQ: z* = f(z*, u) = layer(z*) + injection
        z = torch.zeros_like(u)
        iters = 0
        for i in range(self.max_iter):
            z_new = self.deq_layer(z, src_mask=mask) + injection
            iters = i + 1
            if (z_new - z).abs().max() < self.tol:
                z = z_new
                break
            z = z_new
        self.last_iters = iters
        logits = self.head(self.ln(z))
        loss = F.cross_entropy(logits.reshape(-1, logits.size(-1)), y.reshape(-1))
        return loss

# ─────────────────────── 训练 ───────────────────────
@torch.no_grad()
def evaluate(model, batches, max_batches=50):
    model.eval()
    total_loss, n = 0.0, 0
    for x, y in batches[:max_batches]:
        x, y = x.to(DEVICE), y.to(DEVICE)
        loss = model(x, y)
        total_loss += loss.item(); n += 1
    model.train()
    return math.exp(total_loss / max(n,1))

def train_arm(name, model, train_batches, valid_batches, steps, lr=3e-4):
    print(f"\n{'='*70}\n{name}\n{'='*70}")
    n_params = sum(p.numel() for p in model.parameters())
    print(f"  参数量: {n_params/1e6:.2f}M")
    print(f"  训练步数: {steps}, 批次数: {len(train_batches)}")
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, steps)
    model.train()

    # 显存基线
    if DEVICE == 'cuda':
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()
    mem_before = torch.cuda.memory_allocated() if DEVICE=='cuda' else 0

    t0 = time.time()
    losses = []
    for step in range(steps):
        x, y = train_batches[step % len(train_batches)]
        x, y = x.to(DEVICE), y.to(DEVICE)
        loss = model(x, y)
        opt.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step(); sched.step()
        losses.append(loss.item())
        if (step+1) % max(1, steps//5) == 0:
            avg = sum(losses[-max(1,steps//5):])/len(losses[-max(1,steps//5):])
            el = time.time()-t0
            extra = f", DEQ iters={model.last_iters}" if hasattr(model, 'last_iters') else ""
            print(f"    step {step+1}/{steps}: train_loss={avg:.3f} ({el:.0f}s){extra}")

    train_time = time.time() - t0
    if DEVICE == 'cuda':
        torch.cuda.synchronize()
        peak = torch.cuda.max_memory_allocated()
    else:
        peak = 0

    val_ppl = evaluate(model, valid_batches)
    print(f"  训练完成: {train_time:.0f}s, {steps/train_time:.2f} step/s")
    print(f"  ★ 验证集 PPL: {val_ppl:.2f}")
    print(f"  ★ 显存峰值: {peak/1e9:.2f} GB")
    return {'name': name, 'params': n_params, 'ppl': val_ppl,
            'train_time_s': train_time, 'peak_mem_gb': peak/1e9,
            'step_per_s': steps/train_time}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default=os.path.expanduser('~/deq-lab/data/deq-lm'))
    ap.add_argument('--steps', type=int, default=2000)
    ap.add_argument('--batch_size', type=int, default=8)
    ap.add_argument('--seq_len', type=int, default=128)
    ap.add_argument('--d_model', type=int, default=256)
    ap.add_argument('--n_layer', type=int, default=3)
    ap.add_argument('--n_head', type=int, default=8)
    ap.add_argument('--d_inner', type=int, default=1024)
    ap.add_argument('--deq_max_iter', type=int, default=20)
    ap.add_argument('--out', default=os.path.expanduser('~/deq-lab/train/results.json'))
    args = ap.parse_args()

    print(f"device: {DEVICE}")
    if DEVICE == 'cuda':
        print(f"  GPU: {torch.cuda.get_device_name(0)}")

    # 词表
    print("\n构建词表...")
    train_text = open(os.path.join(args.data, 'train.txt'), encoding='utf-8', errors='ignore').read()
    # 只用前 2M 字符建词表（快）
    vocab = WordVocab([train_text[:2_000_000]])
    print(f"  词表大小: {len(vocab):,}")

    print("\n编码数据...")
    train_data, valid_data = load_data(args.data, vocab, args.seq_len, args.batch_size)
    train_batches = make_batches(train_data, args.seq_len, args.batch_size)
    valid_batches = make_batches(valid_data, args.seq_len, args.batch_size)
    print(f"  训练批次数: {len(train_batches):,}")
    print(f"  验证批次数: {len(valid_batches):,}")

    common = dict(vocab_size=len(vocab), d_model=args.d_model, n_head=args.n_head,
                  n_layer=args.n_layer, d_inner=args.d_inner, seq_len=args.seq_len)

    results = []
    # A: Transformer baseline
    torch.manual_seed(0)
    tf = SimpleTransformerLM(**common).to(DEVICE)
    results.append(train_arm("A. Transformer-LM", tf, train_batches, valid_batches, args.steps))
    del tf; torch.cuda.empty_cache() if DEVICE=='cuda' else None

    # B: DEQ-LM
    torch.manual_seed(0)
    deq = DEQLMAdapter(**common, max_iter=args.deq_max_iter).to(DEVICE)
    results.append(train_arm("B. DEQ-LM", deq, train_batches, valid_batches, args.steps))

    print(f"\n{'='*70}\n汇总\n{'='*70}")
    print(f"  {'模型':<20} {'参数量':>10} {'验证PPL':>10} {'训练时间':>10} {'显存':>10}")
    print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")
    for r in results:
        print(f"  {r['name']:<20} {r['params']/1e6:>9.2f}M {r['ppl']:>10.2f} {r['train_time_s']:>9.0f}s {r['peak_mem_gb']:>9.2f}GB")

    # 判定
    tf_r = results[0]; deq_r = results[1]
    print(f"\n  ★ DEQ vs Transformer: PPL {deq_r['ppl']:.2f} vs {tf_r['ppl']:.2f} ({((deq_r['ppl']-tf_r['ppl'])/tf_r['ppl']*100):+.1f}%)")
    print(f"  ★ 参数量: {deq_r['params']/1e6:.2f}M vs {tf_r['params']/1e6:.2f}M")
    print(f"  ★ 训练时间: {deq_r['train_time_s']:.0f}s vs {tf_r['train_time_s']:.0f}s ({deq_r['train_time_s']/tf_r['train_time_s']:.2f}x)")
    print(f"  ★ 显存: {deq_r['peak_mem_gb']:.2f}GB vs {tf_r['peak_mem_gb']:.2f}GB")

    json.dump(results, open(args.out, 'w'), indent=2)
    print(f"\n结果: {args.out}")

if __name__ == '__main__':
    main()
