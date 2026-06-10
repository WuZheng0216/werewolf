from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .archive import serialize_event
from .llm import ChatClient, build_chat_client_from_env, extract_json_object
from .models import EvaluationReport, GameEvent, GameResult, Role, game_rule_summary, to_jsonable


DEFAULT_REVIEW_TIMEOUT_SECONDS = 180.0
DEFAULT_REVIEW_MAX_RETRIES = 2
DEFAULT_REVIEW_MAX_TOKENS = 2600
DEFAULT_REVIEW_RAW_DIR = Path("logs") / "reviews"


class ReviewRepairError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        raw_output_path: str | None = None,
        repair_output_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_output_path = raw_output_path
        self.repair_output_path = repair_output_path


class LLMGameReviewer:
    """God-view post-game reviewer.

    This reviewer runs after a game is over, so it is intentionally allowed to
    see hidden roles and night actions. Its output is separate from in-game
    Agent observations and can be used as long-term strategy memory.
    """

    def __init__(self, chat_client: ChatClient, *, max_events: int = 90, max_tokens: int | None = None):
        self.chat_client = chat_client
        self.max_events = max_events
        self.max_tokens = max_tokens or _env_int("LLM_REVIEW_MAX_TOKENS", DEFAULT_REVIEW_MAX_TOKENS)

    def review_game(self, result: GameResult, report: EvaluationReport) -> dict[str, Any]:
        started = time.perf_counter()
        messages = self._messages(result, report)
        content = self.chat_client.chat(messages, temperature=0.15, max_tokens=self.max_tokens)
        repair_attempts = 0
        raw_output_path: str | None = None
        repair_output_path: str | None = None
        try:
            review = self._parse_review(content, result)
        except Exception as first_exc:
            raw_output_path = _save_review_raw_output(result, "invalid", content, first_exc)
            repair_attempts = 1
            repair_messages = self._repair_messages(messages, content, first_exc)
            repaired_content = self.chat_client.chat(repair_messages, temperature=0.0, max_tokens=self.max_tokens)
            try:
                review = self._parse_review(repaired_content, result)
            except Exception as repair_exc:
                repair_output_path = _save_review_raw_output(result, "repair_failed", repaired_content, repair_exc)
                raise ReviewRepairError(
                    f"LLM review JSON repair failed after initial {type(first_exc).__name__}: {repair_exc}",
                    raw_output_path=raw_output_path,
                    repair_output_path=repair_output_path,
                ) from repair_exc

        review["metadata"] = {
            "review_backend": "llm",
            "llm_provider": self.chat_client.config.provider,
            "llm_model": self.chat_client.config.model,
            "llm_max_output_tokens": self.max_tokens,
            "llm_timeout_seconds": self.chat_client.config.timeout_seconds,
            "llm_max_retries": self.chat_client.config.max_retries,
            "llm_repair_attempts": repair_attempts,
            "llm_latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        if raw_output_path:
            review["metadata"]["raw_invalid_output_path"] = raw_output_path
        if repair_output_path:
            review["metadata"]["repair_failed_output_path"] = repair_output_path
        return review

    def _parse_review(self, content: str, result: GameResult) -> dict[str, Any]:
        raw = extract_json_object(content)
        return _normalize_review(raw, result)

    def _repair_messages(
        self,
        previous_messages: list[dict[str, str]],
        invalid_content: str,
        error: Exception,
    ) -> list[dict[str, str]]:
        repair_payload = {
            "任务": "上一次复盘输出不是合法 JSON。请基于同一局游戏上下文，重新输出一个完整、合法、可被 json.loads 解析的 JSON 对象。",
            "错误类型": type(error).__name__,
            "错误信息": str(error),
            "硬性要求": [
                "只输出 JSON 对象，不要 Markdown，不要解释。",
                "必须包含 game_summary、good_cases、bad_cases、role_reflections、memory_candidates、confidence。",
                "good_cases 和 bad_cases 必须是数组；role_reflections 必须包含 werewolf/seer/witch/hunter/villager。",
                "memory_candidates 必须是数组，元素包含 role、memory、evidence_event_ids、confidence。",
                "如果本局有狼人发言或投票，memory_candidates 至少包含 1 条 werewolf 欺骗/伪装/对跳/倒钩/分票相关记忆。",
                "event id 必须来自上一条输入中的 timeline。",
            ],
            "上一次非法输出片段": invalid_content[:12000],
        }
        return previous_messages + [{"role": "user", "content": json.dumps(repair_payload, ensure_ascii=False)}]

    def _messages(self, result: GameResult, report: EvaluationReport) -> list[dict[str, str]]:
        rules = result.game_rules or game_rule_summary()
        system = (
            "你是 AI 狼人杀系统的上帝视角复盘官。"
            "你可以查看完整身份、夜间行动、公开发言、投票和规则评测结果。"
            "你必须按本次输入的 GameRules 复盘，GameRules 高于你预训练中的其他狼人杀规则。"
            "你的任务是客观复盘本局做得好和做得不好的地方，并产出可复用的角色策略记忆。"
            "必须基于给定 event id 做证据引用，不要编造未出现的事实。"
            "只输出一个 JSON 对象，不要 Markdown，不要额外解释。"
        )
        user_payload = {
            "任务": "请以上帝视角复盘这一局狼人杀，并生成可沉淀到下一版 Agent 的记忆候选。",
            "游戏规则_GameRules": rules,
            "输出格式": {
                "game_summary": "一句话总结本局胜负关键。",
                "good_cases": [
                    {
                        "role": "角色英文名，例如 seer/werewolf/villager",
                        "player_id": "玩家数字 id 或 null",
                        "event_ids": "证据事件 id 数组",
                        "reason": "为什么这是好决策",
                        "lesson": "可复用经验",
                    }
                ],
                "bad_cases": [
                    {
                        "role": "角色英文名",
                        "player_id": "玩家数字 id 或 null",
                        "event_ids": "证据事件 id 数组",
                        "reason": "问题在哪里",
                        "lesson": "下次如何避免",
                    }
                ],
                "role_reflections": {
                    "werewolf": ["角色级反思，最多 3 条"],
                    "seer": [],
                    "witch": [],
                    "hunter": [],
                    "villager": [],
                },
                "memory_candidates": [
                    {
                        "role": "角色英文名",
                        "memory": "可直接写入长期策略记忆的一句话，避免强制固定动作，要表达经验原则",
                        "evidence_event_ids": "证据事件 id 数组",
                        "confidence": "0 到 1",
                    }
                ],
                "confidence": "你对本次复盘可靠性的 0 到 1 评分",
            },
            "硬性约束": [
                "memory_candidates 不要写成固定命令，例如“必须投 P3”，要写成可迁移策略原则。",
                "bad_cases 要区分真正错误和结果论；如果决策信息不足但结果不好，请说明信息不足。",
                "狼人可以欺骗，但要评价欺骗是否自洽、是否暴露狼队协同。",
                "狼人欺骗复盘要单独评价：是否只说“我是好人”、是否制造合理分歧、是否成功误导好人投错、是否避免三狼同模板同票、悍跳/对跳是否有完整查验链和后续查验计划。",
                "如果狼人默认认下单预言家、没有软对跳或质疑查验链，应沉淀 werewolf 记忆：当场上只有单预言家且没有强查杀时，狼人可选择软对跳、质疑查验链或制造第二视角，而不是全员默认认下。",
                "如果狼队过度保护队友，应沉淀 werewolf 记忆：狼队不一定都要保护悍跳狼，至少一名狼人可以倒钩或轻踩队友以换取深水身份。",
                "如果狼人尝试悍跳或对跳，应评价其查验对象、查验结论、后续查验计划、公开证据包装是否完整。",
                "好人阵营要评价信息传递、站边、归票和技能使用质量。",
                "如果预言家首日只有金水或信息不足却固定起跳，应沉淀 seer 记忆：只有金水或信息不足时不必机械首日起跳，可先隐忍或软铺垫，等查杀、被强质疑、出现对跳或需要带队时再公开身份。",
                "如果女巫首夜非自救直接使用解药，应沉淀 witch 记忆：首夜救人不是固定动作，非自救时要比较被刀目标价值、保留解药保护关键神职的收益和后续轮次风险。",
                "所有 event_ids/evidence_event_ids 必须来自下面的 timeline。",
                "本局没有警长、警徽、警徽流、撕警徽、移交警徽、警长票权重；不要把这些术语写入复盘或 memory_candidates。",
                "预言家的后续安排应称为“后续查验计划”，不要称为警徽流。",
                "如果历史发言中玩家提到不存在的规则，应作为规则误用风险复盘，而不是当成有效机制。",
            ],
            "game": {
                "game_id": result.game_id,
                "seed": result.seed,
                "version_id": result.version_id,
                "winner": result.winner.value,
                "winner_zh": result.winner.zh,
                "win_reason": result.win_reason,
                "day": result.day,
                "players": [
                    {
                        "id": player.id,
                        "name": player.name,
                        "role": player.role.value,
                        "role_zh": player.role.zh,
                        "faction": player.faction.value,
                        "alive": player.alive,
                        "death_day": player.death_day,
                        "death_reason": player.death_reason,
                    }
                    for player in result.players
                ],
            },
            "rule_evaluation": {
                "scores": report.scores,
                "mistakes": report.mistakes[:12],
                "key_decisions": report.key_decisions[:20],
                "recommendations": report.recommendations,
                "summary": report.summary,
            },
            "timeline": _compact_timeline(result.events, self.max_events),
        }
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]


def review_game_with_llm(
    result: GameResult,
    report: EvaluationReport,
    *,
    llm_provider: str,
    llm_model: str | None,
    enabled: bool = True,
) -> dict[str, Any] | None:
    if not enabled:
        return None
    client = build_chat_client_from_env(llm_provider, llm_model)
    if client is None:
        return {
            "review_status": "skipped",
            "error": "no_chat_client",
            "message": "LLM reviewer skipped because no chat client could be built from environment.",
            "metadata": {"review_backend": "llm", "llm_provider": llm_provider, "llm_model": llm_model},
        }
    _configure_review_client(client)
    try:
        return LLMGameReviewer(client).review_game(result, report)
    except Exception as exc:
        metadata = {
            "review_backend": "llm",
            "llm_provider": client.config.provider,
            "llm_model": client.config.model,
            "llm_timeout_seconds": client.config.timeout_seconds,
            "llm_max_retries": client.config.max_retries,
        }
        raw_output_path = getattr(exc, "raw_output_path", None)
        repair_output_path = getattr(exc, "repair_output_path", None)
        if raw_output_path:
            metadata["raw_invalid_output_path"] = raw_output_path
        if repair_output_path:
            metadata["repair_failed_output_path"] = repair_output_path
        return {
            "review_status": "failed",
            "error": type(exc).__name__,
            "message": str(exc),
            "metadata": metadata,
        }


def _configure_review_client(client: ChatClient) -> None:
    config = client.config
    current_timeout = _positive_float(getattr(config, "timeout_seconds", 0), default=0.0)
    current_retries = _bounded_int(getattr(config, "max_retries", 0), default=0)
    config.timeout_seconds = _env_float(
        "LLM_REVIEW_TIMEOUT_SECONDS",
        max(current_timeout, DEFAULT_REVIEW_TIMEOUT_SECONDS),
    )
    config.max_retries = _env_int(
        "LLM_REVIEW_MAX_RETRIES",
        max(current_retries, DEFAULT_REVIEW_MAX_RETRIES),
    )


def _save_review_raw_output(result: GameResult, label: str, content: str, error: Exception) -> str:
    directory = Path(os.environ.get("LLM_REVIEW_RAW_DIR", str(DEFAULT_REVIEW_RAW_DIR)))
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    game_id = result.game_id.replace("/", "_").replace("\\", "_")
    path = directory / f"{stamp}_{game_id}_{label}.json"
    payload = {
        "schema_version": 1,
        "kind": "llm_review_raw_output",
        "game_id": result.game_id,
        "version_id": result.version_id,
        "label": label,
        "error": type(error).__name__,
        "message": str(error),
        "raw_output": content,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _compact_timeline(events: list[GameEvent], max_events: int) -> list[dict[str, Any]]:
    selected = events
    if len(events) > max_events:
        head = max(10, max_events // 4)
        selected = events[:head] + events[-(max_events - head) :]
    return [_compact_event(event) for event in selected]


def _compact_event(event: GameEvent) -> dict[str, Any]:
    serialized = serialize_event(event)
    decision = serialized.get("decision") or {}
    return {
        "id": serialized["id"],
        "day": serialized["day"],
        "phase": serialized["phase"],
        "phase_zh": serialized["phase_zh"],
        "event_type": serialized["event_type"],
        "actor_id": serialized["actor_id"],
        "target_id": serialized["target_id"],
        "secondary_target_id": serialized["secondary_target_id"],
        "public_text": serialized["public_text"],
        "public_payload": _compact_payload(serialized.get("public_payload") or {}),
        "private_payload": _compact_payload(serialized.get("private_payload") or {}),
        "decision": {
            "action": decision.get("action"),
            "target_id": decision.get("target_id"),
            "speech": _short_text(decision.get("speech"), 180),
            "reason": _short_text(decision.get("reason"), 180),
            "confidence": decision.get("confidence"),
            "evidence_event_ids": (decision.get("metadata") or {}).get("evidence_event_ids", []),
            "quoted_evidence": (decision.get("metadata") or {}).get("quoted_evidence", []),
        }
        if decision
        else None,
    }


def _compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "speech",
        "votes",
        "vote_counts",
        "abstain",
        "exiled_id",
        "tie",
        "tied_targets",
        "revote",
        "first_votes",
        "first_abstentions",
        "allowed_vote_targets",
        "tie_pk",
        "death_ids",
        "visible_to_players",
        "target_id",
        "result",
        "saved_target",
        "poisoned_target",
        "shot_target",
        "reason",
    }
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in allowed:
            continue
        result[key] = _short_text(value, 220) if isinstance(value, str) else to_jsonable(value)
    return result


def _normalize_review(raw: dict[str, Any], result: GameResult) -> dict[str, Any]:
    events = result.events
    valid_event_ids = {event.id for event in events}
    role_reflections: dict[str, list[str]] = {}
    raw_reflections = raw.get("role_reflections") if isinstance(raw.get("role_reflections"), dict) else {}
    for role in Role:
        items = raw_reflections.get(role.value, [])
        role_reflections[role.value] = [_short_text(item, 180) for item in _as_list(items)[:3] if _short_text(item, 180)]

    memory_candidates = []
    for item in _as_list(raw.get("memory_candidates"))[:12]:
        if not isinstance(item, dict):
            continue
        role = _valid_role(item.get("role"))
        memory = _short_text(item.get("memory"), 180)
        if not role or not memory:
            continue
        memory_candidates.append(
            {
                "role": role,
                "memory": memory,
                "evidence_event_ids": _event_ids(item.get("evidence_event_ids"), valid_event_ids),
                "confidence": _bounded_float(item.get("confidence"), default=0.65),
            }
        )
    _ensure_werewolf_deception_memory(memory_candidates, result, valid_event_ids)
    _ensure_power_role_timing_memories(memory_candidates, result, valid_event_ids)

    return {
        "review_status": "ok",
        "game_summary": _short_text(raw.get("game_summary"), 260),
        "good_cases": _normalize_cases(raw.get("good_cases"), valid_event_ids),
        "bad_cases": _normalize_cases(raw.get("bad_cases"), valid_event_ids),
        "role_reflections": role_reflections,
        "memory_candidates": memory_candidates,
        "confidence": _bounded_float(raw.get("confidence"), default=0.7),
    }


def _ensure_werewolf_deception_memory(
    memory_candidates: list[dict[str, Any]],
    result: GameResult,
    valid_event_ids: set[int],
) -> None:
    players = {player.id: player for player in result.players}
    wolf_speeches = [
        event
        for event in result.events
        if event.event_type == "speech"
        and event.actor_id in players
        and players[event.actor_id].role == Role.WEREWOLF
    ]
    if not wolf_speeches:
        return
    if any(
        item.get("role") == Role.WEREWOLF.value
        and any(token in str(item.get("memory", "")) for token in ("欺骗", "伪装", "对跳", "悍跳", "倒钩", "分票", "查验链"))
        for item in memory_candidates
    ):
        return

    evidence_ids = [event.id for event in wolf_speeches[:3] if event.id in valid_event_ids]
    if _has_non_wolf_seer_claim(result) and not _has_wolf_seer_counterplay(result):
        memory = "当场上只有单预言家且没有强查杀时，狼人可选择软对跳、质疑查验链或制造第二视角，而不是全员默认认下。"
    elif _has_wolf_fake_seer_claim(result):
        memory = "狼人悍跳或对跳预言家时，要补齐查验对象、查验结论、后续查验计划和公开证据包装，避免只喊身份不成链。"
    else:
        memory = "狼人公开欺骗应基于已发生的发言和票型包装，可用倒钩、分票、拉拢或抗推制造合理分歧，不能只重复“我是好人”。"
    memory_candidates.append(
        {
            "role": Role.WEREWOLF.value,
            "memory": memory,
            "evidence_event_ids": evidence_ids,
            "confidence": 0.88,
        }
    )


def _ensure_power_role_timing_memories(
    memory_candidates: list[dict[str, Any]],
    result: GameResult,
    valid_event_ids: set[int],
) -> None:
    seer_event = _seer_mechanical_day1_claim_event(result)
    if seer_event and not _has_role_memory(memory_candidates, Role.SEER, ("首日", "起跳", "隐忍", "公开身份")):
        memory_candidates.append(
            {
                "role": Role.SEER.value,
                "memory": "只有金水或信息不足时，预言家不必机械首日公开身份；可先隐忍或软铺垫站边与后续查验计划，等查杀、被强质疑、出现对跳或需要带队时再跳。",
                "evidence_event_ids": [seer_event.id] if seer_event.id in valid_event_ids else [],
                "confidence": 0.88,
            }
        )

    witch_event = _witch_mechanical_n1_save_event(result)
    if witch_event and not _has_role_memory(memory_candidates, Role.WITCH, ("首夜", "解药", "救人", "保留解药")):
        memory_candidates.append(
            {
                "role": Role.WITCH.value,
                "memory": "女巫首夜救人不是固定动作；非自救时先比较被刀目标价值、保留解药保护关键神职的收益和后续轮次风险，理由不足可以不救。",
                "evidence_event_ids": [witch_event.id] if witch_event.id in valid_event_ids else [],
                "confidence": 0.88,
            }
        )


def _seer_mechanical_day1_claim_event(result: GameResult) -> GameEvent | None:
    players = {player.id: player for player in result.players}
    for event in sorted(result.events, key=lambda item: (item.day, item.id)):
        if event.event_type != "speech" or event.day != 1 or event.actor_id not in players:
            continue
        if players[event.actor_id].role != Role.SEER:
            continue
        speech = event.public_payload.get("speech", "")
        if not _mentions_role_claim(speech, "预言家"):
            continue
        if _seer_has_wolf_inspection_before(result.events, event.actor_id, event):
            continue
        if any(token in speech for token in ("查杀", "验到狼", "查到狼", "是狼人")):
            continue
        return event
    return None


def _witch_mechanical_n1_save_event(result: GameResult) -> GameEvent | None:
    players = {player.id: player for player in result.players}
    for event in result.events:
        if event.event_type != "witch_action" or event.day != 1 or event.actor_id not in players:
            continue
        if players[event.actor_id].role != Role.WITCH:
            continue
        saved = event.private_payload.get("saved_target")
        if saved and saved != event.actor_id:
            return event
    return None


def _seer_has_wolf_inspection_before(events: list[GameEvent], seer_id: int, speech_event: GameEvent) -> bool:
    return any(
        event.event_type == "seer_inspect"
        and event.actor_id == seer_id
        and (event.day < speech_event.day or (event.day == speech_event.day and event.id < speech_event.id))
        and event.private_payload.get("result") == "werewolf"
        for event in events
    )


def _has_role_memory(memory_candidates: list[dict[str, Any]], role: Role, terms: tuple[str, ...]) -> bool:
    return any(
        item.get("role") == role.value
        and any(term in str(item.get("memory") or "") for term in terms)
        for item in memory_candidates
    )


def _has_non_wolf_seer_claim(result: GameResult) -> bool:
    players = {player.id: player for player in result.players}
    return any(
        event.event_type == "speech"
        and event.actor_id in players
        and players[event.actor_id].role != Role.WEREWOLF
        and _mentions_role_claim(event.public_payload.get("speech", ""), "预言家")
        for event in result.events
    )


def _has_wolf_seer_counterplay(result: GameResult) -> bool:
    players = {player.id: player for player in result.players}
    counter_tokens = ("预言家", "查验", "查杀", "金水", "对跳", "不认", "质疑", "可信度", "验人", "悍跳")
    return any(
        event.event_type == "speech"
        and event.actor_id in players
        and players[event.actor_id].role == Role.WEREWOLF
        and any(token in event.public_payload.get("speech", "") for token in counter_tokens)
        for event in result.events
    )


def _has_wolf_fake_seer_claim(result: GameResult) -> bool:
    players = {player.id: player for player in result.players}
    return any(
        event.event_type == "speech"
        and event.actor_id in players
        and players[event.actor_id].role == Role.WEREWOLF
        and _wolf_claims_fake_seer(event.public_payload.get("speech", ""))
        for event in result.events
    )


def _wolf_claims_fake_seer(speech: str) -> bool:
    if _mentions_role_claim(speech, "预言家"):
        return True
    compact = "".join(str(speech or "").split())
    return bool(
        re.search(r"我.{0,6}(对跳|悍跳|起跳|跳).{0,6}预言家", compact)
        or re.search(r"我.{0,10}(查验|验|查了|验了).{0,14}P\d+", compact)
        or re.search(r"(我这里|我报|我的查验|我验|我查).{0,18}P\d+.{0,10}(查杀|金水|好人|狼人)", compact)
    )


def _mentions_role_claim(speech: str, role_name: str) -> bool:
    compact = "".join(str(speech or "").split())
    patterns = (
        f"我跳{role_name}",
        f"我起跳{role_name}",
        f"我对跳{role_name}",
        f"我悍跳{role_name}",
        f"我是{role_name}",
        f"我才是{role_name}",
        f"我这里是{role_name}",
        f"我是全场唯一真{role_name}",
        f"我是唯一{role_name}",
        f"我为{role_name}",
        f"我认{role_name}",
        f"我拍{role_name}",
        f"我有{role_name}视角",
    )
    return any(pattern in compact for pattern in patterns)


def _normalize_cases(value: Any, valid_event_ids: set[int]) -> list[dict[str, Any]]:
    cases = []
    for item in _as_list(value)[:8]:
        if not isinstance(item, dict):
            continue
        cases.append(
            {
                "role": _valid_role(item.get("role")),
                "player_id": _optional_int(item.get("player_id")),
                "event_ids": _event_ids(item.get("event_ids"), valid_event_ids),
                "reason": _short_text(item.get("reason"), 220),
                "lesson": _short_text(item.get("lesson"), 180),
            }
        )
    return cases


def _valid_role(value: Any) -> str | None:
    text = str(value or "").strip()
    valid = {role.value for role in Role}
    return text if text in valid else None


def _event_ids(value: Any, valid_event_ids: set[int]) -> list[int]:
    ids: list[int] = []
    for item in _as_list(value)[:8]:
        try:
            event_id = int(item)
        except (TypeError, ValueError):
            continue
        if event_id in valid_event_ids and event_id not in ids:
            ids.append(event_id)
    return ids


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "null"}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bounded_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(1.0, parsed))


def _bounded_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0, parsed)


def _positive_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, parsed)


def _env_float(key: str, default: float) -> float:
    try:
        return max(1.0, float(os.environ.get(key, default)))
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(key, default)))
    except (TypeError, ValueError):
        return default


def _short_text(value: Any, limit: int) -> str:
    return str(value or "").strip().replace("\n", " ")[:limit]


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
