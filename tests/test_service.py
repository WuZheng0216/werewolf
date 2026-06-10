import unittest
from unittest.mock import patch

from werewolf_ai.models import Role, StrategyVersion
from werewolf_ai.service import run_frozen_eval


class FrozenEvalTests(unittest.TestCase):
    def test_frozen_eval_disables_evolution_writes(self) -> None:
        def fake_profiles(version_id: str) -> dict[Role, StrategyVersion]:
            return {
                role: StrategyVersion(
                    role=role,
                    version_id=version_id,
                    prompt_name=f"{role.value}_test",
                    prompt_summary="test profile",
                )
                for role in Role
            }

        class FakeManager:
            instances = []

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.calls = []
                FakeManager.instances.append(self)

            def run_batch(
                self,
                *,
                profiles,
                version_id,
                games,
                seed_offset,
                seed_sequence=None,
                progress_callback=None,
            ):
                self.calls.append(
                    {
                        "version_id": version_id,
                        "games": games,
                        "seed_offset": seed_offset,
                        "seed_sequence": seed_sequence,
                    }
                )
                if progress_callback:
                    progress_callback(
                        "game_started",
                        {
                            "batch": {
                                "version_id": version_id,
                                "game_index": 0,
                                "games_requested": games,
                            }
                        },
                    )
                overall = 80.0 if version_id == "evolved" else 70.0
                mistakes = 0.5 if version_id == "evolved" else 1.5
                completed_seeds = list(seed_sequence or [seed_offset + index for index in range(games)])
                return {
                    "aggregate": {
                        "average_scores": {
                            "overall": overall,
                            "vote_quality": overall - 10,
                            "skill_quality": overall - 5,
                        },
                        "mistakes_per_game": mistakes,
                        "winner_counts": {"villagers": 1, "werewolves": 1},
                    },
                    "archive_paths": [f"logs/games/{version_id}.json"],
                    "errors": [],
                    "completed_games": games,
                    "completed_seeds": completed_seeds,
                }

        with (
            patch("werewolf_ai.service.EvolutionManager", FakeManager),
            patch("werewolf_ai.service.get_profiles", side_effect=fake_profiles),
        ):
            progress_events = []
            result = run_frozen_eval(
                version="evolved",
                baseline_version="initial",
                games=2,
                progress_callback=lambda event, data: progress_events.append((event, data)),
            )

        manager = FakeManager.instances[0]
        self.assertTrue(result["frozen"])
        self.assertTrue(result["writes_disabled"]["memory_bank"])
        self.assertTrue(result["writes_disabled"]["latest_promoted"])
        self.assertFalse(manager.kwargs["enable_llm_review"])
        self.assertFalse(manager.kwargs["enable_strategy_planning"])
        self.assertTrue(manager.kwargs["archive_games"])
        self.assertEqual([call["version_id"] for call in manager.calls], ["evolved", "initial"])
        self.assertEqual({call["seed_offset"] for call in manager.calls}, {60_000})
        self.assertIsNone(manager.calls[0]["seed_sequence"])
        self.assertEqual(manager.calls[1]["seed_sequence"], [60_000, 60_001])
        self.assertEqual(result["seed_alignment"]["candidate_completed_seeds"], [60_000, 60_001])
        self.assertEqual(result["seed_alignment"]["baseline_completed_seeds"], [60_000, 60_001])
        self.assertEqual(result["delta"]["overall"], 10.0)
        self.assertEqual(result["delta"]["mistakes_per_game"], -1.0)
        self.assertIn("frozen_eval_started", [event for event, _ in progress_events])
        self.assertIn("frozen_eval_seed_alignment", [event for event, _ in progress_events])
        self.assertIn("frozen_eval_completed", [event for event, _ in progress_events])
        game_started = [data for event, data in progress_events if event == "game_started"]
        self.assertEqual([item["eval_side"] for item in game_started], ["candidate", "baseline"])


if __name__ == "__main__":
    unittest.main()
