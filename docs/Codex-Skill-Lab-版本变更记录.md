# Codex Skill Lab 版本变更记录

本文档记录由 Codex 基于真实对局日志审查后生成的 skill 候选版本。原则是：豆包负责真实对局采样，Codex 只根据归档日志、评测指标和冻结测评结果修改 skill，并保留每次修改的原因和验证状态。

结构化记录文件：`data/memory/skill_change_log.json`

## 当前主线

目标失败模式：`werewolf_exposed_pack_vote`

含义：狼人白天投票时多名狼人在同一轮、同一目标、相似理由上形成明显抱团，导致好人或评测器能够识别狼队队形。

当前候选链路：

```text
evolved_skill_r12_v2
-> codex_skill_pack_vote_r1
-> codex_skill_pack_vote_r2
-> codex_skill_pack_vote_r3
```

当前结论：

- `r1`：候选版本，主要强化投票前暴露检查。
- `r2`：冻结烟测显示有局部改善，但 D2/D3 仍会复发，不晋级。
- `r3`：候选版本，强化跨日重复检查，正在等待冻结烟测结果。

## r1：狼队投票分线

候选文件：`data/memory/role_memory_codex_skill_pack_vote_r1.json`

父版本：`evolved_skill_r12_v2`

核心改动：

- 将狼人投票分线 skill 的触发条件改为“投票前暴露检查”。
- 明确区分强公开证据和弱怀疑证据。
- 允许狼人自主选择协同、分票、倒钩、弃票或轻压，不硬编码固定行动。
- 要求发言和投票理由必须兼容公开历史，不能暴露狼队私有信息。

为什么这样改：

采样日志显示，旧版本经常出现三狼同投同目标，而且即使部分狼人召回了旧 skill，也没有稳定改变投票行为。r1 的目标是让 skill 从“泛化建议”变成“投票前可执行的检查清单”。

## r2：适配并行投票机制

候选文件：`data/memory/role_memory_codex_skill_pack_vote_r2.json`

父版本：`codex_skill_pack_vote_r1`

核心改动：

- 把投票机制明确视为并行决策：狼人不能假装已经看见队友本轮真实投票。
- 要求根据狼队日间计划、公开发言承诺、历史票型和自己的即将投票推测同票风险。
- 继续保留自主性：只做风险比较，不规定必须分票。

为什么这样改：

r1 仍然隐含了“看到队友已经同投后再调整”的逻辑，但项目中的投票是并行发生的。r2 把 skill 改成“预测队友票向风险”，更符合真实调用机制。

冻结烟测结果：

```text
candidate: codex_skill_pack_vote_r2
baseline: evolved_skill_r12_v2
games: 1
seed: 20320521
```

主要指标：

- overall：75.92 vs 75.00，候选 +0.92
- mistakes_per_game：2.0 vs 3.0，候选 -1.0
- werewolf_exposed_pack_vote：1 vs 2，候选减少 1 次
- wolf_deception_quality：92.0 vs 74.0，候选 +18.0
- vote_quality：48.57 vs 52.0，候选下降

结论：

r2 是局部有效版本。它在 D1 避免了基线版本的三狼同票，但 D2 三狼仍然同时投向 P4，因此不能晋级，只能作为 r3 的父版本继续修订。

## r3：狼队跨日投票分线

候选文件：`data/memory/role_memory_codex_skill_pack_vote_r3.json`

父版本：`codex_skill_pack_vote_r2`

核心 skill：`werewolf_skill_53608c4baf_v6_codex`

核心改动：

- 触发条件从“投票前检查”扩展为“每一次白天投票前都重新检查”，包括 D1、D2、D3 和 PK 二次投票。
- 增加跨日重复风险：上一轮已经出现同目标或同叙事，本轮继续同目标默认暴露成本更高。
- 增加“目标有嫌疑”和“多狼同压是否安全”的分离判断。
- 明确弱证据不足以支撑多狼集火，强公开证据才可以考虑协同压票。
- 要求不同狼人即便投同一目标，也应使用不同公开证据链和座位风格。

为什么这样改：

r2 的烟测已经证明 D1 recall 和初始行为有改善，但 D2/D3 仍会复发，说明问题不是 skill 不可用，而是 skill 没有在后续轮次持续生效。r3 专门强化“跨日持续检查”和“重复叙事风险”，目标是解决后续轮次重新抱团的问题。

当前验证状态：

```text
status: candidate
decision: pending_seed_aligned_retest
output_dir: logs/frozen_eval_smoke/r3_vs_baseline_20260609
```

计划命令：

```powershell
python scripts/run_frozen_eval_job.py --version codex_skill_pack_vote_r3 --baseline-version evolved_skill_r12_v2 --games 1 --llm-provider ark --output-dir logs/frozen_eval_smoke/r3_vs_baseline_20260609 --internal-llm-retries 1
```

接受标准：

- `werewolf_exposed_pack_vote` 低于基线。
- 总分不出现明显回退。
- 投票质量不能因为过度分票而崩掉。
- 日志中能看到 r3 skill 被召回，并且狼人投票理由更分散、更兼容公开证据。

首次烟测结果：

```text
candidate: codex_skill_pack_vote_r3
baseline: evolved_skill_r12_v2
candidate completed seed: 20320522
baseline completed seed: 20320521
```

该次烟测不作为严格晋级证据，因为 candidate 第一次尝试在 seed `20320521` 的 D3 投票阶段触发 Ark `EndpointTPMExceeded`，随后自动换到 seed `20320522` 补跑；baseline 仍使用 seed `20320521`，导致双方不是同一局面。

观察指标：

- candidate overall：78.51
- baseline overall：85.72
- candidate mistakes_per_game：3.0
- baseline mistakes_per_game：1.0
- candidate `werewolf_exposed_pack_vote`：1
- baseline `werewolf_exposed_pack_vote`：0

结论：

r3 在这次非严格对比中不满足晋级标准；同时暴露出冻结测评在 candidate 失败补跑后没有对齐 baseline seed 的实验设计问题。代码已修复：后续 `run_frozen_eval` 会让 baseline 使用 candidate 实际完成的 seeds，从而避免 429 补跑造成 A/B 对比漂移。

seed-aligned 重测计划：

```text
watcher_pid: 8860
cooldown_seconds: 300
output_dir: logs/frozen_eval_smoke/r3_seed_aligned_20260609
```

这次重测会在代码修复后运行，预期结果文件中应包含 `seed_alignment` 字段，且 baseline completed seeds 应与 candidate completed seeds 一致。

seed-aligned 重测结果：

```text
result_path: logs/frozen_eval_smoke/r3_seed_aligned_20260609/20260609_074317_824810_frozen_eval_result.json
seed_alignment: candidate 20320521 == baseline 20320521
candidate overall: 79.39
baseline overall: 65.34
delta overall: +14.05
candidate mistakes_per_game: 2.0
baseline mistakes_per_game: 10.0
candidate werewolf_exposed_pack_vote: 1
baseline werewolf_exposed_pack_vote: 2
candidate vote_quality: 52.00
baseline vote_quality: 47.57
candidate wolf_deception_quality: 100.0
baseline wolf_deception_quality: 86.0
```

结论：

r3 在同 seed 小样本上明显优于基线，目标错误 `werewolf_exposed_pack_vote` 从 2 次降到 1 次，总分和狼人欺骗质量也有提升。但候选版仍然存在 1 次狼队抱团票，而且样本只有 1 局，因此不能晋级，只能记为 `partial_pass_expand_sample`。下一步应在 API 冷却后跑低频 `games=3-5` 的 seed-aligned frozen eval，并优先读取结果 JSON 的 aggregate，而不是反复 tail 完整对局日志。

扩样本任务：

```text
status: running
games: 3
watcher_pid: 20252
python_pid: 3324
cooldown_seconds: 600
output_dir: logs/frozen_eval_smoke/r3_seed_aligned_expand_20260609
```

该任务用于验证 r3 的小样本优势是否稳定。运行期间不轮询完整 `progress.jsonl`；等批次结束后只先读取 `*_frozen_eval_result.json` 的 aggregate、seed_alignment、errors 和 archives。

为进一步降低复盘上下文消耗，已新增 compact summary 工具：

```powershell
python scripts/summarize_frozen_eval_result.py logs/frozen_eval_smoke/r3_seed_aligned_expand_20260609
```

当前已挂起 summary watcher：

```text
watcher_pid: 5020
waits_for_pid: 3324
```

它会等待 3 局冻结测评进程结束后，自动从最终 `*_frozen_eval_result.json` 生成 `*_compact_summary.json`。之后优先审查这个小摘要文件。

工具验证：

```powershell
python -m py_compile scripts\summarize_frozen_eval_result.py
python -m unittest discover -s tests -p "test_summarize_frozen_eval_result.py" -v
```

该测试覆盖 compact summary 的关键字段提取、推荐状态生成，以及从目录中选择最新 `*_frozen_eval_result.json` 的行为。

已新增自动写回工具：

```powershell
python scripts/record_frozen_eval_summary.py --change-id codex_skill_pack_vote_r3_change_1 --summary logs/frozen_eval_smoke/r3_seed_aligned_expand_20260609
```

该工具只读取 `*_compact_summary.json`，把 aggregate 指标、seed alignment、errors、archives 和推荐状态写回 `skill_change_log.json`，不会读取 `progress.jsonl` 或完整单局归档，也不会自动晋级版本。

已改进 `scripts/run_frozen_eval_job.py`：后续每个冻结测评 job 完成后，会自动从最终 `*_frozen_eval_result.json` 生成同名 `*_compact_summary.json`。这意味着新的 frozen eval 不再需要额外挂 summary watcher；当前正在运行的 3 局任务启动早于此改动，仍由 PID `5020` 的 watcher 负责补生成摘要。

当前已挂起 record watcher：

```text
watcher_pid: 13096
waits_for_pid: 5020
```

链路为：

```text
3-game frozen eval PID 3324
-> summary watcher PID 5020 生成 compact summary
-> record watcher PID 13096 写回 skill_change_log
```

低 token 复盘约束：

- 不在对局运行中频繁 tail 大型 `progress.jsonl` 或完整 `logs/games/*.json`。
- 等一局或一批结束后，先读 `*_frozen_eval_result.json`、`*_codex_review_pack.json`、aggregate 指标。
- 只有当 aggregate 结论无法解释时，才打开对应单局归档，并优先按事件类型抽取票型、mistake、matched skill 等小片段。
- 每次 skill 修改只绑定一个主要失败模式，避免一次复盘消耗太多上下文。

## 后续实验建议

1. 先等待 r3 `games=1` 烟测完成。
2. 先重跑 r3 seed-aligned `games=1` 烟测。
3. 如果 r3 比 r2 更稳，再扩展到 `games=5`。
4. 如果 r3 仍然 D2/D3 抱团，下一版不要继续加规则，而应检查 skill 召回质量和狼人夜间协同计划是否过强绑定同一白天目标。
5. 只有冻结测评稳定优于 `evolved_skill_r12_v2` 后，才考虑晋级或更新 `latest_promoted.json`。

## 运行稳定性记录

2026-06-09 的 r3 烟测中，candidate 侧第一次尝试在 D3 投票阶段触发 Ark `EndpointTPMExceeded`，属于共享 endpoint 的每分钟 token 限流，不是候选 skill 的逻辑错误。任务已自动归档错误局并换 seed 重试。

为降低后续持续实验的失败率，已将 `.env` 中的等待参数调得更保守：

- `LLM_TPM_RATE_LIMIT_RETRY_SECONDS`: 90 -> 150
- `EVOLUTION_RATE_LIMIT_WAIT_SECONDS`: 90 -> 150
- `EVOLUTION_GAME_COOLDOWN_SECONDS`: 20 -> 60

这项调整只影响新启动的实验进程，不会中断或修改已经运行中的 r3 watcher。

## Low-Token Status Check

为避免在实验运行中反复读取大型 `progress.jsonl` 或完整单局归档，新增只读文件元数据的状态检查工具：

```powershell
python scripts/check_frozen_eval_status.py logs/frozen_eval_smoke/r3_seed_aligned_expand_20260609
python scripts/check_frozen_eval_status.py logs/frozen_eval_smoke/r3_seed_aligned_expand_20260609 --json
```

该工具只统计：

- `*_frozen_eval_result.json` 是否出现；
- `*_compact_summary.json` 是否出现；
- `*_progress.jsonl` 的数量、大小和最近写入时间；
- 输出目录中最新文件的大小和更新时间。

它不会读取 `progress.jsonl` 正文，也不会打开完整 `logs/games/*.json`。后续持续实验时，运行中只用这个脚本看状态；等一局或一批结束后，再优先读取 compact summary 做集中复盘。

验证命令：

```powershell
python -m py_compile scripts\check_frozen_eval_status.py
python -m unittest discover -s tests -p "test_check_frozen_eval_status.py" -v
```

## Compact Summary Gate

新增冻结测评候选版本 gate：

```powershell
python scripts/gate_frozen_eval_summary.py logs/frozen_eval_smoke/r3_seed_aligned_expand_20260609
```

它只读取 `*_compact_summary.json`，根据以下保守条件给出建议：

- seed 是否对齐；
- candidate / baseline 是否都有足够局数；
- 目标 mistake（默认 `werewolf_exposed_pack_vote`）是否下降；
- overall 是否未明显回退；
- mistakes per game 是否未变差；
- vote quality / wolf deception quality 是否未崩塌；
- 测评是否无错误。

输出建议分为：

- `rerun_required`
- `revise_skill`
- `tradeoff_review_required`
- `expand_sample`
- `promote_after_codex_review`

验证命令：

```powershell
python -m py_compile scripts\gate_frozen_eval_summary.py tests\test_gate_frozen_eval_summary.py
python -m unittest discover -s tests -p "test_gate_frozen_eval_summary.py" -v
```

`record_frozen_eval_summary.py` 已接入该 gate。之后自动写回 `skill_change_log.json` 时，会一并保存：

- `gate_recommendation`
- `gate_checks`
- `gate_metrics`
- `gate_why`

这样当前长跑批次结束后，日志会同时包含 aggregate 指标和保守晋级建议，不需要再打开大日志做第一轮判断。

后续新启动的 `run_frozen_eval_job.py` 会自动生成三类小产物：

```text
*_frozen_eval_result.json
*_frozen_eval_result_compact_summary.json
*_frozen_eval_gate.json
```

其中 gate 文件是 compact summary 的保守判定结果。`check_frozen_eval_status.py` 也已支持只用元数据统计 gate 文件数量；因此新的复盘顺序固定为：

```text
status metadata -> compact summary -> gate -> only then inspect selected game archives if needed
```

Watcher stdout/stderr logs are also reported by metadata only. Do not open their bodies during a running batch unless the compact result path is missing after the process exits.

## Frozen Eval Finalizer

新增一键收口脚本：

```powershell
python scripts/finalize_frozen_eval_artifacts.py logs\frozen_eval_smoke\r3_seed_aligned_expand_20260609 --change-id codex_skill_pack_vote_r3_change_1
```

行为：

- 如果目录中还没有 `*_frozen_eval_result.json`，返回 `not_ready`，不读取 `progress.jsonl`；
- 如果 result 已出现但 compact summary 缺失，生成 compact summary；
- 如果 gate 缺失，生成 `*_frozen_eval_gate.json`；
- 如果传入 `--change-id`，把 summary + gate decision 写回 `skill_change_log.json`。

当前扩样目录测试结果：

```text
status: not_ready
reason: No *_frozen_eval_result.json found.
```

## Low-Token PID Probe

`check_frozen_eval_status.py` supports optional PID probes:

```powershell
python scripts/check_frozen_eval_status.py logs\frozen_eval_smoke\r3_seed_aligned_expand_20260609 --pid 3324 --pid 20252 --pid 5020 --pid 13096
```

If it reports `running_process_alive`, the batch is still owned by live processes even when result artifacts are not ready. Keep waiting instead of opening large progress or watcher logs.

## DashScope Flash Fallback

Ark 429 can prevent a clean multi-game frozen eval even with retries. A one-game fallback smoke was started with Bailian / DashScope:

```powershell
python scripts\run_frozen_eval_job.py --version codex_skill_pack_vote_r3 --baseline-version evolved_skill_r12_v2 --games 1 --llm-provider dashscope --llm-model deepseek-v4-flash --output-dir logs\frozen_eval_smoke\r3_dashscope_flash_20260609_172302 --internal-llm-retries 1
```

`run_frozen_eval_job.py` now maps `--internal-llm-retries` to the provider-specific retry variable:

- Ark: `ARK_MAX_RETRIES`
- DashScope compatible mode: `DASHSCOPE_MAX_RETRIES`
- DashScope native Generation: `DASHSCOPE_GENERATION_MAX_RETRIES`

Current status probe:

```text
state: running_process_alive
output_dir: logs\frozen_eval_smoke\r3_dashscope_flash_20260609_172302
```

Review this run only after result / compact summary / gate artifacts appear.

A finalizer watcher is also scheduled:

```text
watcher_pid: 25976
waits_for_pid: 22308
output_dir: logs\frozen_eval_smoke\r3_dashscope_flash_20260609_172302
```

It will run `finalize_frozen_eval_artifacts.py` after the smoke process exits and record the compact result into `skill_change_log.json`.

## Frozen Eval Runtime Guard

Future frozen eval jobs can set a wall-clock guard:

```powershell
python scripts\run_frozen_eval_job.py --version codex_skill_pack_vote_r3 --baseline-version evolved_skill_r12_v2 --games 1 --llm-provider dashscope --llm-model deepseek-v4-flash --max-runtime-seconds 1800
```

Equivalent environment variable:

```text
FROZEN_EVAL_MAX_RUNTIME_SECONDS=1800
```

The guard is checked from progress callbacks. If exceeded, the job writes an error artifact instead of continuing indefinitely. It is disabled by default, so already-running experiments are unaffected.

## r4 Candidate

DashScope `deepseek-v4-flash` smoke for r3 completed cleanly but gate returned `revise_skill`:

```text
candidate: codex_skill_pack_vote_r3
baseline: evolved_skill_r12_v2
target_mistake_delta: +1
overall_delta: +2.74
vote_quality_delta: +9.34
recommendation: revise_skill
```

Evidence: candidate D2 had checked wolf P5 voting P2, and teammate P9 also voting P2, while P7 abstained. The split-vote skill was recalled, but it did not prevent two-wolf rescue/counter-vote exposure.

Created candidate:

```text
version_id: codex_skill_pack_vote_r4
parent_id: codex_skill_pack_vote_r3
file: data/memory/role_memory_codex_skill_pack_vote_r4.json
main skill: werewolf_skill_53608c4baf_v7_codex
```

Change: refine the werewolf vote-split skill into a checked-teammate rescue-risk gate. It asks non-checked wolves to compare rescue vote, bus/light-bus, abstain, or independent public suspicion when a teammate is publicly checked or counter-voting the seer. This keeps the agent autonomous and avoids a fixed vote command.

Smoke started:

```powershell
python scripts\run_frozen_eval_job.py --version codex_skill_pack_vote_r4 --baseline-version codex_skill_pack_vote_r3 --games 1 --llm-provider dashscope --llm-model deepseek-v4-flash --output-dir logs\frozen_eval_smoke\r4_vs_r3_dashscope_flash_20260609_174816 --internal-llm-retries 1 --max-runtime-seconds 1800
```

Watcher:

```text
python_pid: 24892
watcher_pid: 25948
finalizer_watcher_pid: 12788
```

## Low-Token Frozen Eval Polling

The previous DashScope `deepseek-v4-flash` one-game smoke took about 19.5 minutes. For long games, do not repeatedly inspect progress logs. Use the metadata-only status checker with an expected duration:

```powershell
python scripts\check_frozen_eval_status.py logs\frozen_eval_smoke\r4_vs_r3_dashscope_flash_20260609_174816 --pid 24892 --pid 25948 --pid 12788 --expected-duration-seconds 1200
```

The checker now reports:

- result / compact summary / gate counts;
- review pack counts;
- newest file timestamp and process liveness;
- run age estimated from progress file metadata;
- optional max-runtime remaining / exceeded status;
- recommended next-check interval and wall-clock time.

For runs started with a wall-clock guard, also pass the max runtime:

```powershell
python scripts\check_frozen_eval_status.py logs\frozen_eval_smoke\r4_vs_r3_dashscope_flash_20260609_174816 --pid 24892 --pid 25948 --pid 12788 --pid 6564 --expected-duration-seconds 1200 --max-runtime-seconds 1800
```

If it says `well_before_expected_completion`, wait until the recommended time instead of polling. If it says `result_artifact_ready`, run the finalizer or review compact summary/gate artifacts.

Validation:

```powershell
python -m py_compile scripts\check_frozen_eval_status.py
python -m unittest discover -s tests -p "test_check_frozen_eval_status.py" -v
```

## Frozen Eval Skill Review Pack

Future `run_frozen_eval_job.py` runs now write a compact review pack after the result JSON:

```text
*_frozen_eval_result_skill_review_pack.json
```

The review pack is designed for Codex-side skill iteration. It reads completed archives once, then extracts only:

- target mistake metadata, default `werewolf_exposed_pack_vote`;
- relevant public `day_speech` / `day_vote` snippets;
- matched skill IDs, titles, scores, and signals;
- relevant player roles for post-game review.

It does not include night/private events by default. Use `--include-night` only when the failure pattern needs private-action evidence.

Manual generation example:

```powershell
python scripts\build_frozen_eval_review_pack.py logs\frozen_eval_smoke\r3_dashscope_flash_20260609_172302
```

The one-shot finalizer can also build this pack:

```powershell
python scripts\finalize_frozen_eval_artifacts.py logs\frozen_eval_smoke\r4_vs_r3_dashscope_flash_20260609_174816 --change-id codex_skill_pack_vote_r4_change_1 --review-pack
```

For the already-running r4 smoke, a separate watcher was attached because that run started before automatic review-pack generation was added:

```text
review_pack_watcher_pid: 6564
waits_for_pid: 24892
stdout: logs\frozen_eval_smoke\r4_vs_r3_dashscope_flash_20260609_174816\review_pack_watcher_stdout.log
stderr: logs\frozen_eval_smoke\r4_vs_r3_dashscope_flash_20260609_174816\review_pack_watcher_stderr.log
```

Validation sample:

```text
logs\frozen_eval_smoke\r3_dashscope_flash_20260609_172302\20260609_172304_118983_frozen_eval_result_skill_review_pack.json
```

When a compact summary is recorded into `skill_change_log.json`, the record now includes:

```text
source_review_pack_path
source_review_pack_exists
```

This links each skill-version decision to the compact evidence pack used for Codex review.

## r4 Timeout And r5 Candidate

The `codex_skill_pack_vote_r4` vs `codex_skill_pack_vote_r3` DashScope smoke did not produce a complete A/B result:

```text
error: TimeoutError
message: frozen eval exceeded max runtime: elapsed=1818.3s limit=1800.0s
output_dir: logs\frozen_eval_smoke\r4_vs_r3_dashscope_flash_20260609_174816
```

Candidate r4 completed one game before baseline timed out:

```text
candidate_version: codex_skill_pack_vote_r4
winner: werewolves
overall: 77.22
mistakes_per_game: 4.0
werewolf_exposed_pack_vote: 1
archive: logs\games\20260609_175914_535058_game-20320521-1058e6.json
```

The target mistake was not the checked-teammate rescue case that r4 focused on. It was a broader public-pressure rescue pivot:

- P9 was publicly pressured for D1 abstention.
- P9 redirected suspicion to P6.
- P7 and P9, both wolves, voted P6 on D2.
- The evaluator marked `werewolf_exposed_pack_vote`.

Created candidate:

```text
version_id: codex_skill_pack_vote_r5
parent_id: codex_skill_pack_vote_r4
file: data/memory/role_memory_codex_skill_pack_vote_r5.json
main skill: werewolf_skill_53608c4baf_v8_codex
title: 狼队公开压力位救援与转火分线
partial evidence pack: logs\frozen_eval_smoke\r4_vs_r3_dashscope_flash_20260609_174816\20260609_174818_818984_partial_candidate_skill_review_pack.json
```

r5 expands the trigger from “checked teammate rescue” to “teammate under public pressure due to abstention, suspicious voting, accusations, or self-defense.” It still does not force a fixed vote. It asks the wolf Agent to compare direct rescue pivot, light pressure, bus/cut, abstain, third-party vote, delayed vote, and independent public evidence.

Validation:

```powershell
python -m json.tool data\memory\role_memory_codex_skill_pack_vote_r5.json
python -m json.tool logs\frozen_eval_smoke\r4_vs_r3_dashscope_flash_20260609_174816\20260609_174818_818984_partial_candidate_skill_review_pack.json
python -m unittest discover -s tests -p "test_memory_store.py" -v
```

Next eval should use a longer guard, for example:

```powershell
python scripts\run_frozen_eval_job.py --version codex_skill_pack_vote_r5 --baseline-version codex_skill_pack_vote_r4 --games 1 --llm-provider dashscope --llm-model deepseek-v4-flash --output-dir logs\frozen_eval_smoke\r5_vs_r4_dashscope_flash_20260609 --internal-llm-retries 1 --max-runtime-seconds 2700
```

Started smoke:

```text
output_dir: logs\frozen_eval_smoke\r5_vs_r4_dashscope_flash_20260609_182200
python_pid: 9200
watcher_pid: 25004
max_runtime_seconds: 2700
expected_duration_seconds: 2400
status: running_process_alive
next_check: around 2026-06-09 18:53:42
```

Completed smoke:

```text
candidate_version: codex_skill_pack_vote_r5
baseline_version: codex_skill_pack_vote_r4
completed_games: candidate=1, baseline=1
winner: both werewolves
candidate_overall: 80.99
baseline_overall: 81.21
overall_delta: -0.22
candidate_vote_quality: 70.00
baseline_vote_quality: 61.09
vote_quality_delta: +8.91
candidate_mistakes_per_game: 1.0
baseline_mistakes_per_game: 1.0
candidate_mistakes: seer_failed_to_reveal_wolf=1
baseline_mistakes: witch_mechanical_n1_save=1
werewolf_exposed_pack_vote: 0 vs 0
```

Gate result after refining the gate logic:

```text
recommendation: expand_sample
reason: target mistake did not appear for either side, so this one-game sample cannot prove targeted improvement.
change_log decision: expanded_eval_expand_sample
```

Gate logic note:

```text
target_absent_both=true
```

If candidate and baseline both have zero target mistakes, the sample is treated as “target scenario not reproduced,” not as a failed skill. With a small sample and no quality collapse, the correct recommendation is `expand_sample`.

Interpretation:

- r5 did not regress on the target mistake in this seed, but the target scenario did not reproduce for r4 either.
- r5 improved vote quality substantially in this sample.
- r5 slightly reduced overall score, mostly because the single-game non-target mistake profile changed.
- This is not evidence for promotion or another skill rewrite. The correct next step is a 3-game seed-aligned expansion.

Started 3-game expansion:

```text
candidate_version: codex_skill_pack_vote_r5
baseline_version: codex_skill_pack_vote_r4
games: 3
llm_provider: dashscope
llm_model: deepseek-v4-flash
output_dir: logs\frozen_eval_smoke\r5_vs_r4_dashscope_flash_expand_20260610_091500
python_pid: 9088
watcher_pid: 12360
max_runtime_seconds: 9000
expected_duration_seconds: 7200
next_check: around 2026-06-10 10:35:00
```

Finalizer watcher:

```text
watcher_pid: 27976
waits_for_pid: 9088
stdout: logs\frozen_eval_smoke\r5_vs_r4_dashscope_flash_expand_20260610_091500\finalizer_record_watcher_stdout.log
stderr: logs\frozen_eval_smoke\r5_vs_r4_dashscope_flash_expand_20260610_091500\finalizer_record_watcher_stderr.log
```

Failure handling:

```text
scripts/finalize_frozen_eval_artifacts.py
```

now records `*_error.json` into `skill_change_log.json` when no result artifact exists. This covers timeout or failed frozen eval runs without opening progress logs. The change was validated by:

```powershell
python -m py_compile scripts\finalize_frozen_eval_artifacts.py
python -m unittest discover -s tests -p "test_finalize_frozen_eval_artifacts.py" -v
```

Status checker visibility:

```text
scripts/check_frozen_eval_status.py
```

now also reports `errors: <count>` and returns `failed_error_artifact` when a frozen eval has `*_error.json` but no result and no live process. This keeps failed runs visible from metadata only.

Do not inspect progress logs while this is running. Review only result / compact summary / gate / skill review pack after completion.
