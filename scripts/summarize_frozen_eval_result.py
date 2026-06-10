from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(description="Write a compact summary for a frozen-eval result JSON.")
    parser.add_argument("path", help="Result JSON file or directory containing *_frozen_eval_result.json")
    parser.add_argument("--output", default=None, help="Optional output JSON path")
    args = parser.parse_args()

    result_path = _resolve_result_path(Path(args.path))
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    summary = summarize(payload, result_path=result_path)

    output_path = Path(args.output) if args.output else result_path.with_name(result_path.stem + "_compact_summary.json")
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result_path": str(result_path), "summary_path": str(output_path)}, ensure_ascii=False))


def summarize(payload: dict[str, Any], *, result_path: Path) -> dict[str, Any]:
    candidate = payload.get("candidate") or {}
    baseline = payload.get("baseline") or {}
    candidate_scores = candidate.get("average_scores") or {}
    baseline_scores = baseline.get("average_scores") or {}
    return {
        "source_result_path": str(result_path),
        "candidate_version": payload.get("version_id"),
        "baseline_version": payload.get("baseline_version_id"),
        "games": payload.get("games"),
        "completed_games": payload.get("completed_games"),
        "seed_alignment": payload.get("seed_alignment"),
        "candidate": {
            "winner_counts": candidate.get("winner_counts"),
            "overall": candidate_scores.get("overall"),
            "vote_quality": candidate_scores.get("vote_quality"),
            "skill_quality": candidate_scores.get("skill_quality"),
            "team_contribution": candidate_scores.get("team_contribution"),
            "wolf_deception_quality": candidate_scores.get("wolf_deception_quality"),
            "mistakes_per_game": candidate.get("mistakes_per_game"),
            "mistake_counts": candidate.get("mistake_counts"),
        },
        "baseline": {
            "winner_counts": baseline.get("winner_counts"),
            "overall": baseline_scores.get("overall"),
            "vote_quality": baseline_scores.get("vote_quality"),
            "skill_quality": baseline_scores.get("skill_quality"),
            "team_contribution": baseline_scores.get("team_contribution"),
            "wolf_deception_quality": baseline_scores.get("wolf_deception_quality"),
            "mistakes_per_game": baseline.get("mistakes_per_game"),
            "mistake_counts": baseline.get("mistake_counts"),
        },
        "delta": payload.get("delta"),
        "archives": payload.get("archives"),
        "errors": payload.get("errors"),
        "recommendation": _recommendation(candidate, baseline, payload.get("delta") or {}),
    }


def _recommendation(candidate: dict[str, Any], baseline: dict[str, Any], delta: dict[str, Any]) -> str:
    candidate_mistakes = candidate.get("mistake_counts") or {}
    baseline_mistakes = baseline.get("mistake_counts") or {}
    target = "werewolf_exposed_pack_vote"
    candidate_target = candidate_mistakes.get(target, 0)
    baseline_target = baseline_mistakes.get(target, 0)
    target_delta = candidate_target - baseline_target
    overall_delta = float(delta.get("overall") or 0.0)
    mistakes_delta = float(delta.get("mistakes_per_game") or 0.0)
    if candidate_target == 0 and baseline_target == 0:
        return "target_not_reproduced_expand_sample"
    if target_delta < 0 and overall_delta >= 0 and mistakes_delta <= 0:
        return "candidate_promising_expand_or_promote_after_review"
    if target_delta < 0:
        return "candidate_improves_target_but_needs_tradeoff_review"
    if target_delta == 0 and overall_delta >= 0:
        return "candidate_neutral_on_target_review_secondary_metrics"
    return "candidate_needs_revision"


def _resolve_result_path(path: Path) -> Path:
    if path.is_file():
        return path
    matches = sorted(path.glob("*_frozen_eval_result.json"), key=lambda item: item.stat().st_mtime)
    if not matches:
        raise FileNotFoundError(f"No *_frozen_eval_result.json found in {path}")
    return matches[-1]


if __name__ == "__main__":
    main()
