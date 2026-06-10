from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.summarize_frozen_eval_result import _resolve_result_path, summarize


class SummarizeFrozenEvalResultTests(unittest.TestCase):
    def test_summarize_keeps_compact_metrics_and_recommendation(self) -> None:
        payload = {
            "version_id": "candidate_v1",
            "baseline_version_id": "baseline_v1",
            "games": 3,
            "completed_games": {"candidate": 3, "baseline": 3},
            "seed_alignment": {
                "candidate_completed_seeds": [1, 2, 3],
                "baseline_completed_seeds": [1, 2, 3],
            },
            "candidate": {
                "average_scores": {
                    "overall": 82.0,
                    "vote_quality": 70.0,
                    "skill_quality": 81.0,
                    "team_contribution": 75.0,
                    "wolf_deception_quality": 90.0,
                },
                "winner_counts": {"werewolves": 2, "villagers": 1},
                "mistakes_per_game": 1.0,
                "mistake_counts": {"werewolf_exposed_pack_vote": 1},
            },
            "baseline": {
                "average_scores": {
                    "overall": 78.0,
                    "vote_quality": 68.0,
                    "skill_quality": 80.0,
                    "team_contribution": 73.0,
                    "wolf_deception_quality": 84.0,
                },
                "winner_counts": {"werewolves": 1, "villagers": 2},
                "mistakes_per_game": 2.0,
                "mistake_counts": {"werewolf_exposed_pack_vote": 3},
            },
            "delta": {"overall": 4.0, "mistakes_per_game": -1.0},
            "archives": {"candidate": ["candidate.json"], "baseline": ["baseline.json"]},
            "errors": {"candidate": [], "baseline": []},
        }

        summary = summarize(payload, result_path=Path("result.json"))

        self.assertEqual(summary["candidate_version"], "candidate_v1")
        self.assertEqual(summary["baseline_version"], "baseline_v1")
        self.assertEqual(summary["candidate"]["overall"], 82.0)
        self.assertEqual(summary["baseline"]["mistake_counts"]["werewolf_exposed_pack_vote"], 3)
        self.assertEqual(summary["recommendation"], "candidate_promising_expand_or_promote_after_review")

    def test_resolve_result_path_uses_latest_result_in_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = root / "a_frozen_eval_result.json"
            newer = root / "b_frozen_eval_result.json"
            older.write_text(json.dumps({"old": True}), encoding="utf-8")
            newer.write_text(json.dumps({"new": True}), encoding="utf-8")

            self.assertEqual(_resolve_result_path(root), newer)

    def test_recommendation_expands_when_target_mistake_not_reproduced(self) -> None:
        payload = {
            "version_id": "candidate_v1",
            "baseline_version_id": "baseline_v1",
            "candidate": {
                "average_scores": {"overall": 80.0},
                "mistakes_per_game": 1.0,
                "mistake_counts": {"seer_failed_to_reveal_wolf": 1},
            },
            "baseline": {
                "average_scores": {"overall": 80.2},
                "mistakes_per_game": 1.0,
                "mistake_counts": {"witch_mechanical_n1_save": 1},
            },
            "delta": {"overall": -0.2, "mistakes_per_game": 0.0},
        }

        summary = summarize(payload, result_path=Path("result.json"))

        self.assertEqual(summary["recommendation"], "target_not_reproduced_expand_sample")


if __name__ == "__main__":
    unittest.main()
