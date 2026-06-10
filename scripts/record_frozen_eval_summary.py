from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from gate_frozen_eval_summary import gate_summary
except ModuleNotFoundError:
    from scripts.gate_frozen_eval_summary import gate_summary


DEFAULT_LOG_PATH = Path("data/memory/skill_change_log.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Record a compact frozen-eval summary into skill_change_log.json.")
    parser.add_argument("--change-id", required=True)
    parser.add_argument("--summary", required=True, help="Compact summary JSON file or directory containing one")
    parser.add_argument("--log", default=str(DEFAULT_LOG_PATH))
    args = parser.parse_args()

    summary_path = _resolve_summary_path(Path(args.summary))
    log_path = Path(args.log)
    record = build_record(json.loads(summary_path.read_text(encoding="utf-8")), summary_path=summary_path)
    data = json.loads(log_path.read_text(encoding="utf-8"))
    update_log(data, change_id=args.change_id, record=record)
    log_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"change_id": args.change_id, "summary_path": str(summary_path), "result": record["result"]}, ensure_ascii=False))


def build_record(summary: dict[str, Any], *, summary_path: Path) -> dict[str, Any]:
    candidate = summary.get("candidate") or {}
    baseline = summary.get("baseline") or {}
    candidate_mistakes = candidate.get("mistake_counts") or {}
    baseline_mistakes = baseline.get("mistake_counts") or {}
    target = "werewolf_exposed_pack_vote"
    gate = gate_summary(summary, summary_path=summary_path, target_mistake=target)
    review_pack_path = _review_pack_path(summary, summary_path=summary_path)
    return {
        "type": "seed_aligned_frozen_eval_expand_summary",
        "recorded_at": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "result": summary.get("recommendation") or "needs_codex_review",
        "gate_recommendation": gate["recommendation"],
        "gate_checks": gate["checks"],
        "gate_metrics": gate["metrics"],
        "gate_why": gate["why"],
        "source_summary_path": str(summary_path),
        "source_result_path": summary.get("source_result_path"),
        "source_review_pack_path": str(review_pack_path) if review_pack_path else None,
        "source_review_pack_exists": review_pack_path.exists() if review_pack_path else False,
        "candidate_version": summary.get("candidate_version"),
        "baseline_version": summary.get("baseline_version"),
        "games": summary.get("games"),
        "completed_games": summary.get("completed_games"),
        "seed_alignment": summary.get("seed_alignment"),
        "candidate_overall": candidate.get("overall"),
        "baseline_overall": baseline.get("overall"),
        "delta": summary.get("delta"),
        "candidate_mistakes_per_game": candidate.get("mistakes_per_game"),
        "baseline_mistakes_per_game": baseline.get("mistakes_per_game"),
        "candidate_pack_vote_mistakes": candidate_mistakes.get(target, 0),
        "baseline_pack_vote_mistakes": baseline_mistakes.get(target, 0),
        "candidate_mistake_counts": candidate_mistakes,
        "baseline_mistake_counts": baseline_mistakes,
        "candidate_vote_quality": candidate.get("vote_quality"),
        "baseline_vote_quality": baseline.get("vote_quality"),
        "candidate_wolf_deception_quality": candidate.get("wolf_deception_quality"),
        "baseline_wolf_deception_quality": baseline.get("wolf_deception_quality"),
        "archives": summary.get("archives"),
        "errors": summary.get("errors"),
        "notes": [
            "Recorded from compact summary only; no progress log or full game archive was read.",
            "This record does not promote a version automatically. Codex should review aggregate tradeoffs before promotion or r4 revision.",
        ],
    }


def update_log(data: dict[str, Any], *, change_id: str, record: dict[str, Any]) -> None:
    entry = next((item for item in data.get("entries", []) if item.get("change_id") == change_id), None)
    if entry is None:
        raise KeyError(f"change_id not found: {change_id}")
    validation = entry.setdefault("validation", [])
    source_summary_path = record.get("source_summary_path")
    entry["validation"] = [
        item
        for item in validation
        if not (item.get("type") == record["type"] and item.get("source_summary_path") == source_summary_path)
    ] + [record]
    recommendation = str(record.get("result") or "")
    gate_recommendation = str(record.get("gate_recommendation") or "")
    if gate_recommendation == "promote_after_codex_review":
        entry["decision"] = "expanded_eval_ready_for_codex_review"
    elif gate_recommendation == "expand_sample":
        entry["decision"] = "expanded_eval_expand_sample"
    elif gate_recommendation == "revise_skill" or recommendation == "candidate_needs_revision":
        entry["decision"] = "expanded_eval_needs_revision_review"
    elif gate_recommendation == "rerun_required":
        entry["decision"] = "expanded_eval_rerun_required"
    else:
        entry["decision"] = "expanded_eval_tradeoff_review"


def _resolve_summary_path(path: Path) -> Path:
    if path.is_file():
        return path
    matches = sorted(path.glob("*_compact_summary.json"), key=lambda item: item.stat().st_mtime)
    if not matches:
        raise FileNotFoundError(f"No *_compact_summary.json found in {path}")
    return matches[-1]


def _review_pack_path(summary: dict[str, Any], *, summary_path: Path) -> Path | None:
    source_result = summary.get("source_result_path")
    if source_result:
        result_path = Path(str(source_result))
        return result_path.with_name(result_path.stem + "_skill_review_pack.json")
    suffix = "_compact_summary"
    stem = summary_path.stem
    if stem.endswith(suffix):
        result_stem = stem[: -len(suffix)]
        return summary_path.with_name(result_stem + "_skill_review_pack.json")
    return None


if __name__ == "__main__":
    main()
