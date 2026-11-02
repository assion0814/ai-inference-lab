#!/bin/bash
# RK3588 新硬件诊断脚本（摄像头 + 语音唤醒 + 十轴 IMU）
# 用法：在板子上以 root 运行  bash rk3588_hw_probe.sh 2>&1 | tee /root/hw_probe.txt
# 然后把 /root/hw_probe.txt 拷回来

echo "=============================================="
echo " RK3588 硬件诊断  $(date)"
echo "=============================================="
echo

echo "【0. 系统信息】"
uname -a
cat /etc/os-release 2>/dev/null | head -3
echo "启动时间: $(uptime -s)  已运行: $(uptime -p)"
echo "dmesg 行数: $(dmesg 2>/dev/null | wc -l)"
echo

echo "【1. 摄像头检测】"
echo "--- USB 摄像头（UVC）---"
lsusb 2>/dev/null | grep -iE 'camera|video|uvc|webcam' || echo "  无 USB 摄像头"
echo "--- V4L2 设备 ---"
if command -v v4l2-ctl >/dev/null 2>&1; then
    v4l2-ctl --list-devices 2>/dev/null
else
    echo "  v4l2-ctl 未安装，改用 /dev/video*"
fi
ls -la /dev/video* /dev/media* 2>/dev/null || echo "  无 /dev/video* 节点"
echo "--- 内核里的摄像头驱动 ---"
dmesg 2>/dev/null | grep -iE 'uvcvideo|csi|rkisp|ov[0-9]{4}|imx[0-9]{3}|gc[0-9]{4}|camera' | tail -10 || echo "  无摄像头驱动日志"
echo "--- 设备树里的 camera 节点 ---"
ls /proc/device-tree/ 2>/dev/null | grep -iE 'camera|csi|isp' || echo "  无 camera 节点"
echo

echo "【2. 语音唤醒模块】"
echo "--- USB 音频设备 ---"
lsusb 2>/dev/null | grep -iE 'audio|sound|mic|speaker|respeaker' || echo "  无 USB 音频设备"
echo "--- ALSA 声卡 ---"
cat /proc/asound/cards 2>/dev/null || echo "  无 ALSA 声卡"
echo "--- 录音设备列表 ---"
arecord -l 2>/dev/null || echo "  arecord 未安装或无设备"
echo "--- UART 设备（SU-03T 类模块常用）---"
ls -la /dev/ttyS* /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | head -15
echo "--- 内核串口日志 ---"
dmesg 2>/dev/null | grep -iE 'ttyS|ttyUSB|ttyACM|serial|uart' | tail -10 || echo "  无串口日志"
echo "--- I2S / 音频 codec ---"
dmesg 2>/dev/null | grep -iE 'i2s|codec|es8388|rk809|rt5651|nau8822' | tail -8 || echo "  无 codec 日志"
echo

echo "【3. 十轴传感器（IMU）】"
echo "--- I2C 总线列表 ---"
ls -la /dev/i2c-* 2>/dev/null || echo "  无 I2C 设备节点"
echo "--- I2C 设备扫描 ---"
for bus in $(ls /sys/bus/i2c/devices/ 2>/dev/null | grep -oE '^i2c-[0-9]+' | sort -u); do
    echo "  扫描 $bus:"
    if command -v i2cdetect >/dev/null 2>&1; then
        i2cdetect -y ${bus#i2c-} 2>/dev/null | head -10
    else
        echo "    i2cdetect 未安装（apt install i2c-tools）"
    fi
done
echo "--- I2C 设备节点详情 ---"
ls -la /sys/bus/i2c/devices/ 2>/dev/null | head -20
echo "--- 已注册的 IMU 驱动（IIO 子系统）---"
ls /sys/bus/iio/devices/ 2>/dev/null || echo "  无 IIO 设备"
for d in /sys/bus/iio/devices/iio:device*; do
    [ -d "$d" ] && {
        echo "  $(basename $d): $(cat $d/name 2>/dev/null)"
        ls $d | grep -E 'in_(accel|anglvel|magn|press)' | head -6
    }
done
echo "--- SPI 设备 ---"
ls /dev/spidev* 2>/dev/null || echo "  无 SPI 设备节点"
echo "--- 内核里的 IMU 驱动日志 ---"
dmesg 2>/dev/null | grep -iE 'mpu9250|mpu6050|icm20948|icm20608|bmp280|bme280|ak8963|lsm|iio' | tail -10 || echo "  无 IMU 驱动日志"
echo

echo "【4. 相关内核模块】"
lsmod 2>/dev/null | grep -iE 'uvc|camera|video|i2c|spi|iio|snd|rockchip' | head -20
echo

echo "【5. 引脚 / GPIO 状态】"
if command -v gpiodetect >/dev/null 2>&1; then
    gpiodetect 2>/dev/null | head -10
elif command -v gpioinfo >/dev/null 2>&1; then
    gpioinfo 2>/dev/null | head -10
else
    echo "  gpio 工具未安装"
    ls /sys/class/gpio/ 2>/dev/null | head -10
fi
echo

echo "【6. 启动错误检查（关键！）】"
echo "--- dmesg 里的错误 ---"
dmesg 2>/dev/null | grep -iE 'error|fail|denied|timeout|conflict|can.t|cannot' | tail -20 || echo "  无错误"
echo "--- 启动失败的服务 ---"
systemctl list-units --failed --no-pager 2>/dev/null | head -10 || echo "  无失败服务"
echo

echo "【7. 资源占用】"
free -h
df -h / 2>/dev/null | tail -1
cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | head -5 | while read t; do
    echo "  温度: $(echo "scale=1; $t/1000" | bc)°C"
done
echo

echo "=============================================="
echo " 诊断完成。把本文件拷回主机："
echo " scp root@<板子IP>:/root/hw_probe.txt ."
echo "=============================================="
