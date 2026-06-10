import os
import tempfile
import unittest

from werewolf_ai.engine import GameEngine
from werewolf_ai.memory_retriever import retrieve_memories
from werewolf_ai.memory_store import save_review_memory_candidates
from werewolf_ai.models import ActionType, Phase, Role


class MemoryRetrieverTests(unittest.TestCase):
    def test_bm25_retrieves_role_phase_relevant_memories(self) -> None:
        items = [
            {
                "id": "wolf-vote",
                "role": "werewolf",
                "memory": "狼人白天投票前如果队友都压同一目标，应主动分票或弃票，避免票型暴露狼队协同。",
                "status": "approved_auto",
                "confidence": 0.93,
                "seen_count": 4,
            },
            {
                "id": "wolf-night",
                "role": "werewolf",
                "memory": "狼人夜晚优先选择疑似神职作为落刀目标，白天不要复述队友模板。",
                "status": "approved_auto",
                "confidence": 0.88,
                "seen_count": 2,
            },
            {
                "id": "seer-claim",
                "role": "seer",
                "memory": "预言家查到狼人后应明确报出 P 号和归票方向。",
                "status": "approved_auto",
                "confidence": 0.95,
            },
            {
                "id": "pending",
                "role": "werewolf",
                "memory": "狼人任何时候都应该强行悍跳预言家。",
                "status": "pending",
                "confidence": 0.99,
            },
        ]

        result = retrieve_memories(
            role=Role.WEREWOLF,
            phase=Phase.DAY_VOTE,
            public_history=[
                {
                    "id": 12,
                    "event_type": "speech",
                    "public_text": "P4 说 P9 像狼，P2 和 P3 都跟着压 P9。",
                }
            ],
            private_knowledge={
                "pack_ids": [1, 2, 3],
                "pack_plan": {"tactic_id": "split_pressure", "goal": "避免三狼同票同理由。"},
            },
            legal_actions=[ActionType.VOTE, ActionType.PASS],
            player_profile={"persona": "节奏推动型", "vote_style": "可以分票或弃票降低暴露"},
            max_items=2,
            memory_items=items,
        )

        self.assertGreaterEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "wolf-vote")
        self.assertIn("vote", result[0]["tags"])
        self.assertTrue(any(term in result[0]["matched_terms"] for term in {"投票", "票型", "分票", "弃票"}))
        self.assertNotIn("pending", {item["id"] for item in result})

    def test_retrieval_keeps_roles_isolated(self) -> None:
        result = retrieve_memories(
            role=Role.VILLAGER,
            phase=Phase.DAY_SPEECH,
            public_history=[],
            private_knowledge={},
            legal_actions=[ActionType.SPEAK],
            memory_items=[
                {
                    "id": "wolf-only",
                    "role": "werewolf",
                    "memory": "狼人要避免同票暴露。",
                    "status": "approved_auto",
                    "confidence": 0.9,
                }
            ],
        )

        self.assertEqual(result, [])

    def test_retrieval_deweights_mechanical_power_role_memories(self) -> None:
        result = retrieve_memories(
            role=Role.WITCH,
            phase=Phase.NIGHT_WITCH,
            public_history=[],
            private_knowledge={"attacked_tonight": 3, "has_antidote": True, "has_poison": True},
            legal_actions=[ActionType.SAVE, ActionType.PASS],
            max_items=2,
            memory_items=[
                {
                    "id": "witch-mechanical",
                    "role": "witch",
                    "memory": "女巫首夜应该优先使用解药救人，避免第一晚死人。",
                    "status": "approved_auto",
                    "confidence": 0.99,
                    "seen_count": 8,
                },
                {
                    "id": "witch-conditional",
                    "role": "witch",
                    "memory": "女巫首夜救人不是固定动作；非自救时先比较被刀目标价值和保留解药收益，理由不足可以不救。",
                    "status": "approved_auto",
                    "confidence": 0.78,
                    "seen_count": 1,
                },
            ],
        )

        self.assertGreaterEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "witch-conditional")
        mechanical = next(item for item in result if item["id"] == "witch-mechanical")
        self.assertGreater(mechanical["memory_penalty"], 0)

    def test_engine_injects_bm25_retrieval_into_observation(self) -> None:
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            os.chdir(tmp)
            try:
                save_review_memory_candidates(
                    {
                        "review_status": "ok",
                        "memory_candidates": [
                            {
                                "role": "werewolf",
                                "memory": "狼人投票阶段如果证据不足，可以分票或弃票，避免三狼同票暴露。",
                                "confidence": 0.96,
                            }
                        ],
                    },
                    version_id="evolved_r3",
                    game_id="game-test",
                )
                engine = GameEngine(seed=43, llm_decider=object(), memory_retrieval_top_k=3)
                engine.day = 1
                wolf = next(player for player in engine.players if player.role == Role.WEREWOLF)
                observation = engine.build_observation(wolf.id, Phase.DAY_VOTE, [ActionType.VOTE, ActionType.PASS])

                self.assertTrue(observation.retrieved_memories)
                self.assertTrue(any("BM25记忆召回" in item for item in observation.strategy_memory))
                self.assertEqual(observation.redacted_for_log()["retrieved_memory_count"], 1)
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
