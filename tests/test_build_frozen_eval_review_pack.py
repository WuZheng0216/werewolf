from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_frozen_eval_review_pack import build_review_pack


class BuildFrozenEvalReviewPackTests(unittest.TestCase):
    def test_builds_target_mistake_pack_with_skill_snippets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = root / "candidate_game.json"
            archive_path.write_text(
                json.dumps(
                    {
                        "game": {
                            "game_id": "g1",
                            "seed": 42,
                            "version_id": "candidate",
                            "winner": "villagers",
                            "players": [
                                {"id": 5, "name": "P5", "role": "werewolf", "alive": True},
                                {"id": 9, "name": "P9", "role": "werewolf", "alive": True},
                                {"id": 2, "name": "P2", "role": "seer", "alive": True},
                            ],
                            "events": [
                                {
                                    "id": 9,
                                    "day": 2,
                                    "phase": "night_wolf",
                                    "event_type": "kill",
                                    "actor_id": 5,
                                    "target_id": 2,
                                    "public_text": "private night event",
                                },
                                {
                                    "id": 10,
                                    "day": 2,
                                    "phase": "day_vote",
                                    "event_type": "vote",
                                    "actor_id": 5,
                                    "target_id": 2,
                                    "public_text": "P5 投票给 P2。",
                                    "decision": {
                                        "action": "vote",
                                        "target_id": 2,
                                        "reason": "质疑 P2 查验链。",
                                        "metadata": {
                                            "skill_usage": {
                                                "matched_skills": [
                                                    {
                                                        "skill_id": "wolf_vote_split",
                                                        "title": "狼队投票分线",
                                                        "score": 0.7,
                                                        "signals": ["split_vote"],
                                                    }
                                                ]
                                            }
                                        },
                                    },
                                },
                                {
                                    "id": 11,
                                    "day": 2,
                                    "phase": "day_vote",
                                    "event_type": "vote",
                                    "actor_id": 9,
                                    "target_id": 2,
                                    "public_text": "P9 投票给 P2。",
                                    "decision": {"action": "vote", "target_id": 2, "reason": "跟随质疑。"},
                                },
                                {
                                    "id": 12,
                                    "day": 1,
                                    "phase": "day_vote",
                                    "event_type": "vote",
                                    "actor_id": 1,
                                    "target_id": 3,
                                    "public_text": "unrelated",
                                },
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
            result_path = root / "run_frozen_eval_result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "version_id": "candidate",
                        "baseline_version_id": "baseline",
                        "llm_provider": "dashscope",
                        "llm_model": "deepseek-v4-flash",
                        "completed_games": {"candidate": 1, "baseline": 0},
                        "candidate": {
                            "mistake_counts": {"werewolf_exposed_pack_vote": 1},
                            "mistakes_per_game": 1.0,
                        },
                        "baseline": {"mistake_counts": {}, "mistakes_per_game": 0.0},
                        "delta": {"overall": -1.0},
                        "archives": {"candidate": [str(archive_path)], "baseline": []},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            pack = build_review_pack(result_path)

            self.assertEqual(pack["type"], "frozen_eval_skill_review_pack")
            self.assertFalse(pack["include_night"])
            self.assertEqual(pack["case_count"], 1)
            case = pack["cases"][0]
            self.assertEqual(case["mistake"]["type"], "werewolf_exposed_pack_vote")
            self.assertEqual([event["id"] for event in case["events"]], [10, 11])
            matched = case["events"][0]["decision"]["matched_skills"][0]
            self.assertEqual(matched["skill_id"], "wolf_vote_split")
            self.assertEqual({player["id"] for player in case["relevant_players"]}, {2, 5, 9})


if __name__ == "__main__":
    unittest.main()
