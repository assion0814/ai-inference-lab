#!/usr/bin/env python3
"""DEQ-LM v3：加 spectral normalization（DEQ 论文的关键技巧）"""
import os, math, json, time, argparse
import torch, torch.nn as nn, torch.nn.functional as F
from collections import Counter

DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
print(f"device: {DEVICE}")

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

# ───── 带 spectral norm 的 DEQ 层 ─────
class SpectralW(nn.Module):
    """权重归一化：W = W_hat * min(1, target/||W_hat||_2)，保证谱半径 <= target"""
    def __init__(self, d, target_norm=1.0):
        super().__init__()
        self.d, self.target = d, target_norm
        self.W_hat = nn.Parameter(torch.randn(d, d) * (1.0/d**0.5))
    def forward(self):
        # 幂迭代估计谱范数（1 次足够）
        with torch.no_grad():
            v = self.W_hat[0].detach().clone()
            v = v / (v.norm()+1e-12)
            for _ in range(2):
                u = self.W_hat @ v
                u = u / (u.norm()+1e-12)
                v = self.W_hat.T @ u
                v = v / (v.norm()+1e-12)
            sigma = (self.W_hat @ v).norm().item()
        scale = min(1.0, self.target / (sigma+1e-12))
        return self.W_hat * scale

def deq_f(z, inj, W, b, U):
    return torch.tanh(z @ W + inj @ U + b)

class DEQImplicit(torch.autograd.Function):
    @staticmethod
    def forward(ctx, inj, W, b, U, tol, max_iter):
        with torch.no_grad():
            z = torch.zeros(inj.size(0), W.size(0), device=inj.device, dtype=inj.dtype)
            iters = 0
            for i in range(max_iter):
                z_new = deq_f(z, inj, W, b, U)
                iters = i+1
                if (z_new-z).abs().max() < tol: z = z_new; break
                z = z_new
        ctx.save_for_backward(z, inj, W, b, U)
        ctx.tol, ctx.max_iter = tol, max_iter
        ctx.iters = iters
        return z
    @staticmethod
    def backward(ctx, grad_z):
        z, inj, W, b, U = ctx.saved_tensors
        def fT(v): return grad_z + ((1-z**2)*v) @ W.T
        v = grad_z.clone()
        for _ in range(ctx.max_iter):
            v_new = fT(v)
            if (v_new-v).abs().max() < ctx.tol: v = v_new; break
            v = v_new
        dz = (1-z**2)*v
        return None, z.T @ dz, dz.sum(0), inj.T @ dz, None, None

class DEQLM(nn.Module):
    def __init__(self, vocab_size, d_model=256, d_eq=512, seq_len=128,
                 max_iter=25, tol=1e-4, implicit=True, use_wn=True, target=0.9):
        super().__init__()
        self.emb = nn.Embedding(vocab_size, d_model)
        self.pos = nn.Embedding(seq_len, d_model)
        self.inj = nn.Linear(d_model, d_eq)
        self.wn = SpectralW(d_eq, target) if use_wn else None
        if not use_wn:
            self.W = nn.Parameter(torch.randn(d_eq,d_eq) * (0.5/d_eq**0.5))
        self.b = nn.Parameter(torch.zeros(d_eq))
        self.U = nn.Parameter(torch.randn(d_eq,d_eq) * (1.0/d_eq**0.5))
        self.ln = nn.LayerNorm(d_eq)
        self.head = nn.Linear(d_eq, vocab_size)
        self.max_iter, self.tol, self.implicit = max_iter, tol, implicit
        self.last_iters = 0
        self.last_sigma = 0.0
    def get_W(self):
        if self.wn is not None:
            with torch.no_grad():
                v = self.wn.W_hat[0].detach().clone()
                v = v/(v.norm()+1e-12)
                for _ in range(2):
                    u = self.wn.W_hat @ v; u = u/(u.norm()+1e-12)
                    v = self.wn.W_hat.T @ u; v = v/(v.norm()+1e-12)
                self.last_sigma = (self.wn.W_hat @ v).norm().item()
            return self.wn()
        return self.W
    def forward(self, x, y):
        B,L = x.shape
        pos = torch.arange(L, device=x.device).unsqueeze(0).expand(B,-1)
        u = self.emb(x) + self.pos(pos)
        injection = self.inj(u)
        W = self.get_W()
        if self.implicit:
            inj_flat = injection.reshape(-1, injection.size(-1))
            z = DEQImplicit.apply(inj_flat, W, self.b, self.U, self.tol, self.max_iter)
            z = z.reshape(B,L,-1)
            self.last_iters = self.max_iter
        else:
            z = torch.zeros_like(injection)
            for i in range(self.max_iter):
                z_new = deq_f(z, injection, W, self.b, self.U)
                self.last_iters = i+1
                if (z_new-z).abs().max() < self.tol: z=z_new; break
                z = z_new
        logits = self.head(self.ln(z))
        return F.cross_entropy(logits.reshape(-1,logits.size(-1)), y.reshape(-1))

class SimpleTransformerLM(nn.Module):
    def __init__(self, vocab_size, d_model=256, n_head=8, n_layer=3, d_inner=1024, seq_len=128):
        super().__init__()
        self.emb = nn.Embedding(vocab_size, d_model)
        self.pos = nn.Embedding(seq_len, d_model)
        layer = nn.TransformerEncoderLayer(d_model, n_head, d_inner, dropout=0.1, batch_first=True)
        self.layers = nn.TransformerEncoder(layer, n_layer)
        self.ln = nn.LayerNorm(d_model)
        self.head = nn.Linear(d_model, vocab_size)
    def forward(self,x,y):
        B,L = x.shape
        pos = torch.arange(L,device=x.device).unsqueeze(0).expand(B,-1)
        h = self.emb(x)+self.pos(pos)
        mask = torch.triu(torch.ones(L,L,device=x.device),diagonal=1).bool()
        h = self.layers(h, mask=mask)
        logits = self.head(self.ln(h))
        return F.cross_entropy(logits.reshape(-1,logits.size(-1)), y.reshape(-1))

@torch.no_grad()
def evaluate(m, bs, n=30):
    m.eval(); tot=0.0; c=0
    for x,y in bs[:n]:
        x,y=x.to(DEVICE),y.to(DEVICE)
        tot+=m(x,y).item(); c+=1
    m.train(); return math.exp(tot/max(c,1))

def train_arm(name, model, tr, va, steps, lr=3e-4):
    print(f"\n{'='*70}\n{name}\n{'='*70}")
    np_=sum(p.numel() for p in model.parameters())
    print(f"  参数量: {np_/1e6:.2f}M")
    opt=torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    sch=torch.optim.lr_scheduler.CosineAnnealingLR(opt, steps)
    model.train()
    if DEVICE=='cuda': torch.cuda.reset_peak_memory_stats(); torch.cuda.synchronize()
    t0=time.time(); losses=[]
    for step in range(steps):
        x,y=tr[step%len(tr)]; x,y=x.to(DEVICE),y.to(DEVICE)
        loss=model(x,y)
        opt.zero_grad(); loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(),1.0)
        opt.step(); sch.step(); losses.append(loss.item())
        if (step+1)%max(1,steps//5)==0:
            k=max(1,steps//5); el=time.time()-t0
            extra=""
            if hasattr(model,'last_iters'): extra=f", it={model.last_iters}"
            if hasattr(model,'last_sigma') and model.last_sigma>0: extra+=f", σ={model.last_sigma:.2f}"
            print(f"    {step+1}/{steps}: loss={sum(losses[-k:])/len(losses[-k:]):.3f} ({el:.0f}s){extra}")
    tt=time.time()-t0
    if DEVICE=='cuda': torch.cuda.synchronize(); peak=torch.cuda.max_memory_allocated()
    else: peak=0
    ppl=evaluate(model,va)
    print(f"  ★ PPL={ppl:.2f}, 时间={tt:.0f}s, 显存={peak/1e9:.2f}GB")
    return {'name':name,'params':np_,'ppl':ppl,'time':tt,'mem_gb':peak/1e9}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--data', default=os.path.expanduser('~/deq-lab/data/deq-lm'))
    ap.add_argument('--steps',type=int,default=3000)
    ap.add_argument('--batch',type=int,default=16)
    ap.add_argument('--seq',type=int,default=128)
    ap.add_argument('--d_model',type=int,default=256)
    ap.add_argument('--d_eq',type=int,default=512)
    ap.add_argument('--deq_iters',type=int,default=25)
    ap.add_argument('--target',type=float,default=0.9)
    ap.add_argument('--out',default=os.path.expanduser('~/deq-lab/train/results_wn.json'))
    a=ap.parse_args()

    tr_text=open(os.path.join(a.data,'train.txt'),encoding='utf-8',errors='ignore').read()
    vocab=WordVocab(tr_text[:2_000_000])
    print(f"\n词表: {len(vocab):,}")
    tr=make_batches(torch.tensor(vocab.encode(tr_text),dtype=torch.long),a.seq,a.batch)
    va_text=open(os.path.join(a.data,'valid.txt'),encoding='utf-8',errors='ignore').read()
    va=make_batches(torch.tensor(vocab.encode(va_text),dtype=torch.long),a.seq,a.batch)
    print(f"训练批: {len(tr)}, 验证批: {len(va)}")

    R=[]
    torch.manual_seed(0)
    R.append(train_arm("A. Transformer-3层", SimpleTransformerLM(len(vocab),a.d_model,8,3,1024,a.seq).to(DEVICE),tr,va,a.steps))
    torch.cuda.empty_cache()
    torch.manual_seed(0)
    R.append(train_arm("B. DEQ-implicit (无WN)", DEQLM(len(vocab),a.d_model,a.d_eq,a.seq,a.deq_iters,implicit=True,use_wn=False).to(DEVICE),tr,va,a.steps))
    torch.cuda.empty_cache()
    torch.manual_seed(0)
    R.append(train_arm(f"C. DEQ-implicit +WN(σ≤{a.target})", DEQLM(len(vocab),a.d_model,a.d_eq,a.seq,a.deq_iters,implicit=True,use_wn=True,target=a.target).to(DEVICE),tr,va,a.steps))
    torch.cuda.empty_cache()
    torch.manual_seed(0)
    R.append(train_arm("D. DEQ-unrolled +WN", DEQLM(len(vocab),a.d_model,a.d_eq,a.seq,a.deq_iters,implicit=False,use_wn=True,target=a.target).to(DEVICE),tr,va,a.steps))

    print(f"\n{'='*70}\n汇总\n{'='*70}")
    print(f"  {'模型':<28} {'参数':>9} {'PPL':>9} {'时间':>8} {'显存':>9}")
    print(f"  {'-'*28} {'-'*9} {'-'*9} {'-'*8} {'-'*9}")
    for r in R:
        print(f"  {r['name']:<28} {r['params']/1e6:>8.2f}M {r['ppl']:>9.2f} {r['time']:>7.0f}s {r['mem_gb']:>8.2f}GB")
    tf=R[0]
    print()
    for r in R[1:]:
        print(f"  {r['name']}: PPL {((r['ppl']-tf['ppl'])/tf['ppl']*100):+.1f}%  时间 {r['time']/tf['time']:.2f}x  显存 {r['mem_gb']/tf['mem_gb']:.2f}x")
    json.dump(R, open(a.out,'w'), indent=2)
    print(f"\n结果: {a.out}")

if __name__=='__main__': main()
