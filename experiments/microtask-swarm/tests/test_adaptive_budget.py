from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest import mock

from microtask_swarm.adaptive_budget import (
    AdaptivePolicy,
    BudgetPolicySimulator,
    EqualSplitPolicy,
    FixedRoutingPolicy,
    GreedyPolicy,
    run_adaptive_budget_benchmark,
)
from microtask_swarm.fixtures import load_fixtures
from microtask_swarm.production_stop import FROZEN_EXPECTED, _frozen_hashes
from microtask_swarm.runner import load_config

from tests.support import CONFIG_PATH, ROOT

BUDGET_CONFIG = ROOT / "config" / "adaptive-budget.json"
ASYNC_CONFIG = ROOT / "config" / "async-benchmark.json"


def _load(path):
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _simulator() -> BudgetPolicySimulator:
    experiment = load_config(CONFIG_PATH)
    suite = load_fixtures(
        ROOT / experiment["fixtures"]["gold"],
        expected_sha256=experiment["fixtures"]["sha256"],
    )
    return BudgetPolicySimulator(
        suite=suite,
        async_config=_load(ASYNC_CONFIG),
        experiment_config=experiment,
        budget_config=_load(BUDGET_CONFIG),
    )


class SplitAndTruthTests(unittest.TestCase):
    def test_train_and_heldout_are_disjoint(self) -> None:
        sim = _simulator()
        train = {item.item_id for item in sim.training_items}
        heldout = {item.item_id for item in sim.heldout_items}
        self.assertFalse(train & heldout)
        self.assertEqual(len(train), 5)
        self.assertEqual(len(heldout), 5)
        self.assertEqual(len(sim.training_tasks), 25)
        self.assertEqual(len(sim.heldout_tasks), 25)

    def test_training_stats_use_training_split_only(self) -> None:
        sim = _simulator()
        for stats in sim.training_stats.values():
            self.assertEqual(stats.count, 5)

    def test_heldout_truth_is_not_precomputed_for_policy(self) -> None:
        sim = _simulator()
        for per_model in sim.heldout_outcomes.values():
            for outcome in per_model.values():
                self.assertFalse(outcome.correct)
                self.assertFalse(outcome.false_support)
                self.assertFalse(hasattr(outcome.view(), "correct"))

    def test_frozen_baselines_unchanged(self) -> None:
        hashes = _frozen_hashes(ROOT)
        for name, expected in FROZEN_EXPECTED.items():
            self.assertEqual(hashes[name], expected, name)


class PolicyTests(unittest.TestCase):
    def test_equal_split_denies_expensive_extraction(self) -> None:
        sim = _simulator()
        run = sim.run(EqualSplitPolicy())
        self.assertGreater(run["budget"]["denied_tasks"], 0)
        self.assertEqual(run["quality"]["extraction"]["coverage"], 0.0)
        self.assertEqual(run["budget"]["overspent_milli"], 0)

    def test_greedy_and_fixed_respect_the_cap(self) -> None:
        sim = _simulator()
        for policy in (
            GreedyPolicy(),
            FixedRoutingPolicy(_load(BUDGET_CONFIG).get("fixed_routes")),
        ):
            run = sim.run(policy)
            budget = run["budget"]
            self.assertEqual(budget["overspent_milli"], 0)
            self.assertLessEqual(budget["settled_milli"], budget["cap_milli"])
            self.assertEqual(
                budget["settled_milli"] + budget["available_milli"],
                budget["cap_milli"],
            )

    def test_adaptive_speculation_has_pending_late_accounting(self) -> None:
        sim = _simulator()
        run = sim.run(
            AdaptivePolicy(
                name="adaptive",
                speculative=True,
                max_speculative_extractions=5,
                min_secondary_utility_gain=0.0,
            )
        )
        budget = run["budget"]
        self.assertGreater(run["speculative_tasks"], 0)
        self.assertGreater(budget["pending_before_reconcile_milli"], 0)
        self.assertEqual(
            budget["late_billed_milli"] + budget["released_milli"],
            budget["pending_before_reconcile_milli"],
        )
        self.assertEqual(budget["pending_after_reconcile_milli"], 0)
        self.assertEqual(budget["overspent_milli"], 0)
        self.assertLessEqual(budget["settled_milli"], budget["cap_milli"])

    def test_adaptive_no_spec_has_no_pending(self) -> None:
        sim = _simulator()
        run = sim.run(
            AdaptivePolicy(
                name="adaptive_no_spec",
                speculative=False,
                max_speculative_extractions=0,
            )
        )
        self.assertEqual(run["speculative_tasks"], 0)
        self.assertEqual(run["budget"]["pending_before_reconcile_milli"], 0)
        self.assertEqual(run["budget"]["overspent_milli"], 0)

    def test_all_policies_have_posthoc_quality_and_no_overspend(self) -> None:
        sim = _simulator()
        for policy in (
            EqualSplitPolicy(),
            GreedyPolicy(),
            FixedRoutingPolicy(_load(BUDGET_CONFIG).get("fixed_routes")),
            AdaptivePolicy(speculative=True, min_secondary_utility_gain=0.0),
            AdaptivePolicy(
                name="adaptive_no_spec",
                speculative=False,
                max_speculative_extractions=0,
            ),
        ):
            run = sim.run(policy)
            self.assertIn("accuracy", run["quality"])
            self.assertIn("extraction", run["quality"])
            self.assertEqual(run["budget"]["overspent_milli"], 0)
            self.assertLessEqual(
                run["budget"]["settled_milli"], run["budget"]["cap_milli"]
            )


class MatrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_matrix_is_offline_and_complete(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", side_effect=AssertionError("network")
        ):
            report = await run_adaptive_budget_benchmark(
                CONFIG_PATH, ASYNC_CONFIG, BUDGET_CONFIG
            )
        self.assertEqual(report["evidence_kind"], "offline_adaptive_budget_policy")
        self.assertEqual(len(report["runs"]), 5)
        self.assertEqual(
            set(report["train_item_ids"]) & set(report["heldout_item_ids"]),
            set(),
        )
        policies = {run["policy"] for run in report["runs"]}
        self.assertEqual(
            policies,
            {
                "equal_split",
                "greedy",
                "fixed_routing",
                "adaptive",
                "adaptive_no_spec",
            },
        )
        for run in report["runs"]:
            self.assertEqual(run["budget"]["overspent_milli"], 0)
            self.assertIn("quality", run)
            self.assertIn("cost", run)


if __name__ == "__main__":
    unittest.main()
