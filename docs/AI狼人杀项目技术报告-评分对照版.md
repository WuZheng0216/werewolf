# AI 狼人杀 Agent Teams 技术报告

更新时间：2026-06-10

## 1. 项目概述

本项目面向字节跳动「Agent Teams 实践 - AI 狼人杀」挑战，目标是实现一个能够自主完成狼人杀对局的多智能体系统，并在基础对局能力之上完成进阶路线：

- **B. 评测 + 复盘**：建立不只看胜负的多维评测、关键失误定位、结构化复盘报告和 Leaderboard。
- **C. 自进化 Agent**：实现自动对局、复盘分析、策略记忆/skill 演进、AB 对战验证和版本回溯。

项目主线采用 9 人局：

```text
3 狼人 + 1 预言家 + 1 女巫 + 1 猎人 + 3 村民
```

系统已从最初的基础 LLM 对局，逐步演进为一个包含实时前端、信息隔离、长期记忆、BM25 召回、技能卡自进化、冻结评测、Codex 审查式 skill 优化的实验平台。

## 2. 对照评分要求的完成情况

| 评分项 | 权重 | 项目实现 | 当前状态 |
|---|---:|---|---|
| 单 Agent 能力 | 20% | 五类角色独立 Prompt、角色目标、动作空间、策略记忆、skill 召回、座位人格、决策理由与引用约束 | 已完成，并有多轮调优痕迹 |
| 多 Agent 协作与系统设计 | 20% | 公共/私有信息隔离、角色技能调度、狼人夜间会议、同步投票、PK 平票、上下文拼接、长期记忆与 skill 召回 | 已完成，仍可继续增强深层博弈 |
| 工程实现与系统完整度 | 30% | 9 人局引擎、实时前端、人机对战、日志归档、SSE 流式、重试限流、冻结评测、测试脚本和文档 | 已完成，工程链路较完整 |
| 进阶 B：评测 + 复盘 | 30% 可选 | 发言/投票/技能/阵营贡献/狼人欺骗等多维评分，bad case 检测，LLM 上帝视角复盘，Leaderboard，review pack | 已完成主功能 |
| 进阶 C：自进化 Agent | 30% 可选 | 自动对局 -> 评测复盘 -> memory/skill 更新 -> AB/frozen eval -> gate 晋级 -> 版本回溯 | 已完成闭环，统计显著性仍需更大样本 |

整体判断：

```text
基础课题完整达标。
B 路线具备结构化评测与复盘能力。
C 路线具备可审计的自进化闭环，并已有初步效果证据。
后续答辩需要强调：我们不是只展示胜负，而是展示 Agent 如何被评测、复盘、记忆和验证。
```

## 3. 系统架构

核心模块如下：

| 模块 | 文件 | 作用 |
|---|---|---|
| Game Engine | `werewolf_ai/engine.py` | 负责夜晚行动、白天发言、投票、PK、死亡结算、胜负判定 |
| Models | `werewolf_ai/models.py` | 定义 `GameConfig`、`PrivateObservation`、`AgentDecision`、`GameEvent`、`EvaluationReport` 等数据结构 |
| Role Agents | `werewolf_ai/agents.py` | 五类角色 Prompt、角色目标、记忆注入、skill 注入和决策格式 |
| LLM Client | `werewolf_ai/llm.py` | 接入 Doubao、DashScope DeepSeek，处理 JSON 解析、重试、超时、限流 |
| Evaluator | `werewolf_ai/evaluator.py` | 多维评分、关键失误检测、狼人欺骗质量与社会影响力评估 |
| Reviewer | `werewolf_ai/reviewer.py` | LLM 上帝视角复盘，生成角色级改进建议 |
| Memory Store | `werewolf_ai/memory_store.py` | 长期记忆写入、审批状态、版本化记忆 |
| Memory Retriever | `werewolf_ai/memory_retriever.py` | BM25 + 标签 + 近期性 + 匹配词召回 |
| Skill Evolution | `werewolf_ai/skill_evolution.py` | 把复盘经验转成可触发的 skill card，支持 add/refine/reinforce/retire |
| Evolution Manager | `werewolf_ai/evolution.py` | 自动对局、版本生成、AB 评测、晋级决策 |
| Evolution Strategist | `werewolf_ai/evolution_strategist.py` | 用 LLM 生成下一轮演化假设和策略方向 |
| Service/API | `werewolf_ai/service.py`、`server.py` | 后端接口、SSE 流式、前端任务控制 |
| Codex Skill Lab | `werewolf_ai/codex_skill_lab.py`、`scripts/run_codex_skill_lab_job.py` | Codex 审查日志、优化 skill、记录变更、做冻结评测 |

系统数据流：

```text
GameConfig
  -> GameEngine
  -> Observation Builder 生成每名玩家的 PrivateObservation
  -> RoleAgent 构造中文 Prompt + 历史 + 记忆 + skill + 私有信息
  -> LLM 输出 AgentDecision
  -> Engine 校验动作并写入 GameEvent
  -> Archive 保存完整对局
  -> Evaluator 多维评分
  -> Reviewer 复盘
  -> Memory/Skill Evolution 更新候选版本
  -> AB/Frozen Eval 验证
  -> Leaderboard / Frontend 展示
```

## 4. 基础对局能力

### 4.1 规则实现

已实现的 9 人局规则包括：

- 狼人夜间刀人。
- 预言家夜间查验阵营。
- 女巫解药、毒药各一次。
- 猎人死亡后可开枪。
- 白天顺序发言。
- 白天同步投票，避免后投玩家看到前投玩家票型后跟票。
- 支持弃票。
- 平票进入 PK 发言和二次投票，二次仍平票则无人出局。
- 胜负判定采用屠边规则：狼人全灭则好人胜；平民全灭或神职全灭则狼人胜。

项目中曾专门修复过规则细节，包括：

- 仅 3 个好人阵亡不能直接判定屠边，必须是 3 个平民阵亡或 3 个神职阵亡。
- 投票阶段改为同步投票。
- 增加弃票。
- 增加平票 PK 和二次投票。
- 修复猎人死亡开枪事件的公开性和信息边界。
- 补充狼人夜间可看到狼队友并进行协同。

### 4.2 信息隔离

每个 Agent 的输入由 `PrivateObservation` 构造，原则是：

- 狼人知道自己身份和狼队友，但不知道预言家查验结果、女巫用药私有原因。
- 预言家只知道自己的查验结果。
- 女巫知道夜间被刀目标和自己的药品状态。
- 猎人知道自己身份和是否可开枪。
- 村民只看到公开事件和公开发言。
- 真人玩家在人机对战中也只能看到自己身份允许的信息。

前端支持查看：

- 上帝视角，用于演示和复盘。
- 单 Agent 私有视角，用于证明信息隔离。
- Agent 输入/输出摘要、决策、理由、耗时和 metadata。

## 5. LLM 接入与 Prompt 设计

### 5.1 多模型接入

项目支持多模型切换：

- Doubao-Seed-2.0-pro，使用方舟 Ark API。
- DashScope DeepSeek V4 Flash。
- DashScope DeepSeek V3.2 推理模式。

目前主线实验主要使用 Doubao，部分冻结评测使用 DashScope DeepSeek V4 Flash 以降低成本或缓解限流。

### 5.2 纯 LLM 决策

项目已经移除基于规则的 Agent fallback，保留 LLM 调用路径。这样做的原因是：

- 避免规则策略污染 Agent 行为评测。
- 保证所有角色策略差异都来自 Prompt、记忆、skill 和模型推理。
- 出错时直接暴露 LLMError，便于定位 JSON、超时或限流问题。

引擎仍然保留合法性校验：

- 目标必须存活且合法。
- 女巫救人目标必须是当夜被刀目标。
- 非法动作会被截断、重试或报错。

### 5.3 中文 Prompt 与上下文

所有角色 Prompt 已改为中文，并显式写入：

- 当前 9 人局规则和人数配置。
- 当前阶段允许的动作。
- 本角色目标。
- 当前公开历史和私有信息。
- 需要返回的 JSON 格式。
- 决策理由应引用公开信息，避免编造不存在的事件。
- 狼人允许策略性不诚实，但必须兼容公开历史，不能泄露狼队私有信息。

LLM API 本身每次调用是无状态的，因此项目通过代码主动拼接上下文，让 Agent 在同一局内具备连续认知：

- 公共事件历史。
- 个人短期记忆。
- 座位人格与风格。
- 角色长期策略记忆。
- BM25 召回的相关记忆。
- 当前可用 skill。
- 个人 belief state。

## 6. 多 Agent 协作与博弈机制

### 6.1 狼人夜间会议

狼人夜间不再是简单随机刀人，而是两阶段协同：

1. 每名狼人独立提交刀人提案。
2. LLM 狼队协调器生成私有 `wolf_council_plan`，给出最终刀人目标和次日可选协同路线。

这样既模拟了真实狼人杀中狼人夜间可协商的机制，也避免所有狼人公开发言完全同质化。

### 6.2 同步投票与 PK

投票阶段已经改成同步机制：

```text
冻结所有玩家投票输入
  -> 并行/独立调用投票决策
  -> 全部返回后统一公开票型
```

这能降低“后投玩家跟票”的伪智能现象，更接近真实狼人杀桌游中的同时结算逻辑。

平票后采用 PK：

```text
最高票平票
  -> 平票玩家 PK 发言
  -> 非 PK 玩家在平票对象中二次投票
  -> 二次仍平票则无人出局
```

### 6.3 座位人格与角色策略

项目同时保留两类个体差异：

- **角色差异**：狼人、预言家、女巫、猎人、村民有不同目标、动作空间和策略记忆。
- **座位人格差异**：不同座位有独立风格，用于降低模板化发言和狼队同质化行为。

这兼顾了测评可控性和真实游戏观感：

- 按角色评估更容易量化策略质量。
- 按座位保留人格更接近真实玩家体验。

## 7. 进阶 B：评测与复盘

### 7.1 多维评分

Evaluator 不只统计胜负，而是评估多个维度：

| 指标 | 含义 |
|---|---|
| `speech_quality` | 发言信息量、逻辑一致性、是否引用公开事件 |
| `vote_quality` | 投票是否符合公开证据、是否推动己方阵营目标 |
| `skill_quality` | 预言家查验、女巫用药、猎人开枪等技能使用质量 |
| `team_contribution` | 行为对阵营胜率的贡献 |
| `social_influence_quality` | 是否能形成归票、站队、质疑、交叉验证等影响 |
| `wolf_deception_quality` | 狼人是否有合理欺骗、伪装、制造分歧和不暴露狼队 |
| `wolf_deception_diversity` | 狼人欺骗行为是否多样，避免全员模板化 |
| `mistake_penalty` | 关键失误扣分 |
| `overall` | 综合得分 |

### 7.1.1 综合分计算方式

当前综合分在 `werewolf_ai/evaluator.py` 中由多个子指标加权得到：

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

这样设计的原因是：

- `skill_quality` 权重最高，因为狼人杀中的查验、用药、开枪、夜刀等技能对胜负影响很大。
- `speech_quality` 和 `vote_quality` 合计占 36%，覆盖白天发言与投票两个核心环节。
- `wolf_deception_quality` 单独计入，避免狼人只靠夜刀和随机票型获胜，却没有真实博弈能力。
- `mistake_penalty` 用来惩罚关键 bad case，防止模型整体表现看似不错但出现严重低级错误。
- `social_influence_quality` 和 `wolf_deception_diversity` 会进入报告和 Leaderboard，用于分析，但当前没有直接进入 overall 主公式，避免指标过多导致主分不稳定。

### 7.1.2 子指标如何测算

`speech_quality` 主要看发言的信息密度和公开逻辑：

- 基础分来自发言长度，最多贡献约 55 分。
- 如果发言包含查杀、金水、投票链、公开信息、解释、发言等证据词，会加分。
- 如果发言包含今天、建议、重点、归票、处理等行动规划词，会加分。
- 如果普通角色不恰当地声称夜间信息，会扣分。

`vote_quality` 根据真实身份与公开查杀情况估算：

- 好人投中狼人，基础高分。
- 狼人投向好人，视为符合狼队目标，也给较高分。
- 好人投好人，低分。
- 狼人投狼人，通常低分，但这不绝对代表错误，因为倒钩可能在欺骗指标中体现。
- 如果当天存在可信公开查杀，好人投向查杀目标会提高到更高分；偏离查杀目标会扣分。

`skill_quality` 按角色技能事件测算：

- 预言家查到狼人得分高于查到好人，因为更直接产生阵营信息。
- 女巫救人给较高分，毒中狼人高分，毒到好人低分，不用药给中性偏低分。
- 狼人夜刀神职高于夜刀平民，因为对狼队收益更大。
- 猎人开枪带走狼人高分，带走好人低分。

`team_contribution` 是投票、技能和阵营胜利倾向的综合：

```text
team_contribution = mean(vote_quality, skill_quality, winner_bonus)
```

其中 winner_bonus 对胜方阵营略高，用于体现行为最终是否服务阵营胜利。

`social_influence_quality` 衡量“发言是否真的影响了后续票型”：

- 好人发言中点到狼人，并影响后续好人投向该狼人，记为好人影响成功。
- 狼人发言中推动好人目标，并影响后续好人误投，记为狼人操纵成功。
- 好人与狼人双方都有可观测影响时，会有额外平衡加分，说明对局更有交互性。

`wolf_deception_quality` 是狼人侧的重点指标，基础分为 62，并根据欺骗行为加减：

- 早期狼队票型分散，加分。
- 对单预言家形成软对跳、质疑查验链或悍跳，加分。
- 悍跳链完整，包括查验对象、查验结论、后续查验计划和公开理由，加分。
- 狼人之间制造合理分歧、倒钩或切割，加分。
- 狼人成功带动好人误投，加分。
- 夜间狼队生成战术计划，加分。
- 多种欺骗意图并存，加分。
- 狼队同票暴露、模板化发言、低质量“我是好人”式伪装、不对跳、不完整悍跳会扣分。

`wolf_deception_diversity` 衡量狼人欺骗动作是否多样：

- 如果没有欺骗意图，只有较低基础分。
- 每出现一种欺骗意图会加分。
- `hide`、`none` 之外的主动欺骗，如 `fake_seer`、`soft_counter`、`bus_teammate`、`split_vote` 会进一步加分。

### 7.1.3 关键失误如何检测

关键失误不是由 LLM 主观评价，而是由事件日志中的结构化信息触发。典型规则如下：

| 失误类型 | 检测逻辑 |
|---|---|
| `witch_poisoned_good` | 女巫毒药目标真实身份不是狼人 |
| `hunter_shot_good` | 猎人开枪目标真实身份不是狼人 |
| `seer_failed_to_reveal_wolf` | 预言家已查到狼人，但后续白天没有明确报出该 P 号和查杀结论 |
| `seer_mechanical_day1_claim` | 预言家 D1 起跳，但没有查杀、没有被公开施压，也没有必要公开身份 |
| `witch_mechanical_n1_save` | 女巫首夜非自救直接用解药，标记为可能机械化 |
| `werewolf_exposed_pack_vote` | D1/D2 多名狼人把票集中投向同一个非狼目标，且所有投票狼人都在该目标上 |
| `werewolf_template_speech` | D1/D2 两名狼人发言二元 token Jaccard 相似度达到阈值，说明模板化过强 |
| `werewolf_low_deception_speech` | 狼人早期发言没有站边、质疑、倒钩、抗推等欺骗动作，只停留在低质量好人伪装 |
| `werewolf_no_seer_counterplay` | 早期出现非狼预言家起跳，但狼队没有形成软对跳、悍跳、质疑查验链或制造分歧 |
| `werewolf_incomplete_fake_seer_claim` | 狼人尝试对跳/悍跳预言家，但缺少查验对象、结果、后续计划或公开证据包装 |
| `villager_ignored_confirmed_wolf` | 存在可信公开查杀时，好人仍投向非狼目标 |

这种设计的优点是复盘可追溯：每个 bad case 都能定位到 day、phase、actor、target 和相关事件，而不是只输出一句“这局打得不好”。

### 7.2 关键失误检测

已实现的典型 bad case 包括：

- `seer_failed_to_reveal_wolf`：预言家查到狼但没有有效传递。
- `seer_mechanical_day1_claim`：预言家机械化首日起跳。
- `witch_mechanical_n1_save`：女巫机械化首夜救人。
- `witch_poisoned_good`：女巫毒死强好人。
- `hunter_shot_good`：猎人带走好人。
- `villager_ignored_confirmed_wolf`：村民无视已确认狼人。
- `werewolf_exposed_pack_vote`：狼队同目标、同理由集体投票导致暴露。
- `werewolf_template_speech`：狼人发言高度模板化。
- `werewolf_low_deception_speech`：狼人只说“我是好人”等低质量伪装。
- `werewolf_no_seer_counterplay`：场上只有一个预言家时狼人完全不对跳、不质疑。
- `werewolf_incomplete_fake_seer_claim`：悍跳链不完整。

这些检测让复盘不再停留在“谁赢了”，而是能指出“为什么赢/输、哪个动作质量差、哪类策略需要改”。

### 7.3 LLM 上帝视角复盘

每局结束后可调用 LLM Reviewer 进行上帝视角复盘：

- 总结阵营胜负关键。
- 找出每个角色做得好和做得差的点。
- 生成可沉淀到 memory bank 的角色级建议。
- 生成后续 Evolution Strategist 可使用的优化方向。

项目还支持历史复盘补跑：

```powershell
python scripts/backfill_reflections_to_memory_bank.py --dry-run
python scripts/backfill_reflections_to_memory_bank.py
```

## 8. 进阶 C：自进化 Agent

### 8.1 Workflow Memory 演进

早期自进化采用 workflow memory：

```text
自动对局
  -> Evaluator 多维评分
  -> Reviewer 角色复盘
  -> 写入 memory bank
  -> 生成候选策略版本
  -> AB 对战
  -> 晋级或拒绝
```

这种方式适合稳定积累经验，但缺点是记忆容易变成宽泛建议，触发条件不够明确。

### 8.2 Skill Evolution 演进

后续增加 skill 模式，把复盘结论转成可触发的技能卡：

```json
{
  "trigger": "什么时候应该考虑该技能",
  "procedure": "执行时应比较哪些选择",
  "avoid": "不能做什么",
  "evidence": "来自哪些对局或错误",
  "confidence": "置信度",
  "support_count": "支持样本数"
}
```

支持四类操作：

- `add`：发现新问题时新增 skill。
- `refine`：同类问题复发时升级旧 skill。
- `reinforce`：高质量经验重复出现时增强置信度。
- `retire_candidate`：低优先级或重复 skill 不直接删除，而是标记为退休候选。

这种设计的优势是：

- 比普通 memory 更结构化。
- 可追踪 parent skill 和版本历史。
- 更适合作为答辩中“Agent 自进化”的证据。
- 不硬编码固定动作，只提供决策工具，让 Agent 自主选择。

### 8.3 BM25 记忆召回

Memory Retriever 使用 BM25 + 标签 + 近期性 + 匹配词进行召回：

```text
当前角色/阶段/可见事件
  -> 构造 query
  -> 从 memory bank 候选经验中检索
  -> 按 BM25、标签匹配、近期性、状态过滤打分
  -> 注入最相关的记忆和 skill
```

这解决了两个问题：

- 不是把所有历史记忆塞进 Prompt，降低 token 消耗。
- 不让 Agent 只依赖最近一局，而是能召回相似情境下的经验。

### 8.3.1 记忆候选过滤

记忆召回入口在 `werewolf_ai/memory_retriever.py`。系统不会对所有 memory 全量打分，而是先做强过滤：

```text
只保留 status in {"approved", "approved_auto"} 的记忆
只保留 role == 当前角色 的记忆
过滤空 memory
过滤不符合当前规则的术语，例如警徽、警长、警徽流等未实现规则
```

这样可以避免两个问题：

- 不把未审核或低质量复盘直接注入 Agent。
- 不让模型因为预训练狼人杀知识提到当前规则中不存在的机制。

### 8.3.2 查询文本如何构造

每次 Agent 决策前，系统会根据当前局面构造一段结构化 query：

```json
{
  "role": "werewolf / seer / witch / hunter / villager",
  "role_zh": "狼人/预言家/女巫/猎人/村民",
  "phase": "day_speech / day_vote / night_wolf ...",
  "phase_zh": "白天发言/白天投票/夜间狼人行动 ...",
  "legal_actions": ["speak", "vote", "pass", "..."],
  "public_history_tail": "最近 10 条公开事件",
  "private_focus": {
    "inspections": "预言家查验结果",
    "attacked_tonight": "女巫可见的当夜被刀目标",
    "has_antidote": "是否还有解药",
    "has_poison": "是否还有毒药",
    "can_shoot": "猎人是否能开枪",
    "pack_plan": "狼人私有夜间计划",
    "public_claims": "公开身份声明"
  },
  "player_profile": {
    "persona": "座位人格",
    "speech_style": "发言风格",
    "risk_preference": "风险偏好",
    "vote_style": "投票风格",
    "anti_template_rule": "反模板约束"
  }
}
```

只放最近公开历史和与身份相关的私有字段，是为了兼顾上下文连续性和 token 成本。

### 8.3.3 分词与领域标签

由于狼人杀日志主要是中文，项目没有直接用英文空格分词，而是组合了几类 token：

- 英文、数字、下划线 token，例如 `P1`、`fake_seer`、`split_vote`。
- 中文单字 token。
- 中文相邻二字 token，用于保留“查杀”“金水”“归票”“倒钩”等短语信息。
- 领域 topic token，例如：

| topic | 触发词示例 |
|---|---|
| `seer_claim` | 预言家、查验、查杀、金水、对跳、悍跳 |
| `counterclaim` | 对跳、悍跳、假跳、不认、质疑、单预 |
| `vote` | 投票、归票、票型、弃票、分票、冲票 |
| `wolf_pack` | 狼队、队友、共边、协同、分线、倒钩 |
| `witch` | 女巫、毒药、解药、救人、银水 |
| `hunter` | 猎人、开枪、带走 |
| `confirmed_wolf` | 查杀、明狼、坐实 |
| `night_kill` | 夜晚、夜间、刀、落刀、神职 |
| `speech` | 发言、引用、证据、逻辑、模板 |

同时，不同阶段会自动补充阶段 topic：

| 阶段 | 自动补充 topic |
|---|---|
| 狼人夜间 | `wolf_pack`、`night_kill` |
| 预言家夜间 | `seer_claim` |
| 女巫夜间 | `witch` |
| 白天发言 | `speech`、`seer_claim`、`counterclaim` |
| 白天投票 | `vote`、`wolf_pack` |
| 猎人开枪 | `hunter`、`vote` |

### 8.3.4 BM25 主公式

项目使用标准 BM25 形式，参数为：

```text
K1 = 1.5
B  = 0.75
```

对每个候选记忆文档：

```text
idf(token) = log(1 + (N - df(token) + 0.5) / (df(token) + 0.5))

score(token) =
  idf(token) * (tf * (K1 + 1))
  / (tf + K1 * (1 - B + B * doc_len / avg_doc_len))

bm25 = sum(score(token) for token in unique(query_tokens))
```

其中：

- `N` 是候选记忆数量。
- `df(token)` 是包含该 token 的记忆数量。
- `tf` 是该 token 在当前记忆中的词频。
- `doc_len` 是当前记忆长度。
- `avg_doc_len` 是候选记忆平均长度。

### 8.3.5 最终召回分数

最终排序不是只看 BM25，而是综合多个信号：

```text
final_score =
  bm25
+ 0.7 * confidence
+ min(0.25, log1p(seen_count) * 0.06)
+ 0.3 * matched_topic_count
+ recency_bonus
- mechanical_memory_penalty
- no_match_penalty
```

各项含义：

| 项 | 作用 |
|---|---|
| `bm25` | 当前局面和记忆文本的词面相关性 |
| `confidence` | LLM 复盘或人工审查给出的置信度 |
| `seen_count` | 相同经验被多次观察到时增加权重 |
| `matched_topic_count` | 当前阶段 topic 与记忆 topic 匹配越多，越相关 |
| `recency_bonus` | 最近更新的经验略微加分，最高 0.2 |
| `mechanical_memory_penalty` | 机械化、容易诱导固定打法的记忆扣分 |
| `no_match_penalty` | 既没有词面匹配也没有 topic 匹配时扣 0.4 |

召回后还会做两层去重：

- 标准化文本完全相同的 memory 只保留一条。
- token Jaccard 相似度大于 0.78 的近重复记忆只保留一条。

默认最多召回 5 条 memory，避免 Prompt 被记忆淹没。

### 8.3.6 一个召回例子

如果当前是狼人白天投票阶段，公开历史中出现：

```text
P6 质疑 P8 划水；
P8 转火 P6；
狼队夜间计划里提到 P8 处于压力位；
当前可选动作为 vote/pass。
```

query 会包含：

```text
role=werewolf
phase=day_vote
topic:vote
topic:wolf_pack
公开历史中的 P6/P8/质疑/转火/票型
pack_plan
座位人格与投票风格
```

此时更容易召回类似：

```text
当队友因弃票、划水、转火或被公开质疑而处于压力位时，狼人不要全员复制该队友的转火目标；应比较直救、轻踩、倒钩、弃票、第三方票等路线，选择与自己公开发言兼容的方案。
```

这就是 BM25 的作用：它不规定狼人一定怎么做，但把相似情境下的经验放到模型面前，让模型自主比较。

### 8.3.7 Skill 召回与 Memory 召回的区别

项目中还有一套 `skill_usage.py`，用于结构化 skill card 的召回。它和 BM25 memory 召回不同：

| 项目 | Memory 召回 | Skill 召回 |
|---|---|---|
| 数据来源 | LLM 复盘、人工补写、memory bank | role memory 中的 `role_skills` |
| 形态 | 自然语言经验 | trigger/procedure/avoid/tags 结构化技能卡 |
| 召回方法 | BM25 + topic + confidence + recency | 词面 overlap + trigger match + confidence + support |
| 作用 | 提醒 Agent 相似局面经验 | 给 Agent 一个可执行的决策工具 |
| 是否强制使用 | 不强制 | 不强制 |

Skill 召回公式大致是：

```text
skill_score =
  lexical_overlap * 0.55
+ trigger_match   * 0.35
+ confidence      * 0.08
+ min(0.05, support_count * 0.01)
```

默认召回 3 张 skill card。召回后的 skill 会进入 Prompt，但最终是否采用仍由 LLM 决定。

### 8.3.8 Skill 使用统计如何计算

为了不干扰模型输出，项目没有要求 LLM 自报“我用了哪个 skill”。相反，系统在决策完成后用被动启发式估计：

- 读取本轮可用 skill。
- 把 Agent 的 action、target、speech、reason、metadata 拼成 decision text。
- 对每张 skill 做专门匹配和词面匹配。
- 如果匹配分大于 0，记录为可能使用。

例如：

- 狼人投票时出现“分票、倒钩、切割、公共逻辑、独立票型”等词，且 action 是 `vote/pass`，会匹配 `werewolf_exposed_pack_vote` 类 skill。
- 狼人发言中出现对跳、查验、金水、后续查验计划，会匹配 `werewolf_incomplete_fake_seer_claim` 类 skill。
- 女巫夜间提到解药价值、自救、保留关键神职，会匹配 `witch_mechanical_n1_save` 类 skill。

统计项包括：

```text
decisions
decisions_with_available_skills
decisions_with_matched_skills
matched_skill_uses
by_role.skills
```

因此前端和实验报告中看到的 skill usage 是“被动估计的使用痕迹”，不是强制模型输出的自我声明。这一点在答辩时可以强调：它不会污染 Agent 的动作格式，也不会强迫 Agent 为了统计而改变输出。

### 8.4 Evolution Strategist

项目增加了 LLM 进化规划器。它不直接晋级版本，而是提供下一轮演进假设：

- 哪个角色或失败模式最值得优化。
- 哪些 memory 或 skill 应该新增/修订。
- 哪些指标应该作为下一轮验证目标。

最终是否晋级仍由 AB 指标和 gate 规则决定，避免 LLM 自我肯定式循环。

### 8.5 Codex Skill Lab

最新阶段引入 Codex 审查式 skill 优化：

```text
Doubao/DeepSeek 负责真实对局采样
Codex 负责离线审查日志、定位失败模式、修改 skill、记录证据
冻结评测负责验证候选 skill 是否真的有效
```

这套方法目前主要针对 `werewolf_exposed_pack_vote` 进行迭代：

- r1：整理狼队投票分线 skill。
- r2：考虑同步投票下看不到队友本轮投票，需要推断同票风险。
- r3：增加跨日持久性，避免 D1 分线后 D2 又同票。
- r4：尝试处理被查杀队友救援与转火。
- r5：扩展到“公开压力位队友救援与转火分线”。

变更记录保存在：

```text
data/memory/skill_change_log.json
docs/Codex-Skill-Lab-版本变更记录.md
docs/Codex-Skill-Lab-方法有效性评估.md
```

## 9. 前端与人机对战

前端已经从简单回放演进为可操作界面：

- 实时流式对局，而不是先生成完整一局再播放。
- 玩家座位、身份、存活状态、票型、死亡原因可视化。
- Agent I/O 面板展示输出、理由、动作、置信度、耗时。
- 支持上帝视角和单 Agent 私有视角。
- 支持人机对战，真人玩家用打字形式参与发言、投票和技能。
- 支持后台演进任务、冻结评测任务和 SSE 进度同步。
- 支持查看复盘报告、Leaderboard、版本对比。

这对应评分中的“前端体验”和“人机交互”加分项。

## 10. 工程稳定性与可观测性

### 10.1 日志与归档

项目保留了完整数据资产：

- `logs/games/`：逐局完整归档，目前已有约 657 个 game/error 文件。
- `data/memory/memory_bank.json`：长期记忆库。
- `data/memory/role_memory_*.json`：角色策略/skill 版本。
- `data/memory/evolution_runs/`：演进 run、晋级记录和 Leaderboard。
- `data/memory/skill_change_log.json`：Codex skill 变更原因和验证证据。
- `logs/frozen_eval_smoke/`：冻结评测结果、compact summary、gate 和 review pack。

### 10.2 API 稳定性

针对真实大模型调用中的问题，项目已加入：

- timeout 配置。
- 最大重试次数。
- 429 限流等待。
- TPM/RPM 区分等待。
- 指数退避和 jitter。
- 失败局补跑。
- 后台任务逐局保存，避免中途中断丢数据。
- error artifact 落盘，避免失败实验静默消失。

### 10.3 冻结评测与 gate

冻结评测用于正式验证，不写入 memory，不更新 latest：

```text
candidate version vs baseline version
same seed alignment
same game count
write result / compact summary / gate / review pack
```

Gate 规则避免误晋级：

- 目标错误必须下降。
- overall 不能明显回退。
- mistakes/game 不能变差。
- vote/skill/deception 不应坍塌。
- 有 error 或 seed 不对齐时要求 rerun。
- 如果目标错误双方都没出现，判定为 `expand_sample`，不能直接晋级。

## 11. 实验效果

### 11.1 正式晋级版本效果

当前最新晋级版本：

```text
version_id: evolved_skill_r12_v2
base_version: evolved_skill_r10_v6
memory_file: data\memory\role_memory_evolved_skill_r12_v2.json
```

正式晋级记录中的指标：

| 指标 | 数值 |
|---|---:|
| games | 3 |
| overall | 83.18 |
| vote_quality | 72.87 |
| skill_quality | 79.19 |
| speech_quality | 86.11 |
| team_contribution | 73.80 |
| social_influence_quality | 77.83 |
| wolf_deception_quality | 96.67 |
| wolf_deception_diversity | 100.00 |
| mistakes/game | 0.67 |
| 胜负 | 狼人 2，村民 1 |

20260607 日志分析中，`evolved_skill_r12_v2` 相比 `evolved_skill_r10_v6`：

| 指标 | 变化 |
|---|---:|
| overall | +4.05 |
| vote_quality | +6.787 |
| skill_quality | +1.63 |
| wolf_deception_quality | +5.334 |
| mistakes/game | -0.833 |

这说明自进化版本在投票质量、狼人欺骗质量和平均失误率上有较明显改善。

### 11.2 r9 -> r10_v6 的早期演进效果

`evolved_skill_r10_v6` 相比 `evolved_skill_r9`：

| 指标 | r9 | r10_v6 | 变化 |
|---|---:|---:|---:|
| overall | 77.12 | 80.35 | +3.23 |
| vote_quality | 65.55 | 72.79 | +7.24 |
| mistakes/game | 2.00 | 1.00 | -1.00 |

该阶段说明 skill/memory 演进已经能够带来可观的行为质量改善。

### 11.3 Codex Skill Lab 的局部优化效果

针对 `werewolf_exposed_pack_vote` 的 skill-lab 迭代，有以下关键结果：

| 版本 | 对比 | 结果 | 结论 |
|---|---|---|---|
| r2 smoke | r2 vs `evolved_skill_r12_v2` | 目标错误 2 -> 1；overall +0.92；wolf deception +18.0；但 D2 仍复发 | 有正向信号，不晋级，继续 refine |
| r3 seed-aligned smoke | r3 vs baseline | 目标错误 2 -> 1；overall +14.05；vote +4.43；wolf deception +14.0 | 小样本强正向，需扩样 |
| r5 smoke | r5 vs r4 | overall -0.22；vote +8.91；目标错误 0 vs 0 | 方向有用，但目标场景未复现，只能 expand_sample |

当前 r5 的 3 局扩展评测仍在后台运行，尚未形成最终结论。因此报告中的稳妥表述应是：

```text
Codex Skill Lab 已经证明了“可审计 skill 进化流程”有效；
对狼队同票暴露问题已有初步改善信号；
但单个候选 skill 的统计显著性仍需更大冻结样本确认。
```

### 11.4 调用耗时

20260607 采样中，典型 LLM 决策耗时：

| 版本 | 决策平均耗时 | 中位数 | p95 |
|---|---:|---:|---:|
| evolved_skill_r10_v6 | 34.24s | 28.14s | 49.92s |
| evolved_skill_r12_v2 | 29.22s | 26.18s | 42.10s |

狼人夜间协调平均约 28-40 秒，复盘调用平均约 57-90 秒。该结果说明真实 LLM 模式下对局耗时较长，因此项目增加了后台任务、SSE 流式、逐局保存和低频状态检查。

## 12. 方法演进总结

项目中实际尝试并保留下来的主要方法包括：

1. **纯 LLM 角色 Agent**：删除规则策略 fallback，让模型行为成为真实评估对象。
2. **中文规则 Prompt**：显式写入 9 人局规则、动作空间和输出格式，减少预训练误用。
3. **信息隔离 Observation**：每个 Agent 只收到身份允许的信息。
4. **上下文拼接**：将公开历史、私有记忆、座位人格、belief state、memory 和 skill 拼接进每次无状态 API 调用。
5. **实时流式前端**：支持观战、人机、Agent I/O、耗时和版本信息展示。
6. **同步投票**：防止投票阶段因调用顺序产生跟票。
7. **平票 PK**：提升规则真实性。
8. **狼人夜间会议**：让狼队有私有协同计划，而不是独立随机行动。
9. **多模型切换**：支持 Doubao、DeepSeek Flash、DeepSeek V3.2 推理模式。
10. **LLM Reviewer**：局后上帝视角复盘，生成角色级经验。
11. **Memory Bank**：把复盘经验独立保存，避免候选版本未晋级时好记忆丢失。
12. **BM25 召回**：按当前场景召回相关经验，而不是全量注入。
13. **Skill Evolution**：把记忆转成可触发、可版本化的技能卡。
14. **Skill Usage 统计**：记录 skill 是否可用、是否匹配、是否被用到。
15. **Evolution Strategist**：用 LLM 为下一轮演进提供方向假设。
16. **AB / Frozen Eval**：用固定版本和固定种子做验证。
17. **Gate 晋级规则**：阻止只因单局胜负或 overall 波动而误晋级。
18. **Codex Skill Lab**：由 Codex 离线审查日志、提出最小 skill 改动、记录证据。
19. **Compact Summary / Review Pack**：减少读取大日志的 token 消耗。
20. **错误归档与重试**：处理 429、timeout、API 错误和中断续跑。

## 13. 剩余问题与改进方向

当前仍需优化的问题：

- 狼人欺骗能力仍有提升空间，尤其是悍跳、倒钩、软对跳和长期身份经营。
- 预言家和女巫仍可能出现机械化策略，如首日固定起跳、首夜固定救人。
- skill 的效果需要更多冻结评测样本确认，不能只依赖 1-3 局。
- API 429 受共享 endpoint 总 token 限制影响，需要继续控制并发和冷却。
- LLM 偶发幻觉仍存在，需要继续强化“引用公开事件”的约束和评测。
- 前端可以继续增强真实游戏氛围、动画和非技术观众理解度。

下一步最值得做的实验：

```text
1. latest promoted vs initial，10-20 局冻结评测，展示整体演进收益。
2. latest promoted vs no-skill ablation，5-10 局，证明 skill 本身贡献。
3. r5/r6 candidate vs parent，5-10 局，验证狼队同票暴露优化。
4. 人机对战体验测试，重点观察真人是否觉得 AI 狼人有真实博弈感。
```

## 14. 答辩建议

答辩时建议按以下顺序讲：

1. **为什么狼人杀适合 Agent Team**：多角色、信息不对称、协作与博弈并存。
2. **基础系统跑通**：9 人局、五角色、夜晚/白天/投票/胜负/日志/前端。
3. **信息隔离证明**：展示上帝视角和单 Agent 私有视角差异。
4. **Agent 决策展示**：展示每次 LLM 输入摘要、输出动作、理由、耗时。
5. **B 路线**：展示多维评测和 bad case 复盘，不只看胜负。
6. **C 路线**：展示 memory/skill 自进化、AB 评测、Leaderboard 和版本回溯。
7. **效果数据**：重点讲 r9 -> r10_v6、r10_v6 -> r12_v2 的指标提升。
8. **Codex Skill Lab**：作为亮点，说明如何低 token、可审计地优化 skill。
9. **边界与诚实性**：承认部分 skill 仍需扩样，但流程已经能阻止误晋级。

推荐一句话总结：

```text
我们实现的不只是一个能跑完狼人杀的 LLM Demo，而是一个可观测、可复盘、可记忆、可演进的多 Agent 博弈系统：Agent 在严格信息隔离下完成协作和欺骗，系统用多维评测发现 bad case，再把经验沉淀为 memory 与 skill，并通过冻结 A/B 评测验证版本是否真的变强。
```

## 15. 关键文件索引

| 类型 | 路径 |
|---|---|
| 运行说明 | `README.md` |
| 评分要求 | `评分.txt` |
| 主技术文档 | `docs/AI狼人杀技术文档.md` |
| 方法有效性评估 | `docs/Codex-Skill-Lab-方法有效性评估.md` |
| Skill 版本记录 | `docs/Codex-Skill-Lab-版本变更记录.md` |
| 实验记录 | `docs/Codex-Skill-Lab-实验记录.md` |
| 最新晋级指针 | `data/memory/latest_promoted.json` |
| 最新晋级角色记忆 | `data/memory/role_memory_evolved_skill_r12_v2.json` |
| 记忆库 | `data/memory/memory_bank.json` |
| Skill 变更日志 | `data/memory/skill_change_log.json` |
| 演进 run | `data/memory/evolution_runs/` |
| 对局日志 | `logs/games/` |
| 冻结评测 | `logs/frozen_eval_smoke/` |
| 前端 | `static/index.html`、`static/app.js`、`static/style.css` |
