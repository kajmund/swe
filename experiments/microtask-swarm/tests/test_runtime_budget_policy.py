"""RuntimeBudget policy integration against the Rust swe_budget ledger.

The ledger cases use worked milli amounts. The policy cases use the frozen
adaptive-budget report as the independent outcome table. No provider is called.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from microtask_swarm.dag import ensure_repo_python_on_path
from microtask_swarm.production_stop import FROZEN_EXPECTED, _frozen_hashes
from microtask_swarm.runtime_budget_policy import (
    T1BM_PR13_COMMIT,
    CarriedMilliPolicy,
    RuntimeBudgetBackend,
    render_runtime_budget_markdown,
    run_runtime_budget_benchmark,
)

from tests.support import CONFIG_PATH, ROOT

BUDGET_CONFIG = ROOT / "config" / "adaptive-budget.json"
ASYNC_CONFIG = ROOT / "config" / "async-benchmark.json"

# Local meter, policy simulator, and frozen report. This integration must not
# rewrite them.
BASELINE_SHA256 = {
    "microtask_swarm/budget.py": (
        "8c46ab69514d837e8e8314b38d6a93ffc8962f400fc71b0d4e139f286aaddc08"
    ),
    "microtask_swarm/adaptive_budget.py": (
        "3cb2300e17914720e17298d0a5d5a9b9dd473ce43016a2babc6c949524ac26a1"
    ),
    "microtask_swarm/production_stop.py": (
        "ade2cb8344234d2270ccac104c4292f48a13f15a195fdcc7f3ddf7f6ea9d14d0"
    ),
    "config/adaptive-budget.json": (
        "3ff98e8756332f3d8fbc87d693d4a974cd1191d4c31d85b4400d868e7d9f84b5"
    ),
    "reports/adaptive-budget.json": (
        "669dd65650d3d49d3a2bf754ac6f15ccf6eba3f2237ee01f666977c1fc79997f"
    ),
    "reports/adaptive-budget.md": (
        "24d95d1cd068cb835e7c1223c7a1f0c1c10a46a48751cc95d613f5f10bed7225"
    ),
    "tests/test_adaptive_budget.py": (
        "5f9f0a46e9ddec95d8e69f81470b715d17818f30c40c04a943cd29c7a08fe174"
    ),
}

# Frozen local-meter outcomes. Available is cap 15000 minus settled, with no
# residual reservation after reconcile.
FROZEN_RUNS = {
    "equal_split": {
        "stopped": False,
        "completed_tasks": 20,
        "accuracy": 0.72,
        "coverage": 0.0,
        "calls": 20,
        "speculative_tasks": 0,
        "settled_milli": 4000,
        "available_milli": 11000,
        "pending_before_reconcile_milli": 0,
        "late_billed_milli": 0,
        "released_milli": 0,
        "denied_tasks": 5,
        "overspent_milli": 0,
    },
    "greedy": {
        "stopped": True,
        "completed_tasks": 23,
        "accuracy": 0.84,
        "coverage": 1.0,
        "calls": 23,
        "speculative_tasks": 0,
        "settled_milli": 7600,
        "available_milli": 7400,
        "pending_before_reconcile_milli": 0,
        "late_billed_milli": 0,
        "released_milli": 0,
        "denied_tasks": 0,
        "overspent_milli": 0,
    },
    "fixed_routing": {
        "stopped": True,
        "completed_tasks": 23,
        "accuracy": 0.84,
        "coverage": 1.0,
        "calls": 23,
        "speculative_tasks": 0,
        "settled_milli": 8600,
        "available_milli": 6400,
        "pending_before_reconcile_milli": 0,
        "late_billed_milli": 0,
        "released_milli": 0,
        "denied_tasks": 0,
        "overspent_milli": 0,
    },
    "adaptive": {
        "stopped": True,
        "completed_tasks": 23,
        "accuracy": 0.84,
        "coverage": 1.0,
        "calls": 28,
        "speculative_tasks": 5,
        "settled_milli": 9400,
        "available_milli": 5600,
        "pending_before_reconcile_milli": 4000,
        "late_billed_milli": 800,
        "released_milli": 3200,
        "denied_tasks": 0,
        "overspent_milli": 0,
    },
    "adaptive_no_spec": {
        "stopped": True,
        "completed_tasks": 23,
        "accuracy": 0.84,
        "coverage": 1.0,
        "calls": 23,
        "speculative_tasks": 0,
        "settled_milli": 7600,
        "available_milli": 7400,
        "pending_before_reconcile_milli": 0,
        "late_billed_milli": 0,
        "released_milli": 0,
        "denied_tasks": 0,
        "overspent_milli": 0,
    },
}


def _ledger_ready() -> bool:
    ensure_repo_python_on_path()
    try:
        from swe.budget import NativeBudgetLedger

        NativeBudgetLedger()
    except Exception:
        return False
    return True


LEDGER_READY = _ledger_ready()


class FrozenBaselineTests(unittest.TestCase):
    def test_frozen_scheduler_baseline_is_unchanged(self) -> None:
        hashes = _frozen_hashes(ROOT)
        for name, expected in FROZEN_EXPECTED.items():
            self.assertEqual(hashes[name], expected, name)

    def test_local_meter_baseline_is_unchanged(self) -> None:
        for name, expected in BASELINE_SHA256.items():
            digest = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            self.assertEqual(digest, expected, name)

    def test_integration_does_not_use_the_local_meter(self) -> None:
        source = (
            ROOT / "microtask_swarm" / "runtime_budget_policy.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("from microtask_swarm.budget import", source)
        self.assertNotIn("agent_contract", source)
        self.assertIn(T1BM_PR13_COMMIT, source)

    def test_carried_milli_policy_uses_the_runtime_budget_surface(self) -> None:
        from microtask_swarm.dag import ensure_repo_python_on_path

        ensure_repo_python_on_path()
        try:
            from swe.budget import BudgetOutcome, BudgetRequest
        except Exception as exc:
            self.skipTest(f"swe.budget is not importable: {exc}")

        policy = CarriedMilliPolicy()
        request = BudgetRequest(
            run_id="run-1",
            invocation_id="task:model:attempt-1",
            node_id="task",
            kind="provider",
            name="nemotron-3-super",
            input={"amount_milli": 40},
        )
        self.assertEqual(policy.reserve(request), 40)
        self.assertEqual(
            policy.settle(request, BudgetOutcome(ok=True, value={"amount_milli": 25})),
            25,
        )
        self.assertEqual(policy.settle(request, BudgetOutcome(ok=False)), 0)


@unittest.skipUnless(LEDGER_READY, "swe_budget wheel is not installed")
class RustLedgerLifecycleTests(unittest.TestCase):
    def test_reserve_settle_pending_cancel_and_replay_never_overspend(self) -> None:
        from swe.budget import BudgetStateError

        backend = RuntimeBudgetBackend(cap_milli=1000, run_id="ledger-lifecycle")
        self.assertEqual(type(backend.ledger).__name__, "NativeBudgetLedger")
        self.assertEqual(type(backend.budget).__name__, "RuntimeBudget")

        held = backend.reserve(
            invocation_id="task:model:settle",
            node_id="task",
            name="model",
            amount_milli=400,
        )
        self.assertIsNotNone(held)
        backend.settle(held, 250)
        self.assertEqual(
            dict(backend.balance()),
            {
                "allocated": 1000,
                "incoming": 0,
                "outgoing": 0,
                "spent": 250,
                "reserved": 0,
                "available": 750,
            },
        )

        cancelled = backend.reserve(
            invocation_id="task:model:cancel",
            node_id="task",
            name="model",
            amount_milli=300,
        )
        self.assertIsNotNone(cancelled)
        self.assertEqual(backend.balance()["reserved"], 300)
        self.assertEqual(backend.balance()["available"], 450)
        with self.assertRaises(BudgetStateError):
            backend.reconcile(cancelled, release=True)
        self.assertEqual(backend.balance()["reserved"], 300)
        backend.mark_pending(cancelled)
        self.assertEqual(backend.balance()["spent"], 250)
        self.assertEqual(backend.balance()["reserved"], 300)
        backend.reconcile(cancelled, release=True)
        self.assertEqual(backend.balance()["spent"], 250)
        self.assertEqual(backend.balance()["reserved"], 0)
        self.assertEqual(backend.balance()["available"], 750)

        denied = backend.reserve(
            invocation_id="task:model:overspend",
            node_id="task",
            name="model",
            amount_milli=800,
        )
        self.assertIsNone(denied)
        self.assertEqual(backend.balance()["available"], 750)
        self.assertGreaterEqual(backend.balance()["available"], 0)
        self.assertEqual(
            backend.balance()["spent"]
            + backend.balance()["reserved"]
            + backend.balance()["available"],
            backend.balance()["allocated"],
        )

        late = backend.reserve(
            invocation_id="task:model:late",
            node_id="task",
            name="model",
            amount_milli=200,
        )
        self.assertIsNotNone(late)
        backend.mark_pending(late)
        backend.reconcile(late, spent_milli=200)
        self.assertEqual(
            dict(backend.balance()),
            {
                "allocated": 1000,
                "incoming": 0,
                "outgoing": 0,
                "spent": 450,
                "reserved": 0,
                "available": 550,
            },
        )
        self.assertEqual(dict(backend.replay_balance()), dict(backend.balance()))
        self.assertGreaterEqual(backend.min_available_milli, 0)
        # Settling 250 of a 400 reservation returns the unused 150. Cancelling
        # the 300 hold returns all of it. The 200 late bill spends the hold.
        self.assertEqual(
            [(event["kind"], event["amount_milli"]) for event in backend.events()],
            [
                ("allocate", 1000),
                ("reserve", 400),
                ("spend", 250),
                ("release", 150),
                ("reserve", 300),
                ("release", 300),
                ("reserve", 200),
                ("spend", 200),
            ],
        )
        reserve_at = {
            event["reservation_id"]: index
            for index, event in enumerate(backend.events())
            if event["kind"] == "reserve"
        }
        for index, event in enumerate(backend.events()):
            if event["kind"] in {"spend", "release"}:
                self.assertLess(reserve_at[event["reservation_id"]], index)


@unittest.skipUnless(LEDGER_READY, "swe_budget wheel is not installed")
class RuntimeBudgetPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_policies_match_frozen_outcomes_with_zero_overspend(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", side_effect=AssertionError("network")
        ):
            report = await run_runtime_budget_benchmark(
                CONFIG_PATH, ASYNC_CONFIG, BUDGET_CONFIG
            )
        self.assertEqual(report["evidence_kind"], "offline_runtime_budget_policy")
        self.assertEqual(report["budget_cap_milli"], 15000)
        self.assertFalse(
            report["runtime_budget"]["local_meter_used_in_integration_path"]
        )
        self.assertEqual(
            report["runtime_budget"]["backend"], "swe.budget.RuntimeBudget"
        )
        self.assertEqual(report["runtime_budget"]["ledger"], "swe_budget.Ledger")
        self.assertEqual(T1BM_PR13_COMMIT, "285d0770147eb5584ad7c8faa23872ed4b9dbb17")
        self.assertIn(T1BM_PR13_COMMIT, " ".join(report["notes"]))
        self.assertIn("experiments/agent-contract", " ".join(report["notes"]))

        runs = {run["policy"]: run for run in report["runs"]}
        self.assertEqual(set(runs), set(FROZEN_RUNS))
        for name, expected in FROZEN_RUNS.items():
            run = runs[name]
            budget = run["budget"]
            balance = run["runtime_budget"]["balance"]
            self.assertEqual(run["stopped"], expected["stopped"], name)
            self.assertEqual(run["completed_tasks"], expected["completed_tasks"], name)
            self.assertEqual(run["quality"]["accuracy"], expected["accuracy"], name)
            self.assertEqual(
                run["quality"]["extraction"]["coverage"], expected["coverage"], name
            )
            self.assertEqual(run["calls"], expected["calls"], name)
            self.assertEqual(
                run["speculative_tasks"], expected["speculative_tasks"], name
            )
            for key in (
                "settled_milli",
                "available_milli",
                "pending_before_reconcile_milli",
                "late_billed_milli",
                "released_milli",
                "denied_tasks",
                "overspent_milli",
            ):
                self.assertEqual(budget[key], expected[key], f"{name} {key}")
            self.assertEqual(budget["pending_after_reconcile_milli"], 0, name)
            self.assertEqual(balance["spent"], expected["settled_milli"], name)
            self.assertEqual(balance["reserved"], 0, name)
            self.assertEqual(balance["available"], expected["available_milli"], name)
            self.assertGreaterEqual(balance["available"], 0, name)
            self.assertEqual(
                balance["spent"] + balance["reserved"] + balance["available"],
                balance["allocated"],
                name,
            )
            self.assertLessEqual(balance["spent"], balance["allocated"], name)
            self.assertTrue(run["runtime_budget"]["replay_ok"], name)
            self.assertTrue(
                run["runtime_budget"]["reservation_before_dispatch"], name
            )
            self.assertTrue(
                run["runtime_budget"]["reservation_before_terminal"], name
            )
            self.assertEqual(
                run["runtime_budget"]["replay_balance"], balance, name
            )

        totals = report["totals"]
        self.assertEqual(totals["calls"], 117)
        self.assertEqual(totals["settled_milli"], 37200)
        self.assertEqual(totals["pending_before_reconcile_milli"], 4000)
        self.assertEqual(totals["late_billed_milli"], 800)
        self.assertEqual(totals["released_milli"], 3200)
        self.assertEqual(totals["denied_tasks"], 5)
        self.assertEqual(totals["overspent_milli"], 0)
        self.assertEqual(totals["actual_usd"], "N/A")
        markdown = render_runtime_budget_markdown(report)
        self.assertIn("swe_budget.Ledger", markdown)
        self.assertIn("| adaptive |", markdown)


@unittest.skipUnless(LEDGER_READY, "swe_budget wheel is not installed")
class RuntimeBudgetCliTests(unittest.TestCase):
    def test_cli_writes_the_offline_report(self) -> None:
        from microtask_swarm.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "runtime-budget-policy.json"
            markdown_path = Path(tmp) / "runtime-budget-policy.md"
            with mock.patch(
                "urllib.request.urlopen", side_effect=AssertionError("network")
            ):
                code = main(
                    [
                        "--config",
                        str(CONFIG_PATH),
                        "runtime-budget-policy",
                        "--async-config",
                        str(ASYNC_CONFIG),
                        "--budget-config",
                        str(BUDGET_CONFIG),
                        "--report",
                        str(report_path),
                        "--markdown",
                        str(markdown_path),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn(T1BM_PR13_COMMIT, report_path.read_text(encoding="utf-8"))
            self.assertIn("Overspend", markdown_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
