# 03 · 异构计算池（Ray + K3s 节点修复）

把 x86_64+V100 与 ARM64 RK3588 组成统一计算池，验证跨架构资源池化与拓扑感知调度。

## 结论

- GPU 任务自动调度 **3/3** 落到 V100 节点；强制 ARM 亲和 **4/4**、强制 x86 亲和 **4/4**
- 跨架构（x86_64 ↔ aarch64）通信正常，异构分工链路跑通
- **1 张 GPU 做不了分布式推理**（无法张量并行），正确用法是**异构任务调度**
- 无 SSH、无串口条件下，用 **K3s 特权 Pod** 修复远端节点登录态

## 集群规格

| 节点 | 架构 | 资源 | 角色 |
|---|---|---|---|
| UM890 Pro | x86_64 | 8 CPU + V100 32GB + 34.6GB RAM | head |
| Orange Pi 5 Plus | aarch64 | 6 CPU + 7.1GB RAM | worker |
| 合计 | — | **14 CPU / 1 GPU / 42GB** | 2.5G 直连 |

## 调度验证

```python
import ray
ray.init(address="192.168.66.2:6379")

@ray.remote(num_gpus=1)           # 自动落到有 GPU 的节点
def llm_inference(prompt): ...

from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy
task = tool_execution.options(
    scheduling_strategy=NodeAffinitySchedulingStrategy(node_id=arm_node_id, soft=False)
).remote("文件读写")               # 强制到 ARM 节点
```

## 结果

| 用例 | 结果 |
|---|---|
| GPU 任务（`num_gpus=1`） | 3/3 调度到 V100 |
| 强制 ARM 节点亲和 | 4/4 调度到 RK3588 |
| 强制 x86 节点亲和 | 4/4 调度到 UM890 |
| 跨架构通信 | 正常 |
| 异构分工 | LLM 在 V100、工具执行在 RK3588 |

## 踩过的坑（6 类）

1. **板子无外网** — `ufw FORWARD DROP` + `rp_filter=2` + 缺 NAT；补 iptables 转发与 MASQUERADE
2. **ufw 拦 6379** — Ray GCS 端口被拦，需放行内网网段
3. **板子无 pip** — apt 源没有，改用 `get-pip.py`
4. **Python 精确版本匹配** — Ray 要求两端完全一致（3.10.21 vs 3.10.12 会被拒绝），需 patch 版本号
5. **`python3 -m ray` 不可用** — 改用 `ray` CLI
6. **Ray 默认偏本地调度** — 必须显式 `NodeAffinitySchedulingStrategy(soft=False)`

## 节点修复（K3s 特权 Pod）

板子 SSH 公钥丢失且无串口可用。解决方式：用主机上的 K3s 创建特权 Pod（`hostNetwork + hostPID + hostPath:/`），挂载宿主机根文件系统后直接改 `authorized_keys`。全程不需要重启、不需要物理接触。

```bash
kubectl apply -f ../05-network-link-tuning/config/fix-node.yaml
kubectl exec -it fix-node -- chroot /host /bin/bash
```

## 文件

- `scripts/setup-ray-cluster.sh` — 一键网络配置 + Ray 集群启动
- `scripts/patch_ray.py` — Python 版本 patch
- `scripts/test_ray_cluster.py` — 集群连通性验证
- `scripts/test_ray_affinity.py` — 节点亲和验证
- `docs/HETEROGENEOUS-AI-CLUSTER-ASSETS.md` — 完整搭建记录

## 边界

- 2 节点、1 张 GPU，**不是生产集群**
- 未做多卡张量并行 / NCCL / RDMA
- Ray 适合任务级调度；K8s 适合服务编排。两者在同一批机器上并存
