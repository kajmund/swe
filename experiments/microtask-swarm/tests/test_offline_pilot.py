from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from microtask_swarm import cli
from microtask_swarm.runner import (
    available_models,
    load_config,
    run_offline,
)

from tests.support import CONFIG_PATH


class OfflinePilotTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_offline_matrix_is_executable_and_bounded(self) -> None:
        config = load_config(CONFIG_PATH)
        models = available_models(config)
        with mock.patch(
            "urllib.request.urlopen", side_effect=AssertionError("network is forbidden")
        ):
            report = await run_offline(
                CONFIG_PATH,
                concurrency_levels=[1, 4],
                modes=[
                    "single_call",
                    "distributed_independent",
                    "heterogeneous_competition",
                ],
            )
        self.assertEqual(report["evidence_kind"], "offline_heuristic")
        self.assertEqual(report["fixture_item_count"], 10)
        self.assertEqual(report["fixture_microtask_count"], 50)
        self.assertEqual(len(report["runs"]), len(models) * 2 * 2 + 2)
        modes = {run["mode"] for run in report["runs"]}
        self.assertEqual(
            modes,
            {
                "single_call",
                "distributed_independent",
                "heterogeneous_competition",
            },
        )
        for run in report["runs"]:
            self.assertLessEqual(
                run["peak_in_flight"], run["effective_concurrency"]
            )
            self.assertEqual(run["quality"]["scored_subtasks"], 50)
            self.assertGreaterEqual(run["quality"]["accuracy"], 0.0)
            self.assertLessEqual(run["quality"]["accuracy"], 1.0)
            self.assertEqual(run["cost"]["all_in"]["actual_usd"], "N/A")
            self.assertGreater(run["sweycoins"]["spent_milli"], 0)
            self.assertIn(run["execution"]["layer_source"], {
                "swe.microtasks.compile_microtasks",
                "microtask_swarm.dag fallback",
            })
        competition = [
            run
            for run in report["runs"]
            if run["mode"] == "heterogeneous_competition"
        ]
        self.assertTrue(any(run["cancelled_count"] > 0 for run in competition))
        self.assertTrue(
            any(run["early_cancelled_tasks"] > 0 for run in competition)
        )
        self.assertIn("heterogeneous_competition", report["summary"]["best_by_mode"])
        self.assertIn(
            report["summary"]["best_by_mode"]["single_call"]["model_ids"][0],
            models,
        )

    async def test_single_call_uses_one_call_per_item(self) -> None:
        report = await run_offline(
            CONFIG_PATH,
            concurrency_levels=[1],
            modes=["single_call"],
            model_ids=["deepseek-v4.1-flash"],
        )
        run = report["runs"][0]
        self.assertEqual(run["call_count"], 10)
        self.assertEqual(run["task_count"], 10)
        self.assertEqual(run["scored_subtasks"], 50)

    async def test_distributed_uses_fifty_independent_calls(self) -> None:
        report = await run_offline(
            CONFIG_PATH,
            concurrency_levels=[1],
            modes=["distributed_independent"],
            model_ids=["deepseek-v4.1-flash"],
        )
        run = report["runs"][0]
        self.assertEqual(run["call_count"], 50)
        self.assertEqual(run["task_count"], 50)
        self.assertEqual(run["scored_subtasks"], 50)

    async def test_rate_limit_exposure_is_reported(self) -> None:
        report = await run_offline(
            CONFIG_PATH,
            concurrency_levels=[1, 4],
            modes=["distributed_independent"],
            model_ids=["deepseek-v4.1-flash"],
        )
        by_concurrency = {
            run["requested_concurrency"]: run for run in report["runs"]
        }
        self.assertFalse(by_concurrency[1]["limit_exceeded"])
        self.assertTrue(by_concurrency[4]["limit_exceeded"])
        self.assertEqual(by_concurrency[4]["provider_concurrency_limit"], 3)


class OfflineCliTests(unittest.TestCase):
    def test_cli_offline_writes_a_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            code = cli.main(
                [
                    "offline",
                    "--modes",
                    "single_call",
                    "--concurrency",
                    "1",
                    "--models",
                    "deepseek-v4.1-flash",
                    "--report",
                    str(path),
                ]
            )
            self.assertEqual(code, 0)
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report["evidence_kind"], "offline_heuristic")
            self.assertEqual(len(report["runs"]), 1)

    def test_cli_verify_fixtures(self) -> None:
        self.assertEqual(cli.main(["verify-fixtures"]), 0)

    def test_cli_estimate_reports_cap_and_credentials(self) -> None:
        self.assertEqual(cli.main(["estimate"]), 0)


if __name__ == "__main__":
    unittest.main()
