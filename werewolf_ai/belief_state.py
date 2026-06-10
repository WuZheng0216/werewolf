from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .models import GameEvent, Phase, PlayerState, Role, to_jsonable


PUBLIC_BELIEF_EVENTS = {
    "setup",
    "dawn_deaths",
    "dawn_peace",
    "speech",
    "vote",
    "vote_tie",
    "exile",
    "hunter_shot",
    "hunter_pass",
    "game_over",
}


def initialize_belief_states(players: list[PlayerState]) -> dict[int, dict[str, Any]]:
    states: dict[int, dict[str, Any]] = {}
    for player in players:
        private_notes: dict[str, Any] = {}
        if player.role == Role.WEREWOLF:
            teammates = [item.id for item in players if item.role == Role.WEREWOLF and item.id != player.id]
            private_notes["known_teammates"] = teammates
            private_notes["cover_identity"] = {
                "pretend_role": "villager",
                "public_story": "普通闭眼好人，基于公开发言和票型推理。",
                "fake_seer_chain": [],
            }
        elif player.role == Role.SEER:
            private_notes["inspections"] = []
        elif player.role == Role.WITCH:
            private_notes["medicine_history"] = []
        elif player.role == Role.HUNTER:
            private_notes["shot_history"] = []

        states[player.id] = {
            "schema_version": 1,
            "player_id": player.id,
            "player_name": player.name,
            "self_role": player.role.value,
            "day": 0,
            "last_event_id": 0,
            "summary": "开局暂无公开信息；先观察发言、票型和身份声明。",
            "suspicions": {
                str(other.id): {
                    "wolf_score": 0.0 if other.id == player.id else 0.5,
                    "trust_score": 1.0 if other.id == player.id else 0.5,
                    "reason": "自己身份已知" if other.id == player.id else "初始无信息",
                    "evidence_event_ids": [],
                }
                for other in players
            },
            "trusted_players": [],
            "role_hypotheses": {"seer": [], "witch": [], "hunter": []},
            "claims": [],
            "vote_memory": [],
            "night_memory": [],
            "public_notes": [],
            "social_belief_matrix": {
                str(other.id): {"speaker_id": other.id, "suspects": {}, "trusts": {}, "votes": []}
                for other in players
            },
            "self_commitments": [],
            "current_plan": {
                "speech_intent": "先基于公开信息输出独立判断。",
                "vote_target": None,
                "fallback_vote": None,
                "night_plan": None,
            },
            "private_notes": private_notes,
        }
    return states


def belief_view_for_player(state: dict[str, Any], *, max_items: int = 6) -> dict[str, Any]:
    view = deepcopy(state)
    suspicions = list((view.get("suspicions") or {}).items())
    suspicions.sort(key=lambda item: item[1].get("wolf_score", 0.0), reverse=True)
    view["top_suspicions"] = [
        {"player_id": int(player_id), **payload}
        for player_id, payload in suspicions
        if int(player_id) != int(view.get("player_id", 0))
    ][:4]
    trust = list((view.get("suspicions") or {}).items())
    trust.sort(key=lambda item: item[1].get("trust_score", 0.0), reverse=True)
    view["trusted_players"] = [
        {"player_id": int(player_id), "trust_score": payload.get("trust_score", 0.0), "reason": payload.get("reason", "")}
        for player_id, payload in trust
        if int(player_id) != int(view.get("player_id", 0)) and payload.get("trust_score", 0.0) > 0.55
    ][:4]
    view.pop("suspicions", None)
    view["claims"] = view.get("claims", [])[-max_items:]
    view["vote_memory"] = view.get("vote_memory", [])[-max_items:]
    view["night_memory"] = view.get("night_memory", [])[-max_items:]
    view["public_notes"] = view.get("public_notes", [])[-max_items:]
    view["self_commitments"] = view.get("self_commitments", [])[-max_items:]
    view["social_belief_matrix"] = _social_belief_matrix_view(view.get("social_belief_matrix", {}), max_items=max_items)
    view["role_hypotheses"] = {
        role: sorted(items, key=lambda item: item.get("confidence", 0.0), reverse=True)[:3]
        for role, items in (view.get("role_hypotheses") or {}).items()
    }
    view["usage_guidance"] = (
        "这是你的连续认知白板，只来自你可见的信息。可以修正判断，但需要基于新的公开证据解释；"
        "发言和投票要尽量保持与 self_commitments 兼容。"
    )
    return to_jsonable(view)


def update_belief_states_after_event(
    belief_states: dict[int, dict[str, Any]],
    event: GameEvent,
    players: list[PlayerState],
) -> None:
    observer_ids = _visible_observer_ids(event, players)
    for observer_id in observer_ids:
        state = belief_states.get(observer_id)
        if not state:
            continue
        state["day"] = event.day
        state["last_event_id"] = event.id
        if event.event_type in PUBLIC_BELIEF_EVENTS:
            _apply_public_event(state, event)
        else:
            _apply_private_event(state, event, observer_id)
        _trim_state(state)


def _visible_observer_ids(event: GameEvent, players: list[PlayerState]) -> list[int]:
    if event.public_payload.get("visible_to_players") is True or event.event_type in PUBLIC_BELIEF_EVENTS:
        return [player.id for player in players]
    if event.event_type in {"wolf_intent", "wolf_pack_plan"}:
        return [player.id for player in players if player.role == Role.WEREWOLF]
    if event.actor_id is not None:
        return [event.actor_id]
    return []


def _apply_public_event(state: dict[str, Any], event: GameEvent) -> None:
    if event.event_type == "setup":
        _append_limited(
            state,
            "public_notes",
            {"event_id": event.id, "type": "setup", "text": event.public_text},
        )
        return

    if event.event_type in {"dawn_deaths", "dawn_peace", "game_over", "hunter_pass"}:
        _append_limited(
            state,
            "public_notes",
            {"event_id": event.id, "type": event.event_type, "text": event.public_text},
        )
        return

    if event.event_type == "hunter_shot":
        _append_limited(
            state,
            "public_notes",
            {"event_id": event.id, "type": "hunter_shot", "actor_id": event.actor_id, "target_id": event.target_id},
        )
        if event.actor_id:
            _upsert_role_hypothesis(state, "hunter", event.actor_id, 0.92, event.id, "公开发动猎人技能")
            _adjust_player_score(state, event.actor_id, trust_delta=0.25, wolf_delta=-0.2, event_id=event.id, reason="公开猎人技能")
        return

    if event.event_type == "speech":
        speech = str(event.public_payload.get("speech") or event.public_text or "")
        actor_id = event.actor_id
        if actor_id is not None:
            _record_speech_claims(state, event, actor_id, speech)
            _update_social_belief_from_speech(state, event, actor_id, speech)
            if actor_id == state.get("player_id"):
                _record_self_commitments(state, event, speech)
        _append_limited(
            state,
            "public_notes",
            {"event_id": event.id, "type": "speech", "actor_id": actor_id, "brief": _short(speech, 120)},
        )
        return

    if event.event_type == "vote":
        vote_target = event.public_payload.get("vote_target")
        abstain = bool(event.public_payload.get("abstain"))
        _append_limited(
            state,
            "vote_memory",
            {
                "event_id": event.id,
                "day": event.day,
                "voter": event.actor_id,
                "target": vote_target,
                "abstain": abstain,
                "interpretation": "弃票" if abstain else f"P{event.actor_id} 投给 P{vote_target}",
            },
            limit=20,
        )
        if event.actor_id == state.get("player_id"):
            state.setdefault("current_plan", {})["last_vote"] = vote_target
        if event.actor_id is not None:
            _update_social_belief_from_vote(state, event, event.actor_id, vote_target, abstain)
        return

    if event.event_type == "vote_tie":
        _append_limited(
            state,
            "public_notes",
            {
                "event_id": event.id,
                "type": "vote_tie",
                "tied_targets": event.public_payload.get("tied_targets", []),
                "votes": event.public_payload.get("votes", [])[-9:],
                "abstentions": event.public_payload.get("abstentions", [])[-9:],
                "interpretation": "首轮平票，进入 PK 发言和二次投票",
            },
        )
        return

    if event.event_type == "exile":
        exiled_id = event.public_payload.get("exiled_id")
        _append_limited(
            state,
            "public_notes",
            {
                "event_id": event.id,
                "type": "exile",
                "exiled_id": exiled_id,
                "tie": bool(event.public_payload.get("tie")),
                "votes": event.public_payload.get("votes", [])[-9:],
                "abstentions": event.public_payload.get("abstentions", [])[-9:],
            },
        )
        return


def _apply_private_event(state: dict[str, Any], event: GameEvent, observer_id: int) -> None:
    if event.event_type == "wolf_pack_plan":
        strategy_options = deepcopy(event.private_payload.get("strategy_options", []))
        state.setdefault("current_plan", {})["wolf_pack_plan"] = {
            "day": event.day,
            "autonomy_mode": event.private_payload.get("autonomy_mode", True),
            "strategy_options": strategy_options[:7],
            "pressure": deepcopy(event.private_payload.get("pressure", {})),
            "private_only": True,
        }
        _append_limited(
            state,
            "night_memory",
            {
                "event_id": event.id,
                "type": "wolf_pack_plan",
                "autonomy_mode": event.private_payload.get("autonomy_mode", True),
                "strategy_option_ids": [item.get("id") for item in strategy_options[:7]],
            },
        )
        return

    if event.event_type == "wolf_intent":
        _append_limited(
            state,
            "night_memory",
            {
                "event_id": event.id,
                "type": "wolf_intent",
                "actor_id": event.actor_id,
                "target_id": event.target_id,
                "reason": event.private_payload.get("reason"),
            },
            limit=16,
        )
        if event.actor_id == observer_id:
            state.setdefault("current_plan", {})["night_plan"] = f"本夜提交刀 P{event.target_id}"
        return

    if event.event_type == "seer_inspect" and event.actor_id == observer_id:
        target_id = event.private_payload.get("target_id") or event.target_id
        result = event.private_payload.get("result")
        note = {"event_id": event.id, "day": event.day, "target_id": target_id, "result": result}
        state.setdefault("private_notes", {}).setdefault("inspections", []).append(note)
        if result == "werewolf":
            _adjust_player_score(state, target_id, wolf_delta=0.45, trust_delta=-0.25, event_id=event.id, reason="自己的查验结果为狼人")
        elif result == "villagers":
            _adjust_player_score(state, target_id, wolf_delta=-0.25, trust_delta=0.3, event_id=event.id, reason="自己的查验结果为好人阵营")
        _append_limited(state, "night_memory", {"event_id": event.id, "type": "seer_inspect", **note})
        return

    if event.event_type == "witch_action" and event.actor_id == observer_id:
        note = {
            "event_id": event.id,
            "day": event.day,
            "action": getattr(event.decision, "action", None).value if event.decision else None,
            "target_id": event.target_id,
            "saved_target": event.private_payload.get("saved_target"),
            "poisoned_target": event.private_payload.get("poisoned_target"),
            "reason": event.private_payload.get("reason"),
        }
        state.setdefault("private_notes", {}).setdefault("medicine_history", []).append(note)
        _append_limited(state, "night_memory", {"type": "witch_action", **note})
        return


def _record_speech_claims(state: dict[str, Any], event: GameEvent, actor_id: int, speech: str) -> None:
    compact = re.sub(r"\s+", "", speech)
    role_claims = [
        ("seer", ("我是预言家", "我跳预言家", "起跳预言家", "我为预言家")),
        ("witch", ("我是女巫", "我跳女巫", "起跳女巫", "我为女巫")),
        ("hunter", ("我是猎人", "我跳猎人", "起跳猎人", "我为猎人")),
    ]
    for role, patterns in role_claims:
        if any(pattern in compact for pattern in patterns):
            claim = {
                "event_id": event.id,
                "day": event.day,
                "player_id": actor_id,
                "claim_role": role,
                "speech_brief": _short(speech, 160),
            }
            _append_limited(state, "claims", claim, limit=18)
            _upsert_role_hypothesis(state, role, actor_id, 0.72, event.id, "公开身份声明")
            _adjust_player_score(state, actor_id, trust_delta=0.08, wolf_delta=-0.04, event_id=event.id, reason=f"公开声称 {role}")

    for target_id, result in _extract_inspection_claims(compact):
        claim = {
            "event_id": event.id,
            "day": event.day,
            "player_id": actor_id,
            "claim_role": "seer",
            "target_id": target_id,
            "claim_result": result,
            "speech_brief": _short(speech, 160),
        }
        _append_limited(state, "claims", claim, limit=18)
        _upsert_role_hypothesis(state, "seer", actor_id, 0.78, event.id, "公开报出查验结果")
        if result == "werewolf":
            _adjust_player_score(state, target_id, wolf_delta=0.25, trust_delta=-0.18, event_id=event.id, reason=f"P{actor_id} 公开报查杀")
        elif result == "villagers":
            _adjust_player_score(state, target_id, wolf_delta=-0.15, trust_delta=0.18, event_id=event.id, reason=f"P{actor_id} 公开报金水")

    for target_id in _mentioned_players_after_keywords(compact, ("重点怀疑", "狼面", "抗推", "投")):
        if target_id != state.get("player_id"):
            _adjust_player_score(state, target_id, wolf_delta=0.04, trust_delta=-0.03, event_id=event.id, reason=f"P{actor_id} 公开施压")


def _record_self_commitments(state: dict[str, Any], event: GameEvent, speech: str) -> None:
    commitments: list[str] = []
    if "后续查验计划" in speech or "下晚" in speech or "下一个晚上" in speech:
        commitments.append(f"D{event.day} 公开过后续查验/行动计划：{_short(speech, 120)}")
    for target_id in _extract_player_ids(speech):
        if any(keyword in speech for keyword in ("重点怀疑", "优先投", "归票", "投给", "查验")):
            commitments.append(f"D{event.day} 公开提到会重点围绕 P{target_id} 行动或判断。")
    if event.decision and event.decision.reason:
        state.setdefault("current_plan", {})["speech_intent"] = _short(event.decision.reason, 100)
    for item in commitments[:3]:
        _append_limited(state, "self_commitments", {"event_id": event.id, "text": item}, limit=12)
    metadata = getattr(event.decision, "metadata", {}) if event.decision else {}
    deception = metadata.get("deception_intent")
    cover = metadata.get("public_cover_story")
    if deception or cover:
        state.setdefault("private_notes", {}).setdefault("cover_identity", {})["last_deception_intent"] = deception
        state.setdefault("private_notes", {}).setdefault("cover_identity", {})["last_cover_story"] = cover


def _update_social_belief_from_speech(state: dict[str, Any], event: GameEvent, actor_id: int, speech: str) -> None:
    compact = re.sub(r"\s+", "", speech)
    mentioned = [target for target in _extract_player_ids(speech) if target != actor_id]
    if not mentioned:
        return
    suspect_keywords = (
        "怀疑",
        "可疑",
        "狼面",
        "狼坑",
        "抗推",
        "查杀",
        "投",
        "问题",
        "矛盾",
        "站不住",
        "不做好",
        "踩",
        "suspect",
        "wolf",
    )
    trust_keywords = (
        "金水",
        "好人",
        "可信",
        "认好",
        "站边",
        "相信",
        "保",
        "铁好",
        "confirmed",
        "trust",
    )
    if any(keyword in compact for keyword in suspect_keywords):
        for target_id in mentioned[:4]:
            _record_social_relation(
                state,
                actor_id,
                "suspects",
                target_id,
                event_id=event.id,
                day=event.day,
                delta=0.18,
                reason=_short(speech, 80),
            )
    if any(keyword in compact for keyword in trust_keywords):
        for target_id in mentioned[:4]:
            _record_social_relation(
                state,
                actor_id,
                "trusts",
                target_id,
                event_id=event.id,
                day=event.day,
                delta=0.16,
                reason=_short(speech, 80),
            )


def _update_social_belief_from_vote(
    state: dict[str, Any],
    event: GameEvent,
    voter_id: int,
    target_id: int | None,
    abstain: bool,
) -> None:
    speaker = state.setdefault("social_belief_matrix", {}).setdefault(
        str(voter_id),
        {"speaker_id": voter_id, "suspects": {}, "trusts": {}, "votes": []},
    )
    votes = speaker.setdefault("votes", [])
    votes.append(
        {
            "event_id": event.id,
            "day": event.day,
            "target_id": target_id,
            "abstain": abstain,
        }
    )
    del votes[:-6]
    if target_id is not None and not abstain:
        _record_social_relation(
            state,
            voter_id,
            "suspects",
            int(target_id),
            event_id=event.id,
            day=event.day,
            delta=0.24,
            reason=f"vote P{target_id}",
        )


def _record_social_relation(
    state: dict[str, Any],
    speaker_id: int,
    relation_key: str,
    target_id: int,
    *,
    event_id: int,
    day: int,
    delta: float,
    reason: str,
) -> None:
    if speaker_id == target_id:
        return
    speaker = state.setdefault("social_belief_matrix", {}).setdefault(
        str(speaker_id),
        {"speaker_id": speaker_id, "suspects": {}, "trusts": {}, "votes": []},
    )
    bucket = speaker.setdefault(relation_key, {})
    entry = bucket.setdefault(
        str(target_id),
        {"target_id": target_id, "score": 0.0, "reason": "", "evidence_event_ids": [], "last_day": day},
    )
    entry["score"] = _clamp(float(entry.get("score", 0.0)) + delta)
    entry["reason"] = reason
    entry["last_day"] = day
    evidence = entry.setdefault("evidence_event_ids", [])
    if event_id not in evidence:
        evidence.append(event_id)
    del evidence[:-5]


def _extract_inspection_claims(compact_speech: str) -> list[tuple[int, str]]:
    results: list[tuple[int, str]] = []
    gap = r"[^，。；;！？!?]{0,28}"
    for pattern in (
        rf"查验P?(\d+)(?:{gap})(狼人|查杀|金水|好人)",
        rf"验P?(\d+)(?:{gap})(狼人|查杀|金水|好人)",
        rf"P?(\d+)(?:{gap})(狼人|查杀|金水|好人)",
    ):
        for match in re.finditer(pattern, compact_speech):
            target = int(match.group(1))
            raw = match.group(2)
            if _is_negated_check_result(compact_speech, match.start(2)):
                continue
            result = "werewolf" if raw in {"狼人", "查杀"} else "villagers"
            if (target, result) not in results:
                results.append((target, result))
    return results[:4]


def _is_negated_check_result(compact_speech: str, result_start: int) -> bool:
    prefix = compact_speech[max(0, result_start - 10) : result_start]
    return any(token in prefix for token in ("没有", "没", "无", "未", "不是", "并非", "非", "暂无", "还没"))


def _mentioned_players_after_keywords(compact_speech: str, keywords: tuple[str, ...]) -> list[int]:
    results: list[int] = []
    for keyword in keywords:
        for match in re.finditer(re.escape(keyword) + r".{0,12}?P?(\d+)", compact_speech):
            target = int(match.group(1))
            if target not in results:
                results.append(target)
    return results[:5]


def _extract_player_ids(text: str) -> list[int]:
    results: list[int] = []
    for match in re.finditer(r"P(\d+)", text):
        player_id = int(match.group(1))
        if player_id not in results:
            results.append(player_id)
    return results


def _adjust_player_score(
    state: dict[str, Any],
    player_id: int | None,
    *,
    wolf_delta: float = 0.0,
    trust_delta: float = 0.0,
    event_id: int,
    reason: str,
) -> None:
    if player_id is None:
        return
    entry = state.setdefault("suspicions", {}).setdefault(
        str(player_id),
        {"wolf_score": 0.5, "trust_score": 0.5, "reason": "", "evidence_event_ids": []},
    )
    entry["wolf_score"] = _clamp(float(entry.get("wolf_score", 0.5)) + wolf_delta)
    entry["trust_score"] = _clamp(float(entry.get("trust_score", 0.5)) + trust_delta)
    entry["reason"] = reason
    evidence = entry.setdefault("evidence_event_ids", [])
    if event_id not in evidence:
        evidence.append(event_id)
    del evidence[:-6]


def _social_belief_matrix_view(matrix: dict[str, Any], *, max_items: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for speaker_id, payload in (matrix or {}).items():
        if not isinstance(payload, dict):
            continue
        suspects = _relation_items(payload.get("suspects", {}), max_items=3)
        trusts = _relation_items(payload.get("trusts", {}), max_items=3)
        votes = list(payload.get("votes", []) or [])[-3:]
        if not suspects and not trusts and not votes:
            continue
        rows.append(
            {
                "speaker_id": int(payload.get("speaker_id") or speaker_id),
                "public_suspects": suspects,
                "public_trusts": trusts,
                "recent_votes": votes,
            }
        )
    rows.sort(key=lambda row: (len(row["public_suspects"]) + len(row["public_trusts"]) + len(row["recent_votes"])), reverse=True)
    return rows[:max_items]


def _relation_items(values: dict[str, Any], *, max_items: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for target_id, payload in (values or {}).items():
        if not isinstance(payload, dict):
            continue
        items.append(
            {
                "target_id": int(payload.get("target_id") or target_id),
                "score": round(float(payload.get("score", 0.0)), 2),
                "reason": payload.get("reason", ""),
                "evidence_event_ids": list(payload.get("evidence_event_ids", []) or [])[-3:],
            }
        )
    items.sort(key=lambda item: item["score"], reverse=True)
    return items[:max_items]


def _upsert_role_hypothesis(
    state: dict[str, Any],
    role: str,
    player_id: int,
    confidence: float,
    event_id: int,
    reason: str,
) -> None:
    items = state.setdefault("role_hypotheses", {}).setdefault(role, [])
    for item in items:
        if item.get("player_id") == player_id:
            item["confidence"] = max(float(item.get("confidence", 0.0)), confidence)
            item["reason"] = reason
            evidence = item.setdefault("evidence_event_ids", [])
            if event_id not in evidence:
                evidence.append(event_id)
            del evidence[:-6]
            return
    items.append(
        {
            "player_id": player_id,
            "confidence": _clamp(confidence),
            "reason": reason,
            "evidence_event_ids": [event_id],
        }
    )


def _append_limited(state: dict[str, Any], key: str, item: dict[str, Any], *, limit: int = 12) -> None:
    values = state.setdefault(key, [])
    values.append(to_jsonable(item))
    del values[:-limit]


def _trim_state(state: dict[str, Any]) -> None:
    for key, limit in (
        ("claims", 18),
        ("vote_memory", 24),
        ("night_memory", 16),
        ("public_notes", 18),
        ("self_commitments", 12),
    ):
        values = state.get(key)
        if isinstance(values, list):
            del values[:-limit]
    for items in (state.get("role_hypotheses") or {}).values():
        if isinstance(items, list):
            items.sort(key=lambda item: item.get("confidence", 0.0), reverse=True)
            del items[6:]


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, round(value, 3)))


def _short(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]
