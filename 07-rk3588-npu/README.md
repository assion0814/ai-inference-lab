# 07 · RK3588 端侧 AI 部署实测与厂商数据打假

在 Orange Pi 5 Plus（RK3588，6 TOPS NPU）上完成端侧推理部署实测，
并对厂商 RK182X 宣传数据做了**独立复现实验**。

## 结论

- **厂商「RK182X」数字不可复现** —— 那是 RK3588 + PCIe 加速卡的**联合测试**，纯 RK3588 达不到
- **CNN 单核 RK3588 持平甚至反超加速卡**（16.14 ms / 62 FPS，INT8）
- **差距只在 LLM 与多核并发**
- **根因：板端 RKNPU 驱动 v0.9.6 的 DMA 上限** 决定 3B 以上 W8A8 模型根本跑不了
- **YOLOv8n INT8 vs FP16**：延迟 ↓2.00×（32.238 → 16.141 ms），COCO mAP@0.5 持平（0.4511 → 0.4515，噪声内）

## 硬件与软件环境

| 项 | 值 |
|---|---|
| 板子 | Orange Pi 5 Plus（RK3588），`rockchip,rk3588-orangepi-5-plus` |
| SoC | RK3588，8 核（4×A76 2.352GHz + 4×A55），**NPU 6 TOPS / 3 核 / 1.0 GHz** |
| 内存 / eMMC | 7.7 GB / 58.2 G |
| 系统 | Orange Pi 1.0.10 Jammy（Ubuntu 22.04），kernel 6.1.43-rockchip-rk3588 |
| NPU 驱动 | **RKNPU v0.9.6**（→ 这是 3B+ 模型跑不了的根因） |
| 网络 | 2 × RTL8125BG 2.5GbE |
| PC 端 | x86_64 Linux，`rknn-toolkit2 2.0.0b0`（Python 3.10） |
| 板端运行时 | `librknnrt 2.0.0b0`、`rknn_server 2.0.0b0`、`librkllmrt 1.3.1` |

**测量协议**：`performance` 锁频（CPU 大核 2.352GHz / DDR 2112MHz / NPU 1.0GHz），
停桌面（lightdm）降噪，仅计 `rknn_run`（排除数据搬运），100 次测量 / 10 次预热，报中位数。

## 实验一：YOLOv8n 量化对比（FP16 vs INT8）

YOLOv8n @640×640，INT8 校准集 20 张（COCO val2017 子集），验证集 500 张
（**已显式排除校准集，无数据泄漏**），推理在板端 NPU 上实跑。

| 指标 | FP16 | INT8 | 差异 |
|---|---:|---:|---|
| 推理延迟（中位） | 32.238 ms | **16.141 ms** | **↓ 2.00×** |
| 吞吐 | 31.0 FPS | **62.0 FPS** | ↑ 2.00× |
| 模型内存（权重） | 6.03 MiB | 3.05 MiB | ↓ 1.98× |
| 模型内存（中间张量） | 11.72 MiB | 4.30 MiB | ↓ 2.73× |
| 模型内存（合计） | 18.87 MiB | 8.31 MiB | **↓ 2.27×** |
| 进程峰值 RSS | 45.5 MB | 25.4 MB | ↓ 1.79× |
| **COCO mAP@0.5** | 0.4511 | **0.4515** | **+0.0004（噪声内）** |
| **COCO mAP@0.5:0.95** | 0.3384 | 0.3396 | +0.0012 |
| 模型文件大小 | 7.24 MB | 4.11 MB | ↓ 43% |

> FP16 的 mAP@0.5:0.95 = 0.3384 对得上 YOLOv8n 官方 COCO 指标（0.37，500 张抽样噪声内），
> 说明评测链路正确，INT8 的差值可信。

**逐层量化误差（174 层，`accuracy_analysis`）**：详见 [docs/22_RKNN完整实测报告.md](./docs/22_RKNN完整实测报告.md)

## 实验二：厂商 RK182X 数据打假

厂商宣传「RK182X」性能数字，实测发现：

| 测试场景 | 厂商宣传 | 纯 RK3588 实测 | 差距 |
|---|---|---|---|
| CNN 单核 | 高 | 16.14 ms（持平或反超） | ✅ 可复现 |
| LLM 3B+ W8A8 | 支持 | **跑不了（DMA 上限）** | ❌ 不可复现 |
| 多核并发 | 高 | 有差距 | ⚠️ 部分 |

**根因**：`RKNPU v0.9.6` 驱动的 DMA 传输上限，3B 以上 W8A8 模型的权重无法完整加载

详见 [docs/REPORT-rk3588-vs-rk182x.md](./docs/REPORT-rk3588-vs-rk182x.md)

## 实验三：Phase2 Tuning

详见 [docs/REPORT-PHASE2-TUNING.md](./docs/REPORT-PHASE2-TUNING.md)

## 与 02 的关系

| 项 | 02-cross-platform-bench | 07-rk3588-npu（本目录） |
|---|---|---|
| 定位 | 跨平台 benchmark（RK3588 / 780M / 4060Ti） | RK3588 单平台深度 |
| 数据 | 14.57 tok/s、68.3 GB/s 带宽 | YOLOv8 量化、RKNN 部署、厂商打假 |
| 深度 | 到性能数字 | 到驱动层根因（RKNPU v0.9.6 DMA） |

**02 明确说「RK3588 NPU 侧只做部署与量化，未深入 C++ runtime」——本目录补上这一块。**

## 文件清单

```
07-rk3588-npu/
├── README.md                          # 本文
├── docs/
│   ├── 22_RKNN完整实测报告.md          (309 行) YOLOv8 量化全数据
│   ├── 21_RKNN项目README.md            (187 行)
│   ├── 25_RKNN简历可用数据.md          (67 行)
│   ├── RK3588-HARDWARE-ASSETS.md       (389 行) 硬件资产清单
│   ├── REPORT-PHASE2-TUNING.md         (252 行) 调优细节
│   ├── REPORT-hardware-vs-opi5plus.md  (161 行) 硬件对比
│   ├── REPORT-rk3588-vs-rk182x.md      (263 行) 厂商打假
│   ├── REPORT-NEW-HARDWARE.md          (290 行) 新硬件报告
│   └── PLAN-EMBODIED-AI.md             (347 行) 具身智能计划
└── scripts/
    └── hw_probe.sh                     # 硬件探针
```

## 为什么这个项目值得看

1. **不是「跑通了」，是「打假了」** —— 厂商数据不可复现，找出根因（驱动 DMA 上限）
2. **量化收益被拆开算** —— INT8 的 2× 加速里，多少来自数据类型、多少来自内存带宽
3. **mAP 持平不是运气** —— 校准集与验证集显式隔离，排除数据泄漏
4. **端侧到数据中心的全链路** —— 这是本仓库从「6 TOPS NPU」到「V100 748B MoE」叙事的第一环
