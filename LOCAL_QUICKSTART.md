# AI 狼人杀本地快速部署

这份说明面向“拿到项目后想在自己电脑上快速跑起来”的同学或评委。默认使用 Windows + Conda；如果你已经有 Python 3.12+ 环境，也可以跳过 Conda。

## 1. 准备条件

需要提前安装：

- Git，或直接拿到项目压缩包。
- Miniconda / Anaconda。
- 至少一个可用模型 API Key：
  - 方舟 Ark：`ARK_API_KEY`，用于 Doubao-Seed-2.0-pro。
  - 百炼 DashScope：`DASHSCOPE_API_KEY`，用于 deepseek-v4-flash / deepseek-v3.2。

不要把自己的真实 API Key 提交到 GitHub。

## 2. 一键准备环境

在项目根目录打开 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_local.ps1
```

脚本会做三件事：

- 创建 `werewolf` conda 环境，如果已经存在则跳过。
- 从 `.env.example` 复制出本地 `.env`，如果 `.env` 已存在则不覆盖。
- 执行 `pip install -r requirements.txt`。

如果你想使用已有 conda 环境：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_local.ps1 -EnvName your_env_name -SkipCondaCreate
```

## 3. 填写 API Key

打开 `.env`，至少配置一个模型。

Doubao 示例：

```dotenv
ARK_API_KEY=你的方舟APIKey
ARK_MODEL=ep-20260514115354-k4jz4
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ARK_TIMEOUT_SECONDS=120
ARK_MAX_RETRIES=5
```

DashScope 示例：

```dotenv
DASHSCOPE_API_KEY=你的百炼APIKey
DASHSCOPE_MODEL=deepseek-v4-flash
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_ENABLE_THINKING=true
```

保存 `.env` 后重新启动后端才会生效。

## 4. 启动前端和后端

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_local.ps1
```

浏览器打开：

```text
http://127.0.0.1:8000
```

如果端口被占用：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_local.ps1 -Port 8010
```

如果你不用 Conda，而是在当前 Python 环境中运行：

```powershell
python -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts\start_local.ps1 -NoConda
```

## 5. 推荐演示方式

第一次给别人演示时，建议这样设置：

```text
Model: Doubao
Version: latest
Seed: random
Evo Rounds: 1
Games / Round: 1
AB Games: 1
```

优先点击单局实时对局或人机对战。不要一开始就让对方跑大规模 evolution / frozen eval，因为这些会消耗大量 API token，且一局真实 LLM 对局可能需要几分钟。

## 6. 常见问题

### 页面能打开，但对局报 401

通常是 `.env` 中 API Key 错误、过期，或者修改 `.env` 后没有重启服务。

### 报 TimeoutError

模型响应过慢。可以在 `.env` 中增大：

```dotenv
ARK_TIMEOUT_SECONDS=180
ARK_MAX_RETRIES=5
LLM_REVIEW_TIMEOUT_SECONDS=300
```

### 报 429

说明模型账号或共享 endpoint 的 TPM/RPM 限流被打满。等待一会儿再跑，或降低同时运行的对局数量。

### 修改 `.env` 后没有变化

后端进程已经读取了旧环境变量。停止服务后重新运行 `scripts\start_local.ps1`。

### PowerShell 提示脚本无法运行

使用本文命令里的 `-ExecutionPolicy Bypass` 即可，只对本次命令生效。

## 7. 发给别人时建议包含哪些文件

如果用压缩包发给别人，建议包含：

```text
werewolf_ai/
static/
scripts/
data/memory/latest_promoted.json
data/memory/role_memory_evolved_skill_r12_v2.json
data/memory/memory_bank.json
requirements.txt
server.py
.env.example
LOCAL_QUICKSTART.md
README.md
```

不要包含：

```text
.env
真实 API Key
过大的临时日志
```

完整实验复现才需要包含 `logs/` 和 `data/memory/evolution_runs/`；普通本地 Demo 可以不带完整历史日志。

也可以直接在项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package_local_demo.ps1
```

它会生成：

```text
dist\werewolf-local-demo.zip
```

这个压缩包会排除 `.env`、`logs/`、`__pycache__/` 和 Python 缓存文件，适合发给别人做本地 Demo。
