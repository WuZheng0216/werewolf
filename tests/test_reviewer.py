import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from werewolf_ai.models import EvaluationReport, Faction, GameEvent, GameResult, Phase, PlayerState, Role
from werewolf_ai.reviewer import DEFAULT_REVIEW_TIMEOUT_SECONDS, LLMGameReviewer, ReviewRepairError, _configure_review_client


class FakeReviewChatClient:
    def __init__(self, content: dict | str | list[dict | str]):
        raw_items = content if isinstance(content, list) else [content]
        self.contents = [
            json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item)
            for item in raw_items
        ]
        self.config = SimpleNamespace(provider="fake", model="review-model", timeout_seconds=60.0, max_retries=1)
        self.messages = []
        self.messages_history = []
        self.calls = 0

    def chat(self, messages, *, temperature, max_tokens):
        self.messages = messages
        self.messages_history.append(messages)
        self.temperature = temperature
        self.max_tokens = max_tokens
        index = min(self.calls, len(self.contents) - 1)
        self.calls += 1
        return self.contents[index]


class ReviewerTests(unittest.TestCase):
    def test_llm_review_normalizes_memory_candidates(self) -> None:
        client = FakeReviewChatClient(
            {
                "game_summary": "狼队集中投票暴露，好人归票成功。",
                "good_cases": [
                    {
                        "role": "seer",
                        "player_id": 2,
                        "event_ids": [1, 999],
                        "reason": "预言家公开查杀并推动归票。",
                        "lesson": "查杀要明确给出 P 号和归票方向。",
                    }
                ],
                "bad_cases": [],
                "role_reflections": {"seer": ["查到狼后尽早建立公共议程。"]},
                "memory_candidates": [
                    {
                        "role": "seer",
                        "memory": "查到狼人后，应明确给出查验轮次、P 号和归票建议。",
                        "evidence_event_ids": [1, 999],
                        "confidence": 0.91,
                    },
                    {
                        "role": "unknown",
                        "memory": "这条会被过滤。",
                        "evidence_event_ids": [1],
                        "confidence": 1,
                    },
                ],
                "confidence": 0.82,
            }
        )
        _configure_review_client(client)
        review = LLMGameReviewer(client).review_game(_game_result(), _report())

        self.assertEqual(review["review_status"], "ok")
        self.assertEqual(review["memory_candidates"][0]["role"], "seer")
        self.assertEqual(review["memory_candidates"][0]["evidence_event_ids"], [1])
        self.assertEqual(len(review["memory_candidates"]), 1)
        self.assertEqual(review["metadata"]["llm_provider"], "fake")
        self.assertEqual(review["metadata"]["llm_timeout_seconds"], DEFAULT_REVIEW_TIMEOUT_SECONDS)
        self.assertIn("上帝视角复盘官", client.messages[0]["content"])
        self.assertIn("游戏规则_GameRules", client.messages[1]["content"])
        self.assertIn("不要称为警徽流", client.messages[1]["content"])

    def test_reviewer_timeout_can_be_configured_independently(self) -> None:
        old_values = {key: os.environ.get(key) for key in ("LLM_REVIEW_TIMEOUT_SECONDS", "LLM_REVIEW_MAX_RETRIES")}
        try:
            os.environ["LLM_REVIEW_TIMEOUT_SECONDS"] = "240"
            os.environ["LLM_REVIEW_MAX_RETRIES"] = "3"
            client = FakeReviewChatClient({})
            client.config.timeout_seconds = 60
            client.config.max_retries = 1

            _configure_review_client(client)

            self.assertEqual(client.config.timeout_seconds, 240.0)
            self.assertEqual(client.config.max_retries, 3)
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_llm_review_tolerates_non_scalar_player_id(self) -> None:
        client = FakeReviewChatClient(
            {
                "game_summary": "复盘。",
                "good_cases": [
                    {
                        "role": "villager",
                        "player_id": [2],
                        "event_ids": [1],
                        "reason": "模型偶尔会把 player_id 输出成数组。",
                        "lesson": "解析器应容错。",
                    }
                ],
                "bad_cases": [],
                "role_reflections": {},
                "memory_candidates": [],
                "confidence": 0.8,
            }
        )
        review = LLMGameReviewer(client).review_game(_game_result(), _report())
        self.assertEqual(review["review_status"], "ok")
        self.assertIsNone(review["good_cases"][0]["player_id"])

    def test_review_adds_werewolf_deception_memory_when_missing(self) -> None:
        client = FakeReviewChatClient(
            {
                "game_summary": "狼人发言保守。",
                "good_cases": [],
                "bad_cases": [],
                "role_reflections": {"werewolf": ["狼队需要提升欺骗性。"]},
                "memory_candidates": [],
                "confidence": 0.78,
            }
        )
        review = LLMGameReviewer(client).review_game(_game_result_with_wolf_speech(), _report())
        wolf_memories = [item for item in review["memory_candidates"] if item["role"] == "werewolf"]

        self.assertEqual(len(wolf_memories), 1)
        self.assertTrue(any(token in wolf_memories[0]["memory"] for token in ("欺骗", "对跳", "质疑查验链", "倒钩", "分票")))
        self.assertEqual(wolf_memories[0]["confidence"], 0.88)

    def test_review_adds_power_role_timing_memories_when_missing(self) -> None:
        client = FakeReviewChatClient(
            {
                "game_summary": "预言家和女巫都出现固定化动作。",
                "good_cases": [],
                "bad_cases": [],
                "role_reflections": {},
                "memory_candidates": [],
                "confidence": 0.78,
            }
        )
        review = LLMGameReviewer(client).review_game(_game_result_with_mechanical_power_roles(), _report())
        seer_memories = [item for item in review["memory_candidates"] if item["role"] == "seer"]
        witch_memories = [item for item in review["memory_candidates"] if item["role"] == "witch"]

        self.assertTrue(any("不必机械首日公开身份" in item["memory"] for item in seer_memories))
        self.assertTrue(any("首夜救人不是固定动作" in item["memory"] for item in witch_memories))
        self.assertEqual(seer_memories[0]["evidence_event_ids"], [3])
        self.assertEqual(witch_memories[0]["evidence_event_ids"], [2])

    def test_llm_review_repairs_invalid_json_and_saves_raw_output(self) -> None:
        valid = {
            "game_summary": "修复后复盘。",
            "good_cases": [],
            "bad_cases": [],
            "role_reflections": {"werewolf": [], "seer": [], "witch": [], "hunter": [], "villager": []},
            "memory_candidates": [],
            "confidence": 0.7,
        }
        old_dir = os.environ.get("LLM_REVIEW_RAW_DIR")
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["LLM_REVIEW_RAW_DIR"] = tmpdir
            try:
                client = FakeReviewChatClient(['{"game_summary":"少一个逗号" "confidence":0.5}', valid])
                review = LLMGameReviewer(client).review_game(_game_result(), _report())
            finally:
                if old_dir is None:
                    os.environ.pop("LLM_REVIEW_RAW_DIR", None)
                else:
                    os.environ["LLM_REVIEW_RAW_DIR"] = old_dir

            self.assertEqual(review["review_status"], "ok")
            self.assertEqual(client.calls, 2)
            self.assertEqual(review["metadata"]["llm_repair_attempts"], 1)
            raw_path = review["metadata"]["raw_invalid_output_path"]
            self.assertTrue(os.path.exists(raw_path))
            self.assertIn("非法输出片段", client.messages_history[1][-1]["content"])

    def test_llm_review_raises_with_debug_paths_when_repair_fails(self) -> None:
        old_dir = os.environ.get("LLM_REVIEW_RAW_DIR")
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["LLM_REVIEW_RAW_DIR"] = tmpdir
            try:
                client = FakeReviewChatClient(['{"bad"', '{"still_bad"'])
                with self.assertRaises(ReviewRepairError) as context:
                    LLMGameReviewer(client).review_game(_game_result(), _report())
            finally:
                if old_dir is None:
                    os.environ.pop("LLM_REVIEW_RAW_DIR", None)
                else:
                    os.environ["LLM_REVIEW_RAW_DIR"] = old_dir

            self.assertTrue(os.path.exists(context.exception.raw_output_path))
            self.assertTrue(os.path.exists(context.exception.repair_output_path))


def _game_result() -> GameResult:
    players = [
        PlayerState(id=1, name="P1", role=Role.WEREWOLF, faction=Faction.WEREWOLVES),
        PlayerState(id=2, name="P2", role=Role.SEER, faction=Faction.VILLAGERS),
    ]
    event = GameEvent(
        id=1,
        day=1,
        phase=Phase.DAY_SPEECH,
        event_type="speech",
        public_text="P2 发言：我查杀 P1，今天归票 P1。",
        actor_id=2,
        public_payload={"speech": "我查杀 P1，今天归票 P1。"},
    )
    return GameResult(
        game_id="review-test",
        seed=1,
        version_id="initial",
        winner=Faction.VILLAGERS,
        win_reason="wolves_eliminated",
        day=1,
        players=players,
        events=[event],
        observation_samples=[],
    )


def _game_result_with_wolf_speech() -> GameResult:
    players = [
        PlayerState(id=1, name="P1", role=Role.WEREWOLF, faction=Faction.WEREWOLVES),
        PlayerState(id=2, name="P2", role=Role.SEER, faction=Faction.VILLAGERS),
        PlayerState(id=3, name="P3", role=Role.VILLAGER, faction=Faction.VILLAGERS),
    ]
    events = [
        GameEvent(
            id=1,
            day=1,
            phase=Phase.DAY_SPEECH,
            event_type="speech",
            public_text="P1 发言：我是好人牌，先听大家发言。",
            actor_id=1,
            public_payload={"speech": "我是好人牌，先听大家发言。"},
        ),
        GameEvent(
            id=2,
            day=1,
            phase=Phase.DAY_SPEECH,
            event_type="speech",
            public_text="P2 发言：我是预言家，昨夜查验 P3 是金水，后续查验 P1。",
            actor_id=2,
            public_payload={"speech": "我是预言家，昨夜查验 P3 是金水，后续查验 P1。"},
        ),
    ]
    return GameResult(
        game_id="review-wolf-test",
        seed=2,
        version_id="initial",
        winner=Faction.VILLAGERS,
        win_reason="crafted",
        day=1,
        players=players,
        events=events,
        observation_samples=[],
    )


def _game_result_with_mechanical_power_roles() -> GameResult:
    players = [
        PlayerState(id=1, name="P1", role=Role.SEER, faction=Faction.VILLAGERS),
        PlayerState(id=2, name="P2", role=Role.WITCH, faction=Faction.VILLAGERS),
        PlayerState(id=3, name="P3", role=Role.VILLAGER, faction=Faction.VILLAGERS),
        PlayerState(id=4, name="P4", role=Role.WEREWOLF, faction=Faction.WEREWOLVES),
    ]
    events = [
        GameEvent(
            id=1,
            day=1,
            phase=Phase.NIGHT_SEER,
            event_type="seer_inspect",
            public_text="P1 已完成查验。",
            actor_id=1,
            target_id=3,
            private_payload={"target_id": 3, "result": "villagers"},
        ),
        GameEvent(
            id=2,
            day=1,
            phase=Phase.NIGHT_WITCH,
            event_type="witch_action",
            public_text="P2 已完成女巫行动。",
            actor_id=2,
            target_id=3,
            private_payload={"attacked_target": 3, "saved_target": 3},
        ),
        GameEvent(
            id=3,
            day=1,
            phase=Phase.DAY_SPEECH,
            event_type="speech",
            public_text="P1 发言：我是预言家，昨晚查验 P3 是金水，后续查验 P4。",
            actor_id=1,
            public_payload={"speech": "我是预言家，昨晚查验 P3 是金水，后续查验 P4。"},
        ),
    ]
    return GameResult(
        game_id="review-power-test",
        seed=3,
        version_id="initial",
        winner=Faction.WEREWOLVES,
        win_reason="crafted",
        day=1,
        players=players,
        events=events,
        observation_samples=[],
    )


def _report() -> EvaluationReport:
    return EvaluationReport(
        game_id="review-test",
        winner=Faction.VILLAGERS,
        scores={"overall": 88.0},
        role_scores={},
        key_decisions=[],
        mistakes=[],
        recommendations={"seer": ["查到狼人后明确报出 P 号。"]},
        summary="好人获胜。",
    )


if __name__ == "__main__":
    unittest.main()
