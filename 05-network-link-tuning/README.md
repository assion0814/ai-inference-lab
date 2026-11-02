# 05 · 链路故障诊断与 TCP 调优

从 0.1 MB/s 恢复到峰值 16.4 MB/s —— 重点不是「调快了」，而是**判定根因在哪一层**。

## 结论

- 现象：夜间下载 8.8 MB/s 骤降到 0.1 MB/s
- 根因：**4G 基站晚高峰拥塞** + **TCP 窗口被高 RTT 锁死**（不是运营商限速、不是 WiFi、不是磁盘）
- 修复：BBR + fq + 128MB 大缓冲 + `tcp_mtu_probing` + DNS 选优
- 结果：平均 2–3 MB/s、峰值 **16.4 MB/s**；根因无法消除（基站物理限制），只能缓解 + 错峰

## 诊断链路（排除法）

| 怀疑对象 | 测法 | 结论 |
|---|---|---|
| WiFi 链路质量 | `iw dev wlp4s0 link` → -52dBm、0 丢包 | 排除（极好） |
| 本地磁盘 IO | `dd` 测试 | 排除 |
| 连接数限速 | 8/16/32/64 并发 | 总带宽不随连接数线性增长 |
| 运营商流量限速 | ping 0% 丢包但延迟 2.7s | 部分对：是拥塞不是限速 |
| **4G 基站拥塞** | 公网 ping 平均 2742ms、抖动 900ms | **根因** |

## TCP 数学（关键推理）

```
TCP 吞吐 ≈ 窗口大小 / RTT

RTT = 2.7s 时：
  64 KB 窗口 → 24 KB/s
  256 KB 窗口 → 95 KB/s
  4 MB 窗口 → 1.5 MB/s

实测 ~100 KB/s ← 完全匹配 256KB 窗口 / 2.7s RTT
```

## 调优配方

```bash
sudo modprobe tcp_bbr
sudo sysctl -w net.core.default_qdisc=fq
sudo sysctl -w net.ipv4.tcp_congestion_control=bbr
sudo sysctl -w net.core.rmem_max=134217728
sudo sysctl -w net.core.wmem_max=134217728
sudo sysctl -w "net.ipv4.tcp_rmem=4096 131072 134217728"
sudo sysctl -w "net.ipv4.tcp_wmem=4096 131072 134217728"
sudo sysctl -w net.ipv4.tcp_mtu_probing=1
sudo sysctl -w net.ipv4.tcp_slow_start_after_idle=0
```

完整配置见 [config/99-4g-tune.conf](./config/99-4g-tune.conf)（写入 `/etc/sysctl.d/` 持久化）

## 踩坑清单（7 条）

1. BBR 调完立即测没效果 —— 需等 10–60 秒
2. 4G 带宽是脉冲式的 —— 5.5 MB/s → 0.2 MB/s，需多次采样取平均
3. DNS 慢被忽略 —— 换 223.5.5.5
4. IPv6 有地址但不通 —— 强制 `-4`
5. 路由器身份误判 —— 查 `/api/system/deviceinfo` 才发现是 4G 移动路由器
6. SIGSTOP 不释放连接 —— 测速时仍被占用
7. aria2 进度显示延迟 —— 看 `DL:` 行的实时值

## 顺带解决的：节点失联

用 K3s 特权 Pod 挂载宿主机根文件系统，在无 SSH / 无串口条件下修复公钥登录。见 [config/fix-node.yaml](./config/fix-node.yaml)。

## 详细报告

[config/../docs/4G-NETWORK-OPTIMIZATION-ASSETS.md](./docs/4G-NETWORK-OPTIMIZATION-ASSETS.md)

## 边界

- 根因（基站拥塞）无法消除，只能缓解
- 峰值 16.4 MB/s 为多 shard 并发合计，平均 2–3 MB/s