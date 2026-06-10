from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gate_frozen_eval_summary import gate_summary
from scripts.record_frozen_eval_summary import DEFAULT_LOG_PATH, build_record, update_log
from scripts.build_frozen_eval_review_pack import build_review_pack
from scripts.summarize_frozen_eval_result import summarize


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Finalize frozen-eval small artifacts without reading progress logs or game archives."
    )
    parser.add_argument("path", help="Frozen-eval output directory or *_frozen_eval_result.json")
    parser.add_argument("--change-id", default=None, help="Optional skill_change_log entry to update")
    parser.add_argument("--log", default=str(DEFAULT_LOG_PATH))
    parser.add_argument(
        "--review-pack",
        action="store_true",
        help="Also build a compact target-mistake skill review pack from completed archives.",
    )
    args = parser.parse_args()

    result = finalize(
        Path(args.path),
        change_id=args.change_id,
        log_path=Path(args.log),
        review_pack=args.review_pack,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def finalize(
    path: Path,
    *,
    change_id: str | None = None,
    log_path: Path = DEFAULT_LOG_PATH,
    review_pack: bool = False,
) -> dict[str, Any]:
    result_path = _find_result_path(path)
    if result_path is None:
        root = path.parent if path.is_file() else path
        error_path = _find_error_path(root)
        actions: list[str] = []
        error_payload = _read_json_or_empty(error_path) if error_path else None
        if error_path and change_id:
            _record_error(log_path, change_id=change_id, error_path=error_path, payload=error_payload or {})
            actions.append("change_log_error_recorded")
        if error_path:
            return {
                "status": "failed_no_result",
                "root": str(root),
                "reason": "No *_frozen_eval_result.json found, but an error artifact exists.",
                "error_path": str(error_path),
                "error": (error_payload or {}).get("error"),
                "message": (error_payload or {}).get("message"),
                "actions": actions,
            }
        return {
            "status": "not_ready",
            "root": str(root),
            "reason": "No *_frozen_eval_result.json found.",
            "actions": [],
        }

    actions: list[str] = []
    summary_path = _summary_path_for(result_path)
    gate_path = _gate_path_for(result_path)
    review_pack_path = _review_pack_path_for(result_path)

    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        actions.append("summary_exists")
    else:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        summary = summarize(payload, result_path=result_path)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        actions.append("summary_created")

    if gate_path.exists():
        actions.append("gate_exists")
    else:
        gate = gate_summary(summary, summary_path=summary_path)
        gate_path.write_text(json.dumps(gate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        actions.append("gate_created")

    if review_pack:
        if review_pack_path.exists():
            actions.append("review_pack_exists")
        else:
            pack = build_review_pack(result_path)
            review_pack_path.write_text(json.dumps(pack, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            actions.append("review_pack_created")

    if change_id:
        data = json.loads(log_path.read_text(encoding="utf-8"))
        record = build_record(summary, summary_path=summary_path)
        update_log(data, change_id=change_id, record=record)
        log_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        actions.append("change_log_recorded")

    return {
        "status": "finalized",
        "result_path": str(result_path),
        "summary_path": str(summary_path),
        "gate_path": str(gate_path),
        "review_pack_path": str(review_pack_path) if review_pack else None,
        "change_id": change_id,
        "actions": actions,
    }


def _find_result_path(path: Path) -> Path | None:
    if path.is_file() and path.name.endswith("_frozen_eval_result.json"):
        return path
    if not path.exists() or not path.is_dir():
        return None
    matches = sorted(path.glob("*_frozen_eval_result.json"), key=lambda item: item.stat().st_mtime)
    return matches[-1] if matches else None


def _find_error_path(root: Path) -> Path | None:
    if root.is_file() and root.name.endswith("_error.json"):
        return root
    if not root.exists() or not root.is_dir():
        return None
    matches = sorted(root.glob("*_error.json"), key=lambda item: item.stat().st_mtime)
    return matches[-1] if matches else None


def _summary_path_for(result_path: Path) -> Path:
    return result_path.with_name(result_path.stem + "_compact_summary.json")


def _gate_path_for(result_path: Path) -> Path:
    suffix = "_frozen_eval_result"
    stem = result_path.stem
    prefix = stem[: -len(suffix)] if stem.endswith(suffix) else stem
    return result_path.with_name(prefix + "_frozen_eval_gate.json")


def _review_pack_path_for(result_path: Path) -> Path:
    return result_path.with_name(result_path.stem + "_skill_review_pack.json")


def _read_json_or_empty(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _record_error(
    log_path: Path,
    *,
    change_id: str,
    error_path: Path,
    payload: dict[str, Any],
) -> None:
    data = json.loads(log_path.read_text(encoding="utf-8"))
    entry = next((item for item in data.get("entries", []) if item.get("change_id") == change_id), None)
    if entry is None:
        raise KeyError(f"change_id not found: {change_id}")
    record = {
        "type": "frozen_eval_error",
        "recorded_at": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "result": "failed",
        "source_error_path": str(error_path),
        "error": payload.get("error"),
        "message": payload.get("message"),
        "progress_path": payload.get("progress_path"),
        "notes": [
            "Recorded from *_error.json only; no progress log or full game archive was read.",
            "Rerun or extend runtime before making a skill promotion decision.",
        ],
    }
    validation = entry.setdefault("validation", [])
    entry["validation"] = [
        item
        for item in validation
        if not (item.get("type") == record["type"] and item.get("source_error_path") == record["source_error_path"])
    ] + [record]
    entry["decision"] = "expanded_eval_rerun_required"
    log_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
