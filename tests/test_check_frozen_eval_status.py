from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from scripts.check_frozen_eval_status import _recommend_next_check, build_status


class CheckFrozenEvalStatusTests(unittest.TestCase):
    def test_completed_summary_ready_when_result_and_summary_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run_frozen_eval_result.json").write_text("{}", encoding="utf-8")
            (root / "run_frozen_eval_result_compact_summary.json").write_text("{}", encoding="utf-8")
            (root / "run_frozen_eval_gate.json").write_text("{}", encoding="utf-8")
            (root / "run_frozen_eval_result_skill_review_pack.json").write_text("{}", encoding="utf-8")
            (root / "run_progress.jsonl").write_text("large log body should not be read", encoding="utf-8")
            (root / "watcher_stdout.log").write_text("status output", encoding="utf-8")

            status = build_status(root)

            self.assertEqual(status["state"], "completed_summary_ready")
            self.assertEqual(status["result_count"], 1)
            self.assertEqual(status["compact_summary_count"], 1)
            self.assertEqual(status["gate_count"], 1)
            self.assertEqual(status["review_pack_count"], 1)
            self.assertEqual(status["progress_count"], 1)
            self.assertEqual(status["stdout_stderr_files"][0]["name"], "watcher_stdout.log")
            self.assertEqual(status["progress_files"][0]["name"], "run_progress.jsonl")

    def test_result_ready_summary_missing_when_only_result_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run_frozen_eval_result.json").write_text("{}", encoding="utf-8")

            status = build_status(root)

            self.assertEqual(status["state"], "result_ready_summary_missing")
            self.assertEqual(status["result_files"][0]["size_bytes"], 2)

    def test_failed_error_artifact_when_error_exists_without_live_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run_error.json").write_text(
                '{"status":"failed","error":"TimeoutError"}',
                encoding="utf-8",
            )

            status = build_status(root)

            self.assertEqual(status["state"], "failed_error_artifact")
            self.assertEqual(status["error_count"], 1)
            self.assertEqual(status["error_files"][0]["name"], "run_error.json")

    def test_missing_path_is_reported_without_error(self) -> None:
        status = build_status(Path("does-not-exist-for-status-test"))

        self.assertFalse(status["exists"])
        self.assertEqual(status["state"], "missing")

    def test_optional_pid_probe_reports_process_liveness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = build_status(Path(tmp), pids=[os.getpid(), 99999999])

            self.assertEqual(status["processes"][0]["pid"], os.getpid())
            self.assertTrue(status["processes"][0]["alive"])
            self.assertFalse(status["processes"][1]["alive"])
            self.assertEqual(status["state"], "running_process_alive")

    def test_recommends_low_frequency_polling_before_expected_completion(self) -> None:
        recommendation = _recommend_next_check(
            "running_process_alive",
            run_age_seconds=240,
            expected_duration_seconds=1200,
            min_check_interval_seconds=600,
        )

        self.assertGreaterEqual(recommendation["seconds"], 600)
        self.assertEqual(recommendation["reason"], "well_before_expected_completion")

    def test_recommends_shorter_polling_near_expected_completion(self) -> None:
        recommendation = _recommend_next_check(
            "running_process_alive",
            run_age_seconds=1080,
            expected_duration_seconds=1200,
            min_check_interval_seconds=600,
        )

        self.assertEqual(recommendation["seconds"], 180)
        self.assertEqual(recommendation["reason"], "approaching_expected_completion")

    def test_recommends_immediate_review_when_result_exists(self) -> None:
        recommendation = _recommend_next_check(
            "completed_summary_ready",
            run_age_seconds=1200,
            expected_duration_seconds=1200,
            min_check_interval_seconds=600,
        )

        self.assertEqual(recommendation["seconds"], 0)
        self.assertEqual(recommendation["reason"], "result_artifact_ready")

    def test_recommends_check_soon_when_approaching_max_runtime(self) -> None:
        recommendation = _recommend_next_check(
            "running_process_alive",
            run_age_seconds=1760,
            expected_duration_seconds=1200,
            min_check_interval_seconds=600,
            max_runtime_seconds=1800,
        )

        self.assertEqual(recommendation["seconds"], 70)
        self.assertEqual(recommendation["reason"], "approaching_max_runtime")

    def test_recommends_check_soon_when_past_max_runtime(self) -> None:
        recommendation = _recommend_next_check(
            "running_process_alive",
            run_age_seconds=1810,
            expected_duration_seconds=1200,
            min_check_interval_seconds=600,
            max_runtime_seconds=1800,
        )

        self.assertEqual(recommendation["seconds"], 180)
        self.assertEqual(recommendation["reason"], "past_max_runtime_process_still_alive")

    def test_status_includes_expected_duration_and_next_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run_progress.jsonl").write_text("large log body should not be read", encoding="utf-8")

            status = build_status(root, expected_duration_seconds=1200, max_runtime_seconds=1800)

            self.assertEqual(status["expected_duration_seconds"], 1200)
            self.assertEqual(status["max_runtime_seconds"], 1800)
            self.assertEqual(status["runtime_budget"]["max_runtime_seconds"], 1800)
            self.assertIn("seconds", status["next_check"])
            self.assertIn("reason", status["next_check"])


if __name__ == "__main__":
    unittest.main()
