import os
import tempfile
import unittest

from werewolf_ai.memory_store import (
    active_memory_bank_by_role,
    list_role_memory_versions,
    load_memory_bank,
    mechanical_memory_penalty,
    save_role_memory_snapshot,
    save_review_memory_candidates,
)
from werewolf_ai.agents import default_strategy_versions
from werewolf_ai.models import Role


class MemoryBankTests(unittest.TestCase):
    def test_role_memory_versions_are_listed_for_ui(self) -> None:
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            os.chdir(tmp)
            try:
                save_role_memory_snapshot(
                    version_id="evolved_skill_r12_v2",
                    profiles=default_strategy_versions("evolved_skill_r12_v2"),
                    aggregate={"average_scores": {"overall": 83.18}, "mistakes_per_game": 0.67},
                    reflections={},
                    source_archives=[],
                    errors=[],
                    parent_id="evolved_skill_r10_v6",
                    promoted=True,
                    llm_provider="ark",
                    llm_model=None,
                    round_index=12,
                )

                versions = list_role_memory_versions()

                self.assertEqual([item["version_id"] for item in versions], ["evolved_skill_r12_v2"])
                self.assertTrue(versions[0]["promoted"])
                self.assertEqual(versions[0]["overall"], 83.18)
                self.assertEqual(versions[0]["mistakes_per_game"], 0.67)
            finally:
                os.chdir(cwd)

    def test_review_candidates_are_saved_and_auto_approved(self) -> None:
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            os.chdir(tmp)
            try:
                ids = save_review_memory_candidates(
                    {
                        "review_status": "ok",
                        "memory_candidates": [
                            {
                                "role": "werewolf",
                                "memory": "狼人早期要使用独立公开理由分散票型。",
                                "evidence_event_ids": [1, 2],
                                "confidence": 0.91,
                            },
                            {
                                "role": "seer",
                                "memory": "查到狼人后要明确报出 P 号和归票方向。",
                                "evidence_event_ids": [3],
                                "confidence": 0.7,
                            },
                            {
                                "role": "seer",
                                "memory": "预言家应该留警徽流。",
                                "evidence_event_ids": [4],
                                "confidence": 0.99,
                            },
                        ],
                    },
                    version_id="evolved_r2",
                    game_id="game-test",
                )
                bank = load_memory_bank()
                active = active_memory_bank_by_role()

                self.assertEqual(len(ids), 2)
                self.assertEqual(len(bank["items"]), 2)
                self.assertEqual(bank["items"][0]["status"], "pending")
                self.assertIn("狼人早期要使用独立公开理由分散票型。", active[Role.WEREWOLF])
                self.assertEqual(active[Role.SEER], [])
                self.assertNotIn("预言家应该留警徽流。", [item["memory"] for item in bank["items"]])
            finally:
                os.chdir(cwd)

    def test_mechanical_power_role_memories_require_review(self) -> None:
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            os.chdir(tmp)
            try:
                ids = save_review_memory_candidates(
                    {
                        "review_status": "ok",
                        "memory_candidates": [
                            {
                                "role": "seer",
                                "memory": "预言家首日应该立即起跳公开身份，尽早带队。",
                                "evidence_event_ids": [1],
                                "confidence": 0.99,
                            },
                            {
                                "role": "witch",
                                "memory": "女巫首夜应该优先使用解药救人，避免第一晚死人。",
                                "evidence_event_ids": [2],
                                "confidence": 0.98,
                            },
                        ],
                    },
                    version_id="evolved_r5",
                    game_id="game-mechanical",
                )
                bank = load_memory_bank()
                active = active_memory_bank_by_role()

                self.assertEqual(len(ids), 2)
                self.assertTrue(all(item["status"] == "pending" for item in bank["items"]))
                self.assertTrue(all(item["mechanical_memory_penalty"] > 0 for item in bank["items"]))
                self.assertEqual(active[Role.SEER], [])
                self.assertEqual(active[Role.WITCH], [])
                seer_item = next(item for item in bank["items"] if item["role"] == "seer")
                self.assertGreater(mechanical_memory_penalty(Role.SEER, seer_item["memory"]), 0)
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
