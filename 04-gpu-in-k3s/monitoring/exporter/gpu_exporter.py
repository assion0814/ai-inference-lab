#!/usr/bin/env python3
"""
最小 GPU exporter —— 零依赖，输出 Prometheus 文本格式
监听 :9400/metrics
"""
import subprocess, time, re
from http.server import HTTPServer, BaseHTTPRequestHandler

def gpu_metrics():
    """用 nvidia-smi 采集 GPU 指标"""
    try:
        out = subprocess.run(
            ['nvidia-smi',
             '--query-gpu=index,name,uuid,driver_version,temperature.gpu,'
             'memory.used,memory.total,utilization.gpu,power.draw,power.limit,'
             'clocks.current.graphics,clocks.current.memory,persistence_mode',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as e:
        return f'# nvidia-smi failed: {e}\n'
    lines = []
    for row in out.split('\n'):
        if not row.strip(): continue
        p = [x.strip() for x in row.split(',')]
        if len(p) < 13: continue
        idx, name, uuid = p[0], p[1], p[2]
        labels = f'gpu="{idx}",name="{name}",uuid="{uuid}"'
        try:
            lines += [
                f'nvidia_gpu_driver_version{{{labels}}} 0',
                f'nvidia_gpu_temperature_celsius{{{labels}}} {float(p[4])}',
                f'nvidia_gpu_memory_used_bytes{{{labels}}} {float(p[5])*1024*1024}',
                f'nvidia_gpu_memory_total_bytes{{{labels}}} {float(p[6])*1024*1024}',
                f'nvidia_gpu_utilization_percent{{{labels}}} {float(p[7])}',
                f'nvidia_gpu_power_draw_watts{{{labels}}} {float(p[8])}',
                f'nvidia_gpu_power_limit_watts{{{labels}}} {float(p[9])}',
                f'nvidia_gpu_clock_graphics_mhz{{{labels}}} {float(p[10])}',
                f'nvidia_gpu_clock_memory_mhz{{{labels}}} {float(p[11])}',
            ]
        except (ValueError, IndexError):
            continue
    return '\n'.join(lines) + '\n'

def system_metrics():
    """系统指标"""
    lines = []
    # load average
    with open('/proc/loadavg') as f:
        la = f.read().split()
    for i, v in zip(['1m','5m','15m'], la[:3]):
        lines.append(f'node_load{{period="{i}"}} {v}')
    # memory
    mem = {}
    with open('/proc/meminfo') as f:
        for line in f:
            k, v = line.split(':', 1)
            mem[k] = int(v.strip().split()[0]) * 1024
    for k in ['MemTotal','MemFree','MemAvailable','Buffers','Cached','SwapTotal','SwapFree']:
        if k in mem:
            lines.append(f'node_memory_{k}_bytes {mem[k]}')
    # uptime
    with open('/proc/uptime') as f:
        up = float(f.read().split()[0])
    lines.append(f'node_uptime_seconds {up}')
    return '\n'.join(lines) + '\n'

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/metrics':
            body = ''
            body += '# HELP nvidia_gpu_temperature_celsius GPU temperature\n'
            body += '# TYPE nvidia_gpu_temperature_celsius gauge\n'
            body += gpu_metrics()
            body += '# HELP node_load System load\n'
            body += '# TYPE node_load gauge\n'
            body += system_metrics()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4')
            self.end_headers()
            self.wfile.write(body.encode())
        elif self.path == '/health':
            self.send_response(200); self.end_headers(); self.wfile.write(b'OK')
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a): pass

if __name__ == '__main__':
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9400
    print(f'GPU exporter listening on :{port}', flush=True)
    HTTPServer(('0.0.0.0', port), Handler).serve_forever()
