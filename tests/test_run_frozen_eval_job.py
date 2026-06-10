import os
import time
import unittest

from scripts.run_frozen_eval_job import (
    _compact_progress_data,
    _raise_if_runtime_exceeded,
    _resolve_max_runtime_seconds,
    _retry_env_overrides,
)


class RunFrozenEvalJobTests(unittest.TestCase):
    def test_compacts_large_progress_payload(self) -> None:
        payload = {
            "strategy_versions": [{"large": True}],
            "event": {
                "id": 7,
                "day": 2,
                "phase": "day_vote",
                "event_type": "vote",
                "public_text": "P1 投票给 P2。" * 50,
                "actor_id": 1,
                "target_id": 2,
                "secondary_target_id": None,
                "decision": {
                    "action": "vote",
                    "target_id": 2,
                    "secondary_target_id": None,
                    "speech": "很长的发言" * 80,
                    "reason": "很长的理由" * 80,
                    "confidence": 0.82,
                    "metadata": {
                        "llm_total_elapsed_ms": 1234.5,
                        "llm_call_count": 1,
                        "skill_usage": {
                            "matched_skills": [
                                {"skill_id": "skill_a", "title": "投票分线", "score": 0.5},
                                {"skill_id": "skill_b", "title": "倒钩", "score": 0.4},
                                {"skill_id": "skill_c", "title": "悍跳", "score": 0.3},
                                {"skill_id": "skill_d", "title": "额外", "score": 0.2},
                            ]
                        },
                    },
                },
            },
            "summary": {
                "day": 2,
                "alive_count": 5,
                "dead_count": 4,
                "wolves_alive": 2,
                "civilians_alive": 1,
                "gods_alive": 2,
                "winner": None,
                "win_reason": "",
                "players": [{"id": 1, "role": "werewolf"}],
            },
            "aggregate": {
                "average_scores": {"overall": 80.0},
                "winner_counts": {"werewolves": 1},
                "mistake_counts": {"werewolf_exposed_pack_vote": 1},
                "mistakes_per_game": 1.0,
                "extra": "drop",
            },
        }

        compact = _compact_progress_data(payload)

        self.assertNotIn("strategy_versions", compact)
        self.assertLessEqual(len(compact["event"]["public_text"]), 220)
        self.assertEqual(compact["event"]["decision"]["llm_total_elapsed_ms"], 1234.5)
        self.assertEqual(len(compact["event"]["decision"]["matched_skills"]), 3)
        self.assertNotIn("players", compact["summary"])
        self.assertEqual(compact["aggregate"]["mistakes_per_game"], 1.0)

    def test_retry_env_overrides_follow_provider(self) -> None:
        self.assertEqual(_retry_env_overrides("ark", None, 3), {"ARK_MAX_RETRIES": "3"})
        self.assertEqual(
            _retry_env_overrides("dashscope", "deepseek-v4-flash", 2),
            {"DASHSCOPE_MAX_RETRIES": "2"},
        )
        self.assertEqual(
            _retry_env_overrides("dashscope-native", "deepseek-v3.2", 1),
            {"DASHSCOPE_GENERATION_MAX_RETRIES": "1"},
        )
        self.assertEqual(
            _retry_env_overrides("dashscope", "deepseek-v3.2", 1),
            {"DASHSCOPE_GENERATION_MAX_RETRIES": "1"},
        )

    def test_max_runtime_resolution_and_guard(self) -> None:
        old = os.environ.get("FROZEN_EVAL_MAX_RUNTIME_SECONDS")
        try:
            os.environ["FROZEN_EVAL_MAX_RUNTIME_SECONDS"] = "12.5"
            self.assertEqual(_resolve_max_runtime_seconds(None), 12.5)
            self.assertIsNone(_resolve_max_runtime_seconds(0))
            self.assertEqual(_resolve_max_runtime_seconds(5), 5)
        finally:
            if old is None:
                os.environ.pop("FROZEN_EVAL_MAX_RUNTIME_SECONDS", None)
            else:
                os.environ["FROZEN_EVAL_MAX_RUNTIME_SECONDS"] = old

        _raise_if_runtime_exceeded(time.monotonic(), None)
        with self.assertRaises(TimeoutError):
            _raise_if_runtime_exceeded(time.monotonic() - 10, 1)


if __name__ == "__main__":
    unittest.main()
