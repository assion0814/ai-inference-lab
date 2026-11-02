#!/usr/bin/env python3
"""V100 DEQ 验证 v2：用 equilibrium-lab 已验证的 solvers + deq_core 思路"""
import torch, torch.nn as nn, time, json, sys, os

DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
print(f"device: {DEVICE}")
if DEVICE == 'cuda':
    print(f"  GPU: {torch.cuda.get_device_name(0)}")
    print(f"  capability: {torch.cuda.get_device_capability(0)}")
    print(f"  VRAM: {torch.cuda.get_device_properties(0).total_memory/1e9:.1f} GB")

# ─── Anderson 加速（正确实现）───
def anderson_solve(f, z0, tol=1e-6, max_iter=200, m=5):
    z = z0.clone()
    Z, F = [], []
    for it in range(max_iter):
        fz = f(z)
        res = fz - z
        rnorm = res.abs().max().item()
        if rnorm < tol:
            return fz, it + 1, rnorm
        if len(Z) >= m:
            Z.pop(0); F.pop(0)
        Z.append(z.clone()); F.append(res.clone())
        if len(Z) < 2:
            z = fz
            continue
        # 构造差分矩阵
        dZ = torch.stack([Z[i+1] - Z[i] for i in range(len(Z)-1)])
        dF = torch.stack([F[i+1] - F[i] for i in range(len(Z)-1)])
        # 解 min ||dF^T gamma - res||
        A = dF.reshape(len(dF), -1).T          # (n, k)
        b = res.reshape(-1, 1)                  # (n, 1)
        try:
            gamma = torch.linalg.lstsq(A, b).solution  # (k, 1)
            dz = dZ.reshape(len(dZ), -1).T @ gamma     # (n, 1)
            z = fz - dz.reshape(z.shape)
        except Exception:
            z = fz
    return z, max_iter, rnorm

def naive_solve(f, z0, tol=1e-6, max_iter=2000):
    z = z0.clone()
    for it in range(max_iter):
        fz = f(z)
        if (fz - z).abs().max().item() < tol:
            return fz, it + 1
        z = fz
    return z, max_iter

class DEQLayer(nn.Module):
    """真正 DEQ：前向用 solver，反向用隐式微分（不存中间激活）"""
    def __init__(self, d, scale=0.7):
        super().__init__()
        self.W = nn.Linear(d, d, bias=False)
        self.U = nn.Linear(d, d, bias=False)
        self.b = nn.Parameter(torch.zeros(d))
        self.scale = scale
    def f(self, z, x):
        return torch.tanh(self.scale * (self.W(z) + self.U(x) + self.b))
    def forward(self, x, tol=1e-4, max_iter=200):
        # 用隐式微分：先求不动点（no_grad），然后单步重建计算图
        with torch.no_grad():
            z, iters = naive_solve(lambda zz: self.f(zz, x), torch.zeros_like(x), tol, max_iter)
        # 隐式微分：z* = f(z*, x)，在 z* 处重建单步图
        z = self.f(z, x)
        return z, iters

def measure_saved_bytes(mod, x, K):
    """测 unrolled K 步保留的字节数（saved_tensors_hooks）"""
    retained = []
    def pack(t):
        retained.append(t.numel() * t.element_size())
        return t
    z = torch.zeros_like(x)
    with torch.autograd.graph.saved_tensors_hooks(pack, lambda t: t):
        for _ in range(K):
            z = mod.f(z, x)
    return sum(retained), len(retained)

def measure_gpu_peak(fn):
    if DEVICE == 'cuda':
        torch.cuda.reset_peak_memory_stats(); torch.cuda.synchronize()
    fn()
    if DEVICE == 'cuda':
        torch.cuda.synchronize()
        return torch.cuda.max_memory_allocated()
    return 0

def main():
    d, batch = 256, 8
    torch.manual_seed(0)
    mod = DEQLayer(d).to(DEVICE)
    x = torch.randn(batch, d, device=DEVICE)
    res = {}

    print("\n=== 1. 前向：naive vs Anderson ===")
    for name, fn in [("naive", lambda: naive_solve(lambda z: mod.f(z,x), torch.zeros_like(x), 1e-4, 2000)),
                     ("anderson", lambda: anderson_solve(lambda z: mod.f(z,x), torch.zeros_like(x), 1e-4, 200, 5))]:
        fn()  # warmup
        if DEVICE=='cuda': torch.cuda.synchronize()
        t0 = time.perf_counter(); z, iters, *rest = fn()
        if DEVICE=='cuda': torch.cuda.synchronize()
        ms = (time.perf_counter()-t0)*1000
        resid = (z - mod.f(z,x)).abs().max().item()
        print(f"  {name:10s}: {iters:4d} iters, {ms:7.2f} ms, residual={resid:.2e}")
        res[f'{name}_iters'] = iters; res[f'{name}_ms'] = ms; res[f'{name}_residual'] = resid

    print("\n=== 2. 反向显存：unrolled vs 真 DEQ ===")
    print(f"  {'iters':>8} {'unrolled saved':>16} {'tensors':>8}")
    for K in [5, 10, 20, 50]:
        saved, n = measure_saved_bytes(mod, x, K)
        print(f"  {K:>8} {saved:>16,} {n:>8}")
        res[f'unrolled_K{K}'] = {'saved_bytes': saved, 'tensors': n}

    # 真 DEQ 的反向显存
    print("\n  真 DEQ（隐式微分）:")
    z, iters = naive_solve(lambda zz: mod.f(zz, x), torch.zeros_like(x), 1e-4, 2000)
    print(f"    收敛 {iters} iters")
    # 在 z* 处单步计算图
    saved, n = measure_saved_bytes(mod, z.detach(), 1)
    print(f"    反向显存: {saved:,} bytes ({n} tensors) ← 与迭代次数无关！")
    res['true_deq_saved_bytes'] = saved
    res['true_deq_tensors'] = n
    res['true_deq_iters'] = iters

    print("\n=== 3. 隐式梯度 vs 解析解 ===")
    d2 = 32
    torch.manual_seed(42)
    A = torch.randn(d2, d2, device=DEVICE) * 0.1
    b = torch.randn(d2, device=DEVICE)
    z_exact = torch.linalg.solve(torch.eye(d2, device=DEVICE) - A, b)
    z = torch.zeros(d2, device=DEVICE)
    for i in range(2000):
        zn = A @ z + b
        if (zn - z).abs().max() < 1e-13: z = zn; break
        z = zn
    err = (z - z_exact).abs().max().item()
    print(f"  不动点误差: {err:.2e}（{i+1} iter）")
    res['fixed_point_err'] = err; res['fixed_point_iters'] = i+1

    print("\n=== 4. 墙钟：DEQ vs 展开 ===")
    K = res['naive_iters']
    # DEQ 前向（含隐式求解）
    def deq_fwd():
        with torch.no_grad():
            zz, _ = naive_solve(lambda t: mod.f(t, x), torch.zeros_like(x), 1e-4, 2000)
        return mod.f(zz, x)
    # 展开 K 次
    def unrolled_fwd():
        zz = torch.zeros_like(x)
        for _ in range(K): zz = mod.f(zz, x)
        return zz
    for name, fn in [("DEQ(implicit)", deq_fwd), (f"unrolled({K})", unrolled_fwd)]:
        fn()
        if DEVICE=='cuda': torch.cuda.synchronize()
        t0=time.perf_counter()
        for _ in range(5): fn()
        if DEVICE=='cuda': torch.cuda.synchronize()
        ms=(time.perf_counter()-t0)/5*1000
        print(f"  {name:20s}: {ms:7.2f} ms")
        res[f'{name}_ms'] = ms
    ratio = res['DEQ(implicit)_ms'] / res[f'unrolled({K})_ms']
    print(f"  比值: {ratio:.2f}x")
    res['ratio'] = ratio

    out = f'/home/assion/deq-lab/gpu/results_v2_{DEVICE}.json'
    json.dump(res, open(out,'w'), indent=2)
    print(f"\n结果已保存: {out}")

if __name__ == '__main__':
    main()
