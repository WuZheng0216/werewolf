import json
import tempfile
import unittest
from pathlib import Path

from scripts.summarize_codex_skill_lab import summarize_target


class CodexSkillLabSummaryTests(unittest.TestCase):
    def test_summarizes_progress_votes_and_latency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            batch_dir = Path(tmp) / "batch_test"
            batch_dir.mkdir()
            progress_path = batch_dir / "sample_progress.jsonl"
            rows = [
                {
                    "time": "2026-06-09T00:00:00",
                    "event": "game_started",
                    "data": {
                        "batch": {"game_index": 0, "attempt_index": 0, "seed": 1},
                        "players": [
                            {"id": 1, "role": "werewolf"},
                            {"id": 2, "role": "werewolf"},
                            {"id": 3, "role": "villager"},
                        ],
                    },
                },
                _vote_row(actor=1, target=3, latency=1000.0),
                _vote_row(actor=2, target=3, latency=2000.0),
                {
                    "time": "2026-06-09T00:00:03",
                    "event": "game_completed",
                    "data": {
                        "batch": {"game_index": 0, "attempt_index": 0, "seed": 1},
                        "game": {"game_id": "g1"},
                        "archive_path": "logs/games/g1.json",
                    },
                },
                {
                    "time": "2026-06-09T00:00:04",
                    "event": "game_completed",
                    "data": {
                        "batch": {"game_index": 1, "attempt_index": 1, "seed": 2},
                        "game": {"game_id": "g2"},
                        "archive_path": "logs/games/g2.json",
                    },
                },
            ]
            progress_path.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
                encoding="utf-8",
            )

            summary = summarize_target(batch_dir)

        progress = summary["progress"]
        self.assertEqual(summary["status"], "running_or_interrupted")
        self.assertEqual(progress["completed_games_seen"], 2)
        self.assertEqual(progress["latency_ms"]["count"], 2)
        self.assertEqual(progress["latency_ms"]["max"], 2000.0)
        self.assertEqual(progress["slow_decisions"][0]["latency_ms"], 2000.0)
        self.assertEqual(progress["slow_decisions"][0]["actor_id"], 2)
        self.assertEqual(progress["wolf_day_votes"][0]["game"], "g0_a0_s1_d1")
        self.assertEqual(progress["wolf_day_votes"][0]["target_counts"]["P3"], 2)
        self.assertEqual(progress["wolf_day_votes"][0]["max_same_target"], 2)
        self.assertEqual(progress["wolf_skill_matches"]["wolf_vote_skill"], 2)


def _vote_row(*, actor: int, target: int, latency: float) -> dict:
    return {
        "time": "2026-06-09T00:00:01",
        "event": "game_event",
        "data": {
            "batch": {"game_index": 0, "attempt_index": 0, "seed": 1},
            "event": {
                "day": 1,
                "phase": "day_vote",
                "event_type": "vote",
                "actor_id": actor,
                "decision": {
                    "action": "vote",
                    "target_id": target,
                    "llm_total_elapsed_ms": latency,
                    "matched_skills": [{"skill_id": "wolf_vote_skill"}],
                },
            },
        },
    }


if __name__ == "__main__":
    unittest.main()
