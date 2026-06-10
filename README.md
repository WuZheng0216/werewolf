# AI 狼人杀 Agent Teams

一个面向字节跳动「Agent Teams 实践 - AI 狼人杀」挑战的多智能体博弈系统。项目实现了 9 人狼人杀完整对局，并在基础能力上扩展了评测复盘、自进化 memory/skill、实时前端观战和人机对战。

主线 9 人局配置：

```text
3 狼人 + 1 预言家 + 1 女巫 + 1 猎人 + 3 村民
```

进阶路线：

- **B. 评测 + 复盘**：不只看胜负，而是对发言、投票、技能、阵营贡献、狼人欺骗和关键失误进行结构化评测。
- **C. 自进化 Agent**：自动对局 -> 复盘反思 -> memory/skill 更新 -> AB / frozen eval 验证 -> 版本晋级与回溯。

## 功能亮点

- 五类角色 Agent：狼人、预言家、女巫、猎人、村民均有独立 Prompt、动作空间、策略记忆和可召回 skill。
- 严格信息隔离：每个 Agent 只看到身份允许的信息，狼人知道狼队友，预言家只知道自己的查验，村民看不到夜间私有行动。
- 完整规则引擎：夜晚行动、白天发言、同步投票、弃票、平票 PK、猎人开枪、屠边胜负判定。
- 真实 LLM 决策：支持 Doubao-Seed-2.0-pro、DashScope DeepSeek V4 Flash、DeepSeek V3.2 推理模式。
- 狼队夜间会议：狼人先独立提案，再由 LLM 协调器生成私有战术计划，减少随机夜刀和公开同质化。
- 长期记忆与 BM25 召回：从 memory bank 中按角色、阶段、公开历史、私有状态召回相关经验。
- Skill Evolution：把复盘经验沉淀成可触发的技能卡，支持新增、强化、改写、退休候选和版本记录。
- 实时前端：支持观战、人机对战、Agent I/O、私有视角、耗时、复盘报告、Leaderboard 和冻结测评进度。
- 可审计实验：逐局归档、失败记录、限流重试、冻结评测、gate 晋级和 Codex Skill Lab 变更日志。

## 快速本地部署

默认推荐 Windows + Conda。其他系统也可以使用 Python 3.12+ 直接运行。

### 1. 克隆项目

```powershell
git clone https://github.com/WuZheng0216/werewolf.git
cd werewolf
```

### 2. 一键准备环境

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_local.ps1
```

脚本会：

- 创建或复用 `werewolf` conda 环境。
- 从 `.env.example` 复制出本地 `.env`。
- 安装 `requirements.txt` 中的依赖。

如果你想用已有 Python 环境：

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

### 3. 配置模型 API

打开 `.env`，至少填入一个可用模型 Key。真实 API Key 只放在本地 `.env`，不要提交到 GitHub。

Doubao 示例：

```dotenv
ARK_API_KEY=replace-with-your-ark-api-key
ARK_MODEL=ep-20260514115354-k4jz4
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
```

DashScope DeepSeek 示例：

```dotenv
DASHSCOPE_API_KEY=replace-with-your-dashscope-api-key
DASHSCOPE_MODEL=deepseek-v4-flash
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_ENABLE_THINKING=true
```

### 4. 启动服务

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

如果不用 Conda：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_local.ps1 -NoConda
```

更详细的本地部署说明见 [LOCAL_QUICKSTART.md](LOCAL_QUICKSTART.md)。

## 前端使用建议

第一次演示建议先小规模运行：

```text
Model: Doubao 或 DeepSeek Flash
Version: latest
Seed: random
Evo Rounds: 1
Games / Round: 1
AB Games: 1
```

常用入口：

- 单局实时对局：观察一局完整 AI 狼人杀。
- 人机对战：真人用打字参与发言、投票和技能行动，不能看到上帝视角隐藏信息。
- Frozen Eval：冻结版本测评，不写入 memory，也不更新 latest，适合答辩展示。
- Evolution：自动对局和自进化，会消耗较多 token，建议确认 Key 和限流后再跑。

## 命令行示例

运行一局 Doubao 对局：

```powershell
python scripts/run_demo.py game --seed 20260521 --version latest --llm-provider ark
```

运行一局 DeepSeek V4 Flash 对局：

```powershell
python scripts/run_demo.py game --seed 20260521 --version latest --llm-provider dashscope --llm-model deepseek-v4-flash
```

运行 skill 模式自进化：

```powershell
python scripts/run_demo.py evolution --rounds 2 --games 10 --evolution-mode skill --llm-provider ark
```

运行冻结评测：

```powershell
python scripts/run_demo.py frozen-eval --version latest --baseline-version initial --games 20 --llm-provider ark
```

后台持续演进：

```powershell
python scripts/run_evolution_job.py --rounds 2 --games 10 --base-version latest --llm-provider ark --seed 20260521 --evolution-mode skill
```

## 评测指标

Evaluator 使用结构化日志自动计算指标，不依赖人工逐局打分，也不让模型自评。核心综合分为：

```text
overall =
  speech_quality          * 0.20
+ vote_quality            * 0.16
+ skill_quality           * 0.25
+ team_contribution       * 0.12
+ wolf_deception_quality  * 0.10
+ mistake_penalty         * 0.17
```

其中：

```text
mistake_penalty = max(0, 100 - 9 * 关键失误数量)
```

关键 bad case 包括预言家查狼未报、女巫毒好人、猎人带走好人、狼人集体同票暴露、狼人模板化发言、低质量伪装、没有对单预言家形成反制等。详细技术说明见 [docs/AI狼人杀项目技术报告-评分对照版.md](docs/AI狼人杀项目技术报告-评分对照版.md)。

## 项目结构

```text
werewolf_ai/
  engine.py              # 对局引擎：夜晚、白天、投票、PK、胜负判定
  agents.py              # 五类角色 Prompt 与决策构造
  llm.py                 # Doubao / DashScope LLM 客户端、重试、限流
  evaluator.py           # 多维评测与关键失误检测
  reviewer.py            # LLM 上帝视角复盘
  memory_store.py        # memory bank 写入与版本管理
  memory_retriever.py    # BM25 + 标签 + 近期性召回
  skill_evolution.py     # skill card 自进化
  evolution.py           # 自动对局、AB、晋级
  service.py             # 后端服务接口
static/                  # 前端页面
scripts/                 # 演示、演进、冻结评测、打包脚本
tests/                   # 单元测试
data/memory/             # 可发布的 memory / skill / latest 版本资产
docs/                    # 技术报告与实验记录
```

## 数据与安全

`.env`、`logs/`、`dist/`、Python 缓存和本地运行日志默认不进入 Git。请不要把真实 API Key 写入代码、README、实验日志或提交记录。

项目运行产生的数据主要在：

- `logs/games/`：逐局完整对局日志，本仓库默认不提交。
- `logs/evolution_jobs/`：后台演进任务进度，本仓库默认不提交。
- `data/memory/memory_bank.json`：长期记忆库，可用于复现实验和演示。
- `data/memory/role_memory_*.json`：角色策略与 skill 版本。
- `data/memory/latest_promoted.json`：当前 latest 指针。

如果只想把项目发给别人快速本地体验，可以运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package_local_demo.ps1
```

它会生成：

```text
dist\werewolf-local-demo.zip
```

压缩包会排除 `.env`、`logs/`、缓存文件和其他本地临时产物。

## 常用验证

```powershell
python -m unittest discover -s tests
python -m compileall werewolf_ai scripts
```

如果出现 `401`，通常是 `.env` 中的 Key 错误、过期，或修改 `.env` 后没有重启服务。如果出现 `429`，通常是共享账号或共享 endpoint 的 TPM/RPM 限流被打满，需要等待后重试或降低并发。

## 文档索引

- [LOCAL_QUICKSTART.md](LOCAL_QUICKSTART.md)：给评委或同学的本地快速部署说明。
- [docs/AI狼人杀技术文档.md](docs/AI狼人杀技术文档.md)：系统架构与实现细节。
- [docs/AI狼人杀项目技术报告-评分对照版.md](docs/AI狼人杀项目技术报告-评分对照版.md)：按比赛评分标准组织的技术报告。
- [docs/Codex-Skill-Lab-方法有效性评估.md](docs/Codex-Skill-Lab-方法有效性评估.md)：Codex 审查式 skill 优化方法。
- [docs/Codex-Skill-Lab-版本变更记录.md](docs/Codex-Skill-Lab-版本变更记录.md)：skill 版本变更与原因。

## 说明

本项目是研究和比赛原型，真实 LLM 对局会消耗较多 token，且单局可能需要几分钟。建议先跑 1 局确认模型和前端正常，再启动多局 evolution 或 frozen eval。
