from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.finalize_frozen_eval_artifacts import finalize


def _result_payload() -> dict:
    return {
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
                "skill_quality": 80.0,
                "team_contribution": 74.0,
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
                "skill_quality": 79.0,
                "team_contribution": 72.0,
                "wolf_deception_quality": 82.0,
            },
            "winner_counts": {"werewolves": 1, "villagers": 2},
            "mistakes_per_game": 2.0,
            "mistake_counts": {"werewolf_exposed_pack_vote": 3},
        },
        "delta": {"overall": 4.0, "mistakes_per_game": -1.0},
        "errors": {"candidate": [], "baseline": []},
        "archives": {"candidate": [], "baseline": []},
    }


class FinalizeFrozenEvalArtifactsTests(unittest.TestCase):
    def test_not_ready_without_result_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = finalize(Path(tmp))

            self.assertEqual(result["status"], "not_ready")
            self.assertEqual(result["actions"], [])

    def test_records_error_artifact_without_result_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            error_path = root / "run_error.json"
            error_path.write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "error": "TimeoutError",
                        "message": "limit exceeded",
                        "progress_path": "run_progress.jsonl",
                    }
                ),
                encoding="utf-8",
            )
            log_path = root / "skill_change_log.json"
            log_path.write_text(
                json.dumps({"entries": [{"change_id": "r5", "validation": []}]}),
                encoding="utf-8",
            )

            result = finalize(root, change_id="r5", log_path=log_path)

            self.assertEqual(result["status"], "failed_no_result")
            self.assertIn("change_log_error_recorded", result["actions"])
            data = json.loads(log_path.read_text(encoding="utf-8"))
            entry = data["entries"][0]
            self.assertEqual(entry["decision"], "expanded_eval_rerun_required")
            record = entry["validation"][0]
            self.assertEqual(record["type"], "frozen_eval_error")
            self.assertEqual(record["error"], "TimeoutError")

    def test_creates_summary_gate_and_records_change_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_path = root / "run_frozen_eval_result.json"
            result_path.write_text(json.dumps(_result_payload()), encoding="utf-8")
            log_path = root / "skill_change_log.json"
            log_path.write_text(
                json.dumps({"entries": [{"change_id": "r3", "validation": []}]}),
                encoding="utf-8",
            )

            result = finalize(root, change_id="r3", log_path=log_path)

            self.assertEqual(result["status"], "finalized")
            self.assertIn("summary_created", result["actions"])
            self.assertIn("gate_created", result["actions"])
            self.assertIn("change_log_recorded", result["actions"])
            self.assertTrue((root / "run_frozen_eval_result_compact_summary.json").exists())
            self.assertTrue((root / "run_frozen_eval_gate.json").exists())
            data = json.loads(log_path.read_text(encoding="utf-8"))
            record = data["entries"][0]["validation"][0]
            self.assertEqual(record["gate_recommendation"], "promote_after_codex_review")

    def test_can_create_review_pack_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = root / "candidate_game.json"
            archive_path.write_text(
                json.dumps(
                    {
                        "game": {
                            "game_id": "g1",
                            "seed": 1,
                            "version_id": "candidate_v1",
                            "winner": "villagers",
                            "players": [
                                {"id": 5, "name": "P5", "role": "werewolf", "alive": True},
                                {"id": 9, "name": "P9", "role": "werewolf", "alive": True},
                                {"id": 2, "name": "P2", "role": "seer", "alive": True},
                            ],
                            "events": [
                                {
                                    "id": 1,
                                    "day": 2,
                                    "phase": "day_vote",
                                    "event_type": "vote",
                                    "actor_id": 5,
                                    "target_id": 2,
                                    "public_text": "P5 投票给 P2。",
                                    "decision": {"action": "vote", "target_id": 2},
                                }
                            ],
                        },
                        "report": {
                            "mistakes": [
                                {
                                    "type": "werewolf_exposed_pack_vote",
                                    "day": 2,
                                    "actor_id": 5,
                                    "target_id": 2,
                                    "wolf_voters": [5, 9],
                                }
                            ]
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            payload = _result_payload()
            payload["archives"] = {"candidate": [str(archive_path)], "baseline": []}
            result_path = root / "run_frozen_eval_result.json"
            result_path.write_text(json.dumps(payload), encoding="utf-8")

            result = finalize(root, review_pack=True)

            self.assertEqual(result["status"], "finalized")
            self.assertIn("review_pack_created", result["actions"])
            review_pack_path = root / "run_frozen_eval_result_skill_review_pack.json"
            self.assertTrue(review_pack_path.exists())
            review_pack = json.loads(review_pack_path.read_text(encoding="utf-8"))
            self.assertEqual(review_pack["case_count"], 1)


if __name__ == "__main__":
    unittest.main()
