import unittest

from werewolf_ai.agents import default_strategy_versions
from werewolf_ai.evolution import EvolutionManager
from werewolf_ai.models import EvaluationReport, Faction, Role
from werewolf_ai.skill_evolution import build_role_skills, evolve_skill_cards, normalize_evolution_mode


class SkillEvolutionTests(unittest.TestCase):
    def test_build_role_skills_from_mistakes_and_reviews(self) -> None:
        report = _report(
            [
                {"type": "witch_mechanical_n1_save", "role": "witch"},
                {"type": "werewolf_exposed_pack_vote", "role": "werewolf"},
            ]
        )
        reviews = [
            {
                "review_status": "ok",
                "memory_candidates": [
                    {
                        "role": "seer",
                        "memory": "只有金水或信息不足时，预言家不必机械首日公开身份。",
                        "confidence": 0.9,
                    }
                ],
            }
        ]

        skills = build_role_skills([report], reviews)

        self.assertTrue(any(skill["title"] == "首夜解药价值比较" for skill in skills[Role.WITCH]))
        self.assertTrue(any(skill["title"] == "狼队投票分线" for skill in skills[Role.WEREWOLF]))
        self.assertTrue(any(skill["source"] == "llm_review" for skill in skills[Role.SEER]))

    def test_skill_mode_evolve_profiles_adds_callable_skills(self) -> None:
        profiles = default_strategy_versions("initial")
        manager = EvolutionManager(llm_decider_factory=object, enable_llm_review=False, evolution_mode="skill")
        evolved = manager.evolve_profiles(
            profiles,
            [_report([{"type": "witch_mechanical_n1_save", "role": "witch"}])],
            version_id="evolved_r_skill",
            llm_reviews=[],
        )

        witch = evolved[Role.WITCH]
        self.assertEqual(witch.parameters["evolution_mode"], "skill")
        self.assertGreaterEqual(witch.parameters["skill_count"], 1)
        self.assertTrue(any("技能卡[首夜解药价值比较]" in memory for memory in witch.strategy_memory))

    def test_evolve_skill_cards_refines_repeated_pattern(self) -> None:
        first = build_role_skills(
            [_report([{"type": "werewolf_exposed_pack_vote", "role": "werewolf"}])],
            [],
        )[Role.WEREWOLF]
        first_result = evolve_skill_cards([], first, version_id="evolved_skill_r1")

        repeated = build_role_skills(
            [
                _report(
                    [
                        {"type": "werewolf_exposed_pack_vote", "role": "werewolf"},
                        {"type": "werewolf_exposed_pack_vote", "role": "werewolf"},
                    ]
                )
            ],
            [],
        )[Role.WEREWOLF]
        second_result = evolve_skill_cards(
            first_result["skills"],
            repeated,
            version_id="evolved_skill_r2",
        )

        wolf_skills = [
            skill
            for skill in second_result["skills"]
            if skill.get("source_type") == "werewolf_exposed_pack_vote"
        ]
        self.assertEqual(len(wolf_skills), 1)
        self.assertEqual(wolf_skills[0]["skill_version"], 2)
        self.assertTrue(wolf_skills[0]["parent_skill_id"])
        self.assertEqual(second_result["operations"][0]["operation"], "refine")

    def test_skill_mode_records_refine_operations_in_profile(self) -> None:
        profiles = default_strategy_versions("initial")
        manager = EvolutionManager(llm_decider_factory=object, enable_llm_review=False, evolution_mode="skill")
        first = manager.evolve_profiles(
            profiles,
            [_report([{"type": "witch_mechanical_n1_save", "role": "witch"}])],
            version_id="evolved_skill_r1",
            llm_reviews=[],
        )
        second = manager.evolve_profiles(
            first,
            [_report([{"type": "witch_mechanical_n1_save", "role": "witch"}])],
            version_id="evolved_skill_r2",
            llm_reviews=[],
        )

        operations = second[Role.WITCH].parameters["skill_evolution_operations"]
        self.assertTrue(any(item["operation"] == "refine" for item in operations))
        self.assertTrue(second[Role.WITCH].parameters["skill_history"])

    def test_workflow_mode_keeps_skill_cards_absent(self) -> None:
        profiles = default_strategy_versions("initial")
        manager = EvolutionManager(llm_decider_factory=object, enable_llm_review=False, evolution_mode="workflow")
        evolved = manager.evolve_profiles(
            profiles,
            [_report([{"type": "witch_mechanical_n1_save", "role": "witch"}])],
            version_id="evolved_r_workflow",
            llm_reviews=[],
        )

        self.assertEqual(evolved[Role.WITCH].parameters["evolution_mode"], "workflow")
        self.assertNotIn("role_skills", evolved[Role.WITCH].parameters)

    def test_mode_aliases(self) -> None:
        self.assertEqual(normalize_evolution_mode("skills"), "skill")
        self.assertEqual(normalize_evolution_mode("memory_workflow"), "workflow")
        self.assertEqual(normalize_evolution_mode("unknown"), "workflow")


def _report(mistakes: list[dict]) -> EvaluationReport:
    return EvaluationReport(
        game_id="skill-test",
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
