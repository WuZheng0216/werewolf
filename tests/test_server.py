import time
import unittest
from unittest.mock import patch

import server


class FrozenEvalJobManagerTests(unittest.TestCase):
    def test_frozen_eval_job_stream_lifecycle(self) -> None:
        def fake_run_frozen_eval(**kwargs):
            kwargs["progress_callback"]("frozen_eval_started", {"version_id": kwargs["version"]})
            return {
                "mode": "frozen_eval",
                "frozen": True,
                "version_id": kwargs["version"],
                "baseline_version_id": kwargs["baseline_version"],
                "games": kwargs["games"],
                "delta": {"overall": 1.0, "mistakes_per_game": -0.5},
            }

        manager = server.FrozenEvalJobManager()
        with patch("server.run_frozen_eval", side_effect=fake_run_frozen_eval):
            job = manager.start(
                version="candidate_v1",
                baseline_version="initial",
                games=1,
                llm_provider="ark",
                llm_model=None,
            )
            for _ in range(50):
                if job.status == "completed":
                    break
                time.sleep(0.02)

        self.assertEqual(job.status, "completed")
        self.assertEqual(job.result["mode"], "frozen_eval")
        self.assertEqual(job.result["version_id"], "candidate_v1")
        self.assertIn("job_completed", [item["event"] for item in job.history])
        self.assertIn("frozen_eval_started", [item["event"] for item in job.history])


if __name__ == "__main__":
    unittest.main()
