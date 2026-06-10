import unittest

from werewolf_ai.agents import default_strategy_versions
from werewolf_ai.evolution import EvolutionManager
from werewolf_ai.evolution_strategist import (
    normalize_strategy_plan,
    strategist_memory_updates,
    strategist_skill_cards,
)
from werewolf_ai.models import EvaluationReport, Faction, Role


class EvolutionStrategistTests(unittest.TestCase):
    def test_normalizes_plan_into_role_skills_and_memory(self) -> None:
        plan = normalize_strategy_plan(
            {
                "plan_id": "wolf_counterplay_plan",
                "diagnosis": [
                    {
                        "claim": "werewolf_no_seer_counterplay remains high",
                        "evidence": "mistake_counts shows werewolf_no_seer_counterplay=2",
                    }
                ],
                "role_plans": {
                    "werewolf": {
                        "focus": "Improve counterplay against a single seer.",
                        "memory_updates": [
                            "When a single seer is building consensus, wolves should compare soft counter, fake seer, bus, and split-vote options."
                        ],
                        "skill_operations": [
                            {
                                "operation": "add",
                                "reason": "Wolves need a reusable counterplay hypothesis.",
                                "skill": {
                                    "title": "Single seer counterplay menu",
                                    "trigger": "Only one seer claim is shaping public consensus.",
                                    "procedure": [
                                        "Compare hide, soft counter, fake seer, bus teammate, and split-vote options.",
                                        "If fake seer is chosen, include checks, result, future check plan, and public evidence.",
                                    ],
                                    "avoid": ["All wolves silently accepting the single seer."],
                                    "tags": ["werewolf", "counterplay"],
                                    "confidence": 0.82,
                                },
                            }
                        ],
                    }
                },
            }
        )

        skills = strategist_skill_cards(plan)
        memories = strategist_memory_updates(plan)

        self.assertEqual(plan["plan_status"], "ok")
        self.assertEqual(skills[Role.WEREWOLF][0]["source"], "llm_strategist")
        self.assertIn("Single seer counterplay", skills[Role.WEREWOLF][0]["title"])
        self.assertTrue(memories[Role.WEREWOLF])

    def test_evolve_profiles_applies_strategist_plan(self) -> None:
        profiles = default_strategy_versions("initial")
        manager = EvolutionManager(
            llm_decider_factory=object,
            enable_llm_review=False,
            enable_strategy_planning=False,
            evolution_mode="skill",
        )
        plan = normalize_strategy_plan(
            {
                "plan_id": "witch_antidote_direction",
                "role_plans": {
                    "witch": {
                        "focus": "Reduce mechanical night-one saves.",
                        "memory_updates": ["N1 antidote use should compare target value and future protection value."],
                        "skill_operations": [
                            {
                                "operation": "add",
                                "skill": {
                                    "title": "N1 antidote value check",
                                    "trigger": "Night one target is not self and public information is scarce.",
                                    "procedure": ["Compare save value, future antidote value, and risk before using antidote."],
                                    "avoid": ["Saving purely because someone was attacked."],
                                    "tags": ["witch", "antidote"],
                                    "confidence": 0.8,
                                },
                            }
                        ],
                    }
                },
            }
        )

        evolved = manager.evolve_profiles(
            profiles,
            [_report([])],
            version_id="evolved_skill_plan_test",
            llm_reviews=[],
            strategist_plan=plan,
        )
        witch = evolved[Role.WITCH]

        self.assertEqual(witch.parameters["last_strategist_plan_id"], "witch_antidote_direction")
        self.assertTrue(any(skill.get("source") == "llm_strategist" for skill in witch.parameters["role_skills"]))
        self.assertTrue(any("N1 antidote" in memory for memory in witch.strategy_memory))


def _report(mistakes: list[dict]) -> EvaluationReport:
    return EvaluationReport(
        game_id="strategy-test",
        winner=Faction.VILLAGERS,
        scores={"overall": 80.0},
        role_scores={},
        key_decisions=[],
        mistakes=mistakes,
        recommendations={},
        summary="test",
    )


if __name__ == "__main__":
    unittest.main()
