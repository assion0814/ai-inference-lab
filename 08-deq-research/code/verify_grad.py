"""验证修复后的隐式微分梯度"""
import torch, torch.nn as nn, torch.nn.functional as F
import sys
sys.path.insert(0, '/home/assion/deq-lab/attach')

DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'

def deq_f(z, x, W, b, U):
    return torch.tanh(z @ W + x @ U + b)

class DEQFn(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, W, b, U, tol, max_iter):
        z = torch.zeros(x.size(0), W.size(0), device=x.device, dtype=x.dtype)
        for i in range(max_iter):
            z_new = deq_f(z, x, W, b, U)
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
            return grad_z + ((1 - z**2) * v) @ W.T
        v = grad_z.clone()
        for _ in range(ctx.max_iter):
            v_new = fT(v)
            if (v_new - v).abs().max() < ctx.tol: v = v_new; break
            v = v_new
        dz = (1 - z**2) * v
        return None, z.T @ dz, dz.sum(0), x.T @ dz, None, None

# 测试
torch.manual_seed(0)
d_in, d_eq = 32, 64
x = torch.randn(8, d_in, device=DEVICE)
W = (torch.randn(d_eq, d_eq, device=DEVICE) * (0.5 / d_eq**0.5))  # 谱半径 ~0.5，收缩
b = torch.zeros(d_eq, device=DEVICE)
U = torch.randn(d_in, d_eq, device=DEVICE) * 0.3
y = torch.randn(8, d_eq, device=DEVICE)

# 隐式梯度
W1, b1, U1 = W.clone().requires_grad_(), b.clone().requires_grad_(), U.clone().requires_grad_()
z = DEQFn.apply(x, W1, b1, U1, 1e-6, 200)
loss = (z * y).sum()
loss.backward()
g_impl = (W1.grad.clone(), b1.grad.clone(), U1.grad.clone())

# 展开梯度（K 很大，应逼近隐式）
W2, b2, U2 = W.clone().requires_grad_(), b.clone().requires_grad_(), U.clone().requires_grad_()
z = torch.zeros(8, d_eq, device=DEVICE)
for _ in range(200):
    z = deq_f(z, x, W2, b2, U2)
loss2 = (z * y).sum()
loss2.backward()
g_unroll = (W2.grad.clone(), b2.grad.clone(), U2.grad.clone())

print("  参数    隐式范数      展开范数      相对误差")
for name, a, c in [("W", g_impl[0], g_unroll[0]), ("b", g_impl[1], g_unroll[1]), ("U", g_impl[2], g_unroll[2])]:
    rel = ((a-c).norm() / (c.norm()+1e-10)).item()
    status = "✅" if rel < 1e-3 else "❌"
    print(f"  {name:<6} {a.norm():>12.4e} {c.norm():>12.4e} {rel:>12.3e}  {status}")

# 判定
all_ok = True
for name, a, c in [("W", g_impl[0], g_unroll[0]), ("b", g_impl[1], g_unroll[1]), ("U", g_impl[2], g_unroll[2])]:
    rel = ((a-c).norm() / (c.norm()+1e-10)).item()
    if rel > 1e-3: all_ok = False
print(f"\n  {'✅ 隐式微分正确' if all_ok else '❌ 仍有错误'}")
if all_ok:
    print("  可以重跑外挂实验")
