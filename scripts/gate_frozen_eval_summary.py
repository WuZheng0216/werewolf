from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_TARGET_MISTAKE = "werewolf_exposed_pack_vote"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Make a conservative skill-candidate gate decision from compact frozen-eval summary."
    )
    parser.add_argument("summary", help="Compact summary JSON file or directory containing one")
    parser.add_argument("--target-mistake", default=DEFAULT_TARGET_MISTAKE)
    parser.add_argument("--min-games", type=int, default=3)
    parser.add_argument("--overall-regression-tolerance", type=float, default=2.0)
    parser.add_argument("--quality-collapse-tolerance", type=float, default=8.0)
    args = parser.parse_args()

    summary_path = _resolve_summary_path(Path(args.summary))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    decision = gate_summary(
        summary,
        summary_path=summary_path,
        target_mistake=args.target_mistake,
        min_games=args.min_games,
        overall_regression_tolerance=args.overall_regression_tolerance,
        quality_collapse_tolerance=args.quality_collapse_tolerance,
    )
    print(json.dumps(decision, ensure_ascii=False, indent=2))


def gate_summary(
    summary: dict[str, Any],
    *,
    summary_path: Path,
    target_mistake: str = DEFAULT_TARGET_MISTAKE,
    min_games: int = 3,
    overall_regression_tolerance: float = 2.0,
    quality_collapse_tolerance: float = 8.0,
) -> dict[str, Any]:
    candidate = summary.get("candidate") or {}
    baseline = summary.get("baseline") or {}
    delta = summary.get("delta") or {}
    games = _completed_pair(summary)
    target_delta = _mistake_count(candidate, target_mistake) - _mistake_count(baseline, target_mistake)
    overall_delta = _float(delta.get("overall"))
    mistakes_delta = _float(delta.get("mistakes_per_game"))
    vote_delta = _float(candidate.get("vote_quality")) - _float(baseline.get("vote_quality"))
    wolf_deception_delta = _float(candidate.get("wolf_deception_quality")) - _float(
        baseline.get("wolf_deception_quality")
    )
    seed_aligned = _seed_aligned(summary)
    errors = summary.get("errors") or {}
    has_errors = bool(errors.get("candidate") or errors.get("baseline"))
    candidate_target_count = _mistake_count(candidate, target_mistake)
    baseline_target_count = _mistake_count(baseline, target_mistake)
    target_absent_both = candidate_target_count == 0 and baseline_target_count == 0

    checks = {
        "seed_aligned": seed_aligned,
        "enough_games": games["candidate"] >= min_games and games["baseline"] >= min_games,
        "target_absent_both": target_absent_both,
        "target_improved": target_delta < 0,
        "overall_not_regressed": overall_delta >= -overall_regression_tolerance,
        "mistakes_not_worse": mistakes_delta <= 0,
        "vote_quality_not_collapsed": vote_delta >= -quality_collapse_tolerance,
        "wolf_deception_not_collapsed": wolf_deception_delta >= -quality_collapse_tolerance,
        "no_eval_errors": not has_errors,
    }
    recommendation = _recommend(checks)
    return {
        "type": "frozen_eval_gate",
        "source_summary_path": str(summary_path),
        "candidate_version": summary.get("candidate_version"),
        "baseline_version": summary.get("baseline_version"),
        "target_mistake": target_mistake,
        "min_games": min_games,
        "completed_games": games,
        "metrics": {
            "target_mistake_delta": target_delta,
            "overall_delta": overall_delta,
            "mistakes_per_game_delta": mistakes_delta,
            "vote_quality_delta": round(vote_delta, 4),
            "wolf_deception_quality_delta": round(wolf_deception_delta, 4),
        },
        "checks": checks,
        "recommendation": recommendation,
        "why": _why(recommendation, checks, target_mistake=target_mistake),
    }


def _completed_pair(summary: dict[str, Any]) -> dict[str, int]:
    completed = summary.get("completed_games") or {}
    if isinstance(completed, dict):
        return {
            "candidate": int(completed.get("candidate") or 0),
            "baseline": int(completed.get("baseline") or 0),
        }
    games = int(summary.get("games") or 0)
    return {"candidate": games, "baseline": games}


def _resolve_summary_path(path: Path) -> Path:
    if path.is_file():
        return path
    matches = sorted(path.glob("*_compact_summary.json"), key=lambda item: item.stat().st_mtime)
    if not matches:
        raise FileNotFoundError(f"No *_compact_summary.json found in {path}")
    return matches[-1]


def _seed_aligned(summary: dict[str, Any]) -> bool:
    seed_alignment = summary.get("seed_alignment") or {}
    candidate = seed_alignment.get("candidate_completed_seeds")
    baseline = seed_alignment.get("baseline_completed_seeds")
    if candidate is None or baseline is None:
        return False
    return candidate == baseline


def _mistake_count(side: dict[str, Any], mistake: str) -> int:
    return int((side.get("mistake_counts") or {}).get(mistake) or 0)


def _float(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def _recommend(checks: dict[str, bool]) -> str:
    if not checks["seed_aligned"] or not checks["no_eval_errors"]:
        return "rerun_required"
    if checks["target_absent_both"]:
        if not (
            checks["overall_not_regressed"]
            and checks["mistakes_not_worse"]
            and checks["vote_quality_not_collapsed"]
            and checks["wolf_deception_not_collapsed"]
        ):
            return "tradeoff_review_required"
        return "expand_sample"
    if not checks["target_improved"]:
        return "revise_skill"
    if not (
        checks["overall_not_regressed"]
        and checks["mistakes_not_worse"]
        and checks["vote_quality_not_collapsed"]
        and checks["wolf_deception_not_collapsed"]
    ):
        return "tradeoff_review_required"
    if not checks["enough_games"]:
        return "expand_sample"
    return "promote_after_codex_review"


def _why(recommendation: str, checks: dict[str, bool], *, target_mistake: str) -> list[str]:
    reasons: list[str] = []
    if not checks["seed_aligned"]:
        reasons.append("Candidate and baseline seeds are not aligned, so the comparison is not strict.")
    if not checks["no_eval_errors"]:
        reasons.append("Evaluation errors were recorded; rerun before changing promotion state.")
    if checks.get("target_absent_both"):
        reasons.append(
            f"Target mistake did not appear for either side: {target_mistake}; this sample cannot prove targeted improvement."
        )
    elif not checks["target_improved"]:
        reasons.append(f"Target mistake did not decrease: {target_mistake}.")
    if not checks["overall_not_regressed"]:
        reasons.append("Overall score regressed beyond tolerance.")
    if not checks["mistakes_not_worse"]:
        reasons.append("Mistakes per game increased.")
    if not checks["vote_quality_not_collapsed"]:
        reasons.append("Vote quality dropped beyond tolerance.")
    if not checks["wolf_deception_not_collapsed"]:
        reasons.append("Wolf deception quality dropped beyond tolerance.")
    if recommendation == "expand_sample":
        reasons.append("Metrics look directionally useful, but the sample is below the minimum game count.")
    if recommendation == "promote_after_codex_review":
        reasons.append("Aggregate checks passed; inspect concise evidence before promoting.")
    return reasons


if __name__ == "__main__":
    main()
