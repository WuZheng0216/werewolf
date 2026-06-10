# Codex Skill Lab 实验记录

## 目标

本实验链路采用“双执行体”分工：

- 豆包模型负责真实对局采样，只在局内扮演 AI 狼人杀玩家。
- Codex 负责离线复盘、审查日志、修订 skill 候选版本，并用冻结评测验证效果。

这样可以避免让豆包同时承担“玩家”和“复盘优化器”两个职责，也能减少额外模型调用，把进化过程保留为可审计证据。

## 当前采样任务

- 模式：`codex_skill_lab`
- 采样版本：`evolved_skill_r12_v2`
- 目标局数：5
- 豆包提供方：`ark`
- 输出目录：`logs/codex_skill_lab/batch_20260609_001441`
- 进度文件：`logs/codex_skill_lab/batch_20260609_001441/20260609_001441_713560_progress.jsonl`
- 归档策略：每局结束后写入 `logs/games`
- 自动写入关闭：LLM 复盘、memory bank、strategy planning、role memory snapshot、latest promoted、auto skill evolution 均关闭

## 已观察到的证据

截至第一局完成，评测器确认主失败模式仍是狼人公开票型暴露：

- 完成局数：1
- 归档文件：`logs/games/20260609_003432_559731_game-20340521-02b356.json`
- 胜方：狼人
- 总分：72.91
- 投票质量：60.0
- 狼人欺骗质量：62.0
- 错误类型：
  - `werewolf_exposed_pack_vote` x 2

离线汇总脚本进一步抽取到：

- D1：三名狼人全部投 P3
- D2：三名狼人全部投 P5
- 相关 skill 命中情况：
  - 悍跳/对跳相关 skill 命中更频繁
  - `werewolf_skill_53608c4baf_v3`（狼队投票分线）命中偏少

进一步检查第一局归档后，投票阶段的直接证据如下：

| 回合 | 狼人 | 投票 | 是否命中 `狼队投票分线` | 主要理由摘要 |
| --- | --- | --- | --- | --- |
| D1 | P7 | P3 | 否 | P3/P4/P5 认民发言同质化 |
| D1 | P8 | P3 | 否 | P3 首发同质化认民发言 |
| D1 | P9 | P3 | 是 | P3/P4/P5 发言同质化，P3 是首发 |
| D2 | P7 | P5 | 否 | P5 质疑 4 人抱团不符合 3 狼规则 |
| D2 | P8 | P5 | 是 | P5 质疑 4 人抱团，且 P3/P4/P5 只剩 P5 |
| D2 | P9 | P5 | 否 | P5 逻辑矛盾，且狼无理由留 P5 |
 
这说明问题不是单一的“无 skill 可用”，而是：

- 召回不稳定：同一投票场景下，只有部分狼人命中投票分线 skill。
- 执行强度不足：即使命中，仍可能选择与队友同目标、同叙事投票。
- 叙事同质化：多个狼人都围绕“发言同质化/逻辑矛盾”进行类似攻击，公开票型和公开理由一起暴露协同。

初步判断：问题不是“缺少投票分线 skill”，而是“投票前暴露检查的召回和执行不稳定”。因此优化不应硬编码狼人必须分票，而应强化一个自主决策工具：投票前比较放逐收益和狼队关系暴露成本。

## 候选 skill 版本

- 候选文件：`data/memory/role_memory_codex_skill_pack_vote_r1.json`
- 父版本：`evolved_skill_r12_v2`
- 状态：未晋级，不覆盖 `latest_promoted.json`

核心改动：

- 将多个重复的“狼队投票分线”类 skill 收敛为一个主 skill。
- 新主 skill：`werewolf_skill_53608c4baf_v4_codex`
- 新触发：白天投票前，如果自己的投票可能造成两名或更多已知狼队友用相同公开叙事集中投同一个非狼目标，先启动投票暴露检查。
- 关键约束：
  - 不强制分票。
  - 强公开证据足够时仍可协同压票。
  - 证据中等或偏弱时，至少一名狼人考虑侧线选择。
  - 所有理由只能来自公开事件，不暴露狼队协同，不编造不存在事件。

## 对照版本

新增 skill ablation 对照版本：

- 文件：`data/memory/role_memory_skill_ablation_latest_no_skills.json`
- 版本：`skill_ablation_latest_no_skills`
- 父版本：`evolved_skill_r12_v2`
- 作用：保留基础 profile，但禁用 role skills，用于证明 skill 对对局质量是否有真实贡献。

后续可运行：

```powershell
python scripts/run_demo.py frozen-eval --version latest --baseline-version skill_ablation_latest_no_skills --games 5 --llm-provider ark
```

## 新增工具

新增离线汇总脚本：

```powershell
python scripts/summarize_codex_skill_lab.py logs/codex_skill_lab/batch_20260609_001441 --json
```

用途：

- 查看 batch 是否完成、失败或仍在运行。
- 统计已完成局数、事件数、最后阶段。
- 统计 LLM 决策耗时。
- 按局和天抽取狼人白天投票是否同目标。
- 统计狼人命中的 skill。

该脚本不调用任何模型，可以在实验中断、限流或后台运行时使用。

新增结构化 skill 变更记录：

```text
data/memory/skill_change_log.json
```

记录内容包括：

- `version_id`：候选 skill 版本。
- `parent_id`：父版本。
- `target_failure_pattern`：本次只针对哪一个主要失败模式。
- `rationale`：为什么要这样改。
- `evidence`：来自哪些对局、分数、错误类型和关键观察。
- `changes`：具体改了哪些 skill、怎么改、为什么这样改。
- `validation`：加载检查、单元测试、冻结评测等验证结果。
- `next_steps`：下一步是否继续评测、修订或晋级。

查看记录：

```powershell
python scripts/record_skill_change.py --show
```

追加记录：

```powershell
python scripts/record_skill_change.py --input path/to/change_entry.json
```

当前已写入记录：

- `codex_skill_pack_vote_r1_change_1`
- 目标失败模式：`werewolf_exposed_pack_vote`
- 父版本：`evolved_skill_r12_v2`
- 候选版本：`codex_skill_pack_vote_r1`
- 状态：`pending_frozen_eval`
- `codex_skill_pack_vote_r2_change_1`
- 目标失败模式：`werewolf_exposed_pack_vote`
- 父版本：`codex_skill_pack_vote_r1`
- 候选版本：`codex_skill_pack_vote_r2`
- 状态：`pending_frozen_eval`

## 当前 20260609 采样汇总

截至当前后台采样已完成：

- 版本：`evolved_skill_r12_v2`
- 完成局种子：20340521、20340522、20340523、20340524、20340526
- 失败重试种子：20340525，因 Ark 端点 TPM 限流失败，已自动换 seed 20340526 重试成功
- 胜方：狼人 5 局
- 平均总分：76.56
- 平均投票质量：62.80
- 平均狼人欺骗质量：82.40
- 总错误数：9
- 平均错误数：1.80 / 完成局
- 错误类型：`werewolf_exposed_pack_vote` x 7，`werewolf_template_speech` x 1，`seer_mechanical_day1_claim` x 1
- 产物：`logs/codex_skill_lab/batch_20260609_001441/20260609_001441_713560_codex_review_pack.json`

单局对照：

- seed 20340521：总分 72.91，`werewolf_exposed_pack_vote` x 2，D1/D2 均出现三狼同投。
- seed 20340522：总分 84.23，无关键错误，D1/D2 狼人投票更分散，是一个有价值的正向对照样本。
- seed 20340523：总分 73.98，`werewolf_exposed_pack_vote` x 2，`werewolf_template_speech` x 1；D1/D2/D3 均出现三狼同投，且部分狼人命中旧投票分线 skill 后仍未改变同票行为。
- seed 20340524：总分 79.36，`werewolf_exposed_pack_vote` x 1；D1 狼人票型分散且旧 skill 均被召回，D2 剩余狼人 P3/P9 同投 P4 被评测器标记，说明旧 skill 有一定作用但后续轮次约束不足。
- seed 20340526：总分 72.30，`werewolf_exposed_pack_vote` x 2，`seer_mechanical_day1_claim` x 1；D1 P2/P6 同投 P5，D2 P2/P6/P9 三狼同投 P7。D2 三个狼人都命中旧 `werewolf_skill_53608c4baf_v3`，但仍未避免同目标压票。

基于第 3 局新增 r2 候选：

- 文件：`data/memory/role_memory_codex_skill_pack_vote_r2.json`
- 父版本：`codex_skill_pack_vote_r1`
- 主 skill：`werewolf_skill_53608c4baf_v5_codex`
- 改动原因：投票是同时发生的，狼人看不到本轮队友真实投票，因此 skill 不能只依赖“发现队友已同投”；需要基于狼队日间计划、公开发言承诺、历史票型和自己的即将投票来推测是否会形成同目标暴露。
- 保留原则：不强制分票，只让 Agent 在行动前比较放逐收益和狼队暴露成本。

运行中异常：

- 第 3 局出现一次超长 LLM 调用，P8 D1 白天发言耗时约 13,116,816ms。
- 判断为 endpoint 排队/限流等待；采样期间不并发启动冻结评测。
- 第 5 局第 4 次尝试出现 Ark `429 RateLimitExceeded.EndpointTPMExceeded`，属于共享端点每分钟 token 额度超限，不是对局逻辑错误。
- 任务已自动重试成功，完成局以 seed 20340526 计入正式统计；seed 20340525 的 error archive 只作为运行异常留痕。
- 后续 frozen eval 先从 `games=1` smoke test 开始。

## 已通过测试

```powershell
python -m py_compile scripts\summarize_codex_skill_lab.py scripts\run_codex_skill_lab_job.py scripts\run_frozen_eval_job.py
python -m unittest discover -s tests -p test_codex_skill_lab_summary.py -v
python -m unittest discover -s tests -p test_codex_skill_lab.py -v
python -m unittest discover -s tests -p test_skill_change_log.py -v
python -m unittest discover -s tests -p test_run_frozen_eval_job.py -v
```

## 下一步冻结评测

当前豆包采样已完成。先做小规模 smoke test：

```powershell
python scripts/run_frozen_eval_job.py --version codex_skill_pack_vote_r2 --baseline-version evolved_skill_r12_v2 --games 1 --llm-provider ark --output-dir logs/frozen_eval_smoke/r2_vs_baseline --internal-llm-retries 1
```

当前已经挂起一个 watcher：等待采样进程 `20952` 结束后，先额外等待 180 秒，再自动启动同样的 `codex_skill_pack_vote_r2` vs `evolved_skill_r12_v2` 小规模冻结测评。这样做是为了降低采样刚结束时继续触发共享端点 TPM 限流的概率。产物目录：

```text
logs/frozen_eval_smoke/r2_after_batch_20260609_001441
```

该 watcher 也已写入 `data/memory/skill_change_log.json` 的 `codex_skill_pack_vote_r2_change_1.validation`，用于说明候选版本何时、为何、如何进入验证阶段。

若 smoke test 能稳定完成，再扩大到：

```powershell
python scripts/run_frozen_eval_job.py --version codex_skill_pack_vote_r2 --baseline-version evolved_skill_r12_v2 --games 5 --llm-provider ark --output-dir logs/frozen_eval_smoke/r2_vs_baseline --internal-llm-retries 1
```

接受候选的标准：

- `werewolf_exposed_pack_vote` 明显下降。
- 总分不发生明显回退。
- 投票质量、狼人欺骗质量和团队贡献不塌。
- 日志中能看到 `werewolf_skill_53608c4baf_v5_codex` 被召回，并且狼人投票理由更加分散、公开证据更兼容。
