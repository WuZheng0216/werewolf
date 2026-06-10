from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from scripts.record_frozen_eval_summary import build_record, update_log


class RecordFrozenEvalSummaryTests(unittest.TestCase):
    def test_build_record_extracts_target_metrics(self) -> None:
        summary = {
            "source_result_path": "result.json",
            "candidate_version": "candidate",
            "baseline_version": "baseline",
            "games": 3,
            "completed_games": {"candidate": 3, "baseline": 3},
            "seed_alignment": {
                "candidate_completed_seeds": [1, 2, 3],
                "baseline_completed_seeds": [1, 2, 3],
            },
            "candidate": {
                "overall": 82.0,
                "vote_quality": 70.0,
                "wolf_deception_quality": 91.0,
                "mistakes_per_game": 1.0,
                "mistake_counts": {"werewolf_exposed_pack_vote": 1},
            },
            "baseline": {
                "overall": 78.0,
                "vote_quality": 65.0,
                "wolf_deception_quality": 80.0,
                "mistakes_per_game": 2.0,
                "mistake_counts": {"werewolf_exposed_pack_vote": 3},
            },
            "delta": {"overall": 4.0, "mistakes_per_game": -1.0},
            "recommendation": "candidate_promising_expand_or_promote_after_review",
        }

        record = build_record(summary, summary_path=Path("summary.json"))

        self.assertEqual(record["type"], "seed_aligned_frozen_eval_expand_summary")
        self.assertEqual(record["candidate_pack_vote_mistakes"], 1)
        self.assertEqual(record["baseline_pack_vote_mistakes"], 3)
        self.assertEqual(record["result"], "candidate_promising_expand_or_promote_after_review")
        self.assertEqual(record["gate_recommendation"], "promote_after_codex_review")
        self.assertTrue(record["gate_checks"]["seed_aligned"])
        self.assertEqual(record["source_review_pack_path"], "result_skill_review_pack.json")
        self.assertFalse(record["source_review_pack_exists"])

    def test_build_record_marks_existing_review_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_path = root / "run_frozen_eval_result.json"
            review_path = root / "run_frozen_eval_result_skill_review_pack.json"
            review_path.write_text("{}", encoding="utf-8")
            summary = {
                "source_result_path": str(result_path),
                "candidate": {},
                "baseline": {},
                "delta": {},
            }

            record = build_record(summary, summary_path=root / "run_frozen_eval_result_compact_summary.json")

            self.assertEqual(record["source_review_pack_path"], str(review_path))
            self.assertTrue(record["source_review_pack_exists"])

    def test_update_log_replaces_same_summary_record_and_sets_review_decision(self) -> None:
        data = {
            "entries": [
                {
                    "change_id": "r3",
                    "decision": "old",
                    "validation": [
                        {
                            "type": "seed_aligned_frozen_eval_expand_summary",
                            "source_summary_path": "summary.json",
                            "result": "old_result",
                        }
                    ],
                }
            ]
        }
        record = {
            "type": "seed_aligned_frozen_eval_expand_summary",
            "source_summary_path": "summary.json",
            "result": "candidate_needs_revision",
        }

        update_log(data, change_id="r3", record=record)

        entry = data["entries"][0]
        self.assertEqual(entry["decision"], "expanded_eval_needs_revision_review")
        self.assertEqual(len(entry["validation"]), 1)
        self.assertEqual(entry["validation"][0]["result"], "candidate_needs_revision")

    def test_update_log_uses_gate_recommendation_for_expand_sample(self) -> None:
        data = {"entries": [{"change_id": "r5", "decision": "old", "validation": []}]}
        record = {
            "type": "seed_aligned_frozen_eval_expand_summary",
            "source_summary_path": "summary.json",
            "result": "target_not_reproduced_expand_sample",
            "gate_recommendation": "expand_sample",
        }

        update_log(data, change_id="r5", record=record)

        self.assertEqual(data["entries"][0]["decision"], "expanded_eval_expand_sample")


if __name__ == "__main__":
    unittest.main()
