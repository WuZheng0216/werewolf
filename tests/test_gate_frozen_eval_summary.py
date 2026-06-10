from __future__ import annotations

import unittest
from pathlib import Path

from scripts.gate_frozen_eval_summary import gate_summary


def _summary(
    *,
    completed: int = 3,
    candidate_pack_vote: int = 1,
    baseline_pack_vote: int = 3,
    overall_delta: float = 4.0,
    mistakes_delta: float = -1.0,
    candidate_seed: list[int] | None = None,
    baseline_seed: list[int] | None = None,
) -> dict:
    candidate_seed = candidate_seed or [1, 2, 3]
    baseline_seed = baseline_seed or [1, 2, 3]
    return {
        "candidate_version": "candidate_v1",
        "baseline_version": "baseline_v1",
        "completed_games": {"candidate": completed, "baseline": completed},
        "seed_alignment": {
            "candidate_completed_seeds": candidate_seed,
            "baseline_completed_seeds": baseline_seed,
        },
        "candidate": {
            "overall": 80.0,
            "vote_quality": 70.0,
            "wolf_deception_quality": 88.0,
            "mistakes_per_game": 1.0,
            "mistake_counts": {"werewolf_exposed_pack_vote": candidate_pack_vote},
        },
        "baseline": {
            "overall": 76.0,
            "vote_quality": 68.0,
            "wolf_deception_quality": 84.0,
            "mistakes_per_game": 2.0,
            "mistake_counts": {"werewolf_exposed_pack_vote": baseline_pack_vote},
        },
        "delta": {"overall": overall_delta, "mistakes_per_game": mistakes_delta},
        "errors": {"candidate": [], "baseline": []},
    }


class GateFrozenEvalSummaryTests(unittest.TestCase):
    def test_promote_after_review_when_checks_pass_and_sample_is_enough(self) -> None:
        decision = gate_summary(_summary(), summary_path=Path("summary.json"))

        self.assertEqual(decision["recommendation"], "promote_after_codex_review")
        self.assertTrue(decision["checks"]["target_improved"])
        self.assertEqual(decision["metrics"]["target_mistake_delta"], -2)

    def test_expand_sample_when_directional_but_too_few_games(self) -> None:
        decision = gate_summary(_summary(completed=1), summary_path=Path("summary.json"), min_games=3)

        self.assertEqual(decision["recommendation"], "expand_sample")
        self.assertFalse(decision["checks"]["enough_games"])

    def test_rerun_required_when_seeds_are_not_aligned(self) -> None:
        decision = gate_summary(
            _summary(candidate_seed=[1, 2, 4], baseline_seed=[1, 2, 3]),
            summary_path=Path("summary.json"),
        )

        self.assertEqual(decision["recommendation"], "rerun_required")
        self.assertFalse(decision["checks"]["seed_aligned"])

    def test_revise_skill_when_target_does_not_improve(self) -> None:
        decision = gate_summary(
            _summary(candidate_pack_vote=3, baseline_pack_vote=3),
            summary_path=Path("summary.json"),
        )

        self.assertEqual(decision["recommendation"], "revise_skill")
        self.assertFalse(decision["checks"]["target_improved"])

    def test_expand_sample_when_target_mistake_absent_on_both_sides(self) -> None:
        decision = gate_summary(
            _summary(
                completed=1,
                candidate_pack_vote=0,
                baseline_pack_vote=0,
                overall_delta=-0.22,
                mistakes_delta=0.0,
            ),
            summary_path=Path("summary.json"),
        )

        self.assertEqual(decision["recommendation"], "expand_sample")
        self.assertTrue(decision["checks"]["target_absent_both"])
        self.assertFalse(decision["checks"]["target_improved"])
        self.assertIn("cannot prove targeted improvement", " ".join(decision["why"]))


if __name__ == "__main__":
    unittest.main()
