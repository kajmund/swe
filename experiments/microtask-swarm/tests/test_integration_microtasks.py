from __future__ import annotations

import unittest

from microtask_swarm.dag import ScheduledTask, compile_layers

try:
    import swe.microtasks as _swe_microtasks  # noqa: F401
except Exception:  # pragma: no cover - depends on optional SWE package/deps
    _swe_microtasks = None

try:
    import swe_budget  # noqa: F401
    import swe_native  # noqa: F401
    from swe.budget import (
        BudgetOutcome,
        BudgetRequest,
        NativeBudgetLedger,
        RuntimeBudget,
    )
    from swe.run_store import MemoryRunStore, WorkflowResources

    _NATIVE_RUNTIME = True
except Exception:  # pragma: no cover - the native wheels are built separately
    _NATIVE_RUNTIME = False


class _OneMilliPolicy:
    """A fixed one-milli ceiling per handler; failures cost nothing."""

    def reserve(self, request: BudgetRequest) -> int:
        return 1

    def settle(self, request: BudgetRequest, outcome: BudgetOutcome) -> int:
        return 1 if outcome.ok else 0


@unittest.skipIf(_swe_microtasks is None, "SWE Python package or jsonschema is absent")
class SweMicrotaskIntegrationTests(unittest.TestCase):
    def test_compiler_is_reused_when_importable(self) -> None:
        plan = compile_layers(
            [
                ScheduledTask("publish", depends_on=("lint", "test")),
                ScheduledTask("lint"),
                ScheduledTask("test", depends_on=("cache",)),
                ScheduledTask("cache"),
            ],
            concurrency=4,
        )
        self.assertEqual(plan.source, "swe.microtasks.compile_microtasks")
        self.assertEqual(plan.layers, (("lint", "cache"), ("test",), ("publish",)))
        self.assertIsNone(plan.fallback_reason)


@unittest.skipUnless(
    _NATIVE_RUNTIME, "swe_native and swe_budget wheels are not installed"
)
class RealWorkflowRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_microtask_batch_runs_with_runtime_budget(self) -> None:
        from swe.microtasks import Microtask, compile_microtasks

        batch = compile_microtasks(
            [
                Microtask("fast", {"ordinal": 2}),
                Microtask("slow", {"ordinal": 1}),
            ],
            handler="swarm.integration",
            concurrency=2,
            name="real_workflow_runner",
            schedule="ready",
        )
        ledger = NativeBudgetLedger()
        ledger.allocate(
            run_id="real-workflow-runner",
            account="integration",
            amount_milli=batch.task_count,
            key="real-workflow-runner:allocate",
        )
        budget = RuntimeBudget(ledger, _OneMilliPolicy(), account="integration")
        seen: list[str] = []

        async def handler(payload):
            seen.append(str(payload["task_id"]))
            return {
                "task_id": payload["task_id"],
                "payload": payload["payload"],
            }

        result = await batch.run(
            handler,
            resources=WorkflowResources(budget=budget),
            run_store=MemoryRunStore(),
            run_id="real-workflow-runner",
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(set(seen), {"fast", "slow"})
        rows = batch.collect_results(result)
        self.assertEqual(len(rows), 2)
        self.assertEqual(budget.balance()["spent"], batch.task_count)
        self.assertEqual(budget.balance()["reserved"], 0)


if __name__ == "__main__":
    unittest.main()
