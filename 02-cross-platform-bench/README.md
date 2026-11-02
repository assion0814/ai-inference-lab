# 02 · 跨平台推理实测与算子级归因

在 3 个架构完全不同的硬件平台上做统一协议实测，并把 decode / prefill 的瓶颈定量拆到算子级。

## 结论

- **带宽天花板 274.1 GB/s**（RTX 4060 Ti，理论 288 GB/s 的 95.2%），且对配置不敏感
- decode 中 **GEMV 占 94.3% 时间**（586/621 ms），其余算子合计仅 5.7%
- decode 达 **96.5% 带宽 roofline**、prefill 达约 **100% 算力 roofline** → 剩余可回收 **0–3.5%**
- 三平台 decode **14.57 / 70.15 / 217.84 tok/s**，带宽利用率 **32% / 81% / 79%**

## 实验平台

| 平台 | 算力单元 | 带宽 | 功耗 |
|---|---|---|---|
| Orange Pi 5 Plus | RK3588 NPU（6 TOPS） | 68.3 GB/s | 5–10 W |
| UM890 Pro | AMD 780M iGPU（12 CU） | 89.6 GB/s | 54 W |
| UM890 Pro | RTX 4060 Ti 8GB（OCuLink Gen4 x4） | 288 GB/s | 130 W |

模型：Qwen2.5/Qwen3 0.5B–32B（7 个）；量化：GGUF Q2_K–Q8_0（8 档）

## 关键数据

| 指标 | 数值 | 意义 |
|---|---:|---|
| 带宽天花板 | 274.1 GB/s | 理论 288 的 95.2% |
| GEMV 时间占比 | 94.3% | decode 的瓶颈就在这里 |
| GEMV 带宽效率 | 96.5% | 已到顶 |
| 规模-效率曲线 | 256 行 23.5% → lm_head 91.6% | 小矩阵打不满内存系统 |
| warp 合并 vs 跨步 | 270.7 vs 94.4 GB/s（2.87×） | 访问模式的决定性作用 |
| prefill GEMM | 29.8 TFLOPS（峰值 47.4，62.9%） | 重复读 2.65 倍 |
| "176 vs 47 TFLOPS" | 176 = FP16 累加 87.4×2（稀疏） | LLM 推理必须 FP32 累加，可用峰值 48 |

## 测量严谨性

- **CPU 负对照**：同板 onnxruntime，最佳 4 线程 106.359 ms vs NPU INT8 16.141 ms = **6.59×**
- **口径分解**：6.59× 拆为架构优势 **3.30×** × 量化收益 **2.00×**
- **三类陷阱**：冷启动（prefill 594 vs 3216 t/s，差 5.4×）、PCIe 空闲态 ASPM 降速（Gen4→Gen1）、PPL 随 chunk 数漂移

## 66 项优化技术验证

22 项有效 / 25 项无效 / 13 项物理不可能。有效项举例：static batching 4.91×、continuous batching 3.29×、prefix caching 1.17×。

## 详细报告

- [docs/算子级优化报告.md](./docs/算子级优化报告.md)
- [docs/全能力扩展测试报告.md](./docs/全能力扩展测试报告.md)

## 边界

- 算子级优化是在 llama.cpp 已有的量化 kernel 基础上做分析，不是重写生产级 kernel
- RK3588 NPU 侧只做部署与量化，未深入 C++ runtime