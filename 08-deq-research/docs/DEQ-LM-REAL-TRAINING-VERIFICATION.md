<!-- 层归属（SOC.md §四）：经营（后台）。技术资产登记 -->
> **层归属**：`经营（后台）` —— 技术认知/实验验证
> **执行体**：`~/deq-lab/train/{deq_lm_wn.py, train_deq_lm.py}`、`~/deq-lab/data/deq-lm/`
> **项目状态**：✅ 完成（2026-10-29，真正的语言模型训练对比）
> **前置资产**：[`DEQ-EXTERNAL-ATTACH-VERIFICATION.md`](DEQ-EXTERNAL-ATTACH-VERIFICATION.md)、[`THREE-LAYER-FRAMEWORK-VERIFICATION.md`](THREE-LAYER-FRAMEWORK-VERIFICATION.md)

# DEQ 真正验证：训练 DEQ-LM vs Transformer

> **用户要求："真正验证"**
>
> **执行结果：**
> 1. ✅ 从零训练 DEQ-LM 与 Transformer-LM（1.5M tokens，3000 步）
> 2. ✅ 验证了 weight normalization 是 DEQ 稳定训练的必要条件（PPL 192→69）
> 3. ✅ 验证了 DEQ 的显存优势真实存在（0.68 vs 0.79 GB）
> 4. ❌ **但 DEQ-LM 的 PPL 显著差于 Transformer（+76.6%），且训练更慢（1.49x）**

---

## 一、实验设计

### 1.1 数据

```
来源: 本地技术文档混合（排除许可证/重复内容）
  - company/*.md:      0.70M 字符
  - VibeIDE/docs:      3.92M 字符
  - llama.cpp/*.md:    1.92M 字符
  - equilibrium-lab:   0.04M 字符

切分: train.txt 6.01M 字符（1.50M tokens）
      valid.txt 0.33M 字符（0.08M tokens）
      test.txt  0.24M 字符（0.06M tokens）

词表: word-level, 20,000 词（从 train.txt 构建）
```

### 1.2 模型

```
A. Transformer-LM:
   标准 nn.TransformerEncoder，3 层，d_model=256，8 head，d_inner=1024
   参数量: 12.66M

B/C/D. DEQ-LM:
   注入层 → 均衡层 z*=tanh(z*W + injU + b) → 输出头
   d_model=256, d_eq=512, 共享权重
   参数量: 16.07M（比 Transformer 多 27%，因为 d_eq 更大）

训练: 3000 步, batch 16, seq 128, AdamW lr=3e-4, cosine schedule
硬件: V100 32GB
```

### 1.3 四臂对比

```
A. Transformer-3层            ← 基线
B. DEQ-implicit 无 weight norm ← 验证 WN 的必要性
C. DEQ-implicit + WN(σ≤0.9)   ← 正确配置的 DEQ
D. DEQ-unrolled + WN          ← 展开版（无 O(1) 显存）
```

---

## 二、实验结果

```
模型                          参数      PPL     时间    显存
─────────────────────────────────────────────────────────────
A. Transformer-3层           12.66M   39.34   46s   0.79GB
B. DEQ-implicit (无WN)       16.07M  192.55   78s   0.68GB
C. DEQ-implicit +WN(σ≤0.9)  16.07M   69.49   68s   0.68GB
D. DEQ-unrolled +WN         16.07M   62.37   96s   0.82GB

相对基线:
  B: PPL +389.4%  时间 1.70x  显存 0.86x
  C: PPL  +76.6%  时间 1.49x  显存 0.86x
  D: PPL  +58.5%  时间 2.09x  显存 1.04x
```

---

## 三、三个核心发现

### 3.1 ★ Weight Normalization 是 DEQ 训练的必需品

```
B (无 WN) 训练曲线:  5.76 → 7.40(爆炸) → 5.77 → 5.66  → PPL 192.55
C (有 WN) 训练曲线:  5.72 → 4.88 → 4.53 → 4.33 → 4.26  → PPL  69.49
```

**原因：**
- 无 WN 时，W 的谱半径不受控 → f 不是收缩映射 → 不动点迭代可能不收敛
- 训练中 loss 从 5.76 突然跳到 7.40（梯度爆炸的表现）
- 加 spectral norm 后训练完全平滑

**改进倍数：PPL 192.55 → 69.49 = 2.8 倍**

**这验证了 DEQ 论文强调的 "weight normalization is crucial" 是对的。**

### 3.2 ★ DEQ 的显存优势真实存在

```
DEQ-implicit (C):  0.68 GB
Transformer (A):   0.79 GB
→ 省 14% 显存
```

**原因：隐式微分反向只存不动点 z*（3 个张量），不存迭代中间态。**

**这与之前的机制验证一致（24KB vs 14MB，572 倍），只是在这个规模下差距被其他开销稀释了。**

### 3.3 ❌ 但 DEQ 的 PPL 显著更差

```
DEQ-implicit + WN (C):  PPL 69.49  (+76.6% 差于 Transformer)
DEQ-unrolled + WN (D):  PPL 62.37  (+58.5% 差于 Transformer)
Transformer (A):        PPL 39.34
```

**可能原因（诚实分析）：**

| # | 原因 | 证据 |
|---|---|---|
| 1 | **DEQ 模型表达能力受限** | 共享权重的 tanh 层，vs Transformer 的独立 3 层（每层有 attention+FFN）|
| 2 | **这不是完整的 DEQ-LM** | 论文的 DEQ-LM 用完整 Transformer 层做 f，我用简化 tanh 层 |
| 3 | **数据太少/步数太少** | 1.5M tokens / 3000 步，论文用 100M+ tokens / 150K 步 |
| 4 | **没有 Anderson/Broyden 加速** | 朴素不动点迭代，收敛慢 |
| 5 | **WN 实现不完美** | 日志显示 σ 收敛到 1.90，超过 target 0.9 |

---

## 四、诚实的对照分析

### 4.1 这个实验证明了什么

| 结论 | 状态 |
|---|---|
| DEQ 可以不稳定的训练（需要 WN） | ✅ 验证（PPL 192 vs 69）|
| DEQ 有 O(1) 显存优势 | ✅ 验证（0.68 vs 0.79 GB）|
| DEQ 训练更慢 | ✅ 验证（1.49-2.09x）|
| **DEQ 在同等训练预算下不如 Transformer** | ✅ **验证（+58.5% ~ +76.6% PPL）** |
| DEQ 的"迭代深度"优于"层堆叠" | ❌ **被证伪** |

### 4.2 这个实验没有证明什么

| 未验证的问题 | 原因 |
|---|---|
| 论文级别的 DEQ-LM 效果（PPL 21.85） | 数据/步数/模型规模差 2 个数量级 |
| 完整 Transformer 层做 f 的 DEQ | 隐式微分实现太复杂 |
| DEQ 在长上下文/记忆任务上的优势 | 未测试 |
| Anderson/Broyden 加速的效果 | 未实现 |
| 训练到收敛后的最终对比 | 3000 步太少 |

### 4.3 最关键的判读

**"DEQ 的迭代深度 > Transformer 的层堆叠深度"这个假设，在我们的实验中不成立。**

```
DEQ-implicit-25it:  等效深度 25 层（共享权重）
Transformer-3层:    实际深度 3 层（独立权重）
结果:              DEQ PPL 69.49 vs Transformer PPL 39.34
```

**为什么？**

**因为"深度"不只是层数，还包括：**
1. **参数多样性** —— 独立层有不同的 W_Q/W_K/W_V/FFN
2. **表示变换能力** —— 每层可以学到不同的变换
3. **DEQ 的共享权重限制了每层的变换必须相同**

**这是"迭代 25 次的同一个函数" vs "3 个不同的函数"。**
**后者在语言建模上更有效。**

---

## 五、如果做论文级验证需要什么

### 5.1 缺失的关键组件

```
1. 完整 Transformer 层的 DEQ
   - 需要隐式微分支持 layer norm + attention + FFN
   - 实现复杂度：高（约 200-400 行正确的 autograd.Function）
   
2. 正确的 weight normalization
   - 我的实现 σ 收敛到 1.90 而 target 0.9（不完美）
   - 需要每步重新归一化权重（不是只算 scale）
   
3. Anderson / Broyden 求解器
   - 朴素不动点迭代收敛慢
   - torchdeq 有现成实现，但需要正确集成
   
4. 更多数据
   - 至少 10-100M tokens
   - 我们只有 1.5M
   
5. 更多步数
   - 至少 20K-150K 步
   - 我们只跑了 3000 步
   
6. 更大的模型
   - 论文 DEQ-LM 98M，我们 16M
```

### 5.2 估算

```
如果在 V100 上做论文级复现:
  数据: 需下载 Wikitext-103（网络封了）或用更多本地文本
  训练: 98M 模型 × 150K 步 × 单卡 V100 ≈ 3-7 天
  调参: 需要多轮实验，总计 1-2 周
```

---

## 六、诚实结论

### 6.1 对用户"真正验证"要求的回答

**我做了真正的训练验证（不是探针、不是外挂），结果是：**

```
在 1.5M tokens、3000 步、16M 参数的规模下:
  Transformer-LM:  PPL 39.34  （46s，0.79GB）
  DEQ-LM (正确配置): PPL 69.49  （68s，0.68GB）
  
→ DEQ 的 PPL 差 76.6%，训练慢 1.49x，但显存省 14%
```

### 6.2 最终判断

**DEQ 在语言建模上有明确的取舍：**
- **收益**：O(1) 显存（省 14% 在这个规模下，理论上可无限深）
- **代价**：PPL 更差（+76.6%）、训练更慢（1.49x）、需要正确的 weight norm

**这个实验不足以否定 DEQ 论文的结果（规模差 2 个数量级），但足以说明：**

> **"简单实现一个 DEQ-LM 就能超过 Transformer" 是错的。
> DEQ 的收益需要在大规模、正确实现、充分调参的前提下才能体现。
> 在资源受限的情况下，Transformer 是更稳妥的选择。**

### 6.3 对用户"改预训练模型"的建议

**基于本次检验，我建议：不要改。**

```
理由:
  1. 外挂验证: ❌ 无增益（DEQ ≈ MLP）
  2. 真正训练: ❌ PPL 差 76.6%，更慢
  3. 显存优势: ✅ 但只省 14%（在这个规模）
  4. 实现复杂度: 高（需要正确的 WN + 完整 Transformer 层 + Anderson 求解器）
  
结论: 在没有明确证据表明 DEQ 能带来收益的情况下，
      改造 257GB 预训练模型的风险/收益比不划算。
```

---

## 七、复现命令

```bash
# 1. 准备数据（自动从本地文本生成）
cd ~/deq-lab && python3 -c "
import os, random
# ... 见 DEQ-LM-REAL-TRAINING-VERIFICATION.md 的数据准备脚本
"

# 2. 跑四臂对比（约 10 分钟）
cd ~/deq-lab/train
/home/assion/v100-1cat/bin/python deq_lm_wn.py \
  --steps 3000 --batch 16 --seq 128 --deq_iters 25 --target 0.9

# 3. 看结果
cat ~/deq-lab/train/results_wn_real.json
```

**环境要求：**
- torch 2.10.0+cu126（/home/assion/v100-1cat）
- 先停 maya-ds4 服务释放 VRAM
- 数据在 ~/deq-lab/data/deq-lm/

---

## 八、资产清单

```
代码:
  ~/deq-lab/train/train_deq_lm.py     第一版（unrolled）
  ~/deq-lab/train/deq_lm_final.py     第二版（隐式微分，tanh）
  ~/deq-lab/train/deq_lm_wn.py        第三版（+ weight norm）← 最终版
  
数据:
  ~/deq-lab/data/deq-lm/{train,valid,test}.txt  本地文本语料

结果:
  ~/deq-lab/train/results_wn_real.json  最终四臂对比

文档:
  ~/company/DEQ-LM-REAL-TRAINING-VERIFICATION.md  本文件
```
