# 08 · DEQ（深度均衡模型）验证：一个诚实的负结果

用 4 个阶段、完整的实验链路，验证 Deep Equilibrium Model 能否替代 Transformer。
**结论：机制真实存在，但在真实 LM 训练上不划算。**

## 结论先行

| 阶段 | 假设 | 结果 |
|---|---|---|
| 1. 机制验证 | DEQ 能 O(1) 显存 | ✅ 24,576 B vs 14,073,856 B = **572× 省** |
| 2. 外挂验证 | 把 DEQ 挂到预训练模型上能提升 | ❌ **DEQ ≈ MLP**（无增益）|
| 3. 真实训练 | DEQ-LM 能匹配 Transformer | ❌ **PPL +76.6%，慢 1.49×** |
| 4. 三路消融 | 异构+MoE+阻尼三者结合最优 | ❌ **D (52.96) 不如 C 单独 (49.15)** |

**最终建议：不要改 257GB 预训练模型。**

## 关于 572× 的正确解读（审计后补）

**572× 是「50 步展开 vs 11 次收敛」的对比，不是同迭代数对比：**

| 对比方式 | 显存比 |
|---|---:|
| 50 步展开 vs 11 次隐式微分（原报告） | 572× |
| **同迭代数折算（11 vs 11）** | **≈126×** |
| 16M 模型实际训练（PPL 表） | **14%** |

**真正的收益是 14%（真实训练），572× 是探针级上限。** 两个数字都对，但必须说清口径。


## 阶段 1：机制验证（572× 显存节省是真的）

```
隐式微分 backward 显存:   24,576 字节（3 个 tensor）
50 步展开 backward 显存: 14,073,856 字节（199 个 tensor）
────────────────────────────────────────────
比值: 572×
```

**同时测到**：
- 收敛迭代数：11 次
- 谱半径：0.51
- 定点误差：4.77e-07
- DEQ forward 比普通 forward 慢 1.75×

## 阶段 2：外挂验证（DEQ ≈ MLP）

把 DEQ 模块挂到 Qwen2.5 / DeepSeek-R1-Distill 等预训练模型上：
- 结果：**DEQ 与普通 MLP 的 PPL 差异 < 2%**
- 说明：在已经训练好的模型里，插入一个"迭代精炼"模块没有额外收益

## 阶段 3：真实训练（+76.6% PPL）

从零训练 DEQ-LM vs Transformer-LM（1.5M tokens，3000 步）：

| 模型 | PPL | 训练时间 | 显存 |
|---|---:|---:|---:|
| A. Transformer-LM（基线，12.66M） | 39.34 | 46 s | 0.79 GB |
| B. DEQ-LM（无 weight norm）| 192.55 | 78 s | 0.68 GB |
| C. DEQ-LM（带 weight norm）| **69.49** | **68 s** | 0.68 GB |

> 数据源：`data/results_wn_real.json`。
> **+76.6% 是 C 相对 A（69.49 vs 39.34）**；阶段 4 的 54.77 基线属另一次运行
> （`data/results_3way_real.json`，11.08M 参数），两次运行参数量不同，**不可混算**。

**关键发现：weight normalization 是 DEQ 稳定训练的必要条件（PPL 192 → 69）**

## 阶段 4：三路消融（"缺一不可"被证伪）

| 方案 | PPL | 参数 | 时间 |
|---|---:|---:|---:|
| A. Baseline | 54.77 | 11.08M | 29 s |
| B. +Equilibrium | 57.28 | 11.28M | 69 s |
| C. +MoE | **49.15** | 17.40M | 47 s |
| D. 三者结合 | 52.96 | 15.23M | 81 s |

**D (52.96) 不去 C 单独 (49.15) → "缺一不可"不成立**

## 一个被修复的数学 bug（最有价值的部分）

**现象**：DEQ 的隐式梯度实现有 bug，导致 DEQ 看起来"灾难性地差"（-54.3 pp）

**排查**：写 `/tmp/verify_grad.py` 用解析展开做 ground truth 比对

**根因**（2 处）：
1. `J^T v` 的顺序错了：应该是 `W @ ((1-z²)⊙v)`，不是 `(1-z²)⊙(Wv)`
2. `dL/dW` 的转置错了：应该是 `z^T @ dz`，不是 `dz.T @ z`

**修复后**：DEQ 从 -54.3 pp 变成 -1.8 pp（接近 MLP）

**教训：任何结论必须先过 apparatus 自检。**

## 文件清单

```
08-deq-research/
├── README.md
├── docs/
│   ├── DEQ-EXTERNAL-ATTACH-VERIFICATION.md   (371 行)
│   ├── DEQ-LM-REAL-TRAINING-VERIFICATION.md  (300 行)
│   ├── THREE-WAY-COMBINATION-ABLATION.md     (258 行)
│   └── MODULE-SCALE-EXPERIMENT.md            (322 行)
├── code/
│   ├── verify_deq_gpu_v2.py      # 572× 显存节省探针
│   ├── verify_grad.py            # 隐式梯度正确性检查（发现 bug 的工具）
│   ├── train_deq_lm.py           # 展开版训练
│   ├── deq_lm_final.py           # 隐式版训练
│   ├── deq_lm_wn.py              # + weight norm
│   ├── combined_3way.py          # 三路消融
│   ├── module_scale.py           # 模块尺度实验
│   └── external_attach_hard.py   # 外挂验证
└── data/
    ├── results_v2_cuda.json
    ├── results_final_real.json
    ├── results_wn_real.json
    ├── results_3way_real.json
    ├── results_module_scale_real.json
    └── results_e2_fixed.json
```

## 为什么这个项目值得看

1. **负结果也是结果** —— 4 个阶段全部证伪，省下了后续投入
2. **apparatus 自检救了整个实验** —— 修了 2 个数学 bug，DEQ 从 -54.3 pp 变 -1.8 pp
3. **不是「我觉得不行」** —— 每个结论都有可复算的数据文件
4. **诚实** —— 明确写出"不要改 257GB 预训练模型"的建议
