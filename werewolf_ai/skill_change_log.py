from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_SKILL_CHANGE_LOG = Path("data/memory/skill_change_log.json")
SCHEMA_VERSION = 1


def load_skill_change_log(path: str | Path = DEFAULT_SKILL_CHANGE_LOG) -> dict[str, Any]:
    log_path = Path(path)
    if not log_path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "skill_change_log",
            "entries": [],
        }
    payload = json.loads(log_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Skill change log must be a JSON object: {log_path}")
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("kind", "skill_change_log")
    payload.setdefault("entries", [])
    if not isinstance(payload["entries"], list):
        raise ValueError(f"Skill change log entries must be a list: {log_path}")
    return payload


def append_skill_change(entry: dict[str, Any], path: str | Path = DEFAULT_SKILL_CHANGE_LOG) -> dict[str, Any]:
    log_path = Path(path)
    payload = load_skill_change_log(log_path)
    normalized = _normalize_entry(entry, existing_entries=payload["entries"])
    payload["entries"].append(normalized)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


def _normalize_entry(entry: dict[str, Any], *, existing_entries: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = dict(entry)
    normalized.setdefault("recorded_at", datetime.now().strftime("%Y%m%d_%H%M%S"))
    normalized.setdefault("status", "candidate")
    normalized.setdefault("decision", "pending_eval")
    normalized.setdefault("evidence", [])
    normalized.setdefault("changes", [])
    normalized.setdefault("validation", [])
    normalized.setdefault("next_steps", [])
    if not normalized.get("change_id"):
        version_id = str(normalized.get("version_id") or "skill_change")
        index = 1 + sum(1 for item in existing_entries if item.get("version_id") == normalized.get("version_id"))
        normalized["change_id"] = f"{version_id}_change_{index}"
    required = ["change_id", "version_id", "parent_id", "target_failure_pattern", "rationale"]
    missing = [key for key in required if not normalized.get(key)]
    if missing:
        raise ValueError(f"Missing required skill change fields: {', '.join(missing)}")
    return normalized
