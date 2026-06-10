from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Role, StrategyVersion, to_jsonable


MEMORY_DIR = Path("data") / "memory"
SNAPSHOT_DIR = MEMORY_DIR / "snapshots"
RUN_DIR = MEMORY_DIR / "evolution_runs"
LATEST_PROMOTED_PATH = MEMORY_DIR / "latest_promoted.json"
MEMORY_BANK_PATH = MEMORY_DIR / "memory_bank.json"
MEMORY_BANK_AUTO_APPROVE_THRESHOLD = 0.85
UNSUPPORTED_RULE_TERMS = ("警徽", "警长", "警徽流", "撕警徽", "移交警徽")
MECHANICAL_MEMORY_SCORE_PENALTY = 3.0


def save_role_memory_snapshot(
    *,
    version_id: str,
    profiles: dict[Role, StrategyVersion],
    aggregate: dict[str, Any],
    reflections: dict[str, list[str]],
    source_archives: list[str],
    errors: list[dict[str, Any]],
    parent_id: str | None,
    promoted: bool,
    llm_provider: str,
    llm_model: str | None,
    round_index: int,
    llm_reviews: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Persist the latest role-level strategy memory and a timestamped snapshot."""

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    payload = {
        "schema_version": 1,
        "kind": "role_memory_snapshot",
        "created_at": stamp,
        "version_id": version_id,
        "parent_id": parent_id,
        "promoted": promoted,
        "round_index": round_index,
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "metrics": to_jsonable(aggregate),
        "source_games": list(source_archives),
        "errors": to_jsonable(errors),
        "reflections": to_jsonable(reflections),
        "llm_reviews": to_jsonable(llm_reviews or []),
        "role_memories": _profiles_payload(profiles),
    }

    latest_path = MEMORY_DIR / f"role_memory_{_safe_name(version_id)}.json"
    snapshot_path = SNAPSHOT_DIR / f"{stamp}_{_safe_name(version_id)}.json"
    _write_json(latest_path, payload)
    _write_json(snapshot_path, payload)
    return {
        "version_id": version_id,
        "latest_path": str(latest_path),
        "snapshot_path": str(snapshot_path),
    }


def save_evolution_run(payload: dict[str, Any]) -> str:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    final_version = _safe_name(str(payload.get("final_version", "unknown")))
    path = RUN_DIR / f"run_{stamp}_{final_version}.json"
    _write_json(
        path,
        {
            "schema_version": 1,
            "kind": "evolution_run",
            "created_at": stamp,
            **to_jsonable(payload),
            "memory_run_file": str(path),
        },
    )
    return str(path)


def update_evolution_run(path: str | Path, payload: dict[str, Any]) -> None:
    run_path = Path(path)
    created_at = _stamp()
    if run_path.exists():
        try:
            created_at = json.loads(run_path.read_text(encoding="utf-8")).get("created_at") or created_at
        except json.JSONDecodeError:
            pass
    _write_json(
        run_path,
        {
            "schema_version": 1,
            "kind": "evolution_run",
            "created_at": created_at,
            **to_jsonable(payload),
            "memory_run_file": str(run_path),
        },
    )


def save_latest_promoted(
    *,
    version_id: str,
    memory_file: str | None,
    snapshot_path: str | None,
    run_file: str | None,
    aggregate: dict[str, Any],
    base_version: str | None,
    llm_provider: str,
    llm_model: str | None,
) -> str:
    stamp = _stamp()
    payload = {
        "schema_version": 1,
        "kind": "latest_promoted",
        "created_at": stamp,
        "version_id": version_id,
        "memory_file": memory_file,
        "snapshot_path": snapshot_path,
        "run_file": run_file,
        "base_version": base_version,
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "metrics": to_jsonable(aggregate),
    }
    _write_json(LATEST_PROMOTED_PATH, payload)
    return str(LATEST_PROMOTED_PATH)


def load_latest_promoted() -> dict[str, Any] | None:
    if not LATEST_PROMOTED_PATH.exists():
        return None
    try:
        return json.loads(LATEST_PROMOTED_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def load_role_memory_profiles(version_id: str) -> dict[Role, StrategyVersion] | None:
    path = MEMORY_DIR / f"role_memory_{_safe_name(version_id)}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    memories = data.get("role_memories", {})
    profiles: dict[Role, StrategyVersion] = {}
    for role_value, item in memories.items():
        role = Role(role_value)
        profiles[role] = StrategyVersion(
            role=role,
            version_id=str(item.get("version_id") or data.get("version_id") or version_id),
            prompt_name=str(item.get("prompt_name") or f"{role.value}_memory"),
            prompt_summary=str(item.get("prompt_summary") or ""),
            strategy_memory=list(item.get("strategy_memory") or []),
            parameters=dict(item.get("parameters") or {}),
            metrics=dict(item.get("metrics") or {}),
            promoted=bool(item.get("promoted", data.get("promoted", False))),
            parent_id=item.get("parent_id") or data.get("parent_id"),
        )
    return profiles or None


def list_role_memory_versions() -> list[dict[str, Any]]:
    """Return persisted role-memory versions for UI/API selection."""

    if not MEMORY_DIR.exists():
        return []
    versions: list[dict[str, Any]] = []
    for path in MEMORY_DIR.glob("role_memory_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        version_id = str(data.get("version_id") or path.stem.removeprefix("role_memory_") or "").strip()
        if not version_id:
            continue
        metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
        average_scores = metrics.get("average_scores") if isinstance(metrics.get("average_scores"), dict) else {}
        versions.append(
            {
                "version_id": version_id,
                "label": version_id,
                "path": str(path),
                "created_at": data.get("created_at"),
                "parent_id": data.get("parent_id"),
                "promoted": bool(data.get("promoted", False)),
                "round_index": data.get("round_index"),
                "llm_provider": data.get("llm_provider"),
                "llm_model": data.get("llm_model"),
                "overall": average_scores.get("overall"),
                "mistakes_per_game": metrics.get("mistakes_per_game"),
                "mtime": path.stat().st_mtime,
            }
        )
    return sorted(
        versions,
        key=lambda item: (
            -float(item.get("mtime") or 0.0),
            str(item.get("version_id") or ""),
        ),
    )


def save_review_memory_candidates(
    review: dict[str, Any] | None,
    *,
    version_id: str,
    game_id: str,
    batch: dict[str, Any] | None = None,
    archive_path: str | None = None,
) -> list[str]:
    if not review or review.get("review_status") != "ok":
        return []
    candidates = review.get("memory_candidates") or []
    if not isinstance(candidates, list):
        return []

    bank = load_memory_bank()
    items = bank.setdefault("items", [])
    now = _stamp()
    saved_ids: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        role = str(candidate.get("role") or "")
        if role not in {role.value for role in Role}:
            continue
        memory = str(candidate.get("memory") or "").strip()
        if not memory:
            continue
        if _mentions_unsupported_rule(memory):
            continue
        confidence = _bounded_float(candidate.get("confidence"), default=0.0)
        normalized = _normalize_memory_text(memory)
        mechanical_penalty = mechanical_memory_penalty(role, memory)
        item = _find_bank_item(items, role, normalized)
        source = {
            "version_id": version_id,
            "game_id": game_id,
            "batch": to_jsonable(batch or {}),
            "archive_path": archive_path,
            "evidence_event_ids": to_jsonable(candidate.get("evidence_event_ids") or []),
            "confidence": confidence,
            "created_at": now,
        }
        if item is None:
            item = {
                "id": _memory_item_id(role, normalized),
                "role": role,
                "memory": memory,
                "normalized_memory": normalized,
                "status": _memory_status(confidence, mechanical_penalty),
                "confidence": confidence,
                "mechanical_memory_penalty": mechanical_penalty,
                "source": "llm_review",
                "created_at": now,
                "updated_at": now,
                "seen_count": 1,
                "sources": [source],
            }
            items.append(item)
        else:
            item["updated_at"] = now
            item["seen_count"] = int(item.get("seen_count", 0)) + 1
            item["confidence"] = max(float(item.get("confidence", 0.0)), confidence)
            item["mechanical_memory_penalty"] = max(
                float(item.get("mechanical_memory_penalty", 0.0)),
                mechanical_penalty,
            )
            item.setdefault("sources", []).append(source)
            if mechanical_penalty > 0 and item.get("status") == "approved_auto":
                item["status"] = "pending"
            elif item.get("status") == "pending" and float(item.get("confidence", 0.0)) >= MEMORY_BANK_AUTO_APPROVE_THRESHOLD:
                item["status"] = "approved_auto"
        saved_ids.append(str(item["id"]))

    bank["updated_at"] = now
    bank["items"] = sorted(items, key=lambda item: (str(item.get("role")), -float(item.get("confidence", 0.0)), str(item.get("memory"))))
    _write_json(MEMORY_BANK_PATH, bank)
    return saved_ids


def load_memory_bank() -> dict[str, Any]:
    if not MEMORY_BANK_PATH.exists():
        payload = {
            "schema_version": 1,
            "kind": "memory_bank",
            "description": (
                "Cross-version LLM review memory bank. Edit item.status to approved, approved_auto, pending, or rejected. "
                "Only approved/approved_auto items are injected into future games."
            ),
            "updated_at": _stamp(),
            "auto_approve_threshold": MEMORY_BANK_AUTO_APPROVE_THRESHOLD,
            "items": [],
        }
        _write_json(MEMORY_BANK_PATH, payload)
        return payload
    try:
        data = json.loads(MEMORY_BANK_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}
    data.setdefault("schema_version", 1)
    data.setdefault("kind", "memory_bank")
    data.setdefault("items", [])
    return data


def active_memory_bank_by_role(*, max_items_per_role: int = 4) -> dict[Role, list[str]]:
    grouped: dict[Role, list[tuple[float, str]]] = {role: [] for role in Role}
    for item in load_memory_bank().get("items", []):
        if not isinstance(item, dict):
            continue
        if item.get("status") not in {"approved", "approved_auto"}:
            continue
        try:
            role = Role(str(item.get("role")))
        except ValueError:
            continue
        memory = str(item.get("memory") or "").strip()
        if not memory:
            continue
        if _mentions_unsupported_rule(memory):
            continue
        adjusted_confidence = float(item.get("confidence", 0.0)) - mechanical_memory_penalty(role, memory)
        grouped[role].append((adjusted_confidence, memory))
    return {
        role: [memory for _, memory in sorted(items, key=lambda pair: pair[0], reverse=True)[:max_items_per_role]]
        for role, items in grouped.items()
    }


def _profiles_payload(profiles: dict[Role, StrategyVersion]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for role, profile in profiles.items():
        result[role.value] = {
            "role": role.value,
            "role_zh": role.zh,
            "version_id": profile.version_id,
            "parent_id": profile.parent_id,
            "prompt_name": profile.prompt_name,
            "prompt_summary": profile.prompt_summary,
            "strategy_memory": list(profile.strategy_memory),
            "parameters": to_jsonable(profile.parameters),
            "metrics": to_jsonable(profile.metrics),
            "promoted": profile.promoted,
        }
    return result


def _find_bank_item(items: list[dict[str, Any]], role: str, normalized_memory: str) -> dict[str, Any] | None:
    for item in items:
        if item.get("role") == role and item.get("normalized_memory") == normalized_memory:
            return item
    return None


def _memory_item_id(role: str, normalized_memory: str) -> str:
    import hashlib

    digest = hashlib.sha1(f"{role}:{normalized_memory}".encode("utf-8")).hexdigest()[:12]
    return f"{role}-{digest}"


def _mentions_unsupported_rule(memory: str) -> bool:
    return any(term in memory for term in UNSUPPORTED_RULE_TERMS)


def _memory_status(confidence: float, mechanical_penalty: float) -> str:
    if mechanical_penalty > 0:
        return "pending"
    return "approved_auto" if confidence >= MEMORY_BANK_AUTO_APPROVE_THRESHOLD else "pending"


def mechanical_memory_penalty(role: Role | str, memory: str) -> float:
    if isinstance(role, Role):
        role_value = role
    else:
        try:
            role_value = Role(str(role))
        except ValueError:
            return 0.0
    compact = _normalize_memory_text(memory)
    if not compact:
        return 0.0
    if role_value == Role.SEER and _looks_like_mechanical_seer_memory(compact):
        return MECHANICAL_MEMORY_SCORE_PENALTY
    if role_value == Role.WITCH and _looks_like_mechanical_witch_memory(compact):
        return MECHANICAL_MEMORY_SCORE_PENALTY
    return 0.0


def _looks_like_mechanical_seer_memory(compact: str) -> bool:
    has_day1 = _contains_any(compact, ("首日", "第一天", "第一日", "首轮", "d1"))
    has_claim = _contains_any(compact, ("起跳", "跳出", "公开身份", "公开预言家", "报出身份", "跳明", "自跳"))
    has_directive = _contains_any(compact, ("必须", "应该", "应当", "立即", "尽早", "尽快", "优先", "默认", "无论", "一定"))
    has_safe_condition = _contains_any(
        compact,
        (
            "查到狼人",
            "验到狼人",
            "查杀",
            "被强烈质疑",
            "出现对跳",
            "需要带队",
            "只有金水",
            "信息不足",
            "不必",
            "不是每局",
            "不要机械",
            "可以隐忍",
            "可隐忍",
            "选择隐忍",
            "取决于",
            "权衡",
        ),
    )
    return has_day1 and has_claim and has_directive and not has_safe_condition


def _looks_like_mechanical_witch_memory(compact: str) -> bool:
    has_n1 = _contains_any(compact, ("首夜", "第一晚", "第一夜", "n1"))
    has_save = _contains_any(compact, ("解药", "救人", "使用解药", "开解药", "救下", "救"))
    has_directive = _contains_any(compact, ("必须", "应该", "应当", "通常", "优先", "尽量", "默认", "无特殊情况", "一定"))
    has_safe_condition = _contains_any(
        compact,
        (
            "自救",
            "自己被刀",
            "自身被刀",
            "女巫被刀",
            "不是固定",
            "可选",
            "可以不救",
            "保留解药",
            "比较",
            "权衡",
            "取决于",
            "目标价值",
            "关键神职",
            "强神",
            "收益",
        ),
    )
    return has_n1 and has_save and has_directive and not has_safe_condition


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _normalize_memory_text(text: str) -> str:
    return "".join(str(text).strip().lower().split())


def _bounded_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(1.0, parsed))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _safe_name(value: str) -> str:
    return value.replace("/", "_").replace("\\", "_").replace(" ", "_")
