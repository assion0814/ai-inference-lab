#!/bin/bash
# DeepSeek V4.1-Flash (Maya-XS) 一键启动
# 依赖：Project Maya 已安装于 ~/project-maya，模型在 /data/kafka/maya-models/Maya-XS
set -e
cd ~/project-maya || exit 1

echo "════════ DeepSeek V4.1-Flash 启动 ════════"

# 1. 检查 V100
if ! nvidia-smi >/dev/null 2>&1; then
    echo "❌ V100 不可用，检查 OCuLink/供电后重试"
    exit 1
fi
echo "✓ V100: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader)"

# 2. 清理占用 V100 的进程
for pid in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null || true
done
sleep 2

# 3. 启动（前台，Ctrl+C 停止）
echo "✓ 启动服务..."
exec ./run-maya-ds4.sh
