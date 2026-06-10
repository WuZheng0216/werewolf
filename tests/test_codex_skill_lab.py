import unittest
from unittest.mock import patch

from werewolf_ai.codex_skill_lab import run_codex_skill_lab_batch
from werewolf_ai.models import Role, StrategyVersion


class CodexSkillLabTests(unittest.TestCase):
    def test_codex_skill_lab_disables_auto_evolution_writes(self) -> None:
        def fake_profiles(version_id: str) -> dict[Role, StrategyVersion]:
            return {
                role: StrategyVersion(
                    role=role,
                    version_id="sample_v1",
                    prompt_name=f"{role.value}_test",
                    prompt_summary="test profile",
                    parameters={
                        "role_skills": [
                            {
                                "skill_id": f"{role.value}_skill",
                                "title": "Test Skill",
                                "trigger": "test trigger",
                                "source_type": "manual",
                                "confidence": 0.8,
                            }
                        ]
                    },
                )
                for role in Role
            }

        class FakeManager:
            instances = []

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                FakeManager.instances.append(self)

            def run_batch(self, *, profiles, version_id, games, seed_offset, progress_callback=None):
                if progress_callback:
                    progress_callback(
                        "game_started",
                        {
                            "version_id": version_id,
                            "strategy_versions": {"werewolf": {"large": "omitted by caller script"}},
                        },
                    )
                return {
                    "aggregate": {
                        "average_scores": {"overall": 75.0, "wolf_deception_quality": 60.0},
                        "mistakes_per_game": 1.0,
                    },
                    "archive_paths": ["logs/games/sample.json"],
                    "errors": [],
                    "completed_games": games,
                    "attempted_games": games,
                    "requested_games": games,
                }

        with (
            patch("werewolf_ai.codex_skill_lab.EvolutionManager", FakeManager),
            patch("werewolf_ai.codex_skill_lab.get_profiles", side_effect=fake_profiles),
        ):
            events = []
            result = run_codex_skill_lab_batch(
                version="latest",
                games=2,
                progress_callback=lambda event, data: events.append((event, data)),
            )

        manager = FakeManager.instances[0]
        self.assertEqual(result["mode"], "codex_skill_lab")
        self.assertTrue(result["codex_review_required"])
        self.assertTrue(result["skip_preflight"])
        self.assertEqual(result["internal_llm_retries"], 1)
        self.assertTrue(result["writes_disabled"]["llm_review"])
        self.assertTrue(result["writes_disabled"]["memory_bank"])
        self.assertTrue(result["writes_disabled"]["auto_skill_evolution"])
        self.assertTrue(manager.kwargs["archive_games"])
        self.assertFalse(manager.kwargs["enable_llm_review"])
        self.assertFalse(manager.kwargs["enable_strategy_planning"])
        self.assertEqual(manager.kwargs["evolution_mode"], "skill")
        self.assertEqual(result["version_id"], "sample_v1")
        self.assertEqual(result["current_skill_summary"]["werewolf"]["skill_count"], 1)
        self.assertIn("codex_skill_lab_started", [event for event, _ in events])
        self.assertIn("codex_skill_lab_completed", [event for event, _ in events])


if __name__ == "__main__":
    unittest.main()
