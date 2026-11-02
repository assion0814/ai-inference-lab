# RK3588 复现官方 RK182X 数据 · 实测报告

> 日期：2026-10-18 · 板子：**Orange Pi 5 Plus（RK3588）** · 执行机：`um890pro`（x86_64）
> 所有数字均由本机实测，命令见 §8；原始数据在 `data/board_measurements.json`

---

## 0. 结论先行

### ① 必须先纠正一个前提：**官方数字不是纯 RK3588 跑出来的**

你给的第 4 张图「测试条件说明」写得很明确：

> **2. RK182X 表示加速芯片可为 RK1820 或 RK1828。**
> **4. 测试基于 RK3588 + RK1820/RK1828，两者之间通过 PCIe 连接，RK3588 使用 performance 模式。**

**所以那 8 款 LLM、3 款全模态、6 款 CNN 的数字，是「RK3588 + RK182X PCIe 加速卡」的联合测试。**
你的 Orange Pi 5 Plus **只有 RK3588，没有加速卡**。
⇒ **严格意义上的"复现"不可能**。能做的是：**用同一套协议测 RK3588 自己，量化加速卡值多少。**

### ② CNN 单核：**RK3588 已经打平甚至略胜**

| 指标 | RK3588 本机实测 | 官方 RK182X | 比值 |
|---|---:|---:|---:|
| YOLOv8s @640 INT8 **单核 FPS** | **33.9** | 33.01 | **1.03×** |
| YOLOv8s @640 INT8 **三核并发 FPS** | **96.6** | 212.32 | **0.45×**（RK182X 是 2.2 倍） |

**结论：单路推理 RK3588 不输；差距在"多路并发吞吐"——这正是官方注脚说的
「多核并发可支撑多路视频流分析」。**

### ③ 锁频是决定性的，不锁会低估 30–42%

| 配置（YOLOv8s 单核） | 调频器默认 | performance 锁频 | 提升 |
|---|---:|---:|---:|
| 中位耗时 | 42.040 ms | **29.504 ms** | −30 % |
| FPS | 23.8 | **33.9** | **+42 %** |

原因：空闲时 DDR 只跑 **528 MHz**，锁频后拉到 **2112 MHz**（4×）。
**官方条件里那句「RK3588 使用 performance 模式」不是客套话，是数量级差异。**

### ④ LLM 部分：**本轮未测，原因是工具链**（不是板子不行）

板子有 `librkllmrt.so` + `rkllm.h`，但**没有任何 `.rkllm` 模型**，而 RKLLM 的模型必须用
`rkllm-toolkit` 在 PC 上从 HF 权重转换。该 SDK 仓库 **484 MB**，本机克隆 20 分钟只下了 27 MB。
⇒ **LLM 对比缺失，如实标注。**

---

## 1. 板子实况（实测）

| 项 | 值 |
|---|---|
| 型号 | **RK3588 OPi 5 Plus**（`/proc/device-tree/model`） |
| 系统 | Orange Pi 1.0.10 Jammy / Ubuntu 22.04 |
| 内核 | 6.1.43-rockchip-rk3588 |
| 内存 | **7.7 GB** |
| eMMC | **58.2 G**（p1 1G vfat /boot；p2 56.6G ext4 /，用 4.7G） |
| **RKNPU 驱动** | **v0.9.6** |
| NPU 频率 | **1.0 GHz**（`fdab0000.npu`，与官方「均为 1GHz」一致） |
| DDR | `dmc` 可用 528/1068/1560/**2112** MHz |
| Python | 3.10.12（无 pip） |
| 编译器 | gcc / g++ ✅ |
| 默认 SSH | `root/orangepi` |

> **板子型号与计划假设不符**：计划里写的是 NanoPC-T6，实际是 **Orange Pi 5 Plus**。

---

## 2. 备份（先说这个，因为它是安全底线）

### 2.1 全盘镜像（已验证）

```
文件      : ~/rk3588-sprint/board-backup/mmcblk0.img.zst
压缩后    : 1.74 GB
解压后    : 62,537,072,640 字节  ← 与 eMMC 容量逐字节一致
校验      : zstd -t 通过（含 XXH64）
恢复命令  : zstd -dc mmcblk0.img.zst | ssh root@<board> 'dd of=/dev/mmcblk0 bs=4M'
```

### 2.2 过程踩的坑（值得记）

| 尝试 | 问题 | 解法 |
|---|---|---|
| 板端 `gzip -1` | RK3588 单核跑 gzip 只有 **0.35 MB/s**（占 77 % CPU），全盘要 2 小时+ | **压缩搬到主机**，板端只 `dd` |
| 主机 `zstd -T8` | 一段时间**输出不增长**，我误判为卡死 | 查 `dd` 的 `/proc/PID/fdinfo/0` 读偏移：**已完成 94 %，是尾部高熵数据压缩比下降** |

> **教训：判断"卡死"要用输入侧的进度（dd 读偏移），不要用输出侧的文件大小。**

### 2.3 设备树（你要的第二件事，已完成）

板子自带 `dtc`，直接反编译：

```bash
# 板端
DTB=/boot/dtb-6.1.43-rockchip-rk3588/rockchip/rk3588-orangepi-5-plus.dtb   # 282 KB
dtc -I dtb -O dts -o board.dts "$DTB"                                      # → 13,264 行 DTS
```

**已拉回本机 `~/rk3588-sprint/board-dt/`**：`board.dts`（351 KB，13,264 行）·
`board.dtb` · 原始 `rk3588-orangepi-5-plus.dtb` · `board-dt.tar.gz`。

```
model      = "RK3588 OPi 5 Plus";
compatible = "rockchip,rk3588-orangepi-5-plus\0rockchip,rk3588";
NPU 节点   = npu@fdab0000
```

> **设备树丢了不用慌**：有板子就能 `dtc -I dtb -O dts` 拿回完整源码。
> 这比"有讯为的源码但设备树要重配"更直接——**这份 DTS 就是这台板子实际在用的配置**。

---

## 3. 兼容性问题：板端运行时太老（已解决）

| | 版本 |
|---|---|
| 板端原 `librknnrt.so` | **1.4.0 (2022-09-09)** |
| 我的 PC `rknn-toolkit2` | **2.0.0b0 (2024-03-24)** |
| 结果 | **`Invalid RKNN model version 6`** → 模型加载失败 |

**修法**：把板端运行时换成 2.0.0b0（已备份原版为 `librknnrt.so.1.4.0.bak`）：

```bash
cp -n /usr/lib/librknnrt.so /usr/lib/librknnrt.so.1.4.0.bak   # 备份
cp /tmp/librknnrt_2.0.0b0.so /usr/lib/librknnrt.so            # 替换
```

**结果：驱动 v0.9.6 + 运行时 2.0.0b0 实测可用**（`rknn_init OK`）。
⇒ **RK3588 不需要重新烧镜像就能跑 2.0 模型，只换一个 .so。**

---

## 4. 测量方法（可复现）

自写 C 基准（`src/rknn_bench.c`，板端 gcc 编译），**只对 `rknn_run` 计时**，
排除 `inputs_set` / `outputs_get` 的数据搬运：

```
100 runs · 10 warmup · 报中位数/均值/标准差/P95
核心掩码 rknn_set_core_mask: 0x1=CORE_0, 0x7=CORE_0_1_2
锁频: /sys/class/devfreq/{dmc,npu,gpu}/governor = performance
      实测锁定 dmc=2112MHz npu=1000MHz gpu=1000MHz
```

**并发吞吐法**（对齐官方"多核"口径）：起 **3 个独立进程**，各 `set_core_mask` 到
`0x1/0x2/0x4`，同时跑，**合计 FPS = 三进程 FPS 之和**。

---

## 5. CNN 实测结果

### 5.1 单流延迟（performance 锁频）

| 模型 | 分辨率 | 精度 | 核心 | 中位耗时 | FPS |
|---|---|---|---:|---:|---:|
| YOLOv8n | 640×640 | INT8 | 单核 | 16.275 ms | **61.4** |
| YOLOv8n | 640×640 | INT8 | 三核(顺序) | 15.699 ms | 63.7 |
| **YOLOv8s** | 640×640 | INT8 | 单核 | **29.504 ms** | **33.9** |
| YOLOv8s | 640×640 | INT8 | 三核(顺序) | 28.797 ms | 34.7 |

> **单流下三核几乎没用（+3~8 %）**：一张图无法跨核并行。
> **多核是给多路并发用的**，不是让单张图变快。

### 5.2 三进程并发吞吐

| 模型 | core0 FPS | core1 FPS | core2 FPS | **合计** | 三核顺序对照 | 并发倍数 |
|---|---:|---:|---:|---:|---:|---:|
| YOLOv8n | 60.1 | 63.3 | 65.6 | **189.0** | 63.7 | **2.97×** |
| **YOLOv8s** | 32.4 | 31.5 | 32.7 | **96.6** | 34.7 | **2.78×** |

### 5.3 与官方数据对比

| 指标 | RK3588 本机 | 官方 RK182X | 比值 | 判读 |
|---|---:|---:|---:|---|
| YOLOv8s 单核 FPS | **33.9** | 33.01 | **1.03×** | **持平** |
| YOLOv8s 三核并发 FPS | **96.6** | 212.32 | **0.45×** | RK182X 约 **2.2×** |

**这是本轮最有价值的结论**：厂商宣传的"千帧吞吐"差距**不在单路性能**，而在**并发扩展性**。
RK3588 单路已经够用（YOLOv8s 33.9 FPS > 单路摄像头 30 FPS 需求），
RK182X 的价值是**同样机箱里塞更多路**。

> 温度 49 °C，未触发降频；NPU 与 DDR 均锁定在最高频点。

---

## 6. LLM 部分：**未测，原因明确**

| 项 | 状态 |
|---|---|
| 板端 RKLLM 运行时 | ✅ `/usr/lib/librkllmrt.so`（11.4 MB）+ `/usr/include/rkllm.h` |
| 板端 `.rkllm` 模型 | ❌ **一个都没有**（全盘搜索无结果） |
| PC 端 `rkllm-toolkit` | ❌ 本机无 |
| SDK 获取 | GitHub `airockchip/rknn-llm` 仓库 **484 MB**，克隆 20 分钟仅下 27 MB |

**RKLLM 与 RKNN 是两条独立链路**：RKNN 不能转 LLM，必须 `rkllm-toolkit` 把 HF 权重转成 `.rkllm`。

**已备好的路径**（`~/rknn-llm` 克隆继续在后台跑，或按 `~/edgeai-project/stages/00-infra/环境-A3b-RKLLM链路.md`）：
1. 从 zbox 取 RKLLM SDK（提取码 `rkllm`）装 `rkllm-toolkit`
2. 转 Qwen2.5-0.5B/1.5B 为 w8a8 `.rkllm`
3. 推到板子，用 `rkllm.h` 写 C 测试：**输入 128 token / 生成 128 token**，报 TTFT / TPOT / Decode TPS（与官方口径一致）

**在未测之前，我不对 LLM 做任何推断。** 官方 RK182X 的 Qwen2.5-1.5B 是
TTFT 49.14 ms / TPOT 6.69 ms / 149.39 TPS；RK3588 纯 NPU 预计显著更低，**但没有实测就不写。**

---

## 7. 本次资产清单

```
~/rk3588-sprint/
├── board-backup/
│   ├── mmcblk0.img.zst      全盘镜像 1.74 GB（解压 62.5 GB，校验通过）
│   ├── BACKUP-INFO.txt      备份元数据与恢复命令
│   └── boot-and-dt.tar.gz   1.4 MB 快速恢复包（boot + dtb + 原运行时）
├── board-dt/
│   ├── board.dts            13,264 行反编译设备树
│   ├── board.dtb            282 KB
│   └── rk3588-orangepi-5-plus.dtb
├── src/rknn_bench.c         板端基准（只计 rknn_run + 核心掩码）
├── src/sim_ab.py            PC 端模拟器精度 A/B
└── data/
    ├── board_measurements.json   全部板端实测 + 官方对照
    ├── sim_ab.json               FP16/INT8 模拟器对比
    └── accuracy_analysis.json    174 层逐层量化误差
```

---

## 8. 复现命令

```bash
# 1. 板子接入（直连需要 DHCP + 防火墙放行）
sudo dnsmasq --interface=enp2s0 --bind-interfaces \
  --dhcp-range=192.168.66.100,192.168.66.200,255.255.255.0,12h --port=0
sudo iptables -I INPUT 1 -i enp2s0 -p udp --dport 67 -j ACCEPT

# 2. 运行时升级
sshpass -p orangepi scp librknnrt.so root@192.168.66.129:/tmp/librknnrt_2.0.0b0.so
ssh root@192.168.66.129 'cp -n /usr/lib/librknnrt.so /usr/lib/librknnrt.so.1.4.0.bak
                         cp /tmp/librknnrt_2.0.0b0.so /usr/lib/librknnrt.so'

# 3. 锁频（对齐官方 performance 模式）
for d in /sys/class/devfreq/*/; do echo performance > $d/governor; done

# 4. 编译并测
scp rknn_bench.c rknn_api.h root@192.168.66.129:/tmp/
ssh root@192.168.66.129 'cd /tmp && gcc -O2 -o rknn_bench rknn_bench.c -I. -lrknnrt -lm
                         LD_LIBRARY_PATH=/usr/lib ./rknn_bench yolov8s_int8.rknn 100 10 0x1'
```

---

## 9. 我明确没有做的 / 没能确定的

- **LLM / 全模态全部未测**（无 `.rkllm` 模型，SDK 下载未完成）。
- **未测 MobileNet / ResNet**（官方表里的分类网络）。可以补，但需要另一批模型转换。
- **未测功耗**（官方表也没给功耗；板子没有功率计接口）。
- **"多核"口径是推断**：官方 212.32 FPS 与单核 33.01 差 6.4×，而我实测 3 核并发只有 2.8–3.0×
  ⇒ 官方可能用了 >3 路并发或 batch，**具体口径未知**。
- **模型精度未验**：本次全程用灰度占位输入只测算力，**没有跑 mAP**。
  精度数据在 `data/sim_ab.json`（模拟器）与 `data/accuracy_analysis.json`（逐层）。
- **运行时升级的风险已用备份兜底**，但驱动 v0.9.6 与 runtime 2.0.0b0 的**长期稳定性未验证**。
