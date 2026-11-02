# 01 · 748B MoE on a Single V100

在 64GB 内存的迷你主机 + 一张 2017 年的 Tesla V100-PCIE-32GB（二手约 4000 元）上，运行 257.63GB 的 DeepSeek-V4.1-Flash（748B 参数 MoE）。

## 结论

- 批处理 decode **24–27 tok/s**，HTTP 服务模式 **14–15 tok/s**，PPL **12.81**
- 对比 Project Maya 官方同配置（1×V100 32GB + 64GB RAM，Maya-S）的 19 tok/s，服务口径达成 **75–79%**
- 瓶颈是**专家权重的读取**，不是 token 生成 —— drafter 实测证伪（14.0 vs 14.9 tok/s）

## 环境

| 项 | 配置 |
|---|---|
| 主机 | UM890 Pro（Ryzen 9 PRO 8945HS，8 核 16 线程） |
| 内存 | 64GB DDR5-5600（2×32GB，主板最大 64GB，已插满） |
| GPU | Tesla V100-PCIE-32GB（2017，Volta sm_70），OCuLink PCIe 3.0 x4 外接 |
| 软件 | CUDA 12.4 + gcc-13 + Python 3.12 |

## 量化方案

- 路由专家 `IQ1_S`（1.56 bpw，误差反馈舍入；注意 IQ1_M 才是 1.75 bpw）、稠密权重 `Q8_0`、Engram 表 `Q6_K`
- 压缩到 **257.63 GB**，5 个 GGUF shard（44.50 / 44.49 / 18.43 / 75.10 / 75.11 GB）
- 专家按热度驻留 VRAM：**3037 / 15360 = 19.8%**（按访问加权的命中率是 **71.7%**，未命中的 28.3% 走 CPU lane）

## 关键数据

| 配置 | decode | 口径说明 |
|---|---:|---|
| **批处理（`--ids`, n=96）** | **24–27 tok/s** | 固定 token id、无 HTTP、无 thinking 切换；`DS4_BENCH_REPEAT=2` 取中位 |
| **HTTP 服务模式** | **14–15 tok/s** | 单请求、含 thinking 切换与 HTTP 开销；这是用户实际体验值 |
| 官方同配置（Maya-S） | 19 tok/s | 官方 HTTP 口径 |

**两个数字差 1.7× 的原因**：批处理模式直接喂 token id（`--ids`），
没有 tokenizer/chat template/thinking 切换/HTTP 帧封装；HTTP 模式每个请求
要经过完整链路。**两者都真实，但不可混用。**

- 与官方对比用 **HTTP 口径**：14–15 / 19 = **74–79%**
- 若用批处理口径：24–27 / 19 = **126–142%**（我们更快，但口径不同，不这么比）

`docs/DEEPSEEK-V41-V100-ASSETS.md` 里记录的 **12.8–15.2 tok/s（均值 14.4）**
是早期单请求 HTTP 测量（2026-10-28）；README 的 14–15 是后续优化后的中位值。

max-context 对比：8192 → 27.03；32768 → 25.73；131072 → 24.09；262144 → 25.59 tok/s（变化 <10%，说明上下文不是瓶颈）

## 复现

```bash
bash scripts/start-deepseek-maya.sh
# 或作为服务
cp scripts/deepseek-maya.service /etc/systemd/system/
```

详细安装步骤见 [docs/DEEPSEEK-V41-V100-ASSETS.md](./docs/DEEPSEEK-V41-V100-ASSETS.md)

## 踩过的坑

1. CUDA 12.4 与 gcc 版本强绑定，gcc 15 编译失败，需降到 gcc-13
2. 系统 Python 3.14 无 venv/ensurepip，需用 uv 建 3.12 环境
3. 模型必须放持久目录（`/tmp` 重启即清），5 个 shard 缺一不可
4. `DS4_STREAM=0` 会误开启 one-GPU path，VRAM 专家从 3037 掉到 1741，速度 10.95 tok/s
5. drafter 下载并校验通过，但实测反而更慢 —— 瓶颈不在 token 生成

## 两个可归因的增益

| 改动 | 效果 | 机制 |
|---|---|---|
| 修复 `DS4_STREAM=0` | 10.95 → 14.70 tok/s（**+34%**） | VRAM 专家 1741 → 3037 |
| 算子融合 | 11.80 → 15.00 tok/s（**+27%**） | A/B 对照 |

## 边界

- 32K 上下文未验证（需要更多内存）
- 主板 64GB 上限，无法升级；V100 32GB 也是该卡上限
- 单卡运行，无张量并行/流水并行
