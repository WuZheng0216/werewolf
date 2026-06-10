# Codex-Skill-Lab 方法有效性评估

更新时间：2026-06-10

## 结论摘要

当前这套“Codex 审查日志 -> 修改 skill -> 冻结评测 -> gate 判定 -> 记录版本原因”的方法是有效的，但要分两层看：

- **方法论有效**：已经形成了可审计、可回滚、可对照的自进化流程。每次 skill 改动都有失败样本、改动原因、候选版本、冻结评测、gate 结论和变更记录。
- **策略效果有初步信号**：针对 `werewolf_exposed_pack_vote`，若干候选版本在小样本中降低了狼队同票暴露，并提升了部分投票质量/欺骗质量指标。
- **还不能说已统计显著提升**：目前 r5 只有 1 局 smoke 结果，目标错误在候选和基线里都没有复现，因此 gate 正确给出 `expand_sample`，不能直接晋级。
- **当前最稳的判断**：这套方案已经适合作为答辩里的“自进化工程闭环”和“评测驱动优化证据”，但还需要等待 r5 的 3 局扩展评测，并最好再补 5-10 局冻结评测，才能把“策略显著提升”说得更硬。

## 方法流程

这套方法不是让 Agent 被硬编码成固定打法，而是让 Codex 基于对局证据维护一组可召回的 role skills：

```text
对局日志/评测结果
  -> Codex 只读取 compact summary / gate / review pack
  -> 定位一个具体失败模式
  -> 修改或新增候选 skill 版本
  -> 运行冻结 A/B 评测
  -> gate 判断 promote / expand_sample / revise_skill / rerun_required
  -> 写入 skill_change_log
```

它的核心价值是：每次进化都有“为什么改、改了什么、是否有效、为什么没有晋级”的证据链，而不是凭感觉手调 Prompt。

## 已有证据

| 阶段 | 对比 | 关键结果 | 判断 |
|---|---|---|---|
| r2 smoke | `codex_skill_pack_vote_r2` vs `evolved_skill_r12_v2` | `werewolf_exposed_pack_vote` 从 2 降到 1；overall +0.92；wolf deception +18.0；vote quality -3.43 | 有正向信号，但 D2 仍复发，所以不晋级，继续修成 r3 |
| r3 seed-aligned smoke | `codex_skill_pack_vote_r3` vs `evolved_skill_r12_v2` | `werewolf_exposed_pack_vote` 从 2 降到 1；overall +14.05；vote quality +4.43；wolf deception +14.0 | 小样本明显变好，但仍有目标错误，适合扩样 |
| r3/r4 后续 | 候选扩展与 DashScope smoke | 出现目标错误复发、timeout、评测链路不稳定等情况 | 说明 gate 必须保守，不能只看 overall 或胜负 |
| r5 smoke | `codex_skill_pack_vote_r5` vs `codex_skill_pack_vote_r4` | completed 1/1；双方狼胜；overall -0.22；vote quality +8.91；wolf deception 持平；`werewolf_exposed_pack_vote` 为 0 vs 0 | 方向有用，但目标场景双方都没复现，所以只允许扩样，不允许晋级 |

r5 的 gate 结论是：

```text
recommendation: expand_sample
target_absent_both: true
target_mistake_delta: 0
overall_delta: -0.22
mistakes_per_game_delta: 0.0
vote_quality_delta: +8.91
wolf_deception_quality_delta: 0.0
```

这说明 r5 没有明显破坏整体表现，投票质量还有提升；但因为目标错误没有在任何一侧出现，它不能证明 r5 解决了目标问题。这个判断是合理的，也体现了 gate 的保守性。

## 为什么说方法有效

第一，它已经能把“观察到的问题”转成“可测试的候选 skill”。例如狼队同票暴露不是直接写死“狼人必须分票”，而是转成：

- 发言和投票前检查公开压力位；
- 评估队友是否因为弃票、可疑票型、自辩或被点名而处于压力下；
- 在直救、轻踩、倒钩、弃票、第三方票、延迟站边之间自主比较；
- 只能使用公开历史，不能泄露狼队私有信息，也不能编造不存在的事件。

第二，它能主动拒绝不可靠结论。r5 smoke 虽然有 vote quality +8.91 的好信号，但因为 `werewolf_exposed_pack_vote` 双方都没有复现，所以没有直接晋级。这比“赢了就说变强”更适合比赛答辩。

第三，工程链路已经能支撑长期实验：

- 候选版本不会覆盖 promoted/latest；
- `skill_change_log.json` 记录每次变更原因；
- frozen eval 产出 compact summary、gate、review pack；
- finalizer 能把成功或失败结果写回记录；
- status checker 可以只看元数据，不反复读取大日志，降低 token 消耗；
- timeout / 429 / error artifact 不会静默丢失。

## 目前不足

这套方法目前最大的不足不是“没效果”，而是“证据还不够厚”：

- 样本量仍偏小，很多结论来自 1 局 smoke。
- 狼人杀随机性强，单局会被身份分布、首夜刀人、预言家查验、女巫用药等因素强烈影响。
- 目标错误可能不复现，导致候选 skill 看起来不错，但无法证明解决了目标问题。
- 目前 skill 主要针对一个失败模式逐步优化，尚未覆盖更广的欺骗策略、悍跳链、倒钩策略和夜间协同质量。

因此，对外表述应当是：

```text
我们已经实现了可审计的自进化闭环，并在狼队同票暴露问题上观察到初步改进信号；
后续通过冻结评测扩样来决定是否晋级 skill 版本，而不是人工主观认定变强。
```

## 当前运行中的验证

当前正在运行 r5 的 3 局扩展评测：

```text
candidate_version: codex_skill_pack_vote_r5
baseline_version: codex_skill_pack_vote_r4
games: 3
llm_provider: dashscope
llm_model: deepseek-v4-flash
output_dir: logs\frozen_eval_smoke\r5_vs_r4_dashscope_flash_expand_20260610_091500
state: running_process_alive
```

本轮截至检查时尚未产出 result / compact summary / gate / review pack / error，仅有 progress 文件在持续更新。为节省 token，不应读取完整 progress。完成后只需要看以下小产物：

```text
*_frozen_eval_result_compact_summary.json
*_frozen_eval_gate.json
*_frozen_eval_result_skill_review_pack.json
*_error.json
```

检查命令：

```powershell
python scripts\check_frozen_eval_status.py logs\frozen_eval_smoke\r5_vs_r4_dashscope_flash_expand_20260610_091500 --pid 9088 --pid 12360 --pid 27976 --expected-duration-seconds 7200 --max-runtime-seconds 9000
```

## 下一步判定标准

r5 扩展评测完成后，按下面规则处理：

| gate 结果 | 处理 |
|---|---|
| `promote_after_codex_review` | Codex 复核 review pack，确认没有副作用后可以考虑晋级 |
| `expand_sample` | 不改 skill，继续扩到 5-10 局 |
| `revise_skill` | 只阅读 review pack 中的目标失败片段，做下一版 r6 |
| `rerun_required` | 优先处理 timeout、429、seed alignment、error artifact，不急着改策略 |

更适合答辩的最终实验是：

```text
latest promoted vs no-skill ablation
latest promoted vs initial
r5/r6 candidate vs parent version
```

其中 `latest promoted vs no-skill ablation` 最能证明 skill 本身的贡献；`latest promoted vs initial` 最适合展示完整自进化收益；`candidate vs parent` 最适合证明一次具体优化是否有效。

## 可引用证据文件

```text
data\memory\skill_change_log.json
data\memory\role_memory_codex_skill_pack_vote_r5.json
logs\frozen_eval_smoke\r5_vs_r4_dashscope_flash_20260609_182200\20260609_182743_980188_frozen_eval_result_compact_summary.json
logs\frozen_eval_smoke\r5_vs_r4_dashscope_flash_20260609_182200\20260609_182743_980188_frozen_eval_gate.json
logs\frozen_eval_smoke\r5_vs_r4_dashscope_flash_20260609_182200\20260609_182743_980188_frozen_eval_result_skill_review_pack.json
logs\frozen_eval_smoke\r4_vs_r3_dashscope_flash_20260609_174816\20260609_174818_818984_error.json
logs\frozen_eval_smoke\r4_vs_r3_dashscope_flash_20260609_174816\20260609_174818_818984_partial_candidate_skill_review_pack.json
```

## 答辩表达建议

可以这样讲：

```text
我们的自进化不是直接让模型随意改 Prompt，而是把每次对局中的失败模式结构化为 skill 更新假设。
每个候选 skill 都必须通过冻结 A/B 评测和 gate 检查，只有目标错误下降且整体质量不坍塌时才考虑晋级。
目前这套流程已经能稳定记录版本、定位失败、生成候选、保留失败原因，并在狼队同票暴露问题上看到初步正向信号。
我们没有把 1 局胜负当作结论，而是保留了 expand_sample / revise_skill / rerun_required 等保守路径，保证演进过程可审计。
```

