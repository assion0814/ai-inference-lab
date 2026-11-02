#!/bin/bash
# 异构 Ray 集群一键搭建（UM890 x86+V100 + RK3588 aarch64）
# 前提：两机网络互通，UM890 有 2.5G 直连板子
set -e

HEAD_IP="${HEAD_IP:-192.168.66.2}"
BOARD_IP="${BOARD_IP:-192.168.66.101}"

echo "=== 1. UM890: 网络配置 ==="
sudo sysctl -w net.ipv4.ip_forward=1
sudo sysctl -w net.ipv4.conf.enp2s0.rp_filter=0
sudo sysctl -w net.ipv4.conf.wlp4s0.rp_filter=0
sudo iptables -t nat -C POSTROUTING -s 192.168.66.0/24 -o wlp4s0 -j MASQUERADE 2>/dev/null || \
  sudo iptables -t nat -A POSTROUTING -s 192.168.66.0/24 -o wlp4s0 -j MASQUERADE
sudo ufw allow from 192.168.66.0/24 2>/dev/null || true

echo "=== 2. UM890: 启动 Ray head (Python 3.10) ==="
if [ ! -x ~/ray-py310/bin/ray ]; then
  uv venv ~/ray-py310 --python python3.10
  uv pip install --python ~/ray-py310/bin/python \
    --index-url https://mirrors.aliyun.com/pypi/simple/ "ray==2.59.0"
fi
pkill -f 'gcs_server|raylet' 2>/dev/null || true
sleep 2
~/ray-py310/bin/ray start --head \
  --node-ip-address="$HEAD_IP" --port=6379 \
  --dashboard-host=0.0.0.0 --dashboard-port=8265 \
  --num-cpus=8 --num-gpus=1

echo "=== 3. RK3588: 加入集群 ==="
ssh board "
export PATH=\$HOME/.local/bin:\$PATH
if ! python3 -c 'import ray' 2>/dev/null; then
  curl -fsSL -o /tmp/get-pip.py https://mirrors.aliyun.com/pypi/get-pip.py
  python3 /tmp/get-pip.py --user -i https://mirrors.aliyun.com/pypi/simple/
  python3 -m pip install --user -i https://mirrors.aliyun.com/pypi/simple/ 'ray==2.59.0'
fi
ray stop --force 2>/dev/null || true
sleep 2
ray start --address=$HEAD_IP:6379 --node-ip-address=$BOARD_IP --num-cpus=6
"

echo "=== 4. 验证 ==="
~/ray-py310/bin/ray status
echo
echo "Dashboard: http://$HEAD_IP:8265"
