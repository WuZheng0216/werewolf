from __future__ import annotations

import hashlib
from typing import Any

from .evaluator import aggregate_reports
from .models import EvaluationReport, Role


SKILL_EVOLUTION_MODE = "skill"
WORKFLOW_EVOLUTION_MODE = "workflow"
EVOLUTION_MODES = {WORKFLOW_EVOLUTION_MODE, SKILL_EVOLUTION_MODE}


def normalize_evolution_mode(value: str | None) -> str:
    mode = (value or WORKFLOW_EVOLUTION_MODE).strip().lower().replace("-", "_")
    aliases = {
        "default": WORKFLOW_EVOLUTION_MODE,
        "memory": WORKFLOW_EVOLUTION_MODE,
        "memory_workflow": WORKFLOW_EVOLUTION_MODE,
        "skill_evolution": SKILL_EVOLUTION_MODE,
        "skills": SKILL_EVOLUTION_MODE,
        "agent_skill": SKILL_EVOLUTION_MODE,
        "agent_skills": SKILL_EVOLUTION_MODE,
    }
    mode = aliases.get(mode, mode)
    return mode if mode in EVOLUTION_MODES else WORKFLOW_EVOLUTION_MODE


def build_role_skills(
    reports: list[EvaluationReport],
    llm_reviews: list[dict[str, Any]] | None = None,
    *,
    max_skills_per_role: int = 6,
) -> dict[Role, list[dict[str, Any]]]:
    """Turn evaluator/reviewer output into compact self-evolution skill cards.

    A skill card is deliberately more procedural than plain memory: it has a
    trigger, an action policy, and an avoid list. Agents can choose whether a
    skill is relevant to the current observation instead of blindly following a
    workflow recommendation.
    """

    aggregate = aggregate_reports(reports)
    mistake_counts = aggregate.get("mistake_counts", {})
    skills: dict[Role, list[dict[str, Any]]] = {role: [] for role in Role}

    for mistake_type, count in sorted(mistake_counts.items(), key=lambda item: (-int(item[1]), item[0])):
        card = _skill_for_mistake(mistake_type, int(count))
        if card:
            skills[card["role"]].append(card)

    for role_value, memories in _review_memories_by_role(llm_reviews or []).items():
        role = Role(role_value)
        for memory in memories[:4]:
            skills[role].append(_review_memory_skill(role, memory))

    return {
        role: _dedupe_skill_cards(items)[:max_skills_per_role]
        for role, items in skills.items()
    }


def merge_skill_cards(
    existing: list[dict[str, Any]] | None,
    learned: list[dict[str, Any]],
    *,
    max_items: int = 8,
) -> list[dict[str, Any]]:
    merged = _dedupe_skill_cards(list(existing or []) + list(learned or []))
    return merged[:max_items]


def evolve_skill_cards(
    existing: list[dict[str, Any]] | None,
    learned: list[dict[str, Any]],
    *,
    max_items: int = 8,
    version_id: str | None = None,
) -> dict[str, Any]:
    """Evolve role skill cards with auditable add/refine operations."""

    skills_by_key: dict[str, dict[str, Any]] = {}
    operations: list[dict[str, Any]] = []

    for card in existing or []:
        if not isinstance(card, dict):
            continue
        prepared = _ensure_skill_version(card, version_id=version_id)
        key = _skill_match_key(prepared)
        current = skills_by_key.get(key)
        if current is None or _skill_rank(prepared) > _skill_rank(current):
            skills_by_key[key] = prepared

    for card in learned or []:
        if not isinstance(card, dict):
            continue
        incoming = _ensure_skill_version(card, version_id=version_id, learned=True)
        key = _skill_match_key(incoming)
        current = skills_by_key.get(key)
        if current is None:
            added = _activate_new_skill(incoming, version_id=version_id)
            skills_by_key[key] = added
            operations.append(_skill_operation("add", added, reason="new_replay_pattern"))
            continue

        if _should_refine_skill(current, incoming, version_id=version_id):
            refined = _refine_skill(current, incoming, version_id=version_id)
            skills_by_key[key] = refined
            operations.append(
                _skill_operation(
                    "refine",
                    refined,
                    previous=current,
                    reason=_refine_reason(current, incoming),
                )
            )
        else:
            reinforced = _reinforce_skill(current, incoming, version_id=version_id)
            skills_by_key[key] = reinforced
            operations.append(
                _skill_operation(
                    "reinforce",
                    reinforced,
                    previous=current,
                    reason="same_skill_observed_again",
                )
            )

    ranked = _dedupe_skill_cards(list(skills_by_key.values()))
    active = ranked[:max_items]
    dropped = ranked[max_items:]
    for card in dropped:
        operations.append(_skill_operation("retire_candidate", card, reason="prompt_skill_budget"))

    return {
        "skills": active,
        "operations": operations,
        "dropped_skills": dropped,
        "summary": _summarize_skill_operations(operations),
    }


def format_skill_for_memory(skill: dict[str, Any]) -> str:
    title = str(skill.get("title") or skill.get("skill_id") or "未命名技能")
    trigger = str(skill.get("trigger") or "观察当前局面")
    procedure = "；".join(str(item) for item in skill.get("procedure", [])[:3])
    avoid = "；".join(str(item) for item in skill.get("avoid", [])[:2])
    return f"技能卡[{title}] 触发：{trigger}。执行：{procedure}。避免：{avoid}"


def _skill_for_mistake(mistake_type: str, count: int) -> dict[str, Any] | None:
    severity = min(1.0, 0.62 + count * 0.05)
    templates: dict[str, dict[str, Any]] = {
        "witch_mechanical_n1_save": {
            "role": Role.WITCH,
            "title": "首夜解药价值比较",
            "trigger": "夜一看到非自己被刀，且没有公开信息能确认被刀者是关键神职或强好人。",
            "procedure": [
                "先比较救人收益、保留解药收益和后续神职保护价值。",
                "如果救人理由只是不想死人，应倾向保留或给出明确目标价值。",
                "reason 中必须写明为什么当前目标值得救，或为什么选择不救。",
            ],
            "avoid": ["把首夜救人当成固定动作。", "忽略保留解药保护已跳神职的价值。"],
            "tags": ["witch", "antidote", "timing"],
        },
        "seer_mechanical_day1_claim": {
            "role": Role.SEER,
            "title": "预言家公开身份时机判断",
            "trigger": "首日只有金水或信息不足，且没有被强推、没有对跳、没有查杀。",
            "procedure": [
                "比较公开身份和隐忍一轮的收益。",
                "可以软铺垫站边与后续查验计划，而不是直接跳明。",
                "查到狼、被强质疑、出现对跳或需要带队时再公开身份。",
            ],
            "avoid": ["无查杀时机械首日起跳。", "公开后不给清晰后续查验计划。"],
            "tags": ["seer", "claim_timing"],
        },
        "werewolf_exposed_pack_vote": {
            "role": Role.WEREWOLF,
            "title": "狼队投票分线",
            "trigger": "白天投票前发现多名狼队友想压同一好人目标，且公开证据不足。",
            "procedure": [
                "至少一名狼人分票、弃票、倒钩或轻踩队友。",
                "每个投票理由必须来自不同公开事件。",
                "投票前检查是否会形成三狼同票同理由。",
            ],
            "avoid": ["三狼裸冲同一目标。", "复制队友发言理由。"],
            "tags": ["werewolf", "vote", "split"],
        },
        "werewolf_incomplete_fake_seer_claim": {
            "role": Role.WEREWOLF,
            "title": "悍跳预言家完整链",
            "trigger": "决定悍跳或对跳预言家时。",
            "procedure": [
                "同时给出查验对象、查验结论和下一晚查验计划。",
                "用公开发言或票型包装为什么这么验。",
                "准备被追问时的后续叙事，不要只喊身份。",
            ],
            "avoid": ["只说自己是预言家。", "编造不存在的公开事件。"],
            "tags": ["werewolf", "fake_seer", "deception"],
        },
        "werewolf_low_deception_speech": {
            "role": Role.WEREWOLF,
            "title": "公开欺骗意图选择",
            "trigger": "白天发言时没有明确攻击目标或伪装路线。",
            "procedure": [
                "从隐藏、软对跳、倒钩、分票、拉拢或抗推中选择一个意图。",
                "用当前 public_history 包装理由。",
                "保持座位人格差异，避免模板化好人发言。",
            ],
            "avoid": ["只重复我是好人。", "暴露狼队私有信息。"],
            "tags": ["werewolf", "speech", "deception"],
        },
        "seer_failed_to_reveal_wolf": {
            "role": Role.SEER,
            "title": "查杀信息传递",
            "trigger": "夜间查到狼人且目标仍存活。",
            "procedure": [
                "白天明确报出 P 号、查验轮次和查杀结论。",
                "给出归票建议和后续查验计划。",
                "被质疑时区分查验事实和公开推理。",
            ],
            "avoid": ["查到狼却只模糊暗示。", "把推理说成查验。"],
            "tags": ["seer", "wolf_check"],
        },
        "villager_ignored_confirmed_wolf": {
            "role": Role.VILLAGER,
            "title": "公开查杀优先级",
            "trigger": "场上存在可信预言家公开查杀，且查杀目标未出局。",
            "procedure": [
                "优先围绕查杀目标归票。",
                "如果不跟查杀，必须引用公开证据说明为什么查杀不可信。",
                "避免低依据散票。",
            ],
            "avoid": ["无理由投向另一个好人。", "忽略可验证查验链。"],
            "tags": ["villager", "vote"],
        },
        "witch_poisoned_good": {
            "role": Role.WITCH,
            "title": "毒药硬证据门槛",
            "trigger": "考虑使用毒药但没有公开查杀或多轮强狼面证据。",
            "procedure": [
                "检查目标是否有公开查杀、票型异常或多轮发言矛盾。",
                "证据不足时保留毒药。",
                "毒人理由必须引用公开事件。",
            ],
            "avoid": ["凭单点怀疑撒毒。", "毒掉强好人或明神。"],
            "tags": ["witch", "poison"],
        },
        "hunter_shot_good": {
            "role": Role.HUNTER,
            "title": "猎人开枪置信度",
            "trigger": "死亡后可开枪但没有公开查杀或高置信目标。",
            "procedure": [
                "优先选择公开查杀或多轮票型最高嫌疑。",
                "置信度不足时可以不开枪。",
                "开枪理由只引用公开证据。",
            ],
            "avoid": ["情绪化带走低证据目标。", "为了开枪而开枪。"],
            "tags": ["hunter", "shot"],
        },
    }
    base = templates.get(mistake_type)
    if not base:
        return None
    return _card(
        role=base["role"],
        title=base["title"],
        trigger=base["trigger"],
        procedure=base["procedure"],
        avoid=base["avoid"],
        tags=base["tags"],
        source="mistake",
        source_type=mistake_type,
        confidence=round(severity, 2),
        support_count=count,
    )


def _review_memory_skill(role: Role, memory: str) -> dict[str, Any]:
    return _card(
        role=role,
        title="复盘经验转技能",
        trigger="当前局面与这条复盘经验相似时。",
        procedure=[
            memory,
            "先判断触发条件是否真的成立，再决定是否采用。",
            "如果当前公开信息不足，应保守使用该经验。",
        ],
        avoid=["把经验当成强制命令。", "忽略当前座位人格和公开历史。"],
        tags=[role.value, "review_memory"],
        source="llm_review",
        source_type="memory_candidate",
        confidence=0.78,
        support_count=1,
    )


def _card(
    *,
    role: Role,
    title: str,
    trigger: str,
    procedure: list[str],
    avoid: list[str],
    tags: list[str],
    source: str,
    source_type: str,
    confidence: float,
    support_count: int,
) -> dict[str, Any]:
    raw_id = f"{role.value}:{title}:{trigger}:{source_type}"
    digest = hashlib.sha1(raw_id.encode("utf-8")).hexdigest()[:10]
    return {
        "skill_id": f"{role.value}_skill_{digest}",
        "role": role.value,
        "title": title,
        "trigger": trigger,
        "procedure": list(procedure),
        "avoid": list(avoid),
        "tags": list(tags),
        "source": source,
        "source_type": source_type,
        "confidence": round(max(0.0, min(1.0, confidence)), 2),
        "support_count": int(support_count),
    }


def _ensure_skill_version(
    skill: dict[str, Any],
    *,
    version_id: str | None = None,
    learned: bool = False,
) -> dict[str, Any]:
    card = dict(skill)
    base_id = _base_skill_id(card)
    card["base_skill_id"] = base_id
    card.setdefault("skill_id", base_id)
    card["skill_version"] = _skill_version_int(card)
    card.setdefault("status", "active")
    card.setdefault("evolution_history", [])
    if version_id:
        key = "learned_in_version_id" if learned else "loaded_in_version_id"
        card.setdefault(key, version_id)
    return card


def _activate_new_skill(skill: dict[str, Any], *, version_id: str | None) -> dict[str, Any]:
    card = dict(skill)
    card["skill_version"] = 1
    card["base_skill_id"] = _base_skill_id(card)
    card["status"] = "active"
    card["evolution_operation"] = "add"
    if version_id:
        card["created_in_version_id"] = version_id
        card["last_evolved_version_id"] = version_id
    return card


def _should_refine_skill(
    current: dict[str, Any],
    incoming: dict[str, Any],
    *,
    version_id: str | None,
) -> bool:
    if version_id and current.get("last_evolved_version_id") == version_id:
        return False
    source_type = str(incoming.get("source_type") or "")
    if source_type == "memory_candidate":
        return _support_count(incoming) > _support_count(current)
    if source_type:
        return True
    return _confidence(incoming) > _confidence(current) + 0.03


def _refine_skill(
    current: dict[str, Any],
    incoming: dict[str, Any],
    *,
    version_id: str | None,
) -> dict[str, Any]:
    parent_id = str(current.get("skill_id") or "")
    base_id = _base_skill_id(current)
    next_version = _skill_version_int(current) + 1
    card = dict(incoming)
    card["skill_id"] = f"{base_id}_v{next_version}"
    card["base_skill_id"] = base_id
    card["parent_skill_id"] = parent_id
    card["skill_version"] = next_version
    card["status"] = "active"
    card["confidence"] = round(min(1.0, max(_confidence(current), _confidence(incoming)) + 0.03), 2)
    card["support_count"] = _support_count(current) + max(1, _support_count(incoming))
    card["procedure"] = _dedupe_text(
        list(incoming.get("procedure") or [])
        + list(current.get("procedure") or [])
        + ["同类问题再次出现时，先确认触发条件、公开证据和座位人格，再决定是否调用本技能。"]
    )[:6]
    card["avoid"] = _dedupe_text(list(incoming.get("avoid") or []) + list(current.get("avoid") or []))[:5]
    card["tags"] = _dedupe_text([str(item) for item in incoming.get("tags") or []] + [str(item) for item in current.get("tags") or []] + ["refined"])
    history = list(current.get("evolution_history") or [])
    history.append(_skill_snapshot(current))
    card["evolution_history"] = history[-5:]
    card["evolution_operation"] = "refine"
    card["evolution_reason"] = _refine_reason(current, incoming)
    if version_id:
        card["last_evolved_version_id"] = version_id
    return card


def _reinforce_skill(
    current: dict[str, Any],
    incoming: dict[str, Any],
    *,
    version_id: str | None,
) -> dict[str, Any]:
    card = dict(current)
    card["support_count"] = _support_count(current) + max(1, _support_count(incoming))
    card["confidence"] = round(min(1.0, max(_confidence(current), _confidence(incoming)) + 0.01), 2)
    card["procedure"] = _dedupe_text(list(current.get("procedure") or []) + list(incoming.get("procedure") or []))[:6]
    card["avoid"] = _dedupe_text(list(current.get("avoid") or []) + list(incoming.get("avoid") or []))[:5]
    card["tags"] = _dedupe_text([str(item) for item in current.get("tags") or []] + [str(item) for item in incoming.get("tags") or []] + ["reinforced"])
    card["evolution_operation"] = "reinforce"
    if version_id:
        card["last_reinforced_version_id"] = version_id
    return card


def _skill_operation(
    operation: str,
    skill: dict[str, Any],
    *,
    reason: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "operation": operation,
        "reason": reason,
        "skill_id": str(skill.get("skill_id") or ""),
        "base_skill_id": str(skill.get("base_skill_id") or skill.get("skill_id") or ""),
        "parent_skill_id": str((previous or {}).get("skill_id") or skill.get("parent_skill_id") or ""),
        "skill_version": _skill_version_int(skill),
        "role": str(skill.get("role") or ""),
        "title": str(skill.get("title") or skill.get("skill_id") or ""),
        "source": str(skill.get("source") or ""),
        "source_type": str(skill.get("source_type") or ""),
        "support_count": _support_count(skill),
        "confidence": _confidence(skill),
    }
    if previous:
        payload["previous_version"] = _skill_version_int(previous)
        payload["previous_confidence"] = _confidence(previous)
        payload["previous_support_count"] = _support_count(previous)
    return payload


def _summarize_skill_operations(operations: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for operation in operations:
        name = str(operation.get("operation") or "unknown")
        counts[name] = counts.get(name, 0) + 1
    return {
        "total": len(operations),
        "counts": counts,
        "changed": counts.get("add", 0) + counts.get("refine", 0) + counts.get("reinforce", 0),
    }


def _skill_match_key(skill: dict[str, Any]) -> str:
    role = str(skill.get("role") or "")
    source_type = str(skill.get("source_type") or "").strip()
    if source_type and source_type != "memory_candidate":
        return f"{role}:source_type:{source_type}"
    if source_type == "memory_candidate":
        primary = _primary_procedure_text(skill) or str(skill.get("trigger") or "")
        digest = hashlib.sha1(primary.encode("utf-8")).hexdigest()[:10]
        return f"{role}:memory:{digest}"
    title = _normalized_text(skill.get("title"))
    trigger = _normalized_text(skill.get("trigger"))
    return f"{role}:title:{title}:trigger:{trigger[:80]}"


def _skill_rank(skill: dict[str, Any]) -> tuple[float, int, int, str]:
    return (
        _confidence(skill),
        _support_count(skill),
        _skill_version_int(skill),
        str(skill.get("skill_id") or ""),
    )


def _base_skill_id(skill: dict[str, Any]) -> str:
    existing = str(skill.get("base_skill_id") or skill.get("skill_id") or "").strip()
    if existing:
        return existing
    raw = f"{skill.get('role')}:{skill.get('title')}:{skill.get('trigger')}:{skill.get('source_type')}"
    return f"skill_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:10]}"


def _skill_version_int(skill: dict[str, Any]) -> int:
    try:
        return max(1, int(skill.get("skill_version") or 1))
    except (TypeError, ValueError):
        return 1


def _support_count(skill: dict[str, Any]) -> int:
    try:
        return max(0, int(skill.get("support_count") or 0))
    except (TypeError, ValueError):
        return 0


def _confidence(skill: dict[str, Any]) -> float:
    return _bounded_float(skill.get("confidence"), default=0.5)


def _refine_reason(current: dict[str, Any], incoming: dict[str, Any]) -> str:
    if str(incoming.get("source_type") or ""):
        return "repeated_same_failure_or_pattern"
    if _confidence(incoming) > _confidence(current):
        return "higher_confidence_candidate"
    return "newer_candidate_supersedes_parent"


def _skill_snapshot(skill: dict[str, Any]) -> dict[str, Any]:
    return {
        "skill_id": str(skill.get("skill_id") or ""),
        "skill_version": _skill_version_int(skill),
        "title": str(skill.get("title") or ""),
        "source_type": str(skill.get("source_type") or ""),
        "support_count": _support_count(skill),
        "confidence": _confidence(skill),
    }


def _primary_procedure_text(skill: dict[str, Any]) -> str:
    for item in skill.get("procedure") or []:
        text = str(item or "").strip()
        if text:
            return text
    return ""


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _review_memories_by_role(reviews: list[dict[str, Any]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {role.value: [] for role in Role}
    for review in reviews:
        if review.get("review_status") != "ok":
            continue
        for item in review.get("memory_candidates", []) or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            memory = str(item.get("memory") or "").strip()
            confidence = _bounded_float(item.get("confidence"), default=0.0)
            if role in grouped and memory and confidence >= 0.75:
                grouped[role].append(memory)
    return {role: _dedupe_text(items)[:6] for role, items in grouped.items()}


def _dedupe_skill_cards(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in sorted(
        items,
        key=lambda card: (
            float(card.get("confidence", 0.0)),
            int(card.get("support_count", 0)),
            str(card.get("skill_id", "")),
        ),
        reverse=True,
    ):
        skill_id = str(item.get("skill_id") or "")
        key = skill_id or f"{item.get('role')}:{item.get('title')}:{item.get('trigger')}"
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(item))
    return result


def _dedupe_text(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _bounded_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(1.0, parsed))
