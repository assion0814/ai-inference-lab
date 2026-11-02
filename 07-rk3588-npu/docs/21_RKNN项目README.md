# RK3588 端侧 AI 部署实测

在 **Orange Pi 5 Plus（RK3588，8GB）** 上完成的端侧推理部署实测，含 YOLOv8 量化对比、
Qwen2.5/Qwen3 的 RKLLM 部署、逐层 profiling，以及与厂商 RK182X 宣传数据的对照。

> **一句话结论**：厂商宣传的「RK182X」数字是 **RK3588 + PCIe 加速卡**的联合测试，纯 RK3588 不可复现。
> 但实测发现：**CNN 单核 RK3588 持平甚至反超加速卡**，差距只在 LLM 与多核并发；
> 而**板端 RKNPU 驱动 v0.9.6 的 DMA 上限**决定了 3B 以上 W8A8 模型根本跑不了。

---

## 硬件与软件环境

| 项 | 值 |
|---|---|
| 板子 | Orange Pi 5 Plus（RK3588），`compatible = rockchip,rk3588-orangepi-5-plus` |
| SoC | RK3588，8 核（4×A76 2.352GHz + 4×A55），**NPU 6 TOPS / 3 核 / 1.0 GHz** |
| 内存 / eMMC | 7.7 GB / 58.2 G |
| 系统 | Orange Pi 1.0.10 Jammy（Ubuntu 22.04），kernel 6.1.43-rockchip-rk3588 |
| NPU 驱动 | **RKNPU v0.9.6**（→ 这是 3B+ 模型跑不了的根因） |
| 网络 | 2 × RTL8125BG 2.5GbE |
| PC 端 | x86_64 Linux，`rknn-toolkit2 2.0.0b0`（Python 3.10） |
| 板端运行时 | `librknnrt 2.0.0b0`、`rknn_server 2.0.0b0`、`librkllmrt 1.3.1` |

**测量协议**：`performance` 锁频（CPU 大核 2.352GHz / DDR 2112MHz / NPU 1.0GHz），
停桌面（lightdm）降噪，仅计 `rknn_run`（排除数据搬运），100 次测量 / 10 次预热，报中位数。

---

## 实验一：YOLOv8n 量化对比（FP16 vs INT8）

YOLOv8n @640×640，INT8 校准集 20 张（COCO val2017 子集），验证集 500 张（**已显式排除校准集，无数据泄漏**），
推理在板端 NPU 上实跑。

| 指标 | FP16 | INT8 | 差异 |
|---|---:|---:|---|
| 推理延迟（中位） | 32.238 ms | **16.141 ms** | **↓ 2.00×** |
| 吞吐 | 31.0 FPS | **62.0 FPS** | ↑ 2.00× |
| 模型内存（权重） | 6.03 MiB | **3.05 MiB** | ↓ 1.98× |
| 模型内存（中间张量） | 11.72 MiB | **4.30 MiB** | ↓ 2.73× |
| 模型内存（合计） | 18.87 MiB | **8.31 MiB** | **↓ 2.27×** |
| 进程峰值 RSS | 45.5 MB | **25.4 MB** | ↓ 1.79× |
| **COCO mAP@0.5**（500 张） | 0.4511 | **0.4515** | **+0.0004（反而略好，噪声内）** |
| **COCO mAP@0.5:0.95** | 0.3384 | **0.3396** | +0.0012 |
| 模型文件大小 | 7.24 MB | **4.11 MB** | ↓ 43% |

> FP16 的 mAP@0.5:0.95 = 0.3384 对得上 YOLOv8n 官方 COCO 指标（0.37，500 张抽样噪声内），
> 说明评测链路正确，INT8 的差值可信。

### 逐层量化误差（174 层，`accuracy_analysis`）

| 算子类型 | 层数 | 平均单层余弦 | 最低 |
|---|---:|---:|---:|
| Clip | 3 | 0.99786 | 0.99398 |
| Sigmoid | 3 | 0.99845 | 0.99610 |
| Conv | 60 | **0.99987** | 0.99944 |
| MaxPool | 3 | 1.00000 | 1.00000 |

**没有一层单层余弦低于 0.99。掉点集中在输出头（`Clip`/`Sigmoid`，DFL 框解码分支），主干 `Conv` 几乎无损。**

---

## 实验二：Qwen2.5 / Qwen3 的 RKLLM 部署与 Prefill/Decode 分析

模型用 `rkllm-toolkit 1.3.1` 自行从 HuggingFace 权重转换（W8A8，`optimization_level=0`），
板端用自写 C 基准逐 token 打点测 TTFT / TPOT / TPS。

| 模型 | 状态 | TTFT (Prefill) | Decode TPS | 官方 RK182X | RK182X 倍数 |
|---|---|---:|---:|---:|---:|
| Qwen2.5-0.5B | ✅ | 41.12 ms | **32.17** | 223.40 | 6.94× |
| Qwen3-0.6B | ✅ | 48.41 ms | **26.17** | 182.26 | 6.97× |
| Qwen2.5-1.5B | ✅ | 78.98 ms | **14.57** | 149.39 | 10.25× |
| Qwen3-1.7B | ✅ | 85.03 ms | **13.32** | 139.39 | 10.47× |
| Qwen2.5-3B | ❌ | — | — | 103.24 | 撞 DMA 上限 |
| Qwen3-4B | ❌ | — | — | 88.47 | 同上 |
| Qwen2.5-7B | ❌ | — | — | 70.47 | 3.78GiB 分配失败 |
| Qwen3-8B | ❌ | — | — | 61.34 | 8.2GB > 可用 7.1GB |

### 瓶颈结论：Memory-bound，但达不到理论带宽上限

**推导**：
- Qwen2.5-1.5B W8A8 每生成一个 token 要读约 **1.5 GB** 权重
- DDR 实测频点 2112 MHz（LPDDR4x-4266），128-bit → **理论 68.3 GB/s**，实际按 60–75% 算 = **41–51 GB/s**
- **理论 Decode 上限 = 41–51 GB/s ÷ 1.5 GB = 27–34 tok/s**
- **实测 14.57 tok/s → 只达到理论上限的 43–54%**

**⇒ 是 Memory-bound（趋势对），但存在显著额外开销**（KV cache 访存、逐 token 调度、
量化 kernel 未走满、CPU 侧采样），不是"带宽打满"的理想情况。

> 这个结论是**预注册后证伪**的：我先预测 W8A8 1.5B 应落在 30–45 tok/s，
> 实测 14.57 显著低于区间下界 ⇒ 判定"瓶颈不只是带宽"。保留证伪结果而非修改预测。

### 3B 及以上跑不了的硬件根因（实测证据）

```
RKNPU fdab0000.npu: RKNPU: rknpu_gem_get_pages: dma map 3085697024 fail   ← 3B, 2.87GiB
E RKNN: failed to malloc npu memory, size: 3085697024, errno: 14, Bad address
```

- **边界**：2.3GB 分配能过（1.7B 跑通）；2.87GB 分配失败（3B）
- **排除 CMA**：`cma=128M` → `cma=1024M` 重启后复测，**仍然失败**（已恢复原配置）
- **排除内存不足**：`MemAvailable` 7.1 GB，只申请 2.87 GB
- **根因**：板端 **RKNPU 驱动 v0.9.6**，RKLLM 每次运行都警告 `please upgrade to 0.9.7`
- **绕不过**：3B 转 W4A16 也失败 —— `target_platform: rk3588 not support quantized_dtype: w4a16!`（W4A16 是 RK3576 的）

---

## 实验三：与厂商 RK182X 宣传数据的对照

厂商测试条件明写「测试基于 **RK3588 + RK1820/RK1828，两者之间通过 PCIe 连接**」。

**CNN 单核 RK3588 持平甚至反超，差距只在多核并发与 LLM：**

| 模型 | RK3588 单核 | 官方 RK182X 单核 | 比值 | RK3588 三进程并发 | 官方多核 | 比值 |
|---|---:|---:|---:|---:|---:|---:|
| MobileNetV2 @224 | **445.4** | 279.67 | **1.59×** | 1249 | 1280.93 | 0.98× |
| ResNet50V2 @224 | **109.8** | 112.58 | 0.98× | 292.7 | 843.24 | 0.35× |
| YOLOv5s @640 | **64.9** | 34.54 | **1.88×** | 183.6 | 214.49 | 0.86× |
| YOLOv8s @640 | **33.7** | 33.01 | 1.02× | 96.6 | 212.32 | 0.45× |

**多核扩展性差异**：RK3588 三核扩展 ~2.8×（符合 3 核）；RK182X 扩展 4.6–7.5×（核数远多于 3）。

**⇒ 加速卡的护城河是「大内存带宽 LLM」+「多核并发扩展」，不是单路 CNN 算力。**

---

## 关键工程配方

### 锁频（决定性，不锁会低估 30–42%）

```bash
# ⚠️ /sys/class/devfreq/*/ 只覆盖 dmc/gpu/npu，不含 CPU！
for c in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > $c; done
for d in /sys/class/devfreq/*/; do echo performance > $d/governor; done
```

不锁时 DDR 停在 528MHz（应 2112MHz），YOLOv8s 单核从 33.9 → 23.8 FPS；CPU 大核停在 1.8GHz（应 2.352GHz），0.5B 从 32 → 23 TPS。

### 三个 .so 必须与 PC 工具链版本对齐

| 组件 | 症状 | 处置 |
|---|---|---|
| `librknnrt.so` | `Invalid RKNN model version 6` | 1.4.0 → 2.0.0b0 |
| `rknn_server` | `eval_memory` 静默返回空 | 1.3.0 → 2.0.0b0 |
| `librkllmrt.so` | RKLLM 模型加载失败 | 1.1.4 → 1.3.1（与 toolkit 同版本） |

> **`eval_perf` 版本不匹配时仍能跑，只有 `eval_memory` 会静默返回空。「能跑」≠「版本对」。**

### COCO mAP 的塌方陷阱

`pycocotools` 的 `COCOeval` **默认在全部 5000 张 val2017 上评估**。只跑 500 张却不设 `imgIds`，
其余 4500 张全算漏检 ⇒ **mAP 从 0.45 塌到 0.018**。

```python
e.params.imgIds = sorted({p['image_id'] for p in preds})   # 必须
```

---

## 诚实的边界

- 官方表 **VLM / 全模态一个都没跑**（需多模态 runtime）
- **一个未能解释的测量差异**：同一模型同参数，早期 ad-hoc 测 0.5B=40.48 / 1.5B=16.82，
  受控协议测 32.17 / 14.57（低 13–21%）。已验证**不是热降频**（连跑 20 次 TPS 31.49–31.91 极稳，
  温度 50.8→54.5°C 但频点锁死）。矛盾点：早期值更接近官方基准。**原因未查明，两组值都保留。**
- 无法排除高仿板（看不到丝印/料号），但软件标识与关键器件（2×RTL8125BG）均与官方 OPi 5 Plus 一致

---

## 文件结构

```
REPORT-final.md              完整实测报告（含全部环境坑与复现命令）
REPORT-hardware-vs-opi5plus.md  硬件核对：板子实报 vs 手册规格 vs 原理图
REPORT-rk3588-vs-rk182x.md   RK182X 数据复现尝试
PLAN-v2.md                   判据版计划（预注册 + 负对照）
COMPLETION-STATUS.md         计划完成度逐条核对
data/board_measurements.json 全部板端实测数据（CNN/LLM/内存/并发/官方对照）
data/coco_map.json           COCO mAP: FP16 vs INT8
data/accuracy_analysis.json  174 层逐层量化误差
src/rknn_bench.c             板端 RKNN C 基准（只计 rknn_run + 核心掩码 + 内存）
src/rkllm_bench_v2.cpp       板端 RKLLM C 基准（TTFT/TPOT/TPS，适配 1.3.1 API）
src/coco_map.py              COCO mAP 评测（含 imgIds 修正）
src/board_eval.py            PC→板端 eval_perf/eval_memory
src/llm_official_pipeline.sh 6 个 LLM 的自动化流水线
board-dt/board.dts           从板子实际 dtb 反编译的设备树（13,264 行）
```
