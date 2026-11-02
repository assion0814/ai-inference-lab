#!/usr/bin/env python3
"""
GPU 巡检脚本 —— 采集 + 阈值判断 + 告警 + JSON 报告
用法:
  python3 gpu-patrol.py              # 单次巡检，输出报表
  python3 gpu-patrol.py --json       # 输出 JSON（给监控系统）
  python3 gpu-patrol.py --watch 60   # 每 60 秒巡检
"""
import subprocess, json, time, sys, os
from datetime import datetime

THRESHOLDS = {
    'temp_warn': 80, 'temp_crit': 90,
    'mem_warn_pct': 95, 'mem_crit_pct': 99,
    'power_warn_pct': 90,
    'ecc_errors_max': 0,
    'xid_errors_max': 0,
}

def run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ''

def collect():
    r = {'timestamp': datetime.now().isoformat(), 'host': os.uname().nodename}
    # GPU 基础指标
    out = run(['nvidia-smi','--query-gpu=index,name,uuid,driver_version,temperature.gpu,'
               'memory.used,memory.total,utilization.gpu,power.draw,power.limit,'
               'clocks.current.graphics,clocks.current.memory,persistence_mode,ecc.errors.uncorrected.volatile.total',
               '--format=csv,noheader,nounits'])
    gpus = []
    for row in out.split('\n'):
        if not row.strip(): continue
        p = [x.strip() for x in row.split(',')]
        if len(p) < 11: continue
        try:
            g = dict(index=int(p[0]), name=p[1], uuid=p[2], driver=p[3],
                     temp_c=float(p[4]), mem_used_mib=float(p[5]), mem_total_mib=float(p[6]),
                     util_pct=float(p[7]), power_w=float(p[8]) if p[8] not in ('N/A','') else None,
                     power_limit_w=float(p[9]) if p[9] not in ('N/A','') else None,
                     clock_sm_mhz=float(p[10]) if p[10] not in ('N/A','') else None,
                     clock_mem_mhz=float(p[11]) if len(p)>11 and p[11] not in ('N/A','') else None,
                     persistence=p[12] if len(p)>12 else None)
            g['mem_pct'] = 100.0*g['mem_used_mib']/g['mem_total_mib'] if g['mem_total_mib'] else 0
            g['power_pct'] = 100.0*g['power_w']/g['power_limit_w'] if g.get('power_limit_w') else 0
            gpus.append(g)
        except (ValueError, IndexError):
            continue
    r['gpus'] = gpus
    # GPU 进程
    procs = []
    out = run(['nvidia-smi','--query-compute-apps=pid,process_name,used_memory','--format=csv,noheader,nounits'])
    for row in out.split('\n'):
        if not row.strip(): continue
        p = [x.strip() for x in row.split(',')]
        if len(p) >= 3:
            procs.append(dict(pid=int(p[0]), name=p[1], mem_mib=float(p[2])))
    r['processes'] = procs
    # 系统
    with open('/proc/loadavg') as f: la = f.read().split()
    r['load'] = {'1m': float(la[0]), '5m': float(la[1]), '15m': float(la[2])}
    mem = {}
    with open('/proc/meminfo') as f:
        for line in f:
            k, v = line.split(':',1); mem[k] = int(v.strip().split()[0])
    r['memory'] = {'total_mib': mem['MemTotal']//1024,
                   'available_mib': mem.get('MemAvailable',0)//1024,
                   'used_pct': 100.0*(mem['MemTotal']-mem.get('MemAvailable',0))/mem['MemTotal']}
    with open('/proc/uptime') as f: r['uptime_s'] = float(f.read().split()[0])
    # Xid 错误（dmesg）
    r['xid_errors'] = [l for l in run(['bash','-c','dmesg 2>/dev/null | grep -i "Xid" | tail -5']).split('\n') if l]
    # k3s 状态
    r['k3s'] = {'nodes': [], 'pods_not_running': []}
    out = run(['kubectl','get','nodes','--no-headers'])
    for line in out.split('\n'):
        if line.strip(): r['k3s']['nodes'].append(line.split()[0])
    out = run(['kubectl','get','pods','-A','--no-headers'])
    r['k3s']['pods_total'] = len([l for l in out.split('\n') if l.strip()])
    r['k3s']['pods_not_running'] = [l for l in out.split('\n') if l.strip() and 'Running' not in l and 'Completed' not in l]
    return r

def evaluate(r):
    alerts = []
    for g in r['gpus']:
        t = g['temp_c']
        if t >= THRESHOLDS['temp_crit']:
            alerts.append(dict(level='CRITICAL', gpu=g['index'], metric='temperature', value=t,
                               msg=f"GPU{g['index']} 温度 {t}°C ≥ {THRESHOLDS['temp_crit']}°C"))
        elif t >= THRESHOLDS['temp_warn']:
            alerts.append(dict(level='WARN', gpu=g['index'], metric='temperature', value=t,
                               msg=f"GPU{g['index']} 温度 {t}°C ≥ {THRESHOLDS['temp_warn']}°C"))
        mp = g['mem_pct']
        if mp >= THRESHOLDS['mem_crit_pct']:
            alerts.append(dict(level='CRITICAL', gpu=g['index'], metric='memory', value=mp,
                               msg=f"GPU{g['index']} 显存 {mp:.1f}% ≥ {THRESHOLDS['mem_crit_pct']}%"))
        elif mp >= THRESHOLDS['mem_warn_pct']:
            alerts.append(dict(level='WARN', gpu=g['index'], metric='memory', value=mp,
                               msg=f"GPU{g['index']} 显存 {mp:.1f}% ≥ {THRESHOLDS['mem_warn_pct']}%"))
        if g.get('power_pct', 0) >= THRESHOLDS['power_warn_pct']:
            alerts.append(dict(level='WARN', gpu=g['index'], metric='power', value=g['power_pct'],
                               msg=f"GPU{g['index']} 功耗 {g['power_pct']:.1f}% ≥ {THRESHOLDS['power_warn_pct']}%"))
    if r['xid_errors']:
        alerts.append(dict(level='CRITICAL', metric='xid', value=len(r['xid_errors']),
                           msg=f"发现 {len(r['xid_errors'])} 条 Xid 错误"))
    if r['k3s']['pods_not_running']:
        alerts.append(dict(level='WARN', metric='k8s_pods', value=len(r['k3s']['pods_not_running']),
                           msg=f"{len(r['k3s']['pods_not_running'])} 个 Pod 非 Running 状态"))
    if r['memory']['used_pct'] >= 90:
        alerts.append(dict(level='WARN', metric='node_memory', value=r['memory']['used_pct'],
                           msg=f"节点内存使用 {r['memory']['used_pct']:.1f}% ≥ 90%"))
    return alerts

def report(r, alerts):
    print(f"═══ GPU 巡检报告 {r['timestamp']} ═══")
    print(f"  主机: {r['host']}  运行: {r['uptime_s']/3600:.1f}h")
    print(f"  系统负载: 1m={r['load']['1m']:.2f} 5m={r['load']['5m']:.2f} 15m={r['load']['15m']:.2f}")
    print(f"  内存: {r['memory']['used_pct']:.1f}% (可用 {r['memory']['available_mib']//1024} GiB)")
    print()
    for g in r['gpus']:
        print(f"  GPU{g['index']} {g['name']}")
        print(f"    温度 {g['temp_c']}°C | 显存 {g['mem_used_mib']:.0f}/{g['mem_total_mib']:.0f} MiB ({g['mem_pct']:.1f}%)")
        print(f"    利用率 {g['util_pct']}% | 功耗 {g['power_w']:.1f}W ({g['power_pct']:.1f}%)")
        print(f"    时钟 SM {g['clock_sm_mhz']}MHz / MEM {g.get('clock_mem_mhz')}MHz | 持久化 {g['persistence']}")
    if r['processes']:
        print(f"\n  GPU 进程 ({len(r['processes'])} 个):")
        for p in r['processes']:
            print(f"    PID {p['pid']}: {p['name']} ({p['mem_mib']:.0f} MiB)")
    print(f"\n  k3s: {len(r['k3s']['nodes'])} 节点, {r['k3s']['pods_total']} Pod")
    print()
    if alerts:
        print(f"  ⚠️ 告警 ({len(alerts)} 条):")
        for a in alerts:
            icon = '🔴' if a['level']=='CRITICAL' else '🟡'
            print(f"    {icon} [{a['level']}] {a['msg']}")
    else:
        print("  ✅ 无告警（所有指标正常）")
    return len([a for a in alerts if a['level']=='CRITICAL'])

if __name__ == '__main__':
    if '--watch' in sys.argv:
        interval = int(sys.argv[sys.argv.index('--watch')+1])
        while True:
            os.system('clear')
            r = collect(); a = evaluate(r); report(r, a)
            print(f"\n  ({interval}s 后刷新，Ctrl+C 退出)")
            time.sleep(interval)
    else:
        r = collect(); a = evaluate(r)
        crit = report(r, a)
        if '--json' in sys.argv:
            print(json.dumps({'report': r, 'alerts': a}, ensure_ascii=False, indent=2))
        sys.exit(1 if crit > 0 else 0)
