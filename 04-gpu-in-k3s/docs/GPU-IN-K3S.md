# V100 接入 k3s：从 0 到可调度的完整记录

> 时间：2026-11-02 00:20 UTC
> 环境：Ubuntu 26.04 + k3s v1.34.1 + Tesla V100-PCIE-32GB (driver 580.178.04)
> 结果：`nvidia.com/gpu: 1` 可调度，Pod 内 nvidia-smi 正常

## 最终验证

```
$ kubectl get nodes -o custom-columns='NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu'
NAME            GPU
orangepi5plus   <none>
um890pro        1

$ kubectl logs -n test gpu-final-test
NVIDIA-SMI 580.178.04    Driver Version: 580.178.04    CUDA Version: 13.0
Tesla V100-PCIE-32GB   35C   23W / 250W   1058MiB / 32768MiB   0%
```

## 五道坎（每道都是真实踩过的）

### 坎 1：GPG key 的 `--dearmor` 失败

```bash
curl -fsSL .../gpgkey | gpg --dearmor -o ...
# gpg: no valid OpenPGP data found
```

**解决**：用 `--import` + `--export` 绕过：
```bash
export GNUPGHOME=/tmp/gpg && gpg --batch --import key.gpg
gpg --batch --export > /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
```

### 坎 2：k3s 不读 `/etc/containerd/conf.d/`

nvidia-ctk v1.20 把配置写到 `/etc/containerd/conf.d/99-nvidia.toml`，
但 k3s 用自己的 containerd，不读这个目录。

**解决**：手动写 k3s 的 `config.toml.tmpl`：
```toml
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.nvidia]
  runtime_type = "io.containerd.runc.v2"
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.nvidia.options]
  BinaryName = "/usr/bin/nvidia-container-runtime"
  SystemdCgroup = true
```

### 坎 3：device plugin 的 kubelet.sock 路径

k3s 的 kubelet 用 `/var/lib/kubelet/device-plugins/`（默认路径），
**不是** `/var/lib/rancher/k3s/agent/kubelet/device-plugins/`。

**解决**：
```yaml
volumes:
- name: device-plugin
  hostPath:
    path: /var/lib/kubelet/device-plugins   # ← 这个才对
```

### 坎 4：挂载整个 lib 目录会破坏容器 glibc

device plugin 镜像是 RHEL 9.4（glibc 2.34），宿主机是 Ubuntu 26.04（glibc 2.43）。
把宿主 `/usr/lib/x86_64-linux-gnu` 挂到同路径 → 容器崩溃：
```
symbol lookup error: libc.so.6: undefined symbol: __tunable_is_initialized
```

**解决**：只挂 NVIDIA 自己的库到独立目录：
```bash
mkdir -p /home/assion/gpu-k8s/nvidia-libs
cp /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.580.178.04 → libnvidia-ml.so.1
cp /usr/lib/x86_64-linux-gnu/libcuda.so.580.178.04 → libcuda.so.1
# 然后 LD_LIBRARY_PATH=/nvidia-libs
```

### 坎 5：`privileged: true` 与 `allowPrivilegeEscalation: false` 冲突

```yaml
securityContext:
  privileged: true
  allowPrivilegeEscalation: false   # ← 不能同时设
# Error: cannot set allowPrivilegeEscalation to false and privileged to true
```

**解决**：只保留 `privileged: true`。

## 还可以改进的点

| 项 | 现状 | 生产建议 |
|---|---|---|
| 库挂载 | 手拷 4 个 .so 到目录 | 用 CDI（`/var/run/cdi/nvidia.yaml` 已自动生成！）|
| device plugin 镜像 | RHEL 版需要 LD_LIBRARY_PATH | 用 ubuntu 版镜像省掉这一步 |
| 单 GPU | 1 张 V100 | 多卡时注意 `nvidia.com/gpu` 计数 |
| 无 GPU 隔离 | device plugin 独占 | 需要 time-slicing 时配 `sharing.timeSlicing` |
