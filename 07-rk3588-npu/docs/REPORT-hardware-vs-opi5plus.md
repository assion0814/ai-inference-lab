# 硬件核对报告：这块板子 vs Orange Pi 5 Plus 参考设计

> 依据：**OPi 5 Plus 用户手册 v2.2（631 页）** + **OPI 5 PLUS V1.3.1 原理图（33 页）**
> 方法：把**板子自己报出来的硬件**（设备树 / lspci / debugfs）与手册规格、原理图元件逐项对撞。
> 原始 PDF 已转文本：`/tmp/opi/{manual,schematic}.txt`

---

## 0. 结论

**软件层面：无疑就是 Orange Pi 5 Plus。**
**硬件层面：关键器件与官方规格逐项吻合 ⇒ 无论它是原厂板还是参考设计，用这两份文档做硬件依据都成立。**

| 证据 | 板子实报 | 官方文档 | 判定 |
|---|---|---|---|
| 设备树 model | `RK3588 OPi 5 Plus` | — | ✓ 一致 |
| compatible | `rockchip,rk3588-orangepi-5-plus` | — | ✓ 一致 |
| hostname | `orangepi5plus` | — | ✓ 一致 |
| **以太网** | **2 × RTL8125 2.5GbE**（lspci 两个 PCI 设备） | 手册 §规格：**2 x PCIe 2.5G 以太网口（RTL8125BG）** | ✓ **完全一致** |
| NPU 核数 | debugfs `Core0/Core1/Core2` 三核 | 手册 §3.40 示例：三核负载 | ✓ 一致 |
| NPU 频率 | 1.0 GHz | — | ✓ |
| NPU 算力 | — | 手册：**6 TOPS，支持 INT4/INT8/INT16/FP16** | ✓ 同型号 |
| RAM | 7.7 GiB 可用（8 GB） | 可选 4/8/16/32 GB | ✓ |
| eMMC | **CRTE42，64 GB**（58.2 GiB） | 支持 eMMC | ✓ |

### 原理图里那个"矛盾"已解开

原理图里 **RTL8111K 出现 42 次、RTL8125B 出现 36 次**，一开始看像冲突。查第 12804 行：

```
RTL8125BG/RTL8111K        ← 兼容封装（同一套焊盘，两种 BOM 可选）
For RTL8111K(I) with SWR              ← 1G 变体的上电方案
For RTL8111K(I) with External power or LDO
```

**⇒ 原理图设计成"2.5G/1G 二选一"的兼容封装；这块板实际贴的是 2.5G 版本（RTL8125），与官方 OPi 5 Plus 配置相同。**

---

## 1. 原理图里读到的核心器件

| 器件 | 出现次数 | 作用 |
|---|---:|---|
| **RK3588** | 37 | 主 SoC |
| **RTL8125B / RTL8125BG** | 36+4 | 2.5G 以太网控制器 ×2 |
| RTL8111K | 42 | 千兆变体（兼容封装，未贴） |
| **RK806-1** | 6 | 主 PMIC |
| 电源轨 | DVDD10 / DVDD33 / AVDD33 / DVDD09 / AVDD10 / AVDD09 / EVDD33 / VDD10 / VDD09 | 多路供电 |
| ESD0402-6 / ESD5341N / ESD5304D | 13 / 10 / 6 | ESD 保护 |
| DFN2510P10E / WNM6002-3 / MT08W | 7 / 8 / 7 | MOSFET 等 |

**关键信号域**：`PCIE20 / PCIE30`（两个 M.2 与 2.5G 网口走 PCIe）、`USB30`、`SATA30`、`RJ45 ×6`。

---

## 2. 手册里的关键规格（§规格表）

| 项 | 规格 |
|---|---|
| NPU | 内嵌 **INT4/INT8/INT16/FP16**，算力 **6 TOPS** |
| 以太网 | **2 × PCIe 2.5G（RTL8125BG）** |
| 视频输出 | 2 × HDMI 2.1（8K@60）、1 × Type-C DP1.4A（4K@60） |
| 视频输入 | **1 × HDMI 输入（4K@60）** |
| 存储扩展 | M.2 M-KEY **PCIe 3.0 ×4**（2280 NVMe）；M.2 E-KEY PCIe2.0×1（2230 WiFi6/BT） |
| 40pin | UART / I2C / SPI / CAN / PWM / GPIO |
| USB | 2 × USB3.0 HOST + 2 × USB2.0 HOST |
| 供电 | **Type-C 5V@4A** |
| 按键 | **MaskROM / RECOVERY / 开关机** |
| 调试 | **3pin UART，波特率 1500000** |
| 其它 | 红外接收、RGB LED、2pin 风扇（PWM）、2pin RTC 电池 |
| 系统 | Ubuntu 20.04/22.04/26.04、Debian 11/12/13、OpenWRT、Android12 |

---

## 3. 对你有直接用的两条

### 3.1 救砖路径（板子已经在用，但要知道怎么回去）

手册 §2.5.1 明确写了 MaskROM 模式的操作：

```
1. 断开电源
2. 按住 MaskROM 键不放
3. 插上 Type-C 电源上电
4. 松开 MaskROM 键
5. 烧录工具（RKDevTool）提示"发现一个 MASKROM 设备"
```

**调试串口**：3pin UART，**波特率 1500000**（不是常见的 115200）。

> 这两条 + 我做的全盘镜像备份，构成本次的完整保险：
> **镜像可回滚（`board-backup/mmcblk0.img.zst`）+ 进得去 MaskROM（手册有步骤）+ 有串口可看启动日志。**

### 3.2 ⚠️ 手册推荐的 RKNN 版本比我装的新

手册 **§3.40.3.2（第 10467 行）** 写的是：

```bash
git clone https://github.com/airockchip/rknn-toolkit2 -b v2.3.0
pip3 install .../rknn_toolkit2-2.3.0-cp310-cp310-linux_x86_64.whl
```

**而我装的是 `2.0.0b0`（2024-03）**——来自你机器上那份旧 SDK。

| | 版本 | 状态 |
|---|---|---|
| 板端原始 runtime | 1.4.0（2022） | 太老，拒绝 2.0 模型 |
| 我换上并验证可用 | 2.0.0b0（2024-03） | ✅ 实测可跑 |
| **手册推荐** | **2.3.0** | 未试（没有该版本包） |

**⇒ 建议**：既然手册明确点名 2.3.0，下次可以按它来装——**版本对齐能避免我今天踩的那类
"Invalid RKNN model version" 问题**。目前 2.0.0b0 可用，但不一定是最优/最新支持。

---

## 4. 这对"没有源码"的担心意味着什么

你说：「我们有讯为3588的源码，只是设备树需要重新配置」。

**这块板子不是讯为（Forlinx）的，是 Orange Pi 5 Plus（迅龙软件）。** 所以：

| 你担心的 | 实际状况 |
|---|---|
| 没有硬件文档 | ✅ **手册 631 页 + 原理图 33 页全都有**（你已提供） |
| 设备树要重配 | ✅ **不需要重配**——我已经从板子把**实际在用的 13,264 行 DTS** 反编译出来了（`board-dt/board.dts`） |
| 没有源码 | ⚠️ 板子上的 Ubuntu 镜像没有内核源码树，但 **RK3588 的 BSP 源码是公开的**（Rockchip + 各板厂），拿到 DTS 后可以直接对回源码树 |
| 刷坏了没法救 | ✅ 全盘镜像已有 + MaskROM 可进 |

**⇒ 用讯为的 RK3588 源码作为内核源码基础是可行的**（同是 RK3588 BSP），
但**设备树不要用讯为那份**——直接用我从板子导出的 `board.dts` 才是这台板子的真实配置。

---

## 5. 我做的核对 vs 没做的

**做了**：
- 设备树 compatible / model / hostname 三项标识比对
- lspci 以太网控制器型号 vs 手册规格 vs 原理图 BOM
- debugfs NPU 三核 + 频点
- eMMC 型号、内存容量
- 手册规格表、MaskROM 步骤、串口波特率、RKNN 推荐版本

**没做**：
- **没有物理对照**（我看不到板子丝印、料号、屏蔽罩），所以**无法排除"高仿板"**；
  但软件与关键器件均一致，**作为硬件参考已足够**
- 没有逐页读原理图（33 页，只做了元件统计 + 关键网络抽取）
- 没有提取 40pin 排针逐脚定义（需要时可从原理图特定页抽）
- 没有验证 HDMI/CSI/DSI/M.2 等其余接口

---

## 6. 文件位置

```
附件原件（只读）：
  OrangePi_5_Plus_RK3588_用户手册_v2.2.pdf   (36.8 MB, 631 页)
  OPI+5+PLUS_V1_3_1_原理图.pdf              (2.6 MB, 33 页)
已转文本：
  /tmp/opi/manual.txt       (0.56 MB)
  /tmp/opi/schematic.txt    (2.75 MB)
```
