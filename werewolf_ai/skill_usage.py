from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from .models import ActionType, AgentDecision, GameEvent, Phase, Role


MAX_MATCHED_SKILLS = 4
DEFAULT_RECALLED_SKILLS = 3


def select_relevant_skills(
    *,
    observation: Any,
    role_skills: list[dict[str, Any]] | None,
    max_items: int = DEFAULT_RECALLED_SKILLS,
) -> list[dict[str, Any]]:
    """Recall the most relevant skill cards for the current observation.

    This is intentionally lightweight and deterministic: it narrows the skill
    library before prompting, while the LLM still decides whether to use a
    recalled skill in its final action.
    """

    skills = [skill for skill in role_skills or [] if isinstance(skill, dict)]
    if not skills or max_items <= 0:
        return []

    query = _skill_recall_query(observation)
    query_tokens = _tokens(query)
    scored: list[tuple[float, float, int, int, dict[str, Any]]] = []
    for index, skill in enumerate(skills):
        skill_tokens = _tokens(_skill_text(skill))
        overlap = _recall_overlap(query_tokens, skill_tokens)
        trigger = _trigger_match_score(skill, observation, query)
        confidence = _bounded_score(skill.get("confidence"), default=0.5)
        support = int(skill.get("support_count") or 0)
        score = overlap * 0.55 + trigger * 0.35 + confidence * 0.08 + min(0.05, support * 0.01)
        recalled = dict(skill)
        recalled["recall_score"] = round(score, 4)
        recalled["recall_signals"] = _recall_signals(overlap, trigger, confidence, support)
        scored.append((score, confidence, support, -index, recalled))

    scored.sort(key=lambda item: item[:4], reverse=True)
    selected = [item[-1] for item in scored[:max_items]]
    if selected and all(float(item.get("recall_score", 0.0)) <= 0.08 for item in selected):
        for item in selected:
            item.setdefault("recall_signals", []).append("fallback_top_confidence")
    return selected


def estimate_skill_usage(
    decision: AgentDecision,
    *,
    phase: Phase,
    role_skills: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Estimate which visible skill cards the agent appears to have used.

    This is intentionally a passive, post-decision heuristic. It does not add
    fields to the LLM output schema and does not ask the model to self-report.
    """

    skills = [skill for skill in role_skills or [] if isinstance(skill, dict)]
    usage: dict[str, Any] = {
        "mode": "passive_heuristic",
        "available_count": len(skills),
        "matched_count": 0,
        "matched_skills": [],
    }
    if not skills:
        return usage

    matches: list[dict[str, Any]] = []
    for skill in skills:
        match = _match_skill(skill, decision, phase)
        if match["score"] > 0:
            matches.append(match)

    matches.sort(key=lambda item: (float(item["score"]), item["title"], item["skill_id"]), reverse=True)
    usage["matched_skills"] = matches[:MAX_MATCHED_SKILLS]
    usage["matched_count"] = len(matches)
    return usage


def summarize_skill_usage(events: list[GameEvent]) -> dict[str, Any]:
    by_role: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "decisions": 0,
            "decisions_with_available_skills": 0,
            "decisions_with_matched_skills": 0,
            "matched_skill_uses": 0,
            "skills": Counter(),
        }
    )
    total = {
        "decisions": 0,
        "decisions_with_available_skills": 0,
        "decisions_with_matched_skills": 0,
        "matched_skill_uses": 0,
    }

    for event in events:
        decision = event.decision
        if not decision:
            continue
        usage = (decision.metadata or {}).get("skill_usage") or {}
        if not usage:
            continue
        role = decision.role.value
        bucket = by_role[role]
        bucket["decisions"] += 1
        total["decisions"] += 1
        available = int(usage.get("available_count") or 0)
        matched = int(usage.get("matched_count") or 0)
        if available:
            bucket["decisions_with_available_skills"] += 1
            total["decisions_with_available_skills"] += 1
        if matched:
            bucket["decisions_with_matched_skills"] += 1
            total["decisions_with_matched_skills"] += 1
        matched_skills = usage.get("matched_skills") or []
        bucket["matched_skill_uses"] += len(matched_skills)
        total["matched_skill_uses"] += len(matched_skills)
        for skill in matched_skills:
            title = str(skill.get("title") or skill.get("skill_id") or "skill")
            bucket["skills"][title] += 1

    return {
        "mode": "passive_heuristic",
        "total": total,
        "by_role": {
            role: {
                **{key: value for key, value in bucket.items() if key != "skills"},
                "skills": [
                    {"title": title, "count": count}
                    for title, count in bucket["skills"].most_common()
                ],
            }
            for role, bucket in sorted(by_role.items())
        },
    }


def _match_skill(skill: dict[str, Any], decision: AgentDecision, phase: Phase) -> dict[str, Any]:
    source_type = str(skill.get("source_type") or "")
    text = _decision_text(decision)
    signals: list[str] = []
    score = 0.0

    specialized = _specialized_match(source_type, decision, phase, text)
    score += specialized["score"]
    signals.extend(specialized["signals"])

    overlap = _lexical_overlap(_skill_text(skill), text)
    if overlap >= 0.12:
        score += min(0.3, overlap)
        signals.append(f"lexical_overlap={overlap:.2f}")

    if not signals:
        return {
            "skill_id": str(skill.get("skill_id") or ""),
            "title": str(skill.get("title") or skill.get("skill_id") or "skill"),
            "source_type": source_type,
            "score": 0.0,
            "signals": [],
        }

    return {
        "skill_id": str(skill.get("skill_id") or ""),
        "title": str(skill.get("title") or skill.get("skill_id") or "skill"),
        "source_type": source_type,
        "score": round(min(1.0, score), 2),
        "signals": signals[:5],
    }


def _specialized_match(source_type: str, decision: AgentDecision, phase: Phase, text: str) -> dict[str, Any]:
    action = decision.action
    role = decision.role
    signals: list[str] = []
    score = 0.0

    if source_type == "werewolf_incomplete_fake_seer_claim" and role == Role.WEREWOLF and action == ActionType.SPEAK:
        if _has_any(text, ("预言家", "查验", "查杀", "金水")):
            score += 0.35
            signals.append("werewolf_fake_seer_claim_terms")
        if _has_any(text, ("后续", "计划", "下一晚", "今晚验", "明晚验")):
            score += 0.25
            signals.append("seer_chain_plan")

    elif source_type == "werewolf_exposed_pack_vote" and role == Role.WEREWOLF and action in {ActionType.VOTE, ActionType.PASS}:
        if action == ActionType.PASS:
            score += 0.3
            signals.append("vote_abstain_or_split")
        if _has_any(text, ("分票", "倒钩", "切割", "公共逻辑", "好人视角", "独立", "票型")):
            score += 0.35
            signals.append("wolf_vote_split_or_cover")
        intent = str((decision.metadata or {}).get("deception_intent") or "")
        if intent in {"split_vote", "bus_teammate"}:
            score += 0.35
            signals.append(f"deception_intent={intent}")

    elif source_type == "werewolf_low_deception_speech" and role == Role.WEREWOLF and action == ActionType.SPEAK:
        intent = str((decision.metadata or {}).get("deception_intent") or "")
        if intent and intent != "none":
            score += 0.35
            signals.append(f"deception_intent={intent}")
        if _has_any(text, ("质疑", "站边", "倒钩", "抗推", "拉拢", "软对跳", "伪装")):
            score += 0.3
            signals.append("public_deception_terms")

    elif source_type == "witch_mechanical_n1_save" and role == Role.WITCH and phase == Phase.NIGHT_WITCH:
        if action in {ActionType.SAVE, ActionType.PASS} and _has_any(text, ("解药", "救", "保留", "价值", "收益", "自救", "关键神职")):
            score += 0.55
            signals.append("witch_antidote_value_reasoning")

    elif source_type == "witch_poisoned_good" and role == Role.WITCH and phase == Phase.NIGHT_WITCH:
        if action in {ActionType.POISON, ActionType.PASS} and _has_any(text, ("毒", "查杀", "票型", "证据", "狼面", "保留")):
            score += 0.55
            signals.append("witch_poison_evidence_reasoning")

    elif source_type == "seer_failed_to_reveal_wolf" and role == Role.SEER and action == ActionType.SPEAK:
        if _has_any(text, ("查杀", "查验结果", "狼人", "归票", "后续查验", "验人")):
            score += 0.55
            signals.append("seer_revealed_check_chain")

    elif source_type == "villager_ignored_confirmed_wolf" and role == Role.VILLAGER and action in {ActionType.SPEAK, ActionType.VOTE}:
        if _has_any(text, ("查杀", "预言家", "归票", "站边", "可信")):
            score += 0.5
            signals.append("villager_followed_public_check")

    elif source_type == "hunter_shot_good" and role == Role.HUNTER and phase == Phase.HUNTER_SHOT:
        if _has_any(text, ("查杀", "票型", "置信", "不开枪", "开枪", "证据")):
            score += 0.55
            signals.append("hunter_shot_confidence_reasoning")

    elif source_type == "memory_candidate":
        if _has_any(text, ("权衡", "计划", "查验", "票型", "证据", "倒钩", "切割", "保留", "查杀", "站边")):
            score += 0.2
            signals.append("review_memory_style_match")

    return {"score": score, "signals": signals}


def _decision_text(decision: AgentDecision) -> str:
    metadata = decision.metadata or {}
    raw = metadata.get("raw_decision") if isinstance(metadata.get("raw_decision"), dict) else {}
    parts = [
        decision.action.value,
        str(decision.target_id or ""),
        decision.speech,
        decision.reason,
        str(metadata.get("deception_intent") or raw.get("deception_intent") or ""),
        str(metadata.get("public_cover_story") or raw.get("public_cover_story") or ""),
        " ".join(str(item) for item in metadata.get("quoted_evidence") or raw.get("quoted_evidence") or []),
    ]
    return " ".join(part for part in parts if part).lower()


def _skill_text(skill: dict[str, Any]) -> str:
    values = [
        skill.get("title"),
        skill.get("trigger"),
        " ".join(str(item) for item in skill.get("procedure") or []),
        " ".join(str(item) for item in skill.get("avoid") or []),
        " ".join(str(item) for item in skill.get("tags") or []),
        skill.get("source_type"),
    ]
    return " ".join(str(value) for value in values if value).lower()


def _skill_recall_query(observation: Any) -> str:
    private_knowledge = getattr(observation, "private_knowledge", {}) or {}
    public_history = getattr(observation, "public_history", []) or []
    belief_state = getattr(observation, "belief_state", {}) or {}
    parts = [
        str(getattr(getattr(observation, "self_role", ""), "value", getattr(observation, "self_role", ""))),
        str(getattr(getattr(observation, "phase", ""), "value", getattr(observation, "phase", ""))),
        str(getattr(getattr(observation, "phase", ""), "zh", "")),
        str(getattr(observation, "day", "")),
        " ".join(str(action.value) for action in getattr(observation, "legal_actions", []) or []),
        " ".join(str(item) for item in private_knowledge.keys()),
        " ".join(str(item) for item in private_knowledge.get("action_context", {}).keys())
        if isinstance(private_knowledge.get("action_context"), dict)
        else "",
        " ".join(str(item.get("public_text") or item.get("speech") or item) for item in public_history[-8:] if isinstance(item, dict)),
        " ".join(str(item) for item in belief_state.get("top_suspicions", [])[:3]),
        " ".join(str(item) for item in belief_state.get("self_commitments", [])[-3:]),
    ]
    return " ".join(part for part in parts if part).lower()


def _trigger_match_score(skill: dict[str, Any], observation: Any, query: str) -> float:
    source_type = str(skill.get("source_type") or "")
    phase = getattr(observation, "phase", None)
    role = getattr(observation, "self_role", None)
    private_knowledge = getattr(observation, "private_knowledge", {}) or {}
    legal_actions = set(getattr(observation, "legal_actions", []) or [])
    score = 0.0

    if source_type.startswith("werewolf_") and role == Role.WEREWOLF:
        score += 0.25
        if phase in {Phase.DAY_SPEECH, Phase.DAY_VOTE}:
            score += 0.3
        if "wolf_strategy_space" in private_knowledge:
            score += 0.15
    elif source_type.startswith("seer_") and role == Role.SEER:
        score += 0.25
        if phase == Phase.DAY_SPEECH:
            score += 0.2
        inspections = private_knowledge.get("inspections") or {}
        if isinstance(inspections, dict) and any(str(value) == "werewolf" for value in inspections.values()):
            score += 0.35
    elif source_type.startswith("witch_") and role == Role.WITCH:
        score += 0.25
        if phase == Phase.NIGHT_WITCH:
            score += 0.25
        if private_knowledge.get("attacked_tonight") is not None:
            score += 0.2
        if ActionType.POISON in legal_actions:
            score += 0.1
    elif source_type.startswith("hunter_") and role == Role.HUNTER:
        score += 0.25
        if phase == Phase.HUNTER_SHOT:
            score += 0.35
    elif source_type.startswith("villager_") and role == Role.VILLAGER:
        score += 0.25
        if phase in {Phase.DAY_SPEECH, Phase.DAY_VOTE}:
            score += 0.2
    elif source_type == "memory_candidate":
        score += 0.12

    trigger_text = " ".join(
        str(value)
        for value in (
            skill.get("trigger"),
            " ".join(str(item) for item in skill.get("tags") or []),
            skill.get("title"),
        )
        if value
    ).lower()
    if trigger_text:
        score += min(0.25, _recall_overlap(_tokens(trigger_text), _tokens(query)))
    return max(0.0, min(1.0, score))


def _recall_overlap(left_tokens: set[str], right_tokens: set[str]) -> float:
    if not left_tokens or not right_tokens:
        return 0.0
    shared = left_tokens & right_tokens
    return len(shared) / max(1, min(len(left_tokens), len(right_tokens)))


def _bounded_score(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(1.0, parsed))


def _recall_signals(overlap: float, trigger: float, confidence: float, support: int) -> list[str]:
    signals: list[str] = []
    if overlap > 0:
        signals.append(f"lexical_overlap={overlap:.2f}")
    if trigger > 0:
        signals.append(f"trigger_match={trigger:.2f}")
    if confidence:
        signals.append(f"confidence={confidence:.2f}")
    if support:
        signals.append(f"support={support}")
    return signals


def _lexical_overlap(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    shared = left_tokens.intersection(right_tokens)
    return len(shared) / max(1, min(len(left_tokens), len(right_tokens)))


def _tokens(text: str) -> set[str]:
    words = set(re.findall(r"[a-zA-Z0-9_]+", text.lower()))
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    words.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    return {token for token in words if token.strip()}


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in text for term in terms)
