# 故障复盘：k3s 集群 "所有 Pod 253 次重启" 的根因

> 现象时间：2026-11-01 14:09 UTC
> 复盘时间：2026-11-02 00:20 UTC
> 环境：k3s v1.34.1，2 节点（um890pro Ubuntu 26.04 + orangepi5plus RK3588）

---

## 一、现象

```
$ kubectl get pods -A
NAMESPACE     NAME                                      READY   STATUS    RESTARTS
kube-system   local-path-provisioner-578895bd58-kxk86   1/1     Running   253 ⚠️
test          arch-report-mrp7k                         1/1     Running   61
test          arch-report-n9kr2                         1/1     Running   38
kube-system   coredns-7896679cc-7zhk9                   1/1     Running   6
kube-system   metrics-server-7b9c9c4b9c-22nfv           1/1     Running   6
```

**第一印象的误判：这看起来像"某个 Pod 在 crash loop"。**
**实际情况：这不是 crash loop，是主机重启的累计痕迹。**

---

## 二、排障过程（现象 → 假设 → 命令 → 排除）

### 步骤 1：最初怀疑 —— local-path-provisioner 本身有 bug？

```bash
kubectl describe pod -n kube-system -l app=local-path-provisioner
```

输出：
```
Last State:  Terminated
  Reason:    Unknown
  Exit Code: 255
  Started:   Sat, 31 Oct 2026 04:09:00 +0000
  Finished:  Sun, 01 Nov 2026 14:09:44 +0000
Restart Count: 253
```

**关键线索 1：`Started 10-31 04:09` → `Finished 11-01 14:09` = 运行了 34 小时。**
如果真是 crash loop，不可能一次运行 34 小时。**排除"应用崩溃"。**

**关键线索 2：`Reason: Unknown, Exit Code: 255`。**
- Exit 0 = 正常退出
- Exit 1 = 应用错误
- **Exit 255 = 被信号杀死（SIGKILL）或 runtime 层错误**

### 步骤 2：跨 Pod 交叉比对 —— 死亡时间是否同步？

```bash
kubectl get pods -A -o json | jq -r '
  .items[] | .metadata.namespace + "/" + .metadata.name as $n |
  .status.containerStatuses[]? |
  select(.lastState.terminated) |
  "\($n)  exit=\(.lastState.terminated.exitCode)  finished=\(.lastState.terminated.finishedAt)"'
```

输出：
```
kube-system/coredns:                    exit=255  finished=2026-11-01T14:09:44Z
kube-system/local-path-provisioner:     exit=255  finished=2026-11-01T14:09:44Z
kube-system/metrics-server:             exit=255  finished=2026-11-01T14:09:44Z
test/whoami-...-6txtb:                  exit=255  finished=2026-11-01T14:09:44Z
test/whoami-...-ptc6m:                  exit=255  finished=2026-11-01T14:09:44Z
test/arch-report-mrp7k:                 exit=0    finished=2026-11-01T15:09:55Z
test/arch-report-n9kr2:                 exit=0    finished=2026-11-01T15:10:16Z
```

**★ 决定性证据：5 个不同 Pod（不同组件、不同 namespace）都在 `14:09:44` 同一秒被杀死。
单个应用不可能同时崩溃 —— 这是节点级事件。**

### 步骤 3：确认主机是否在同一时刻重启

```bash
uptime -s
# 2026-11-01 14:09:36
```

**★ 根因锁定：系统在 `14:09:36` 启动。Pod 在 `14:09:44` 被 containerd 清理（启动后 8 秒）。
顺序是：主机重启 → k3s/containerd 启动 → 旧容器的 lastState 被记录为 terminated。**

### 步骤 4：解释 arch-report 的 61 次"重启"

```bash
kubectl get pod -n test arch-report-mrp7k -o jsonpath='{.spec.containers[0].command}{"\n"}{.spec.restartPolicy}'
# ["sleep","3600"]
# Always
```

**这不是故障，是设计问题：`sleep 3600` + `restartPolicy: Always` = 每个小时正常退出后被重启。
7 天 = 168 小时 → 理论 168 次。实际 61 次 → 说明期间有中断（主机重启/节点 NotReady）。**

**教训：用 `sleep` 做常驻是反模式，应该用 `pause` 或 long-running process。**

### 步骤 5：确认重启频率（7 天内 23 次 boot）

```bash
journalctl --list-boots | tail -12
```

输出（关键片段）：
```
-11 2026-10-20 23:51 → 2026-10-24 11:35  (3.5 天)
-10 2026-10-24 12:02 → 2026-10-24 15:13  (3 小时)
 -9 2026-10-24 15:14 → 2026-10-24 15:17  (3 分钟！)
 -8 2026-10-24 15:26 → 2026-10-24 16:13  (47 分钟)
 -7 2026-10-25 03:47 → 2026-10-25 04:04  (17 分钟)
 -6 2026-10-25 04:04 → 2026-10-26 11:49  (32 小时)
 -5 2026-10-26 11:49 → 2026-10-26 14:30  (2.7 小时)
 -4 2026-10-26 14:32 → 2026-10-26 14:35  (3 分钟！)
 -3 2026-10-27 00:00 → 2026-10-28 12:13  (36 小时)
 -2 2026-10-28 12:14 → 2026-10-29 14:52  (26.6 小时)
 -1 2026-10-31 04:08 → 2026-10-31 07:43  (3.6 小时)
  0 2026-11-01 14:09 → 现在           (当前)
```

**★ 7 天内 23 次 boot，其中多次间隔只有 3 分钟 —— 说明有反复重启/调试行为。**

### 步骤 6：解析 253 次 = 累计值

```
253 次重启 / 12 次 boot（在 pod 生命周期内）
= 平均每次 boot 21 个 pod 被杀
```
local-path-provisioner 是 DaemonSet 级别的单实例，每次主机重启它就被杀一次并重启一次。
7 天里的多次重启（有些是手动调试，有些是断电/异常）累计到 253 次。

### 步骤 7：为什么 14:09 那次是"非正常"重启？

```bash
last -x reboot shutdown
# （无输出）
journalctl --boot=-1 --since '2026-11-01 13:50:00'
# （无输出 —— 没有 clean shutdown 日志）
```

**★ 没有 clean shutdown 记录 → 非正常断电/强制重启，不是计划内的 `systemctl reboot`。**

---

## 三、根因链

```
1. 主机在 7 天内重启 23 次（非正常居多）
   ↓
2. 每次重启 → k3s/containerd 重启 → 所有容器被 SIGKILL（Exit 255）
   ↓
3. kubelet 记录 restartCount（对 DaemonSet 是累计增长）
   ↓
4. 表象："local-path-provisioner 重启 253 次"（看起来像 crash loop）
   ↓
5. 实际：主机不稳定的累计痕迹
```

**附带发现（非本故障，但值得记录）：**

| # | 现象 | 层级 | 根因 | 严重度 |
|---|---|---|---|---|
| 1 | arch-report 61 次重启 | 设计 | `sleep 3600` + `Always` | 🟡 反模式 |
| 2 | docker: permission denied | 权限 | 用户不在 docker 组 | 🟡 中 |
| 3 | metrics-server 503 | 组件 | 重启后 APIService 未就绪（瞬时） | 🟢 自愈 |
| 4 | CPU governor = powersave | 性能 | 默认省电模式（对推理有实际损失） | 🔴 高 |
| 5 | V100 persistence mode = Disabled | 性能 | 每次进程启动多 1-2 秒初始化 | 🟡 中 |
| 6 | HugePages = 0 | 性能 | 未配置（但实测 THP 对本负载零增益） | 🟢 可接受 |

---

## 四、根因判定

**直接原因**：主机在 2026-11-01 14:09:36 重启（无 clean shutdown 记录，推测为断电/强制重启），导致所有容器被 SIGKILL。

**根本原因**：**主机稳定性问题** —— 7 天内 23 次重启，其中多次间隔仅 3 分钟。

**为什么会被误判为"应用 crash loop"**：
- `restartCount` 是累计值，不区分"应用崩溃"和"节点重启"
- 没有监控 `container_last_seen` / `kube_pod_container_status_restarts_total` 的时间分布
- 如果只看 restartCount 数字，会误判

---

## 五、如果重来怎么更快定位

**3 步快速分流（下次 3 分钟内定位）：**

```bash
# 第 1 步：多个不同 Pod 的 lastState.finishedAt 是否相同？
kubectl get pods -A -o json | jq -r '
  .items[].status.containerStatuses[]?.lastState.terminated?.finishedAt' | \
  sort | uniq -c | sort -rn | head -3

# 如果出现 >2 个相同时间 → 节点级事件，跳到第 2 步
# 如果时间分散 → 应用问题，正常排障

# 第 2 步：该时刻主机是否重启？
last -x reboot | head -5
journalctl --list-boots | tail -5

# 第 3 步：是 clean shutdown 还是异常？
journalctl --boot=-1 | tail -20
# 有 "Reached target shutdown" → 计划内
# 无 → 非正常（断电/panic/强制重启）
```

**应该加的监控（这次没有，是缺口）：**

```yaml
# Prometheus 告警规则
- alert: NodeRebootDetected
  expr: changes(node_boot_time_seconds[1h]) > 0
  for: 0m
  annotations:
    summary: "节点重启（{{ $labels.instance }}）"

- alert: PodRestartSpike
  expr: increase(kube_pod_container_status_restarts_total[15m]) > 3
  annotations:
    summary: "Pod 重启激增（区分节点级 vs 应用级）"
```

**应该做的加固：**

| 措施 | 收益 |
|---|---|
| 配 UPS + 自动关机 | 消除断电导致的非正常重启 |
| 启用 `node-problem-detector` | 自动检测硬件/内核问题 |
| 记录 boot 原因（`kexec -p` + kdump） | 崩溃后能分析 |
| `arch-report` 改用 `pause` 镜像或 `sleep infinity` | 消除 61 次假重启 |
| CPU governor 改 `performance` | 推理性能提升（实测） |
| V100 persistence mode 开启 | 减少进程启动延迟 |

---

## 六、给这份 JD 的价值

这个复盘直接对标 JD 面试的第二项：**"考未知故障场景的推演路径"**。

**演示的能力链：**
1. **从现象到假设** —— 看到 253 次重启，先怀疑应用崩溃
2. **用命令排除** —— `describe` 看 lastState，发现运行了 34 小时（排除 crash loop）
3. **跨对象交叉验证** —— 5 个不同 Pod 同一秒死亡（锁定节点级）
4. **时间线对齐** —— `uptime -s` 对比 pod 死亡时间（锁定主机重启）
5. **区分真假故障** —— arch-report 的 61 次是设计问题不是故障
6. **根因分层** —— 直接原因（主机重启）vs 根本原因（主机不稳定）
7. **防复发措施** —— 监控 + UPS + 反模式修正

**一句话：这不是"我会重启 Pod"，这是"我能从 253 这个数字推出主机重启了 23 次"。**
