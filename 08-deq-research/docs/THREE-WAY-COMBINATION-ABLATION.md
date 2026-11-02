
# 三者结合验证：4 臂消融实验

> **用户质疑："你确定验证了上文中的三者结合吗？"**
>
> **答案：之前没有。现在做了。**
>
> **结果：三者结合（PPL 52.96）不如方案一单独（PPL 49.15）。"缺一不可"被证伪。**

---

## 一、为什么之前的验证不算

**用户框架的三者：**

| 方案 | 层次 | 内容 |
|---|---|---|
| **方案三** | 宏观 | 异构架构：大 Transformer（吸收知识）+ 小 Equilibrium 模块（迭代寻优）|
| **方案一** | 中观 | MoE 稀疏专家，降低带宽 |
| **方案二** | 微观 | 数值阻尼（Anderson + damping），保证求解器稳定 |

**我之前做的：**

| 实验 | 实际测的 | 缺什么 |
|---|---|---|
| DEQ 机制验证 | 只是简单 DEQ 层的 O(1) 显存 | 没有 Transformer 底座，没有 MoE |
| 外挂验证 | DEQ 挂到 Qwen2.5-0.5B 输出端 | 不是"异构"，没有 MoE，没有真实训练 |
| DEQ-LM 训练 | DEQ 替代 Transformer | 没有 MoE，没有"异构分工" |
| 三层框架验证 | 在**现有 DeepSeek** 上测 Sinkhorn | 那不是 DEQ 求解器的阻尼 |

**所以：我测的是三个方案的各自片段，没有测"结合在一起"。**

**用户的批评是对的。**

---

## 二、真正的三者结合实验设计

### 2.1 四臂消融

```
A. Baseline:        Transformer-3层（纯基线）
B. +Equilibrium:    Transformer-2层 + Equilibrium 模块（方案三 + 方案二阻尼）
C. +MoE:            Transformer-3层，FFN 换成 4 专家 top-1 MoE（方案一）
D. 三者结合:        Transformer-2层 + MoE + Equilibrium(Anderson+damping)（三+一+二）
```

### 2.2 各方案的实现细节

**方案一（MoE）：**
```python
class MoEFFN(nn.Module):
    def __init__(self, d_model, d_inner, n_experts=4, top_k=1):
        self.router = nn.Linear(d_model, n_experts, bias=False)
        self.experts = [FFN(d_model, d_inner) for _ in range(n_experts)]
    def forward(self, x):
        weights, indices = self.router(x).topk(top_k)  # 稀疏路由
        # 每个 token 只走 top_k 个专家
```

**方案三（Equilibrium）：**
```python
class EquilibriumBlock(nn.Module):
    """大 Transformer 输出 → 小 Equilibrium 模块迭代精炼"""
    def forward(self, x):
        inj = self.inj(x)                        # 注入
        z = anderson_solve(f, 0)                 # 迭代求解 z*=tanh(z*W + inj)
        return x + self.out(z)                   # 残差
```

**方案二（数值阻尼）：**
```python
def anderson_solve(f, z0, tol, max_iter, m=3, beta=0.5):
    # Anderson 加速 + 阻尼 beta
    z = (1-beta)*z + beta*fz      # 带阻尼的迭代
    # + Anderson 外推
```

**关键：Equilibrium 模块的求解器用 `torch.no_grad()`（不建图），
在不动点处单步重建图。这是标准 DEQ 做法，显存 O(1)。**

### 2.3 实验控制

```
数据:    本地技术文档混合，1.50M tokens 训练 / 82K 验证
词表:    word-level, 20,000
训练:    2000 步 × batch 16 × seq 128 × AdamW lr=3e-4
硬件:    V100 32GB
随机种子: 全部 0
```

---

## 三、实验结果

```
模型                          参数      PPL      时间    显存    平均迭代
────────────────────────────────────────────────────────────────────────
A. Baseline (Transformer-3层) 11.08M   54.77    29s    0.77GB   0
B. +Equilibrium (三+二)        11.28M   57.28    69s    0.74GB   15
C. +MoE (方案一)               17.40M   49.15    47s    0.87GB   0
D. 三者结合 (三+一+二)          15.23M   52.96    81s    0.81GB   15

相对基线:
  B: PPL  +4.6%   时间 2.38x   显存 0.96x
  C: PPL -10.3%   时间 1.62x   显存 1.14x
  D: PPL  -3.3%   时间 2.80x   显存 1.05x
```

---

## 四、关键判读

### 4.1 ★ 三者结合不如最好的单独方案

```
三者结合 (D):      52.96
方案一单独 (C):    49.15   ← 最好
方案三+二单独 (B): 57.28
基线 (A):          54.77
```

**D (52.96) > C (49.15)，即：加了 Equilibrium 模块反而让 MoE 变差了。**

**这是对"缺一不可"的直接证伪。**

### 4.2 各方案的独立效果

| 方案 | PPL 变化 | 时间 | 显存 | 结论 |
|---|---|---|---|---|
| **方案一 MoE** | **-10.3%** | 1.62x | 1.14x | ✅ **有效**（但参数量多 57%）|
| **方案三 Equilibrium** | +4.6% | 2.38x | 0.96x | ❌ **无效**（反而更差）|
| **方案二 阻尼** | 已包含在 B/D 中 | — | — | 使求解稳定（无阻尼时训练不稳定）|

### 4.3 参数量公平性

**注意：MoE 单独（C）参数多 57%（17.40M vs 11.08M），所以 -10.3% 的 PPL 改善可能部分来自参数。**

```
按"每百万参数的 PPL 改善":
  C: (54.77-49.15) / (17.40-11.08) = 0.89 PPL/M
  D: (54.77-52.96) / (15.23-11.08) = 0.44 PPL/M
  
→ MoE 单独的参数效率是三者结合的 2 倍
```

---

## 五、对用户框架的最终判定

### 5.1 原命题

> "如果你只做方案三，迭代跑不起来。
> 如果你只做方案一，MoE 依然陷入局部最优。
> 如果你只做方案二，阻尼再强也带不动千亿参数。
> **只有三管齐下，才是能落地的下一代 AI 架构。**"

### 5.2 实测判定

| 子命题 | 判定 | 证据 |
|---|---|---|
| 只做方案三跑不起来 | ⚠️ **部分成立** | Equilibrium 单独确实更差（+4.6%），但能跑 |
| 只做方案一陷入局部最优 | ❌ **不成立** | MoE 单独最好（-10.3%）|
| 只做方案二带不动 | ⚠️ 无法单独测（阻尼是求解器的配件）| — |
| **三管齐下最优** | ❌ **被证伪** | 三者结合（52.96）不如 MoE 单独（49.15）|

### 5.3 修正后的框架

**基于实测，正确的表述应该是：**

> **"MoE 稀疏化是当前最有效的架构优化（-10.3% PPL，参数效率最高）。
> 数值阻尼是迭代式组件的必需配套（不加会训练不稳定）。
> Equilibrium/异构架构在这个规模下没有增益（+4.6%），
> 且与 MoE 结合后反而拖累（52.96 vs 49.15）。
>
> 三者不是'缺一不可'，而是：方案一是主收益，方案二是必要配件，
> 方案三是可选研究项 —— 在当前规模下不划算。"**

---

## 六、诚实的限制

### 6.1 这个实验的规模限制

| 维度 | 本实验 | 论文级 |
|---|---|---|
| 数据 | 1.5M tokens | 100M+ |
| 训练步数 | 2,000 | 20K-150K |
| 模型规模 | 11-17M | 98M+ |
| MoE 专家数 | 4（top-1）| 64-384（top-2/6）|
| Equilibrium 实现 | 简化 tanh 层 | 完整 Transformer 层 |

### 6.2 这个实验能说明什么

✅ **在这个规模下，三者结合不如 MoE 单独**
✅ **Equilibrium 在当前实现下无增益**
✅ **MoE 是最有效的单项优化**
✅ **"缺一不可"不成立**

### 6.3 这个实验不能说明什么

❌ **大规模下三者结合是否更好**（需要 100M+ 参数、100M+ tokens）
❌ **完整的 Equilibrium 架构**（我的实现是简化版）
❌ **MoE 与 Equilibrium 的深度集成**（我只是简单串联）
❌ **方案二阻尼的最优参数**（beta=0.5 是随便选的）

---

## 七、复现命令

```bash
# 停 maya-ds4 释放 VRAM（必须）
for pid in $(ps -eo pid,args | awk '$0 ~ /maya-ds4 --serve/ && $0 !~ /awk/ {print $1}'); do kill -TERM $pid; done

# 跑 4 臂消融（约 5 分钟）
cd ~/deq-lab/train
/home/assion/v100-1cat/bin/python combined_3way.py \
  --steps 2000 --batch 16 --seq 128

# 结果
cat ~/deq-lab/train/results_3way_real.json
```

---

## 八、资产清单

```
代码: ~/deq-lab/train/combined_3way.py（324 行，含全部四臂实现）
数据: ~/deq-lab/data/deq-lm/{train,valid,test}.txt
结果: ~/deq-lab/train/results_3way_real.json
文档: ~/company/THREE-WAY-COMBINATION-ABLATION.md（本文件）
```

---

## 九、最终答案

**用户问："你确定验证了上文中的三者结合吗？"**

**答：**

> **之前没有 —— 我的三次验证（机制/外挂/DEQ-LM）都只测了方案的片段，
> 从来没测过"三者结合"。**
>
> **现在做了 —— 4 臂消融（基线 / +Equilibrium / +MoE / +三者结合），
> 2000 步真实训练。**
>
> **结果：三者结合（PPL 52.96）不如 MoE 单独（PPL 49.15）。
> "三管齐下缺一不可"被证伪。**
>
> **正确的结论：MoE 是主收益（-10.3%），阻尼是必要配件，
> Equilibrium 在这个规模下无增益（+4.6%）且拖累 MoE。**
