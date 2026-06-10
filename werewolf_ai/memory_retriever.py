from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime
from typing import Any

from .memory_store import UNSUPPORTED_RULE_TERMS, load_memory_bank, mechanical_memory_penalty
from .models import ActionType, Phase, Role, to_jsonable


ACTIVE_STATUSES = {"approved", "approved_auto"}
K1 = 1.5
B = 0.75

DOMAIN_TOPICS: dict[str, tuple[str, ...]] = {
    "seer_claim": ("预言家", "查验", "查杀", "金水", "对跳", "悍跳", "验人"),
    "counterclaim": ("对跳", "悍跳", "假跳", "不认", "质疑", "单预"),
    "vote": ("投票", "归票", "票型", "弃票", "分票", "冲票", "跟票"),
    "wolf_pack": ("狼队", "队友", "共边", "协同", "分线", "裸冲", "倒钩"),
    "witch": ("女巫", "毒药", "解药", "救人", "银水"),
    "hunter": ("猎人", "开枪", "带走", "枪"),
    "confirmed_wolf": ("查杀", "明狼", "坐实", "狼坑"),
    "night_kill": ("夜晚", "夜间", "刀", "落刀", "神职"),
    "speech": ("发言", "引用", "证据", "逻辑", "模板"),
}

PHASE_TOPICS: dict[Phase, tuple[str, ...]] = {
    Phase.NIGHT_WOLF: ("wolf_pack", "night_kill"),
    Phase.NIGHT_SEER: ("seer_claim",),
    Phase.NIGHT_WITCH: ("witch",),
    Phase.DAY_SPEECH: ("speech", "seer_claim", "counterclaim"),
    Phase.DAY_VOTE: ("vote", "wolf_pack"),
    Phase.HUNTER_SHOT: ("hunter", "vote"),
}


def retrieve_memories(
    *,
    role: Role,
    phase: Phase,
    public_history: list[dict[str, Any]],
    private_knowledge: dict[str, Any],
    legal_actions: list[ActionType],
    player_profile: dict[str, Any] | None = None,
    max_items: int = 5,
    memory_items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Retrieve approved role memories that match the current game state."""

    if max_items <= 0:
        return []

    candidates = _candidate_items(role, memory_items)
    if not candidates:
        return []

    docs = [_tokenize(_item_text(item)) for item in candidates]
    query_text = _query_text(
        role=role,
        phase=phase,
        public_history=public_history,
        private_knowledge=private_knowledge,
        legal_actions=legal_actions,
        player_profile=player_profile or {},
    )
    query_tokens = _tokenize(query_text)
    if not query_tokens:
        return []

    avgdl = sum(len(doc) for doc in docs) / max(1, len(docs))
    df = Counter()
    for doc in docs:
        df.update(set(doc))

    query_topic_tags = _topic_tags(query_text) | set(PHASE_TOPICS.get(phase, ()))
    scored: list[tuple[float, dict[str, Any], set[str]]] = []
    for item, doc in zip(candidates, docs):
        bm25 = _bm25(query_tokens, doc, df, len(docs), avgdl)
        memory = str(item.get("memory") or "")
        item_tags = _topic_tags(memory)
        matched_terms = _matched_terms(query_tokens, doc)
        score = bm25
        score += 0.7 * _bounded_float(item.get("confidence"), default=0.0)
        score += min(0.25, math.log1p(int(item.get("seen_count", 0))) * 0.06)
        score += 0.3 * len(query_topic_tags & item_tags)
        score += _recency_bonus(item)
        penalty = mechanical_memory_penalty(role, memory)
        score -= penalty
        if not matched_terms and not (query_topic_tags & item_tags):
            score -= 0.4
        scored.append(
            (
                score,
                {
                    "id": str(item.get("id") or ""),
                    "role": role.value,
                    "memory": memory.strip(),
                    "score": round(score, 4),
                    "bm25": round(bm25, 4),
                    "confidence": _bounded_float(item.get("confidence"), default=0.0),
                    "memory_penalty": round(penalty, 4),
                    "tags": sorted(item_tags),
                    "matched_terms": sorted(matched_terms)[:8],
                    "source": item.get("source"),
                    "status": item.get("status"),
                },
                set(doc),
            )
        )

    selected: list[dict[str, Any]] = []
    selected_tokens: list[set[str]] = []
    seen_memory: set[str] = set()
    for _, payload, doc_tokens in sorted(scored, key=lambda row: row[0], reverse=True):
        normalized = _normalize(payload["memory"])
        if normalized in seen_memory:
            continue
        if any(_jaccard(doc_tokens, existing) > 0.78 for existing in selected_tokens):
            continue
        selected.append(payload)
        selected_tokens.append(doc_tokens)
        seen_memory.add(normalized)
        if len(selected) >= max_items:
            break
    return selected


def _candidate_items(role: Role, memory_items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    raw_items = memory_items if memory_items is not None else load_memory_bank().get("items", [])
    result: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        if item.get("status") not in ACTIVE_STATUSES:
            continue
        if str(item.get("role") or "") != role.value:
            continue
        memory = str(item.get("memory") or "").strip()
        if not memory:
            continue
        if any(term and term in memory for term in UNSUPPORTED_RULE_TERMS):
            continue
        result.append(item)
    return result


def _query_text(
    *,
    role: Role,
    phase: Phase,
    public_history: list[dict[str, Any]],
    private_knowledge: dict[str, Any],
    legal_actions: list[ActionType],
    player_profile: dict[str, Any],
) -> str:
    private_focus = {
        key: value
        for key, value in private_knowledge.items()
        if key
        in {
            "inspections",
            "attacked_tonight",
            "has_antidote",
            "has_poison",
            "can_shoot",
            "pack_plan",
            "public_claims",
            "public_role_assertions",
            "public_role_mentions",
        }
    }
    profile_focus = {
        key: player_profile.get(key)
        for key in ("persona", "speech_style", "risk_preference", "vote_style", "anti_template_rule")
        if player_profile.get(key)
    }
    payload = {
        "role": role.value,
        "role_zh": role.zh,
        "phase": phase.value,
        "phase_zh": phase.zh,
        "legal_actions": [action.value for action in legal_actions],
        "public_history_tail": public_history[-10:],
        "private_focus": private_focus,
        "player_profile": profile_focus,
    }
    return json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True)


def _item_text(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("role") or ""),
        str(item.get("memory") or ""),
        str(item.get("source") or ""),
    ]
    for source in item.get("sources", []) or []:
        if isinstance(source, dict):
            parts.extend(str(value) for value in source.get("evidence_event_ids", []) or [])
    return " ".join(parts)


def _bm25(query_tokens: list[str], doc_tokens: list[str], df: Counter, doc_count: int, avgdl: float) -> float:
    if not doc_tokens:
        return 0.0
    doc_tf = Counter(doc_tokens)
    doc_len = len(doc_tokens)
    score = 0.0
    for token in set(query_tokens):
        tf = doc_tf.get(token, 0)
        if tf <= 0:
            continue
        idf = math.log(1.0 + (doc_count - df[token] + 0.5) / (df[token] + 0.5))
        denom = tf + K1 * (1.0 - B + B * doc_len / max(avgdl, 1e-6))
        score += idf * (tf * (K1 + 1.0)) / denom
    return score


def _tokenize(text: str) -> list[str]:
    text = str(text or "").lower()
    tokens = re.findall(r"[a-zA-Z_]+|\d+", text)
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", text)
    tokens.extend(cjk_chars)
    tokens.extend("".join(pair) for pair in zip(cjk_chars, cjk_chars[1:]))
    for topic, terms in DOMAIN_TOPICS.items():
        if any(term in text for term in terms):
            tokens.append(f"topic:{topic}")
    return tokens


def _topic_tags(text: str) -> set[str]:
    text = str(text or "")
    return {topic for topic, terms in DOMAIN_TOPICS.items() if any(term in text for term in terms)}


def _matched_terms(query_tokens: list[str], doc_tokens: list[str]) -> set[str]:
    query_set = {token for token in query_tokens if len(token) > 1}
    doc_set = {token for token in doc_tokens if len(token) > 1}
    return {
        token
        for token in query_set & doc_set
        if not token.isdigit() and not token.startswith("topic:")
    }


def _recency_bonus(item: dict[str, Any]) -> float:
    updated_at = str(item.get("updated_at") or item.get("created_at") or "")
    try:
        parsed = datetime.strptime(updated_at, "%Y%m%d_%H%M%S_%f")
    except ValueError:
        return 0.0
    age_days = max(0.0, (datetime.now() - parsed).total_seconds() / 86400.0)
    return min(0.2, 0.2 / (1.0 + age_days / 14.0))


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _normalize(text: str) -> str:
    return "".join(str(text).strip().lower().split())


def _bounded_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(1.0, parsed))
