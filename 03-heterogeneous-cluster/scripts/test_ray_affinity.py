#!/usr/bin/env python3
"""测试 Ray 异构集群的显式节点亲和调度"""
import ray
import time
import platform
import subprocess

ray.init(address="192.168.66.2:6379", ignore_reinit_error=True)

# 获取节点信息
nodes = ray.nodes()
print("=" * 70)
print(" Ray 异构集群 · 显式节点亲和测试")
print("=" * 70)
print("节点列表:")
for n in nodes:
    if n.get("Alive"):
        resources = n.get("Resources", {})
        print("  %-18s %s" % (n["NodeManagerAddress"],
              {k: v for k, v in resources.items() if not k.startswith("node:")}))

# 找 ARM 节点
arm_node_ip = None
x86_node_ip = None
for n in nodes:
    if n.get("Alive"):
        addr = n["NodeManagerAddress"]
        # 板子是 192.168.66.101
        if addr == "192.168.66.101":
            arm_node_ip = addr
        elif addr == "192.168.66.2":
            x86_node_ip = addr

print("\n识别结果:")
print("  ARM64 节点 (RK3588):", arm_node_ip)
print("  x86_64 节点 (UM890+V100):", x86_node_ip)
print()

@ray.remote(num_cpus=1)
def report():
    return {
        "host": platform.node(),
        "arch": platform.machine(),
        "cpu": platform.processor() or "unknown",
    }

# 测试 1: 强制调度到 ARM 节点
print("=" * 70)
print("测试 1: 强制调度到 RK3588 (aarch64)")
print("=" * 70)
if arm_node_ip:
    tasks = [report.options(
        scheduling_strategy=ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
            node_id=next(n["NodeID"] for n in nodes if n["NodeManagerAddress"] == arm_node_ip),
            soft=False,
        )
    ).remote() for _ in range(4)]
    results = ray.get(tasks)
    for r in results:
        print("  %-20s %s" % (r["host"], r["arch"]))
else:
    print("  ⚠ 未找到 ARM 节点")

# 测试 2: 强制调度到 x86 节点
print()
print("=" * 70)
print("测试 2: 强制调度到 UM890 (x86_64)")
print("=" * 70)
if x86_node_ip:
    tasks = [report.options(
        scheduling_strategy=ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
            node_id=next(n["NodeID"] for n in nodes if n["NodeManagerAddress"] == x86_node_ip),
            soft=False,
        )
    ).remote() for _ in range(4)]
    results = ray.get(tasks)
    for r in results:
        print("  %-20s %s" % (r["host"], r["arch"]))

# 测试 3: 真实的异构任务分工
print()
print("=" * 70)
print("测试 3: 异构任务分工（LLM 推理 vs 工具执行）")
print("=" * 70)

@ray.remote(num_gpus=1)
def llm_inference(prompt):
    """模拟 LLM 推理（必须 V100）"""
    import subprocess
    gpu = subprocess.run(["nvidia-smi", "--query-gpu=name",
                          "--format=csv,noheader"],
                         capture_output=True, text=True, timeout=5).stdout.strip()
    time.sleep(0.5)  # 模拟推理延迟
    return {"task": "llm", "host": platform.node(), "gpu": gpu, "prompt": prompt[:30]}

@ray.remote(num_cpus=1)
def tool_execution(tool_name):
    """模拟工具执行（可用任意节点，优先 RK3588）"""
    time.sleep(0.2)
    return {"task": "tool", "host": platform.node(), "arch": platform.machine(), "tool": tool_name}

# 分配：LLM 在 V100，工具在 RK3588
llm_futures = [llm_inference.remote("解释一下量子计算") for _ in range(3)]
if arm_node_ip:
    arm_node_id = next(n["NodeID"] for n in nodes if n["NodeManagerAddress"] == arm_node_ip)
    tool_futures = [tool_execution.options(
        scheduling_strategy=ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
            node_id=arm_node_id, soft=False,
        )
    ).remote("文件读写") for _ in range(3)]
else:
    tool_futures = [tool_execution.remote("文件读写") for _ in range(3)]

print("LLM 推理任务:")
for r in ray.get(llm_futures):
    print("  %-8s 主机=%-15s GPU=%s" % (r["task"], r["host"], r["gpu"]))
print("工具执行任务:")
for r in ray.get(tool_futures):
    print("  %-8s 主机=%-15s 架构=%s 工具=%s" % (r["task"], r["host"], r["arch"], r["tool"]))

print()
print("=" * 70)
print(" ✅ 异构调度测试完成")
print("=" * 70)
ray.shutdown()
