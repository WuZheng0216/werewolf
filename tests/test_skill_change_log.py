import tempfile
import unittest
from pathlib import Path

from werewolf_ai.skill_change_log import append_skill_change, load_skill_change_log


class SkillChangeLogTests(unittest.TestCase):
    def test_append_skill_change_normalizes_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skill_change_log.json"
            saved = append_skill_change(
                {
                    "version_id": "candidate_v1",
                    "parent_id": "latest_v0",
                    "target_failure_pattern": "werewolf_exposed_pack_vote",
                    "rationale": "Evidence-backed candidate change.",
                    "changes": [{"operation": "refine_skill"}],
                },
                path,
            )
            payload = load_skill_change_log(path)

        self.assertEqual(saved["change_id"], "candidate_v1_change_1")
        self.assertEqual(saved["status"], "candidate")
        self.assertEqual(saved["decision"], "pending_eval")
        self.assertEqual(len(payload["entries"]), 1)
        self.assertEqual(payload["entries"][0]["version_id"], "candidate_v1")

    def test_append_skill_change_requires_core_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skill_change_log.json"
            with self.assertRaises(ValueError):
                append_skill_change({"version_id": "candidate_v1"}, path)


if __name__ == "__main__":
    unittest.main()
