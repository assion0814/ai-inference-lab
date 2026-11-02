#!/usr/bin/env bash
# GPU in k3s —— 完整安装脚本（已实测通过）
# 环境: Ubuntu 26.04 + k3s v1.34 + Tesla V100 + driver 580.178.04
set -euo pipefail

echo "=== 1. 安装 nvidia-container-toolkit ==="
# GPG key（注意：--dearmor 会失败，用 --import + --export 的方式）
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey -o /tmp/nvidia-gpgkey
export GNUPGHOME=/tmp/gpg-nvidia && rm -rf $GNUPGHOME && mkdir -p $GNUPGHOME && chmod 700 $GNUPGHOME
gpg --batch --import /tmp/nvidia-gpgkey
gpg --batch --export > /tmp/nvidia-keyring.gpg
sudo cp /tmp/nvidia-keyring.gpg /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

echo "deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://nvidia.github.io/libnvidia-container/stable/deb/amd64 /" | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nvidia-container-toolkit

echo "=== 2. 配置 k3s containerd 使用 nvidia runtime ==="
sudo cat /var/lib/rancher/k3s/agent/etc/containerd/config.toml > /tmp/k3s-containerd.toml
cat >> /tmp/k3s-containerd.toml <<'EOF'

[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.nvidia]
  runtime_type = "io.containerd.runc.v2"

[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.nvidia.options]
  BinaryName = "/usr/bin/nvidia-container-runtime"
  SystemdCgroup = true
EOF
sudo cp /tmp/k3s-containerd.toml /var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl
sudo systemctl restart k3s
sleep 15

echo "=== 3. 准备 NVIDIA 库目录（避免挂载整个 lib 目录破坏容器 glibc）==="
mkdir -p /home/assion/gpu-k8s/nvidia-libs
cd /home/assion/gpu-k8s/nvidia-libs
for pair in "libnvidia-ml.so.580.178.04:libnvidia-ml.so.1" \
            "libcuda.so.580.178.04:libcuda.so.1" \
            "libnvidia-ptxjitcompiler.so.580.178.04:libnvidia-ptxjitcompiler.so.1" \
            "libnvidia-nvvm.so.580.178.04:libnvidia-nvvm.so.4"; do
  SRC="${pair%%:*}"; DST="${pair##*:}"
  cp "/usr/lib/x86_64-linux-gnu/$SRC" "$DST"
done

echo "=== 4. 部署 device plugin ==="
kubectl apply -f /home/assion/gpu-k8s/manifests/nvidia-device-plugin.yaml
sleep 20
kubectl get nodes -o custom-columns='NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu'

echo "=== 5. 验证 ==="
kubectl apply -f /home/assion/gpu-k8s/manifests/gpu-pod-test.yaml
sleep 15
kubectl logs -n test gpu-final-test | head -25
echo "=== DONE ==="
