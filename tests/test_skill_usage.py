import unittest

from types import SimpleNamespace

from werewolf_ai.models import ActionType, AgentDecision, GameEvent, Phase, Role
from werewolf_ai.skill_usage import estimate_skill_usage, select_relevant_skills, summarize_skill_usage


class SkillUsageTests(unittest.TestCase):
    def test_selects_relevant_skills_for_current_role_and_phase(self) -> None:
        obs = SimpleNamespace(
            self_role=Role.WEREWOLF,
            phase=Phase.DAY_SPEECH,
            day=1,
            legal_actions=[ActionType.SPEAK],
            private_knowledge={"wolf_strategy_space": {"mode": "autonomous_choice"}},
            public_history=[{"public_text": "P2 跳预言家并报查验。"}],
            belief_state={"top_suspicions": [], "self_commitments": []},
        )
        skills = [
            {
                "skill_id": "witch_antidote",
                "role": "witch",
                "title": "首夜解药价值比较",
                "trigger": "夜一被刀信息出现时。",
                "source_type": "witch_mechanical_n1_save",
                "confidence": 0.99,
            },
            {
                "skill_id": "wolf_fake_seer",
                "role": "werewolf",
                "title": "悍跳预言家完整链",
                "trigger": "决定悍跳或对跳预言家时。",
                "source_type": "werewolf_incomplete_fake_seer_claim",
                "confidence": 0.6,
            },
        ]

        recalled = select_relevant_skills(observation=obs, role_skills=skills, max_items=1)

        self.assertEqual(recalled[0]["skill_id"], "wolf_fake_seer")
        self.assertGreater(recalled[0]["recall_score"], 0)

    def test_estimates_werewolf_fake_seer_skill_without_llm_output_field(self) -> None:
        decision = AgentDecision(
            actor_id=6,
            role=Role.WEREWOLF,
            action=ActionType.SPEAK,
            speech="我是预言家，昨夜查验P8是查杀，下一晚计划验P3。",
            reason="用公开对跳制造分歧。",
            confidence=0.8,
            metadata={"deception_intent": "fake_seer"},
        )
        usage = estimate_skill_usage(
            decision,
            phase=Phase.DAY_SPEECH,
            role_skills=[
                {
                    "skill_id": "wolf_fake_seer",
                    "role": "werewolf",
                    "title": "悍跳预言家完整链",
                    "trigger": "决定悍跳或对跳预言家时。",
                    "procedure": ["同时给出查验对象、查验结论和下一晚查验计划。"],
                    "avoid": ["只说自己是预言家。"],
                    "source_type": "werewolf_incomplete_fake_seer_claim",
                }
            ],
        )

        self.assertEqual(usage["available_count"], 1)
        self.assertEqual(usage["matched_count"], 1)
        self.assertEqual(usage["matched_skills"][0]["skill_id"], "wolf_fake_seer")
        self.assertIn("seer_chain_plan", usage["matched_skills"][0]["signals"])
        self.assertNotIn("used_skills", decision.metadata)

    def test_no_available_skills_has_empty_usage(self) -> None:
        decision = AgentDecision(
            actor_id=1,
            role=Role.VILLAGER,
            action=ActionType.SPEAK,
            speech="我先听后置位发言。",
        )

        usage = estimate_skill_usage(decision, phase=Phase.DAY_SPEECH, role_skills=[])

        self.assertEqual(usage["available_count"], 0)
        self.assertEqual(usage["matched_count"], 0)
        self.assertEqual(usage["matched_skills"], [])

    def test_summarizes_event_skill_usage_by_role(self) -> None:
        decision = AgentDecision(
            actor_id=4,
            role=Role.WITCH,
            action=ActionType.PASS,
            reason="首夜非自救，保留解药保护关键神职价值更高。",
            metadata={
                "skill_usage": {
                    "available_count": 2,
                    "matched_count": 1,
                    "matched_skills": [
                        {
                            "skill_id": "witch_antidote",
                            "title": "首夜解药价值比较",
                            "score": 0.8,
                        }
                    ],
                }
            },
        )
        event = GameEvent(
            id=1,
            day=1,
            phase=Phase.NIGHT_WITCH,
            event_type="witch_action",
            public_text="P4 已完成女巫行动。",
            actor_id=4,
            decision=decision,
        )

        summary = summarize_skill_usage([event])

        self.assertEqual(summary["total"]["decisions_with_available_skills"], 1)
        self.assertEqual(summary["total"]["decisions_with_matched_skills"], 1)
        self.assertEqual(summary["by_role"]["witch"]["skills"][0]["title"], "首夜解药价值比较")


if __name__ == "__main__":
    unittest.main()
