"""Read-only probe for the landed `swe.budget` runtime-budget contract.

Skipped when `swe.budget` is absent in this environment. This test does not
instantiate a native ledger and does not make any model call; it only checks
that the public surface documented in `docs/budget-migration-plan.md` exists.
"""

from __future__ import annotations

import importlib
import unittest

try:
    _budget = importlib.import_module("swe.budget")
except Exception:  # pragma: no cover - the package is not installed here
    _budget = None


@unittest.skipIf(_budget is None, "swe.budget is not installed")
class BudgetMigrationContractTests(unittest.TestCase):
    def test_public_types_exist(self) -> None:
        for name in (
            "BudgetError",
            "BudgetExceeded",
            "BudgetOutcome",
            "BudgetOverrun",
            "BudgetPolicy",
            "BudgetRequest",
            "BudgetStateError",
            "BudgetUnknownCost",
            "NativeBudgetLedger",
            "ReservationState",
            "RuntimeBudget",
        ):
            self.assertTrue(hasattr(_budget, name), name)

    def test_runtime_budget_lifecycle_surface(self) -> None:
        for name in (
            "reserve",
            "settle",
            "release",
            "mark_pending",
            "reconcile",
            "pending",
            "balance",
            "events",
        ):
            self.assertTrue(callable(getattr(_budget.RuntimeBudget, name, None)), name)

    def test_native_ledger_surface(self) -> None:
        for name in (
            "allocate",
            "reserve",
            "settle",
            "release",
            "balance",
            "events",
            "replay",
        ):
            self.assertTrue(
                callable(getattr(_budget.NativeBudgetLedger, name, None)), name
            )

    def test_stable_key_and_request_shape(self) -> None:
        request = _budget.BudgetRequest(
            run_id="run-1",
            invocation_id="task:model:attempt-1",
            node_id="task",
            kind="provider",
            name="model",
            input={},
        )
        self.assertEqual(request.run_id, "run-1")
        self.assertEqual(request.invocation_id, "task:model:attempt-1")
        self.assertEqual(request.kind, "provider")
        outcome = _budget.BudgetOutcome(ok=True, value={"ok": True})
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.cancelled)
        self.assertFalse(outcome.timed_out)


if __name__ == "__main__":
    unittest.main()
