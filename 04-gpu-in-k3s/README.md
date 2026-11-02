# 04 · GPU on k3s + 监控栈

在单机 V100 上把 GPU 接入 k3s，推理服务 K8s 化，并搭起 Prometheus 监控 + 告警 + 巡检。

## 结论

| 项 | 状态 | 验证方式 |
|---|---|---|
| GPU 可调度 | 是（`nvidia.com/gpu: 1`） | `kubectl get nodes -o custom-columns=...` |
| 推理服务 K8s 化 | 是（Deployment + Service） | `curl :8080/v1/models` → 200 |
| 监控栈 | 是（Prometheus + 自研 exporter + 8 条告警） | `/api/v1/targets` → up |
| 多节点调度 | 是（ARM + x86，3 副本分布两节点） | `kubectl get pods -o wide` |
| 故障复盘 | 是（253 次重启根因） | [docs/POSTMORTEM-k3s-restarts.md](./docs/POSTMORTEM-k3s-restarts.md) |
| 巡检脚本 | 是（157 行 + 负对照） | 负对照正确报红 |

## 环境

Ubuntu 26.04 · k3s v1.34.1 · Tesla V100-PCIE-32GB · driver 580.178.04

## 五步走通

1. **nvidia-container-toolkit** — `gpg --dearmor` 报错需 `--import/--export` 绕过；nvidia-ctk 写的 `/etc/containerd/conf.d/` k3s 不读，需手写 `config.toml.tmpl`
2. **device plugin** — kubelet.sock 在 `/var/lib/kubelet/device-plugins/`（不是 rancher 路径）；镜像 glibc 不兼容需只挂 NVIDIA 库；`privileged` 与 `allowPrivilegeEscalation: false` 冲突
3. **监控** — docker.io 不通，Prometheus 用 GitHub 二进制 + hostPath；DCGM exporter 404，**自研 91 行 Python exporter**
4. **推理服务** — 引擎直接跑会 stdin EOF 退出，需 wrapper 管 stdin；`/usr/local/cuda` 不存在（apt 装的）；服务必须绑 `0.0.0.0`
5. **验证** — GPU allocatable、Service 200、targets up、巡检输出正常

## 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 监控 exporter | 自研 Python（91 行） | 官方 DCGM 拉不到；自研零依赖可审计 |
| Prometheus | hostPath 挂二进制 | docker.io 不通但 GitHub 可以 |
| GPU exporter | systemd host 进程 | 避免容器 lib 冲突 |
| 推理服务 | Deployment + Recreate | 单 GPU 不能并行，避免双实例抢卡 |
| 网络 | hostNetwork | 简化；生产应用 CNI + NodePort |

## 文件

```
manifests/  nvidia-device-plugin.yaml, gpu-pod-test.yaml,
            maya-inference-deploy.yaml, multi-node-demo.yaml
scripts/    setup-gpu-k3s.sh, gpu-patrol.py
monitoring/ prometheus.yml, rules/gpu-alerts.yml,
            exporter/gpu_exporter.py, dashboard/index.html
docs/       GPU-IN-K3S.md, POSTMORTEM-k3s-restarts.md
```

## 诚实清单（没做的）

GPU time-slicing、MIG（V100 不支持）、多卡训练、RDMA/IB、Grafana（拉不到镜像，用静态 HTML 替代）、Alertmanager、PV/PVC、RBAC/NetworkPolicy、Harbor 私有仓库。

**这些不是忘了做，是硬件/网络限制下的取舍。**