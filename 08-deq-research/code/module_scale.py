#!/usr/bin/env python3
"""
模块尺度实验：回答"参数对模块来说是不是太大"

两个子实验:
  E1. MoE 稀疏度扫描 — 固定每专家大小，变专家数/top-k
      4专家top1 (25%激活) → 8top1 (12.5%) → 16top1 (6.25%) → 32top1 (3.1%) → 64top1 (1.6%)
      对比: 相同总参数量下的 PPL
      
  E2. 推理模块尺寸扫描 — 变 Equilibrium 块的 d_eq 和迭代数
      d_eq=64/128/256/512 (16K/65K/196K/1.2M 参数)
      迭代 15/30/60
      看"小模块+深迭代"能否弥补
"""
import os, math, json, time, argparse
import torch, torch.nn as nn, torch.nn.functional as F
from collections import Counter

DEVICE='cuda' if torch.cuda.is_available() else 'cpu'
print(f"device: {DEVICE}")

class WordVocab:
    def __init__(self, text, max_size=20000):
        c=Counter(text.split())
        self.itos=['<unk>','<eos>']+[w for w,_ in c.most_common(max_size)]
        self.stoi={w:i for i,w in enumerate(self.itos)}
    def __len__(self): return len(self.itos)
    def encode(self,t): return [self.stoi.get(w,0) for w in t.split()]+[1]

def make_batches(data,seq,bs):
    n=(len(data)-1)//(seq*bs)
    d=data[:n*seq*bs].view(bs,-1).t().contiguous()
    return [(d[i:i+seq].t().contiguous(),d[i+1:i+1+seq].t().contiguous())
            for i in range(0,d.shape[0]-seq,seq)]

# ─────── MoE（稀疏度可调）───────
class MoEFFN(nn.Module):
    def __init__(self, d_model, d_inner, n_experts, top_k):
        super().__init__()
        self.n_experts, self.top_k = n_experts, top_k
        self.router = nn.Linear(d_model, n_experts, bias=False)
        # 每个专家的大小 = d_inner，与专家数无关（便于控制总参数）
        self.experts = nn.ModuleList([
            nn.Sequential(nn.Linear(d_model, d_inner), nn.GELU(), nn.Linear(d_inner, d_model))
            for _ in range(n_experts)])
    def forward(self,x):
        B,L,D=x.shape
        logits=self.router(x)
        w,idx=logits.topk(self.top_k,dim=-1)
        w=F.softmax(w,dim=-1)
        out=torch.zeros_like(x)
        for k in range(self.top_k):
            ind=idx[...,k]; wk=w[...,k].unsqueeze(-1)
            for e in range(self.n_experts):
                m=(ind==e)
                if m.any():
                    out[m]+=wk[m]*self.experts[e](x[m])
        return out

class MoELayer(nn.Module):
    def __init__(self,d_model,n_head,d_inner,n_experts,top_k):
        super().__init__()
        self.ln1=nn.LayerNorm(d_model)
        self.attn=nn.MultiheadAttention(d_model,n_head,batch_first=True)
        self.ln2=nn.LayerNorm(d_model)
        self.moe=MoEFFN(d_model,d_inner,n_experts,top_k)
    def forward(self,x,mask=None):
        h=self.ln1(x); a,_=self.attn(h,h,h,attn_mask=mask); x=x+a
        return x+self.moe(self.ln2(x))

class MoELM(nn.Module):
    def __init__(self,vocab_size,d_model=256,n_head=8,n_layer=3,d_inner=256,
                 seq_len=128,n_experts=4,top_k=1):
        super().__init__()
        self.emb=nn.Embedding(vocab_size,d_model)
        self.pos=nn.Embedding(seq_len,d_model)
        self.layers=nn.ModuleList([MoELayer(d_model,n_head,d_inner,n_experts,top_k)
                                    for _ in range(n_layer)])
        self.ln=nn.LayerNorm(d_model)
        self.head=nn.Linear(d_model,vocab_size)
        self.n_experts, self.top_k = n_experts, top_k
    def forward(self,x,y):
        B,L=x.shape
        pos=torch.arange(L,device=x.device).unsqueeze(0).expand(B,-1)
        h=self.emb(x)+self.pos(pos)
        mask=torch.triu(torch.ones(L,L,device=x.device),diagonal=1).bool()
        for layer in self.layers: h=layer(h,mask)
        logits=self.head(self.ln(h))
        return F.cross_entropy(logits.reshape(-1,logits.size(-1)),y.reshape(-1))

# ─────── Equilibrium（尺寸可调）───────
def deq_f(z,inj,W,b): return torch.tanh(z@W+inj+b)

def anderson(f,z0,tol=1e-3,max_iter=30,m=3,beta=0.5):
    z=z0.clone(); Z=[]; Fs=[]
    for it in range(max_iter):
        fz=f(z); r=fz-z
        if r.abs().max().item()<tol: return fz,it+1
        if len(Z)>=m: Z.pop(0); Fs.pop(0)
        Z.append(z); Fs.append(r)
        if len(Z)<2:
            z=(1-beta)*z+beta*fz; continue
        try:
            dZ=torch.stack([Z[i+1]-Z[i] for i in range(len(Z)-1)])
            dF=torch.stack([Fs[i+1]-Fs[i] for i in range(len(Z)-1)])
            A=dF.reshape(len(dF),-1).T; bv=r.reshape(-1,1)
            g=torch.linalg.lstsq(A,bv).solution
            dz=dZ.reshape(len(dZ),-1).T@g
            z=fz-dz.reshape(z.shape)
        except: z=(1-beta)*z+beta*fz
    return z,max_iter

class EQBlock(nn.Module):
    def __init__(self,d_model,d_eq,max_iter):
        super().__init__()
        self.inj=nn.Linear(d_model,d_eq)
        self.W_hat=nn.Parameter(torch.randn(d_eq,d_eq)*(1.0/d_eq**0.5))
        self.b=nn.Parameter(torch.zeros(d_eq))
        self.out=nn.Linear(d_eq,d_model)
        self.max_iter=max_iter; self.last_iters=0
    def get_W(self):
        with torch.no_grad():
            v=self.W_hat[0].detach().clone(); v=v/(v.norm()+1e-12)
            for _ in range(2):
                u=self.W_hat@v; u=u/(u.norm()+1e-12)
                v=self.W_hat.T@u; v=v/(v.norm()+1e-12)
            sig=(self.W_hat@v).norm().item()
        return self.W_hat*min(1.0,0.9/(sig+1e-12))
    def forward(self,x):
        B,L,D=x.shape
        with torch.no_grad():
            inj_d=self.inj(x).detach().reshape(-1,self.inj.out_features)
            W_d=self.get_W().detach()
            f=lambda z: deq_f(z,inj_d,W_d,self.b.detach())
            z,it=anderson(f,torch.zeros_like(inj_d),tol=1e-3,max_iter=self.max_iter)
        self.last_iters=it
        inj=self.inj(x).reshape(-1,self.inj.out_features)
        z_g=deq_f(z.detach(),inj,self.get_W(),self.b)
        return x+self.out(z_g.reshape(B,L,-1))

class HeteroLM(nn.Module):
    def __init__(self,vocab_size,d_model=256,n_head=8,n_layer=3,d_inner=1024,
                 seq_len=128,d_eq=256,deq_iters=15):
        super().__init__()
        self.emb=nn.Embedding(vocab_size,d_model)
        self.pos=nn.Embedding(seq_len,d_model)
        layer=nn.TransformerEncoderLayer(d_model,n_head,d_inner,dropout=0.1,batch_first=True)
        self.layers=nn.ModuleList([layer for _ in range(n_layer)])
        self.eq=EQBlock(d_model,d_eq,deq_iters)
        self.ln=nn.LayerNorm(d_model)
        self.head=nn.Linear(d_model,vocab_size)
        self.last_iters=0
    def forward(self,x,y):
        B,L=x.shape
        pos=torch.arange(L,device=x.device).unsqueeze(0).expand(B,-1)
        h=self.emb(x)+self.pos(pos)
        mask=torch.triu(torch.ones(L,L,device=x.device),diagonal=1).bool()
        for layer in self.layers: h=layer(h,src_mask=mask)
        h=self.eq(h); self.last_iters=self.eq.last_iters
        logits=self.head(self.ln(h))
        return F.cross_entropy(logits.reshape(-1,logits.size(-1)),y.reshape(-1))

@torch.no_grad()
def evaluate(m,bs,n=25):
    m.eval(); t=0.0; c=0
    for x,y in bs[:n]:
        x,y=x.to(DEVICE),y.to(DEVICE); t+=m(x,y).item(); c+=1
    m.train(); return math.exp(t/max(c,1))

def train_arm(name,model,tr,va,steps,lr=3e-4):
    np_=sum(p.numel() for p in model.parameters())
    opt=torch.optim.AdamW(model.parameters(),lr=lr,weight_decay=0.01)
    sch=torch.optim.lr_scheduler.CosineAnnealingLR(opt,steps)
    model.train()
    t0=time.time(); losses=[]; its=[]
    for s in range(steps):
        x,y=tr[s%len(tr)]; x,y=x.to(DEVICE),y.to(DEVICE)
        loss=model(x,y); opt.zero_grad(); loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(),1.0)
        opt.step(); sch.step(); losses.append(loss.item())
        if hasattr(model,'last_iters') and model.last_iters: its.append(model.last_iters)
    tt=time.time()-t0
    ppl=evaluate(model,va)
    ai=sum(its)/len(its) if its else 0
    return {'name':name,'params':np_,'ppl':ppl,'time':tt,'avg_iters':ai}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--data',default=os.path.expanduser('~/deq-lab/data/deq-lm'))
    ap.add_argument('--steps',type=int,default=1500)
    ap.add_argument('--batch',type=int,default=16)
    ap.add_argument('--seq',type=int,default=128)
    ap.add_argument('--out',default=os.path.expanduser('~/deq-lab/train/results_module_scale.json'))
    a=ap.parse_args()

    tr_text=open(os.path.join(a.data,'train.txt'),encoding='utf-8',errors='ignore').read()
    vocab=WordVocab(tr_text[:2_000_000])
    tr=make_batches(torch.tensor(vocab.encode(tr_text),dtype=torch.long),a.seq,a.batch)
    va_text=open(os.path.join(a.data,'valid.txt'),encoding='utf-8',errors='ignore').read()
    va=make_batches(torch.tensor(vocab.encode(va_text),dtype=torch.long),a.seq,a.batch)
    print(f"词表: {len(vocab):,}, 训练批: {len(tr)}, 验证批: {len(va)}")

    R=[]

    # ═══ E1: MoE 稀疏度扫描 ═══
    print(f"\n{'#'*72}\n# E1: MoE 稀疏度扫描（每专家 d_inner=128 固定）\n{'#'*72}")
    print(f"  {'配置':<22} {'总参数':>10} {'激活率':>8} {'PPL':>9}")
    for n_exp, topk in [(4,1),(8,1),(16,1),(32,1),(64,1)]:
        torch.manual_seed(0)
        m=MoELM(len(vocab),256,8,3,128,a.seq,n_exp,topk).to(DEVICE)
        r=train_arm(f"MoE-{n_exp}exp-top{topk}",m,tr,va,a.steps)
        r['n_experts']=n_exp; r['top_k']=topk
        r['active_rate']=topk/n_exp
        R.append(r)
        print(f"  {r['name']:<22} {r['params']/1e6:>9.2f}M {r['active_rate']*100:>7.1f}% {r['ppl']:>9.2f}")
        del m; torch.cuda.empty_cache()

    # ═══ E2: Equilibrium 模块尺寸扫描 ═══
    print(f"\n{'#'*72}\n# E2: Equilibrium 模块尺寸扫描\n{'#'*72}")
    print(f"  {'配置':<24} {'EQ参数':>10} {'总参数':>10} {'PPL':>9} {'迭代':>6}")
    for d_eq in [64,128,256,512]:
        for iters in [15,60]:
            torch.manual_seed(0)
            m=HeteroLM(len(vocab),256,8,2,1024,a.seq,d_eq,iters).to(DEVICE)
            eq_p=sum(p.numel() for p in m.eq.parameters())
            r=train_arm(f"EQ d={d_eq} it={iters}",m,tr,va,a.steps)
            r['d_eq']=d_eq; r['eq_params']=eq_p
            R.append(r)
            print(f"  {r['name']:<24} {eq_p/1e6:>9.3f}M {r['params']/1e6:>9.2f}M {r['ppl']:>9.2f} {r['avg_iters']:>6.1f}")
            del m; torch.cuda.empty_cache()

    json.dump(R,open(a.out,'w'),indent=2)
    print(f"\n结果: {a.out}")

if __name__=='__main__': main()
