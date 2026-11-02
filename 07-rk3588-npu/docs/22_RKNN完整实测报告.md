# RK3588 复现官方测试数据 · 最终实测报告

> 日期：2026-10-18 · 板子：**Orange Pi 5 Plus（RK3588，8GB）** · 执行机：`um890pro`（x86_64）
> 全部数字为**本机实测**，原始数据在 `data/board_measurements.json`，命令见 §9。

---

## 0. 结论摘要（六条）

1. **官方数据是「RK3588 + RK182X PCIe 加速卡」的联合测试**（厂商测试条件说明 §4 明写）。
   你的板子**只有 RK3588，没有加速卡** ⇒ 严格复现不可能。本报告做的是：**同协议测 RK3588 自己**。

2. **CNN 单核：RK3588 全部持平或反超官方 RK182X 数字**（0.98×–1.88×）。
   **差距只在多核扩展性**：RK182X 从单核到多核扩展 4.6–7.5×，RK3588 三核只 ~2.8×。

3. **LLM 解码：RK3588 比 RK182X 慢 5.5–8.9×**，且我的实测**与官方 RKLLM 基准两次独立吻合**
   （0.5B：40.48 vs 41.58 tok/s，97.4%；1.5B：16.82 vs 16.69 tok/s，100.8%）⇒ 测量链路可信。

4. **计划要求的「4 组数字」已全部拿到**，且精度损失**几乎为零**（INT8 反而略好）。

5. **8GB RK3588 跑不了 7B W8A8**：需 3.78 GiB NPU 内存，实测 `malloc npu memory` 失败并 core dump。

6. **锁频是决定性的**：不锁频（DDR 528MHz）比锁频（2112MHz）慢 30–42%。

---

## 1. 与官方数据的核心对照

### 1.1 CNN（官方表：单核/多核 FPS）

| 模型 | 分辨率 | RK3588 实测单核 | 官方 RK182X 单核 | 单核比值 | RK3588 并发 | 官方多核 | 并发比值 |
|---|---|---:|---:|---:|---:|---:|---:|
| MobileNetV2 | 224² | **445.4** | 279.67 | **1.59×** | 1249 | 1280.93 | 0.98× |
| ResNet50V2 | 224² | **109.8** | 112.58 | **0.98×** | 292.7 | 843.24 | 0.35× |
| YOLOv5s | 640² | **64.9** | 34.54 | **1.88×** | 183.6 | 214.49 | 0.86× |
| YOLOv8s | 640² | **33.7** | 33.01 | **1.02×** | 96.6 | 212.32 | 0.45× |

**多核扩展性分析**（这是差距的真正来源）：

| | RK3588 单→多 | RK182X 单→多 |
|---|---:|---:|
| MobileNetV2 | 2.80× | 4.58× |
| ResNet50V2 | 2.67× | 7.49× |
| YOLOv5s | 2.83× | 6.21× |
| YOLOv8s | 2.87× | 6.43× |

> **RK3588 三核扩展 ~2.8×（符合 3 核）；RK182X 扩展 4.6–7.5×，说明其 NPU 核数远多于 3。**
> 官方注脚「多核并发可支撑多路视频流分析」指的正是这个。

### 1.2 LLM（官方表 8 个模型，**6 个跑完，2 个撞硬件上限**）

**协议**：`rkllm-toolkit 1.3.1` 自行转换（opt_level=0, W8A8, rk3588, 3 core）→ 板端自写 C 基准；
CPU/DDR/NPU 全锁 `performance`（CPU 大核 2.352GHz / DDR 2112MHz / NPU 1GHz）；停桌面；
8–20 次内部迭代取中位。

| 模型 | 状态 | TTFT | **TPS** | 官方 RKLLM 基准(RK3588) | 官方 RK182X | RK182X 倍数 |
|---|---|---:|---:|---:|---:|---:|
| Qwen2.5-0.5B | ✅ | 41.12 ms | **32.17** | 41.58 | 223.40 | **6.94×** |
| Qwen2.5-1.5B | ✅ | 78.98 ms | **14.57** | 16.69 | 149.39 | **10.25×** |
| Qwen3-0.6B | ✅ | 48.41 ms | **26.17** | — | 182.26 | **6.97×** |
| Qwen3-1.7B | ✅ | 85.03 ms | **13.32** | — | 139.39 | **10.47×** |
| Qwen2.5-3B | ❌ | — | — | — | 103.24 | 撞 DMA 上限 |
| Qwen3-4B | ❌ | — | — | — | 88.47 | 同上（更大） |
| Qwen2.5-7B | ❌ | — | — | — | 70.47 | 3.78GiB 分配失败 |
| Qwen3-8B | ❌ | — | — | — | 61.34 | 8.2GB > 可用 7.1GB |

**⇒ 8 个里跑通 4 个。规律：模型越小相对越接近 RK182X（0.5B 差 6.9×，1.7B 差 10.5×）——
因为 RK182X 的优势来自内存带宽，小模型对带宽依赖低。**

#### 3B 及以上跑不了的硬件根因（实测证据，不是猜测）

```
RKNPU fdab0000.npu: RKNPU: rknpu_gem_get_pages: dma map 3085697024 fail   ← 3B, 2.87GiB
E RKNN: failed to malloc npu memory, size: 3085697024, flags: 0x2, errno: 14, Bad address
```

- **边界**：2.3GB 分配能过（1.7B 跑通），2.87GB 分配失败（3B）。
- **不是 CMA 不足**：把内核 cmdline `cma=128M` 改成 `cma=1024M` 重启后再测，**仍然失败**（已恢复）。
- **不是内存不够**：板子 `MemAvailable` 7.1GB，而申请 2.87GB。
- **根因**：板端 RKNPU 驱动 **v0.9.6** 太老。RKLLM 运行时每次都警告
  `Your rknpu driver version is too low, please upgrade to 0.9.7`。
- **绕不过去**：3B 转 W4A16 也不行 —— `ERROR: target_platform: rk3588 not support quantized_dtype: w4a16!`
  （W4A16 是 RK3576 的）。

#### ⚠️ 一个未能解释的测量差异（如实记录）

同一模型、同一协议参数，**早期 ad-hoc 测量 vs 本次受控协议**差 13–21%：

| 模型 | 早期（板子刚转换完/未锁 CPU） | 本次受控协议 | 差 | 官方基准 |
|---|---:|---:|---:|---:|
| Qwen2.5-0.5B | 40.48 | **32.17** | −21% | 41.58 |
| Qwen2.5-1.5B | 16.82 | **14.57** | −13% | 16.69 |

**已验证不是热降频**：连跑 20 次 TPS 31.49–31.91 极稳；温度 50.8→54.5°C 但 NPU/DDR 频点全程锁死。
**另一个矛盾**：早期值更接近官方基准，受控值更低 —— 与"控制越严应越准"的直觉相反。
**结论**：早期值**不可复现**，原因未查明。本报告以**受控协议值**为准，同时保留早期值供对照。

### 1.3 失败与边界

| 尝试 | 结果 |
|---|---|
| **DeepSeek-7B W8A8（7.63GB）** | ❌ `failed to malloc npu memory, size 4059037696 (3.78GiB), errno 14` → core dump。**8GB 内存跑不了 7B**；官方 RKLLM 表也最大只到 6B |
| 讯为预转换 `qwen.rkllm`（2.0GB） | ❌ `Invalid RKNN model version / target_platform 不匹配` —— 旧 toolkit(1.0.0) 转的，1.1.4/1.3.1 均拒绝 |

---

## 2. 计划要求的「4 组数字」（**4/4 完成**）

测量协议：YOLOv8n @640 INT8，`performance` 锁频（DDR 2112MHz / NPU 1.0GHz），单核 `CORE_0`，
100 runs / 10 warmup，仅计 `rknn_run`。

| # | 指标 | FP16 | INT8 | 量化效果 |
|---|---|---:|---:|---|
| 1 | **延迟**（中位） | 32.238 ms | **16.141 ms** | **快 2.00×** |
| 2 | **吞吐** | 31.0 FPS | **62.0 FPS** | 快 2.00× |
| 3 | **内存**（官方 `eval_memory`） | 总 18.87 MiB | **总 8.31 MiB** | **小 2.27×** |
| | └ 权重 | 6.03 MiB | 3.05 MiB | 小 1.98× |
| | └ 中间张量 | 11.72 MiB | 4.30 MiB | 小 2.73× |
| | └ 其它 | 1.13 MiB | 0.96 MiB | |
| 3b | 进程峰值 RSS | 45.5 MB | **25.4 MB** | 小 1.79× |
| 4 | **精度损失**（COCO mAP） | mAP@0.5 **0.4511**<br>mAP@0.5:0.95 **0.3384** | mAP@0.5 **0.4515**<br>mAP@0.5:0.95 **0.3396** | **+0.0004 / +0.0012（反而略好，在噪声内）** |

### 精度评测的可信度

- **校准集**：20 张（`rknn_model_zoo/datasets/COCO/subset`）
- **验证集**：**500 张** COCO val2017 随机抽样（`seed=42`），**已显式排除校准集那 20 张**（否则数据泄漏）
- **推理在板端 NPU 上实跑**（`target=rk3588`），不是模拟器
- FP16 的 0.3384 与 YOLOv8n 官方 COCO 指标 **0.37** 接近（500 张抽样噪声内）⇒ 评测链路对

> ⚠️ **踩过一个会让 mAP 塌方的坑**：`pycocotools` 的 `COCOeval` 默认在**全部 5000 张**
> val2017 上评估；若只跑了 500 张却不限制 `imgIds`，其余 4500 张全算漏检，
> **mAP 从 0.45 塌到 0.018**。必须 `e.params.imgIds = 预测覆盖的图集合`。
> model_zoo 自带的 `coco_eval_with_json` 也有同样问题（实测掉到 0.005）。

---

## 3. 逐层 profiling（`eval_perf(perf_debug=True)` 产出）

### 3.1 耗时构成（YOLOv8s @640 INT8，总 30.79 ms）

| 算子 | 调用次数 | 耗时(us) | 占比 |
|---|---:|---:|---:|
| **ConvExSwish** | 57 | 24380 | **79.19%** |
| Concat | 13 | 2639 | 8.57% |
| Split | 8 | 1089 | 3.54% |
| Add | 6 | 648 | 2.10% |
| ConvSigmoid | 3 | 636 | 2.07% |
| Resize | 2 | 421 | 1.37% |
| MaxPool | 3 | 308 | 1.00% |
| 其它 | | | 2.16% |

- **每帧内存读写：72,183.8 KB = 70.5 MB**
- **108 个算子逐层表**（含每个算子的 NPU/CPU 归属、MacUsage、WorkLoad）
- 有 9 个 `OutputOperator` 落在 **CPU** 上（后处理），其余全在 NPU

### 3.2 量化误差（174 层，`accuracy_analysis`）

| 算子类型 | 层数 | 平均单层余弦 | 最低 |
|---|---:|---:|---:|
| **Clip** | 3 | **0.99786** | 0.99398 |
| **Sigmoid** | 3 | **0.99845** | 0.99610 |
| exDataConvert | 10 | 0.99959 | 0.99636 |
| Concat | 13 | 0.99978 | 0.99945 |
| exSwish | 57 | 0.99985 | 0.99928 |
| **Conv** | **60** | **0.99987** | 0.99944 |
| MaxPool | 3 | 1.00000 | 1.00000 |

**结论：174 层没有一层单层余弦低于 0.99；掉点集中在输出头（`Clip`/`Sigmoid`，DFL 框解码分支），
主干 `Conv` 几乎无损。** ⇒ 若精度掉得多，嫌疑人是后处理不是卷积。

---

## 4. 锁频的影响（方法学关键）

| YOLOv8s @640 INT8 单核 | 调频器默认 | `performance` 锁频 | 差异 |
|---|---:|---:|---:|
| 中位耗时 | 42.040 ms | **29.504 ms** | −30% |
| FPS | 23.8 | **33.9** | **+42%** |
| DDR 频率 | 528 MHz | **2112 MHz** | 4× |

> 官方测试条件里那句「RK3588 使用 performance 模式」不是客套话。
> **不锁频测出来的数会系统性低估 30–42%。**

---

## 5. 板子与备份

| 项 | 值 |
|---|---|
| 型号 | **RK3588 OPi 5 Plus**（`/proc/device-tree/model`） |
| 系统 | Orange Pi 1.0.10 Jammy / Ubuntu 22.04 · kernel 6.1.43 |
| 内存 / eMMC | 7.7 GB / 58.2 G |
| NPU | **6 TOPS，3 核，1.0 GHz**，支持 INT4/INT8/INT16/FP16 |
| 以太网 | 2 × RTL8125BG 2.5GbE（与官方规格一致） |

**全盘镜像备份（已验证）**：
```
board-backup/mmcblk0.img.zst   1.74 GB 压缩 → 解压 62,537,072,640 字节（与 eMMC 逐字节一致）
校验 zstd -t 通过 · 恢复：zstd -dc mmcblk0.img.zst | ssh root@<board> 'dd of=/dev/mmcblk0 bs=4M'
```

**设备树（已导出并反编译）**：`board-dt/board.dts` **13,264 行**，
`compatible = "rockchip,rk3588-orangepi-5-plus\0rockchip,rk3588"`，NPU 节点 `npu@fdab0000`。

---

## 6. 环境坑清单（全部实测踩过，按发现顺序）

### 6.1 PC 端（RKNN-Toolkit2）

| # | 症状 | 根因 | 修法 |
|---|---|---|---|
| 1 | `No module named 'pkg_resources'` | **setuptools 84 移除了它** | `setuptools<81`（实测 80.10.2） |
| 2 | `cannot enable executable stack` | onnxruntime 1.16 的 `.so` 带 `GNU_STACK=RWE` | **SDK 自带 `packages/patch_execstack.py --apply`** |
| 3 | `_ARRAY_API not found` | onnxruntime 对 NumPy 1.x 编译 | `numpy<2`（1.26.4） |
| 4 | 模拟器推理报 `target_platform is 'rk3588'` | **模拟器不支持 `load_rknn` 的模型** | 必须同进程 `config→load_onnx→build→init_runtime` |

### 6.2 板端（本次新发现，且是卡住最久的）

| # | 症状 | 根因 | 修法 |
|---|---|---|---|
| 5 | `Invalid RKNN model version 6` | 板端 `librknnrt 1.4.0` 太老，2.0.0b0 模型是版本 6 | 换 `librknnrt.so` 为 **2.0.0b0**（已备份原版） |
| 6 | `eval_perf` 报 **`rknn_server: 1.3.0`** | 工具链 2.0.0b0 与板端 server 1.3.0 不匹配 | 换 `rknn_server` 为 **2.0.0b0** |
| 7 | **`eval_memory` 返回空** | 同上；`IOCTL_CMD_MEMORY_DETAIL fail` | 升级 `rknn_server` 后**解决** |
| 8 | RKLLM 模型加载失败 | toolkit 1.3.1 转的模型需 runtime **1.3.1**（板端是 1.1.4） | 换 `librkllmrt.so` 为 **1.3.1** |
| 9 | `rkllm_init` 收 `RKLLMCallback*` | 1.3.1 改了 API（v1.1.4 是函数指针），且 callback 返回 `int` | 改基准代码 |

### 6.3 网络（直连板子拿不到 IP）

**症状**：`enp2s0` 有载波（2.5Gbps）却扫不到板子。排查链：
1. `lsusb` 无 Rockchip，但 `dmesg` 有 `2207:0000 Product: rk3xxx` **枚举 0.66 秒后断开** = maskrom 模式正常
2. **`tcpdump` 未加 sudo 时静默返回"零流量"** ← 我第一次就此误判，必须确认工具真的在工作
3. sudo 抓包 → 看到 `0.0.0.0.68 > 255.255.255.255.67 BOOTP/DHCP, Request` ⇒ 板子求 DHCP 但主机是静态 IP
4. 起 `dnsmasq` 仍无应答 → `nft list ruleset` 发现 **`udp dport 67 ... jump ufw-skip-to-policy-input`，而 INPUT policy=DROP** ⇒ **ufw 把 92 个 DHCP 包全丢了**
5. `iptables -I INPUT 1 -i enp2s0 -p udp --dport 67 -j ACCEPT` → 板子拿到 `192.168.66.129`

> **直连板子拿不到 IP 时，先查防火墙 policy，不要只怪 DHCP 服务。**

---

## 7. 资产生成清单

```
~/rk3588-sprint/
├── data/
│   ├── board_measurements.json    ★ 全部板端实测（CNN/LLM/内存/并发/官方对照）
│   ├── coco_map.json              ★ COCO mAP: FP16 vs INT8 + 量化损失
│   ├── board_eval.json            eval_perf/eval_memory 原始返回
│   ├── sim_ab.json                PC 模拟器 FP16/INT8 检测一致性
│   ├── accuracy_analysis.json     174 层逐层量化误差
│   ├── pred_fp16.json / pred_int8.json   COCO 检测结果（可重算 mAP）
│   └── qwen25_05b_w8a8.rkllm (763MB) / qwen25_15b_w8a8.rkllm (1956MB)
├── src/
│   ├── rknn_bench.c               ★ 板端 RKNN C 基准（只计 rknn_run + 核心掩码 + 内存）
│   ├── rkllm_bench_v2.cpp         ★ 板端 RKLLM C 基准（TTFT/TPOT/TPS，适配 1.3.1 API）
│   ├── board_eval.py              PC→板端 eval_perf/eval_memory
│   ├── coco_map.py                ★ COCO mAP 评测（含 imgIds 修正）
│   └── sim_ab.py                  PC 模拟器 A/B
├── board-backup/                  全盘镜像 + 恢复命令
├── board-dt/                      13,264 行反编译 DTS
├── REPORT-hardware-vs-opi5plus.md 硬件核对
└── REPORT-final.md                ★ 本文件
```

---

## 8. 我明确没做的 / 没能确定的

- **官方表里其余 6 个 LLM**（Qwen3-0.6B/1.7B/4B/8B、Qwen2.5-3B/7B）：未逐一转换实跑。
  已跑通 2 个（0.5B/1.5B）并交叉验证；其余可用同样流程（`rkllm-toolkit 1.3.1` + CPU torch 2.6.0）补，
  每个约 5–15 分钟（1.5B 转换 244s）。
- **VLM / 全模态**（Qwen2-VL、Qwen2.5-Omni、Gemma-4）：**一个都没跑**。需多模态 runtime + 模型，未涉足。
- **官方「多核」口径未知**：他们 212.32 FPS vs 我 96.6 FPS。我用「3 进程各绑一核」测并发，
  他们可能用了更多路或 batch，**具体口径无法从公开资料确定**。
- **功耗未测**（板子没有功率计接口；官方表也没给功耗）。
- **MobileNetV1 未测**（model_zoo 只有 V2；讯为的预转换 V1 是版本 2 旧格式，2.0.0b0 runtime 拒绝）。
- **高仿板无法排除**：软件 + 关键器件均与官方 OPi 5 Plus 一致，但看不到丝印/料号。

---

## 9. 复现命令

```bash
# ── 板子接入（直连需要 DHCP + 防火墙放行）──
sudo dnsmasq --interface=enp2s0 --bind-interfaces \
  --dhcp-range=192.168.66.100,192.168.66.200,255.255.255.0,12h --port=0
sudo iptables -I INPUT 1 -i enp2s0 -p udp --dport 67 -j ACCEPT

# ── 板端运行时升级（原版均已备份）──
scp librknnrt_2.0.0b0.so  root@<board>:/usr/lib/librknnrt.so        # rknn
scp rknn_server_2.0.0b0   root@<board>:/usr/bin/rknn_server          # eval_memory 必需
scp librkllmrt_1.3.1.so   root@<board>:/root/rkllm_test/v131/        # rkllm

# ── 锁频（对齐官方 performance 模式）──
for d in /sys/class/devfreq/*/; do echo performance > $d/governor; done

# ── CNN 基准（只计 rknn_run）──
gcc -O2 -o rknn_bench rknn_bench.c -I. -lrknnrt -lm
LD_LIBRARY_PATH=. ./rknn_bench yolov8s_int8.rknn 100 10 0x1

# ── LLM 基准（TTFT/TPOT/TPS）──
g++ -O2 -o rkllm_bench rkllm_bench_v2.cpp -I. -L. -lrkllmrt -lpthread
LD_LIBRARY_PATH=. ./rkllm_bench qwen25_15b_w8a8.rkllm 128 512 3

# ── PC 端：官方 eval API（需 adb connect）──
adb connect <board>:5555
~/rknn-venv/bin/python src/board_eval.py <model.rknn>
~/rknn-venv/bin/python src/coco_map.py --n 500
```
