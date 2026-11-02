<!-- 层归属（SOC.md §四）：经营（后台）。技术资产登记 -->
> **层归属**：`经营（后台）` —— 技术资产登记与可复用工程配方
> **执行体**：`/home/assion/project-maya/run-maya-ds4.sh`（前台服务）
> **项目状态**：**✅ 成功运行（2026-10-28，实测 12.8-15.2 tok/s）**
> **相关资产**：[`PROJECT-MAYA-V100-ASSETS.md`](PROJECT-MAYA-V100-ASSETS.md)、[`STRATA-DSV41-V100-BUILD.md`](STRATA-DSV41-V100-BUILD.md)

# 🏆 DeepSeek-V4.1-Flash（748B MoE）在单张 V100 上成功运行

> **历史性成果：2017 年的 Tesla V100-PCIE-32GB（二手价约 1000 元）成功运行 748B 参数的 DeepSeek V4.1-Flash，实测 12.8-15.2 tok/s。**

---

## 一、★★★ 最终成果

### 1.1 硬件配置

```
主机:     UM890 Pro（Ryzen 9 PRO 8945HS, 16 线程, 60GB RAM）
GPU:      Tesla V100-PCIE-32GB（2017 年，Volta sm_70）
          · 通过 OCuLink（PCIe 3.0 x4）连接
          · 驱动 580.178.04
          · 32GB HBM2
存储:     NVMe SSD（/data/kafka，938GB）
网络:     华为随行WiFi 3 Pro（4G LTE）
```

### 1.2 实测性能（2026-10-28）

| 测试 | prompt tokens | 生成 tokens | 生成速度 |
|---|---:|---:|---:|
| MoE 架构介绍（长文本）| 57 | 399 | **14.9 tok/s** |
| 什么是量子纠缠 | ~40 | 200 | **14.5 tok/s** |
| 简单解释区块链 | ~40 | 200 | **15.2 tok/s** |
| Python GIL | 38 | 200 | **12.8 tok/s** |
| **平均** | | | **~14.4 tok/s** |

**其他指标：**
```
prompt 处理:      6.7-8.3 tok/s
专家缓存命中率:   71.7%
V100 显存:        32168 / 32768 MiB (98%)
V100 利用率:      55%
V100 温度:        47°C
V100 功耗:        43.74 W（远低于 250W 上限）
```

### 1.3 对比官方数据

| 配置 | 速度 | 来源 |
|---|---:|---|
| Project Maya 官方: 1× V100 32GB + 64GB RAM（Maya-S）| up to 19 tok/s | README |
| **我们: 1× V100 32GB + 60GB RAM（Maya-XS）** | **14.4 tok/s** | **实测** |
| 达成率 | **76%** | |

**考虑到我们用的是更激进的量化（IQ1_S 1.75bit vs Maya-S IQ2_XXS 2bit）和更少的 RAM（60 vs 64GB），这已经非常接近。**

---

## 二、★★★ 模型与量化

### 2.1 使用的模型

```
模型:   DeepSeek-V4.1-Flash-1017（748B 参数 MoE）
量化:   Maya-XS（Project Maya 自研）
  · routed experts: IQ1_S（1.75 bit，error-feedback rounding）
  · dense weights:  Q8_0
  · Engram tables:  Q6_K
  · drafter:        IQ2_XXS（3.5GB，可选）

文件:   5 个 GGUF shard，共 276,626,308,260 字节 = 257.63 GB
位置:   /data/kafka/maya-models/Maya-XS/

HF 仓库: peasantsmith/DeepSeek-V4.1-Flash-1017-Maya-GGUF
```

### 2.2 各 shard 准确大小

| shard | 字节数 | GB | sha256 前 8 位 |
|---|---:|---:|---|
| 1 | 47,781,530,400 | 44.50 | |
| 2 | 47,775,748,128 | 44.49 | |
| 3 | 19,784,230,848 | 18.43 | |
| 4 | 80,641,295,472 | 75.10 | |
| 5 | 80,643,503,412 | 75.11 | |
| **总计** | **276,626,308,260** | **257.63** | |

### 2.3 DSpark drafter

```
文件:   /data/kafka/maya-models/dspark/dspark-xs2.bin
大小:   4,251,725,568 字节 = 3.96 GB
sha256: 8dfe21082a6cb320f334fddb454630c241c6d44e0463eef4f8b0b07334511779
json:   dspark-xs2.json（99,146 字节，sha256 56d47ad1...）

★ 实测：DSpark 在这台机器上无效（14.0 vs 14.9 tok/s）
  原因：瓶颈是专家从 RAM/SSD 读取，不是 token 生成
  → setup 自动选择 --dspark none
```

---

## 三、★★★ 完整安装步骤（可复算）

### 3.1 前置条件

```bash
# V100 可用
nvidia-smi   # 应显示 Tesla V100-PCIE-32GB

# CUDA 12.4 + gcc-13（CUDA 12.4 最高支持 gcc 13，gcc 15 会失败）
nvcc --version     # release 12.4
gcc-13 --version   # 13.4.0

# Python 3.12（3.14 太新，部分包不兼容）
python3.12 --version

# 模型已下载（257.63GB）
ls /data/kafka/maya-models/Maya-XS/*.gguf | wc -l   # 应为 5

# drafter（可选，3.96GB）
ls /data/kafka/maya-models/dspark/dspark-xs2.bin
```

### 3.2 安装 Project Maya

```bash
# 1. 克隆到持久目录（不要 /tmp，重启会清）
cd ~
git clone --depth 1 https://gh-proxy.com/https://github.com/mw00/project-maya.git project-maya
cd project-maya

# 2. 用 uv 建 venv（系统 Python 3.14 缺 venv/ensurepip）
~/.local/bin/uv venv .venv --python python3.12

# 3. 装依赖（uv 建的 venv 默认无 pip，用 uv pip install）
~/.local/bin/uv pip install --python .venv/bin/python \
  --index-url https://mirrors.aliyun.com/pypi/simple/ \
  numpy jinja2 regex pyyaml tqdm requests cmake ninja pillow psutil \
  "jsonschema>=4.18,<5" tokenizers pip

# 4. 检查环境
.venv/bin/python maya.py --check
# 应显示: This PC can run Maya.

# 5. 完整 setup（用已有模型 + llama.cpp）
.venv/bin/python maya.py --setup --yes --no-start \
  --model deepseek-maya-xs \
  --model-dir /data/kafka/maya-models/Maya-XS \
  --gpu 0 \
  --context 8192 \
  --llama-dir /home/assion/llama.cpp \
  --nvcc /usr/bin/nvcc \
  --host-compiler g++-13 \
  --no-vision \
  --vram-reserve-mib 512 \
  --mmap-experts auto \
  --port 8080

# 6. 启动
./run-maya-ds4.sh
```

### 3.3 ★ 关键参数说明

| 参数 | 值 | 为什么 |
|---|---|---|
| `--model deepseek-maya-xs` | Maya-XS | 正确识别模型家族 |
| `--model-dir` | 模型目录 | **DeepSeek 必须用这个，不能用 --gguf-dir**（后者只认 GLM）|
| `--host-compiler g++-13` | g++-13 | CUDA 12.4 不支持 gcc 15 |
| `--llama-dir` | ~/llama.cpp | 避免从 GitHub 慢速下载 |
| `--no-vision` | 跳过视觉塔 | 省 1.1GB 和编译时间 |
| `--vram-reserve-mib 512` | 512 | 保留 VRAM 给系统 |
| `--mmap-experts auto` | 自动 | 专家从文件就地读取 |
| `--context 8192` | 8K | 小上下文减少 KV 缓存占用 |

### 3.4 ★ 服务接口

```
OpenAI 兼容 API:
  POST http://127.0.0.1:8080/v1/chat/completions
  POST http://127.0.0.1:8080/v1/responses
  GET  http://127.0.0.1:8080/v1/models

Anthropic 兼容 API:
  POST http://127.0.0.1:8080/v1/messages

Chat UI（浏览器）:
  http://127.0.0.1:8080/

采样默认值: temperature=1.0, top_p=1.0
思考等级: high（可请求时覆盖）
思考预算: 32768 tokens
上下文: 8192 tokens
```

---

## 四、⚠️ 踩坑清单（12 条）

| # | 坑 | 现象 | 解法 |
|---|---|---|---|
| 1 | **/tmp 被重启清空** | 引擎、源码全丢 | 装到持久目录 `~/project-maya` |
| 2 | **系统 Python 3.14 缺 venv** | maya.sh 报 "Python 3.10 or newer needed" | 用 uv 建 Python 3.12 venv |
| 3 | **uv 建的 venv 无 pip** | setup 报 "No module named pip" | `uv pip install pip` 先装 pip |
| 4 | **`--gguf-dir` 只认 GLM** | 报 "no GLM-5.3-Flash GGUF file" | DeepSeek 用 `--model-dir` |
| 5 | **`--model` 不指定→按 GLM 解析** | 同上 | 加 `--model deepseek-maya-xs` |
| 6 | **drafter 路径** | 要放 `/data/kafka/maya-models/dspark/`（模型目录的**父目录**）| 不是模型目录下 |
| 7 | **gcc 15 太新** | CUDA 12.4 编译失败 | `--host-compiler g++-13` |
| 8 | **V100 从 PCIe 消失** | `NVRM: No NVIDIA GPU found` | 检查 OCuLink 线，重启主机 |
| 9 | **板子失联导致 V100 过热** | V100 风扇由 RK3588 控制 | 修板子电源/串口 |
| 10 | **finalize 无限循环** | `set -e` + nvidia-smi 失败 → systemd 重启循环 | 先修 GPU 再跑 finalize |
| 11 | **重复 aria2 进程** | 三重管理冲突 | 单实例锁（flock）|
| 12 | **4G 流量卡达量降速** | 速度锁定 0.6MB/s | 重启路由器重新协商 QoS（流量包刷新后必须重连）|

---

## 五、★ 性能瓶颈分析

### 5.1 当前瓶颈（按贡献排序）

```
1. 专家从 RAM/SSD 读取（主要）
   · VRAM 32GB 只放得下约 4600/15360 个专家（30%）
   · 其余 70% 从 RAM（60GB）和 NVMe SSD 读
   · 专家缓存命中率 71.7%

2. CPU 计算（次要）
   · 未命中 VRAM 的专家在 CPU lane 算（8 线程）
   · CPU lane 占 20-24% 的专家

3. KV 缓存（context 8192）
   · kv=fp4，kv_resident=8192
```

### 5.2 优化空间

| 优化 | 预期效果 | 成本 |
|---|---|---|
| 增加 RAM（60→128GB）| 专家缓存命中率↑，少读 SSD | 内存条 |
| 增加 context（8K→32K）| 长对话能力 | 更多 KV 缓存 |
| 用 Maya-S（316GB, IQ2_XXS）| 更高精度 | 再下 59GB |
| 用 DSpark drafter | ❌ 实测无效 | 已下载 |
| 更大 VRAM（V100 32GB 是上限）| 更多专家驻留 | 换卡 |

---

## 六、诚实边界

| 项 | 状态 |
|---|---|
| **DeepSeek V4.1-Flash 在 V100 上运行** | ✅ **实测成功** |
| 实测生成速度 | ✅ **12.8-15.2 tok/s** |
| 8000 上下文对话 | ✅ 实测 |
| 中文问答质量 | ✅ 实测（回答准确）|
| OpenAI 兼容 API | ✅ 实测 HTTP 200 |
| **与官方数据对比** | ✅ 达到 76% |
| DSpark drafter | ✅ 已下载但实测无效 |
| 32K 上下文 | ❌ 未测（需要更多内存）|
| 重启后自动恢复 | ⚠️ systemd 服务已写好但未启用 |
| 板子（RK3588）| ❌ 仍失联（V100 风扇停转）|

---

## 七、一键启动

```bash
# 前台启动（Ctrl+C 停止）
bash ~/start-deepseek-maya.sh

# 或直接用 Project Maya
cd ~/project-maya && ./run-maya-ds4.sh
```
