#!/usr/bin/env python3
"""测试 Ray 异构集群：跨节点任务调度"""
import ray
import time
import os
import platform

ray.init(address="192.168.66.2:6379", ignore_reinit_error=True)

print("=" * 70)
print(" Ray 异构集群测试")
print("=" * 70)
print("集群资源:")
for k, v in ray.cluster_resources().items():
    print("  %-20s %s" % (k, v))
print()

# 测试 1: 普通任务（会被调度到任意节点）
@ray.remote(num_cpus=1)
def where_am_i(i):
    return {
        "i": i,
        "host": platform.node(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "pid": os.getpid(),
    }

print("=== 测试 1: 16 个任务跨节点调度 ===")
futures = [where_am_i.remote(i) for i in range(16)]
results = ray.get(futures)
# 统计
from collections import Counter
hosts = Counter(r["host"] for r in results)
archs = Counter(r["arch"] for r in results)
pythons = Counter(r["python"] for r in results)
print("  按主机分布:")
for h, c in hosts.items():
    print("    %-20s %d 个任务" % (h, c))
print("  按架构分布:", dict(archs))
print("  按 Python 分布:", dict(pythons))
print()

# 测试 2: GPU 任务（只能调度到 V100 节点）
@ray.remote(num_gpus=1)
def gpu_task():
    import subprocess
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                              "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10)
        return {"gpu": out.stdout.strip(), "host": platform.node()}
    except Exception as e:
        return {"gpu": "error: %s" % e, "host": platform.node()}

print("=== 测试 2: GPU 任务（应调度到 V100）===")
try:
    r = ray.get(gpu_task.remote())
    print("  GPU: %s" % r["gpu"])
    print("  主机: %s" % r["host"])
except Exception as e:
    print("  ❌ 失败:", str(e)[:200])
print()

# 测试 3: CPU 密集任务（比较两个节点的性能）
@ray.remote(num_cpus=1)
def cpu_bench(n):
    t0 = time.time()
    s = 0
    for i in range(n):
        s += i * i
    return {"elapsed": time.time() - t0, "host": platform.node(), "arch": platform.machine()}

print("=== 测试 3: CPU 性能对比（8M 次循环）===")
results = ray.get([cpu_bench.remote(8_000_000) for _ in range(4)])
for r in results:
    print("  %-20s %-8s %.3f 秒" % (r["host"], r["arch"], r["elapsed"]))
print()

print("=" * 70)
print(" ✅ 集群测试完成")
print("=" * 70)
ray.shutdown()
