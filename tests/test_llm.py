import json
import os
import unittest
from types import SimpleNamespace

from werewolf_ai.agents import RoleAgent, default_strategy_versions
from werewolf_ai.llm import (
    LLMDecisionClient,
    LLMError,
    build_chat_client_from_env,
    extract_json_object,
    normalize_provider,
    rate_limit_retry_delay_from_text,
)
from werewolf_ai.models import ActionType, Phase, PrivateObservation, Role


class FakeChatClient:
    def __init__(self, content: str | list[str]):
        self.contents = content if isinstance(content, list) else [content]
        self.config = SimpleNamespace(provider="fake", model="fake-model")
        self.calls = 0
        self.messages_history = []

    def chat(self, messages, *, temperature, max_tokens):
        self.messages = messages
        self.messages_history.append(messages)
        self.temperature = temperature
        self.max_tokens = max_tokens
        index = min(self.calls, len(self.contents) - 1)
        self.calls += 1
        return self.contents[index]


class LLMTests(unittest.TestCase):
    def test_extract_json_object_from_markdown(self) -> None:
        data = extract_json_object('```json\n{"action":"speak","target_id":null}\n```')
        self.assertEqual(data["action"], "speak")

    def test_extract_json_object_handles_control_chars(self) -> None:
        data = extract_json_object('{"action":"speak","target_id":null,"speech":"hello\u0001world"}')
        self.assertEqual(data["action"], "speak")
        self.assertIn("world", data["speech"])

    def test_extract_json_object_wraps_malformed_json(self) -> None:
        with self.assertRaises(LLMError):
            extract_json_object('{"action":"speak","target_id":')

    def test_rate_limit_delay_distinguishes_tpm_and_rpm(self) -> None:
        old_values = {
            key: os.environ.get(key)
            for key in (
                "LLM_TPM_RATE_LIMIT_RETRY_SECONDS",
                "LLM_RPM_RATE_LIMIT_RETRY_SECONDS",
                "LLM_RATE_LIMIT_JITTER_SECONDS",
                "LLM_RATE_LIMIT_BACKOFF_MULTIPLIER",
            )
        }
        try:
            os.environ["LLM_TPM_RATE_LIMIT_RETRY_SECONDS"] = "90"
            os.environ["LLM_RPM_RATE_LIMIT_RETRY_SECONDS"] = "30"
            os.environ["LLM_RATE_LIMIT_JITTER_SECONDS"] = "0"
            os.environ["LLM_RATE_LIMIT_BACKOFF_MULTIPLIER"] = "1"
            tpm_delay = rate_limit_retry_delay_from_text("RateLimitExceeded.EndpointTPMExceeded", 0)
            rpm_delay = rate_limit_retry_delay_from_text("RateLimitExceeded.EndpointRPMExceeded", 0)
            retry_after_delay = rate_limit_retry_delay_from_text(
                "RateLimitExceeded.EndpointRPMExceeded",
                0,
                retry_after_seconds=45,
            )
            self.assertEqual(tpm_delay, 90)
            self.assertEqual(rpm_delay, 30)
            self.assertEqual(retry_after_delay, 45)
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_llm_decision_valid_json(self) -> None:
        obs = _observation(ActionType.SPEAK)
        chat = FakeChatClient('{"action":"speak","target_id":null,"speech":"我先听发言。","reason":"公开信息不足，先保守发言。","confidence":0.7,"evidence_event_ids":[],"quoted_evidence":[]}')
        decider = LLMDecisionClient(chat)
        profile = default_strategy_versions("initial")[Role.VILLAGER]
        decision = decider.decide(obs, profile)
        self.assertEqual(decision.action, ActionType.SPEAK)
        self.assertEqual(decision.metadata["decision_backend"], "llm")
        self.assertEqual(decision.metadata["llm_provider"], "fake")
        self.assertEqual(decision.metadata["llm_model"], "fake-model")
        self.assertEqual(decision.metadata["evidence_event_ids"], [])
        self.assertEqual(decision.metadata["llm_call_count"], 1)
        self.assertIn("llm_latency_ms", decision.metadata)
        self.assertGreaterEqual(decision.metadata["llm_latency_ms"], 0)
        self.assertIn("你是狼人杀多智能体系统中的一个角色 Agent", chat.messages[0]["content"])
        self.assertIn("硬性约束", chat.messages[1]["content"])
        self.assertIn("evidence_event_ids", chat.messages[1]["content"])
        self.assertIn("游戏规则_GameRules", chat.messages[1]["content"])
        self.assertIn("无警长版", chat.messages[1]["content"])
        self.assertIn("后续查验计划", chat.messages[1]["content"])
        self.assertIn("预言家查验结果只表示阵营", chat.messages[1]["content"])
        self.assertIn("被预言家报好人或金水不是报错身份", chat.messages[1]["content"])
        self.assertIn("不能写“村民牌”“平民”“普通村民”“村民身份”", chat.messages[1]["content"])
        self.assertIn("别人给我报错身份", chat.messages[1]["content"])
        self.assertIn("连续认知_BeliefState", chat.messages[1]["content"])
        self.assertIn("BeliefState 是你自己的连续认知白板", chat.messages[0]["content"])

    def test_seer_cannot_report_good_check_as_concrete_villager_role(self) -> None:
        obs = _observation(ActionType.SPEAK, role=Role.SEER)
        bad = {
            "action": "speak",
            "target_id": None,
            "speech": "我跳预言家，昨夜查验P2是好人阵营村民牌。",
            "reason": "公布查验。",
            "confidence": 0.8,
            "evidence_event_ids": [],
            "quoted_evidence": [],
        }
        good = {
            "action": "speak",
            "target_id": None,
            "speech": "我跳预言家，昨夜查验P2是好人阵营金水。",
            "reason": "公布阵营查验，不声称具体身份。",
            "confidence": 0.8,
            "evidence_event_ids": [],
            "quoted_evidence": [],
        }
        chat = FakeChatClient([json.dumps(bad, ensure_ascii=False), json.dumps(good, ensure_ascii=False)])
        decision = LLMDecisionClient(chat, max_decision_retries=1).decide(
            obs,
            default_strategy_versions("initial")[Role.SEER],
        )
        self.assertEqual(chat.calls, 2)
        self.assertIn("金水", decision.speech)
        self.assertNotIn("村民牌", decision.speech)

    def test_pk_revote_vote_target_must_be_tied_target(self) -> None:
        obs = _observation(ActionType.VOTE, ActionType.PASS, phase=Phase.DAY_VOTE)
        obs.private_knowledge["action_context"] = {
            "tie_pk": {"round": "revote", "allowed_vote_targets": [2]}
        }
        chat = FakeChatClient('{"action":"vote","target_id":3,"speech":"","reason":"P3更可疑。","confidence":0.7,"evidence_event_ids":[],"quoted_evidence":[]}')
        decider = LLMDecisionClient(chat, max_decision_retries=0)
        with self.assertRaises(LLMError):
            decider.decide(obs, default_strategy_versions("initial")[Role.VILLAGER])

        valid_chat = FakeChatClient('{"action":"vote","target_id":2,"speech":"","reason":"只能在PK对象中投票。","confidence":0.7,"evidence_event_ids":[],"quoted_evidence":[]}')
        decision = LLMDecisionClient(valid_chat, max_decision_retries=0).decide(
            obs,
            default_strategy_versions("initial")[Role.VILLAGER],
        )
        self.assertEqual(decision.target_id, 2)
        self.assertIn("allowed_vote_targets", valid_chat.messages[1]["content"])

    def test_speech_limit_is_configurable_and_no_longer_220_chars(self) -> None:
        old_values = {key: os.environ.get(key) for key in ("LLM_SPEECH_MAX_CHARS", "LLM_REASON_MAX_CHARS")}
        try:
            os.environ.pop("LLM_SPEECH_MAX_CHARS", None)
            os.environ.pop("LLM_REASON_MAX_CHARS", None)
            long_speech = "a" * 300
            payload = {
                "action": "speak",
                "target_id": None,
                "speech": long_speech,
                "reason": "ok",
                "confidence": 0.7,
                "evidence_event_ids": [],
                "quoted_evidence": [],
            }
            decision = LLMDecisionClient(FakeChatClient(json.dumps(payload))).decide(
                _observation(ActionType.SPEAK),
                default_strategy_versions("initial")[Role.VILLAGER],
            )
            self.assertEqual(len(decision.speech), 300)
            self.assertFalse(decision.metadata["speech_truncated_by_system"])

            os.environ["LLM_SPEECH_MAX_CHARS"] = "120"
            decision = LLMDecisionClient(FakeChatClient(json.dumps(payload))).decide(
                _observation(ActionType.SPEAK),
                default_strategy_versions("initial")[Role.VILLAGER],
            )
            self.assertEqual(len(decision.speech), 120)
            self.assertTrue(decision.metadata["speech_truncated_by_system"])
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_prompt_filters_memory_that_conflicts_with_current_rules(self) -> None:
        obs = _observation(ActionType.SPEAK)
        obs.strategy_memory.append("预言家应该留警徽流。")
        chat = FakeChatClient('{"action":"speak","target_id":null,"speech":"我先听发言。","reason":"公开信息不足。","confidence":0.7,"evidence_event_ids":[],"quoted_evidence":[]}')
        decider = LLMDecisionClient(chat)
        profile = default_strategy_versions("initial")[Role.VILLAGER]
        decider.decide(obs, profile)
        self.assertNotIn("预言家应该留警徽流。", chat.messages[1]["content"])

    def test_prompt_includes_callable_skill_cards_when_present(self) -> None:
        obs = _observation(ActionType.PASS, ActionType.SAVE, role=Role.WITCH, phase=Phase.NIGHT_WITCH)
        obs.private_knowledge["attacked_tonight"] = 2
        chat = FakeChatClient('{"action":"pass","target_id":null,"speech":"","reason":"首夜非自救，保留解药保护后续明神。","confidence":0.7,"evidence_event_ids":[],"quoted_evidence":[]}')
        profile = default_strategy_versions("initial")[Role.WITCH]
        profile.parameters["evolution_mode"] = "skill"
        profile.parameters["role_skills"] = [
            {
                "skill_id": "witch_skill_test",
                "title": "首夜解药价值比较",
                "trigger": "夜一非自救。",
                "procedure": ["比较救人收益和保留解药收益。"],
                "avoid": ["机械救人。"],
            }
        ]

        LLMDecisionClient(chat).decide(obs, profile)

        self.assertIn("callable_skills", chat.messages[1]["content"])
        self.assertIn("首夜解药价值比较", chat.messages[1]["content"])
        self.assertIn("技能是可选策略工具", chat.messages[1]["content"])

    def test_prompt_uses_high_priority_skill_cards_first(self) -> None:
        obs = _observation(ActionType.PASS, ActionType.SAVE, role=Role.WITCH, phase=Phase.NIGHT_WITCH)
        obs.private_knowledge["attacked_tonight"] = 2
        chat = FakeChatClient('{"action":"pass","target_id":null,"speech":"","reason":"保留解药。","confidence":0.7,"evidence_event_ids":[],"quoted_evidence":[]}')
        profile = default_strategy_versions("initial")[Role.WITCH]
        profile.parameters["evolution_mode"] = "skill"
        profile.parameters["role_skills"] = [
            {
                "skill_id": f"high_{index}",
                "title": f"高优先技能{index}",
                "trigger": "当前局面相似。",
                "procedure": ["先判断触发条件。"],
                "avoid": ["机械执行。"],
            }
            for index in range(6)
        ] + [
            {
                "skill_id": "low_0",
                "title": "低优先技能",
                "trigger": "低优先。",
                "procedure": ["不应优先注入。"],
                "avoid": [],
            }
        ]

        LLMDecisionClient(chat).decide(obs, profile)

        content = chat.messages[1]["content"]
        self.assertIn("高优先技能0", content)
        self.assertIn("skill_recall", content)
        self.assertNotIn("低优先技能", content)

    def test_werewolf_prompt_and_metadata_support_deception_intent(self) -> None:
        obs = _observation(ActionType.SPEAK, role=Role.WEREWOLF)
        chat = FakeChatClient(
            json.dumps(
                {
                    "action": "speak",
                    "target_id": None,
                    "speech": "我是好人视角，先基于公开发言质疑 P2 的站边。",
                    "reason": "用公开发言制造可解释分歧。",
                    "confidence": 0.72,
                    "evidence_event_ids": [],
                    "quoted_evidence": [],
                    "deception_intent": "soft_counter",
                    "public_cover_story": "把狼队压力包装成对 P2 站边过快的公开质疑。",
                }
            )
        )
        decision = LLMDecisionClient(chat).decide(obs, default_strategy_versions("initial")[Role.WEREWOLF])

        self.assertEqual(decision.metadata["deception_intent"], "soft_counter")
        self.assertIn("公开质疑", decision.metadata["public_cover_story"])
        self.assertIn("策略性不诚实", chat.messages[0]["content"])
        self.assertIn("deception_intent", chat.messages[1]["content"])

    def test_llm_decision_repairs_bad_witch_save_target(self) -> None:
        obs = _observation(ActionType.PASS, ActionType.SAVE, role=Role.WITCH, phase=Phase.NIGHT_WITCH)
        obs.private_knowledge["attacked_tonight"] = 2
        chat = FakeChatClient(
            [
                '{"action":"save","target_id":3,"speech":"","reason":"想救 P3。","confidence":0.6,"evidence_event_ids":[],"quoted_evidence":[]}',
                '{"action":"save","target_id":2,"speech":"","reason":"解药只能救当夜被刀目标。","confidence":0.8,"evidence_event_ids":[],"quoted_evidence":[]}',
            ]
        )
        decider = LLMDecisionClient(chat)
        profile = default_strategy_versions("initial")[Role.WITCH]
        decision = decider.decide(obs, profile)
        self.assertEqual(decision.action, ActionType.SAVE)
        self.assertEqual(decision.target_id, 2)
        self.assertEqual(decision.metadata["llm_repair_attempts"], 1)
        self.assertEqual(chat.calls, 2)
        self.assertIn("attacked_tonight=2", chat.messages_history[1][-1]["content"])

    def test_public_decision_requires_visible_evidence_id(self) -> None:
        obs = _observation(ActionType.VOTE, phase=Phase.DAY_VOTE)
        obs.public_history.append(
            {
                "id": 7,
                "day": 1,
                "phase": "day_speech",
                "event_type": "speech",
                "public_text": "P2 发言：我先观察。",
                "actor_id": 2,
                "target_id": None,
                "public_payload": {"speech": "我先观察。"},
            }
        )
        chat = FakeChatClient('{"action":"vote","target_id":2,"speech":"","reason":"P2发言空泛。","confidence":0.7,"evidence_event_ids":[7],"quoted_evidence":["我先观察"]}')
        decider = LLMDecisionClient(chat)
        profile = default_strategy_versions("initial")[Role.VILLAGER]
        decision = decider.decide(obs, profile)
        self.assertEqual(decision.metadata["evidence_event_ids"], [7])
        self.assertEqual(decision.metadata["quoted_evidence"], ["我先观察"])

    def test_public_decision_rejects_missing_evidence_when_history_exists(self) -> None:
        obs = _observation(ActionType.VOTE, phase=Phase.DAY_VOTE)
        obs.public_history.append(
            {
                "id": 7,
                "day": 1,
                "phase": "day_speech",
                "event_type": "speech",
                "public_text": "P2 发言：我先观察。",
                "actor_id": 2,
                "target_id": None,
                "public_payload": {"speech": "我先观察。"},
            }
        )
        chat = FakeChatClient('{"action":"vote","target_id":2,"speech":"","reason":"P2发言空泛。","confidence":0.7,"evidence_event_ids":[],"quoted_evidence":[]}')
        decider = LLMDecisionClient(chat, max_decision_retries=0)
        profile = default_strategy_versions("initial")[Role.VILLAGER]
        with self.assertRaises(LLMError):
            decider.decide(obs, profile)

    def test_role_agent_propagates_bad_llm_action(self) -> None:
        obs = _observation(ActionType.SPEAK)
        decider = LLMDecisionClient(FakeChatClient('{"action":"vote","target_id":2,"reason":"bad","confidence":0.7}'))
        agent = RoleAgent(default_strategy_versions("initial")[Role.VILLAGER], llm_decider=decider)
        with self.assertRaises(LLMError):
            agent.decide(obs)

    def test_role_agent_requires_llm_decider(self) -> None:
        obs = _observation(ActionType.SPEAK)
        agent = RoleAgent(default_strategy_versions("initial")[Role.VILLAGER], llm_decider=None)
        with self.assertRaises(RuntimeError):
            agent.decide(obs)

    def test_provider_normalization(self) -> None:
        self.assertEqual(normalize_provider("doubao"), "ark")
        self.assertEqual(normalize_provider("deepseek"), "dashscope")
        self.assertEqual(normalize_provider("dashscope-native"), "dashscope-native")
        self.assertEqual(normalize_provider("deepseek-v32"), "dashscope-native")

    def test_build_dashscope_client_from_env(self) -> None:
        old_values = {key: os.environ.get(key) for key in ("DASHSCOPE_API_KEY", "DASHSCOPE_MODEL")}
        try:
            os.environ["DASHSCOPE_API_KEY"] = "test-key"
            os.environ["DASHSCOPE_MODEL"] = "deepseek-v4-flash"
            client = build_chat_client_from_env("deepseek")
            self.assertIsNotNone(client)
            self.assertEqual(client.config.provider, "dashscope")
            self.assertEqual(client.config.model, "deepseek-v4-flash")
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_build_dashscope_generation_client_from_env(self) -> None:
        keys = ("DASHSCOPE_API_KEY", "DASHSCOPE_GENERATION_MODEL", "DASHSCOPE_GENERATION_ENABLE_THINKING")
        old_values = {key: os.environ.get(key) for key in keys}
        try:
            os.environ["DASHSCOPE_API_KEY"] = "test-key"
            os.environ["DASHSCOPE_GENERATION_MODEL"] = "deepseek-v3.2"
            os.environ["DASHSCOPE_GENERATION_ENABLE_THINKING"] = "true"
            client = build_chat_client_from_env("dashscope-native")
            self.assertIsNotNone(client)
            self.assertEqual(client.config.provider, "dashscope-native")
            self.assertEqual(client.config.model, "deepseek-v3.2")
            self.assertTrue(client.config.extra_body["enable_thinking"])

            routed = build_chat_client_from_env("dashscope", "deepseek-v3.2")
            self.assertIsNotNone(routed)
            self.assertEqual(routed.config.provider, "dashscope-native")
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _observation(
    *legal_actions: ActionType,
    role: Role = Role.VILLAGER,
    phase: Phase = Phase.DAY_SPEECH,
) -> PrivateObservation:
    return PrivateObservation(
        game_id="test",
        day=1,
        phase=phase,
        self_id=1,
        self_name="P1",
        self_role=role,
        self_alive=True,
        alive_players=[
            {"id": 1, "name": "P1", "alive": True},
            {"id": 2, "name": "P2", "alive": True},
            {"id": 3, "name": "P3", "alive": True},
        ],
        public_history=[],
        legal_actions=list(legal_actions),
        private_knowledge={"public_claims": []},
        strategy_memory=["只基于公开信息推理。"],
        prompt_name="test",
        prompt_summary="test",
    )


if __name__ == "__main__":
    unittest.main()
