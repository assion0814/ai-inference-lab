# RK3588 外设接入 · 技术资产与可复用结论

<!-- 层归属（SOC.md §四）：经营（后台）。技术资产登记 -->
> **层归属**：`经营（后台）` —— 技术资产登记与可复用工程配方
> **执行体**：无（人工维护）。实物与原始数据在 `~/rk3588-sprint/`，全盘可复算
> **项目状态**：**进行中**（2026-10-22，语音唤醒模块待确认型号）
> **详细报告**：`~/rk3588-sprint/REPORT-NEW-HARDWARE.md`

> 建立：2026-10-22 · 场景：Orange Pi 5 Plus（RK3588 8GB）+ 亚博智能摄像头/语音/十轴 IMU
> 价值：**三个必踩的坑的解法 + 一套逆向出来的 IMU 协议**
> 适用：任何 RK3588/RK3568 板子接 USB 外设、接串口、接网线直连

---

## 一、★★ 直连板子拿不到 IP：先查 dnsmasq 和 ufw（同一个坑踩了两次）

### 1.1 症状

```
主机网口: carrier=1, 2.5Gbps 协商成功
板子:     ping 不通、SSH 不通、全 254 个 IP 都不通
tcpdump:  只抓到主机自己的广播，板子连 ARP 都不应答
```

**这个症状极容易被误判成「板子没开机」或「硬件坏了」。**

### 1.2 真正的根因（两个叠加）

```bash
# 根因 1: 主机没跑 DHCP 服务，板子求 IP 没人应答
# 根因 2: ufw 的 INPUT policy=DROP，DHCP 包被静默丢弃
iptables -L INPUT -n | head -3
# Chain INPUT (policy DROP) ← 这就是元凶
```

### 1.3 ✅ 正确解法

```bash
# 1) 起 DHCP 服务（板子会拿到 192.168.66.100-200 之间）
sudo dnsmasq --interface=enp2s0 \
  --dhcp-range=192.168.66.100,192.168.66.200,255.255.255.0,12h --port=0

# 2) ★ 放行 DHCP（关键！否则上一步白做）
sudo iptables -I INPUT 1 -i enp2s0 -p udp --dport 67 -j ACCEPT
sudo iptables -I INPUT 1 -i enp2s0 -p udp --dport 68 -j ACCEPT

# 3) 验证（应能看到板子发 ARP Request）
sudo tcpdump -i enp2s0 -n 'udp port 67 or arp'

# 4) 查租约（确认板子拿到了哪个 IP）
cat /var/lib/misc/dnsmasq.leases
# 1792720880 26:0d:5a:10:cc:a8 192.168.66.129 orangepi5plus 01:26:0d:5a:10:cc:a8
```

### 1.4 方法论教训（重要）

1. **`tcpdump` 不加 sudo 时静默返回"零流量"** —— 我第一次就据此误判。
   **必须先确认工具真的在工作**（发个包看能否抓到），再相信"没流量"的结论。
2. **不要一上来怀疑硬件**。网口 carrier=1 说明 PHY 工作，硬件基本是好的。
3. **同理适用于：任何静态 IP 主机直连需要 DHCP 的设备**（开发板、树莓派、交换机管理口）。

---

## 二、★★ brltty 抢走 CH340 串口：第二个串口"消失"的真凶

### 2.1 症状

```
ch341-uart ttyUSB1: ch341-uart converter now attached to ttyUSB1
usb 5-1.2: usbfs: interface 0 claimed by ch341 while 'brltty' sets config #1
ch341-uart ttyUSB1: ch341-uart converter now disconnected from ttyUSB1
```

**表现：设备节点出现几秒后消失，反复插拔都一样。**
**尤其在你插了第二个 CH340 时必然出现（第一个能活，第二个必死）。**

### 2.2 根因

**`brltty`（盲文显示器支持守护进程）把 CH340/CH341 串口芯片误认为盲文显示器**，抢走 USB 接口。
这是 Debian/Ubuntu 的**已知长期 bug**。

**关键：它是 udev 触发的（`brltty-udev.service`），不是普通服务。**
```bash
systemctl stop brltty          # ← 不够
pkill brltty                   # ← 也不够，插新设备又会被拉起
```

### 2.3 ✅ 正确解法（四步缺一不可）

```bash
# 1) 杀掉所有 brltty 进程
for pid in $(pgrep -x brltty); do sudo kill -9 $pid; done

# 2) 停 + mask 两个 unit（mask 防止被依赖拉起）
sudo systemctl stop brltty.service brltty-udev.service
sudo systemctl mask brltty.service brltty-udev.service

# 3) ★ 关键：禁掉 udev 规则（否则插新设备还是被抢）
sudo sed -i 's/^/# disabled by agent /' /usr/lib/udev/rules.d/85-brltty.rules
sudo udevadm control --reload-rules

# 4) 重新加载串口驱动
sudo modprobe -r ch341 && sudo modprobe ch341

# 5) 验证：等 10 秒看设备是否稳定
sleep 10 && ls -la /dev/ttyUSB*
```

### 2.4 更彻底的方案

```bash
sudo apt remove --purge brltty brltty-x11 libbrlapi0.8
```

**如果是服务器/开发板，直接卸载最干净**（这些机器不需要盲文支持）。

---

## 三、★★ 亚博 10 轴 IMU 协议（完整逆向，可直接复用）

### 3.1 硬件连接

| 属性 | 实测值 |
|---|---|
| 设备节点 | `/dev/ttyUSB1`（模块内置 CH340，VID:PID `1a86:7523`）|
| **波特率** | **115200** ⚠️ **不是官方文档说的 921600** |
| 数据率 | **100 fps**（4 种帧 × 25 Hz）|
| 帧长 | 17 / 21 / 23 字节 |

> ⚠️ **官方 README 写「波特率 921600，回传率 200Hz」，实测是 115200 / 100fps。**
> 在 921600 下读到的是纯垃圾（只有 2 个唯一字节值，典型波特率不匹配特征）。
> **实测优先于文档。**

### 3.2 帧格式

```
┌──────┬──────┬──────┬──────┬─────────────┬──────┐
│ 0x7E │ 0x23 │ LEN  │ TYPE │   PAYLOAD   │ CHK  │
└──────┴──────┴──────┴──────┴─────────────┴──────┘
  帧头   固定   总帧长  类型      数据       校验和

校验规则:  CHK = (前面所有字节求和) & 0xFF     ← 已验证
```

### 3.3 四种帧类型

| TYPE | LEN | PAYLOAD 格式 | 内容 |
|---|---|---|---|
| **`0x04`** | 23 | **9 × int16** (LE) | `[acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z, mag_x, mag_y, mag_z]` |
| **`0x16`** | 21 | **4 × float32** (LE) | 四元数 `[w, x, y, z]`，模长恒为 1.0 |
| **`0x26`** | 17 | **3 × float32** (LE) | 欧拉角 `[roll, pitch, yaw]`，单位**弧度** |
| **`0x32`** | 21 | **4 × float32** (LE) | 气压计 + 温度 `[?, 温度°C, 气压Pa, 气压Pa]` |

### 3.4 参考实现（Python，可直接跑）

```python
import os, time, struct, termios, select, math

PORT, BAUD = "/dev/ttyUSB1", 115200

fd = os.open(PORT, os.O_RDONLY | os.O_NONBLOCK)
a = termios.tcgetattr(fd)
a[4] = a[5] = termios.B115200          # ★ 必须用 B 常量，不能写裸数字 115200
a[2] |= termios.CLOCAL | termios.CREAD
a[0] = a[1] = a[3] = 0
termios.tcsetattr(fd, termios.TCSANOW, a)
termios.tcflush(fd, termios.TCIOFLUSH)

buf = b""
while True:
    r,_,_ = select.select([fd], [], [], 0.1)
    if not r: continue
    buf += os.read(fd, 4096)
    while len(buf) >= 5:
        if buf[0] != 0x7e or buf[1] != 0x23:
            buf = buf[1:]; continue
        L = buf[2]
        if not (5 <= L <= 64) or len(buf) < L:
            if len(buf) < L: break
            buf = buf[1:]; continue
        frame = buf[:L]
        if (sum(frame[:-1]) & 0xFF) != frame[-1]:   # 校验
            buf = buf[1:]; continue
        buf = buf[L:]
        t, payload = frame[3], frame[4:-1]
        if t == 0x04 and len(payload) == 18:
            acc = struct.unpack("<3h", payload[0:6])
            gyr = struct.unpack("<3h", payload[6:12])
            mag = struct.unpack("<3h", payload[12:18])
        elif t == 0x16 and len(payload) == 16:
            quat = struct.unpack("<4f", payload)     # w,x,y,z
        elif t == 0x26 and len(payload) == 12:
            euler = struct.unpack("<3f", payload)    # roll,pitch,yaw (rad)
        elif t == 0x32 and len(payload) == 16:
            baro = struct.unpack("<4f", payload)
```

### 3.5 四元数 → 欧拉角

```python
def quat_to_euler(w, x, y, z):
    roll  = math.atan2(2*(w*x + y*z), 1 - 2*(x*x + y*y))
    pitch = math.asin(max(-1, min(1, 2*(w*y - z*x))))
    yaw   = math.atan2(2*(w*z + x*y), 1 - 2*(y*y + z*z))
    return roll, pitch, yaw     # 弧度
```

### 3.6 ★ 交叉验证（判断协议解析对不对的标准）

**用四元数反解的欧拉角，必须和模块自报的欧拉角一致：**

```
模块自报:   roll=-65.7° pitch=-27.5° yaw=20.9°
四元数反算: roll=-65.7° pitch=-27.5° yaw=20.9°   ✅ 完全一致
```

**再加两条佐证：**
- 静止时陀螺仪 `(0,0,0)` ✅
- 四元数模长 `0.9983 ≈ 1.0` ✅

**⇒ 这三条同时成立，协议解析基本无误。**

### 3.7 量程推断（没有手册时的通用方法）

**方法：静止时加速度模长应该等于 1g，反推 LSB/g。**

```
实测模长 = sqrt(956² + 1670² + 737²) = 2061

若 ±2g  (16384 LSB/g): 2061/16384 = 0.126 g  ❌
若 ±8g  ( 4096 LSB/g): 2061/4096  = 0.503 g  ❌
若 ±16g ( 2048 LSB/g): 2061/2048  = 1.006 g  ✅
```

**⇒ 模块配在 ±16g 量程，2048 LSB/g。**

### 3.8 实测数据样本

```
【原始传感器】
  加速度 raw: (956, -1670, 737)     模长 2061 (=1g)
  陀螺仪 raw: (0, 0, 0)             模长 0     ← 静止
  磁力计 raw: (814, 975, -989)      模长 1615

【四元数】 (0.8236, -0.4820, -0.2925, 0.0205)  模长 0.9983
【欧拉角】 roll=-65.7° pitch=-27.5° yaw=20.9°
【气压计】 温度 31.1°C, 气压 101860 Pa (标准 101325，偏高 0.5%，室内合理)
```

---

## 四、★ USB 摄像头：必须用 MJPG，YUYV 是陷阱

### 4.1 实测枚举（USB 2.0 HD Camera, `05a3:9230`）

**MJPG（压缩）：**
| 分辨率 | 帧率 |
|---|---|
| 1920×1080 | 30 fps |
| 1280×720 | **60 fps** |
| 640×480 | **120 fps** |

**YUYV（未压缩）：**
| 分辨率 | 帧率 |
|---|---|
| 1920×1080 | **6 fps** ← 惨不忍睹 |
| 1280×720 | 9 fps |
| 640×480 | 30 fps |

### 4.2 铁律

**USB 2.0 摄像头（480 Mbps 带宽）跑 1080p 必须用 MJPG。**
YUYV 是未压缩的 YUV422（1080p 一帧 3 MB），USB 2.0 带宽根本扛不住。

```bash
# ✅ 正确：指定 input_format mjpeg
ffmpeg -f v4l2 -input_format mjpeg -video_size 1920x1080 -i /dev/video0 -frames:v 1 out.jpg

# ❌ 错误：不指定格式，ffmpeg 可能选 YUYV → 只有几 fps
ffmpeg -f v4l2 -video_size 1920x1080 -i /dev/video0 -frames:v 1 out.jpg
```

### 4.3 USB vs MIPI CSI 的本质区别

| | USB 摄像头 | MIPI CSI 摄像头 |
|---|---|---|
| 走的路 | USB 控制器 → CPU | **RK3588 ISP → 内存** |
| CPU 占用 | 需要 CPU 解 MJPG | **硬件 ISP，零 CPU** |
| 延迟 | 30-60 ms | **<20 ms** |
| 硬件 3A | 摄像头内部做 | **ISP 做（更好）** |
| 降噪/HDR | 无 | **ISP 硬件支持** |
| 驱动 | uvcvideo 通用 | 需要设备树配置 |

**检查 ISP 是否被使用：**
```bash
dmesg | grep rkisp
# rkisp_hw fdcb0000.rkisp: max input:0x0@0fps   ← 没有输入源（USB 摄像头用不上）
```

---

## 五、★ 方法论陷阱（这次又踩了三个）

### 5.1 Python 的 termios 波特率必须用 B 常量

```python
# ❌ 错误：Linux 的 B115200 = 4098，不是 115200
attrs[4] = attrs[5] = 115200       # → OSError: Invalid argument

# ✅ 正确
attrs[4] = attrs[5] = termios.B115200
```

**通用做法：用 `stty` 配置，或维护一张 baud → B常量 的映射表。**

### 5.2 串口读取不能用 `dd bs=N count=M` 等固定字节数

```bash
# ❌ 错误：如果数据率不够，dd 会等到超时返回 0 字节
timeout 3 dd if=/dev/ttyUSB1 bs=512 count=4 > /tmp/x.bin

# ✅ 正确：用 head -c（读到 N 字节或 EOF 就停）或 select 循环
timeout 3 head -c 512 /dev/ttyUSB1 > /tmp/x.bin
```

### 5.3 Python 3.10 的 f-string 不允许表达式里有反斜杠

```python
# ❌ SyntaxError: f-string expression part cannot include a backslash
print(f"  hex: {data.hex(\" \")}")

# ✅ 两种写法
print(f"  hex: {data.hex(' ')}")        # 3.12+ 可以
sep = " "; print(f"  hex: {data.hex(sep)}")   # 3.10 兼容
```

### 5.4 插件/工具静默失败必须先做正对照

**这次的教训再次印证：**
- `tcpdump` 不加 sudo → 静默"零流量"（第 1 次误判）
- `dd` 读串口 → 静默 0 字节（差点以为设备坏了）
- `brltty` 抢设备 → 设备节点"时有时无"（差点以为 USB 接触不良）

**纪律：任何"没有数据"的结论，先做一个正对照（主动制造已知数据，确认工具能读到）。**

---

## 六、一套可复用的硬件点亮流程

```
① 网络
   ├─ 板子有独立 12V 供电？            → 有则排除供电问题
   ├─ 网口 carrier=1？                 → 是则 PHY 工作
   ├─ 主机跑了 dnsmasq？               → 没跑就起
   ├─ ufw 放行 DHCP 了吗？             → ★ 最常见坑
   └─ tcpdump 验证（先做正对照）

② USB 串口
   ├─ lsusb 看到 VID:PID 了吗？        → Rockchip=2207, CH340=1a86:7523
   ├─ dmesg 有 ch341-uart attached？   → 有
   ├─ ★ 设备节点稳定吗（等 10 秒）？    → 消失就是 brltty
   └─ 波特率扫描（9600 → 921600）      → 找数据率

③ 协议识别
   ├─ 统计字节唯一值数量               → 只有 2-4 个 → 波特率不对
   ├─ 找帧头特征（0x55/0x7e/0xAA）    → 重复出现的固定字节
   ├─ 用帧长验证（第 N 字节 = 帧长？）
   ├─ 校验和验证（sum / xor / crc）    → ★ 能验证通才说明帧结构对
   └─ 物理量交叉验证                   → 静止陀螺仪应为 0、四元数模长应为 1

④ 摄像头
   ├─ v4l2-ctl --list-devices
   ├─ v4l2-ctl --list-formats-ext     → 看 MJPG vs YUYV
   ├─ 用 MJPG 抓帧
   └─ 检查 ISP 是否被用上（dmesg | grep rkisp）
```

---

## 七、本次未解决 / 待补充

| 项 | 状态 |
|---|---|
| 语音唤醒模块 | ⚠️ 未跑通（两个候选串口都静默，需确认型号）|
| IMU 波特率与文档不符 | ⚠️ 921600 vs 实测 115200，原因未查明 |
| 加速度量程 | ⚠️ ±16g 是推断，没有实验验证 |
| 气压计 4 个值 | ⚠️ 只确定温度+气压，另外两个含义不明 |
| 磁力计标定 | ❌ 没做椭球拟合，yaw 可能有偏差 |
| RK3588 ISP | ❌ USB 摄像头用不上，MIPI CSI 才有意义 |
