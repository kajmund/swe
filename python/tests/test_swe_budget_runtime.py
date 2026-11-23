"""Runtime budget integration: reserve before dispatch, settle/release once."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

pytest.importorskip("swe_native")

from swe.budget import (
    BudgetExceeded,
    BudgetOutcome,
    BudgetRequest,
    NativeBudgetLedger,
    RuntimeBudget,
)
from swe.cache import InMemoryDecisionCache, scoped_cache_params
from swe.definition import compile_definition
from swe.microtasks import Microtask, MicrotaskMeter, compile_microtasks
from swe.run_store import (
    InvocationRecord,
    MemoryRunStore,
    RunRecord,
    WorkflowResources,
)
from swe.runtime import run_workflow

pytestmark = pytest.mark.swe_runtime


class _FixedPolicy:
    """One milli-unit per call; a failed call costs nothing by default."""

    def __init__(self, *, reserve_milli: int = 1, failure_milli: int = 0) -> None:
        self.reserve_milli = reserve_milli
        self.failure_milli = failure_milli

    def reserve(self, request: BudgetRequest) -> int:
        return self.reserve_milli

    def settle(self, request: BudgetRequest, outcome: BudgetOutcome) -> int:
        return self.reserve_milli if outcome.ok else self.failure_milli


def _request(invocation_id: str, *, run_id: str = "run-1") -> BudgetRequest:
    return BudgetRequest(
        run_id=run_id,
        invocation_id=invocation_id,
        node_id="work",
        kind="handler",
        name="demo.work",
        input={},
    )


def _budget(*, cap_milli: int, account: str = "a", policy=None) -> tuple[RuntimeBudget, NativeBudgetLedger]:
    ledger = NativeBudgetLedger()
    ledger.allocate(
        run_id="run-1",
        account=account,
        amount_milli=cap_milli,
        key=f"{account}:alloc",
    )
    budget = RuntimeBudget(
        ledger,
        policy or _FixedPolicy(),
        account=account,
    )
    return budget, ledger


async def _cancel_swe_tasks() -> None:
    leftover = [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("swe:") and not task.done()
    ]
    for task in leftover:
        task.cancel()
    if leftover:
        await asyncio.gather(*leftover, return_exceptions=True)


@pytest.mark.asyncio
async def test_ten_thousand_microtasks_with_budget_and_bounded_concurrency() -> None:
    tasks = 10_000
    concurrency = 64
    batch = compile_microtasks(
        (Microtask(f"m-{index}", {"ordinal": index}) for index in range(tasks)),
        handler="m.work",
        concurrency=concurrency,
        name="budget_10k",
        timeout_seconds=300,
    )

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        await asyncio.sleep(0)
        return {
            "task_id": payload["task_id"],
            "ordinal": payload["payload"]["ordinal"],
        }

    baseline_meter = MicrotaskMeter(work)
    baseline_started = time.perf_counter_ns()
    baseline = await batch.run(baseline_meter, run_id="baseline-10k")
    baseline_ms = (time.perf_counter_ns() - baseline_started) / 1_000_000
    assert baseline.status == "completed"

    budget, ledger = _budget(cap_milli=tasks)
    meter = MicrotaskMeter(work)
    started = time.perf_counter_ns()
    result = await batch.run(
        meter,
        resources=WorkflowResources(budget=budget),
        run_id="budget-10k",
    )
    budget_ms = (time.perf_counter_ns() - started) / 1_000_000

    assert result.status == "completed"
    assert len(batch.collect_results(result)) == tasks
    metrics = meter.snapshot()
    assert 1 < metrics["peak_concurrency"] <= concurrency
    assert metrics["started"] == tasks
    assert metrics["succeeded"] == tasks

    balance = ledger.balance(account="a")
    assert balance["spent"] == tasks
    assert balance["reserved"] == 0
    assert balance["available"] == 0

    kinds = [event["kind"] for event in ledger.events()]
    assert kinds.count("allocate") == 1
    assert kinds.count("reserve") == tasks
    assert kinds.count("spend") == tasks
    # Every settlement spent the full reservation, so no remainder release.
    assert kinds.count("release") == 0

    overhead_pct = (budget_ms - baseline_ms) / max(baseline_ms, 1e-9) * 100
    print(
        f"10k budget overhead: baseline={baseline_ms:.1f}ms "
        f"budget={budget_ms:.1f}ms overhead={overhead_pct:.1f}%"
    )
    # Generous sanity bound; the exact number is diagnostic, not a gate.
    assert budget_ms <= baseline_ms * 5 + 1_000


@pytest.mark.asyncio
async def test_budget_cap_fails_closed_before_dispatch() -> None:
    calls: list[str] = []
    batch = compile_microtasks(
        (Microtask(f"m-{index}") for index in range(200)),
        handler="m.work",
        concurrency=8,
        name="budget_cap",
        timeout_seconds=30,
    )

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(payload["task_id"])
        await asyncio.sleep(0)
        return {"task_id": payload["task_id"]}

    budget, ledger = _budget(cap_milli=100)
    store = MemoryRunStore()
    result = await batch.run(
        work,
        resources=WorkflowResources(budget=budget),
        run_store=store,
        run_id="budget-cap",
    )

    assert result.status == "failed"
    assert result.error is not None
    # The frozen kernel error enum has no budget kind; the host record keeps it.
    assert result.error["kind"] == "handler"
    assert "budget" in result.error["message"].lower()
    persisted = store.invocations.get("budget-cap", {})
    budget_errors = [
        record.error
        for record in persisted.values()
        if isinstance(record.error, Mapping)
        and record.error.get("kind") == "budget"
    ]
    assert budget_errors
    assert len(calls) == 100
    assert ledger.balance(account="a")["spent"] == 100
    assert ledger.balance(account="a")["reserved"] == 0


@pytest.mark.asyncio
async def test_cancel_keeps_reservation_pending_until_reconciliation() -> None:
    batch = compile_microtasks(
        [Microtask("only")], handler="m.hang", concurrency=1, timeout_seconds=30
    )
    started = asyncio.Event()
    cancel = asyncio.Event()

    async def hang(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        started.set()
        await asyncio.Event().wait()
        return {"ok": True}

    budget, ledger = _budget(cap_milli=1)
    run_task = asyncio.create_task(
        batch.run(
            hang,
            resources=WorkflowResources(budget=budget, cancel_event=cancel),
            run_id="cancel-run",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    cancel.set()
    result = await asyncio.wait_for(run_task, timeout=5)

    assert result.status == "cancelled"
    balance = ledger.balance(account="a")
    assert balance["reserved"] == 1
    assert balance["spent"] == 0
    assert balance["available"] == 0
    pending = budget.pending()
    assert len(pending) == 1

    # The only unit is still held: a second invocation cannot reuse it.
    with pytest.raises(BudgetExceeded):
        budget.reserve(_request("inv-2", run_id="run-2"))

    budget.reconcile(pending[0], release=True)
    assert ledger.balance(account="a")["reserved"] == 0
    assert ledger.balance(account="a")["available"] == 1
    retry = budget.reserve(_request("inv-2", run_id="run-2"))
    assert retry is not None
    budget.release(retry)
    await _cancel_swe_tasks()


@pytest.mark.asyncio
async def test_failure_settles_zero_once_and_releases_the_hold() -> None:
    batch = compile_microtasks(
        [Microtask("only")], handler="m.fail", concurrency=1, timeout_seconds=30
    )

    async def fail(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError("boom")

    budget, ledger = _budget(cap_milli=1)
    result = await batch.run(
        fail,
        resources=WorkflowResources(budget=budget),
        run_id="failure-run",
    )

    assert result.status == "failed"
    balance = ledger.balance(account="a")
    assert balance["spent"] == 0
    assert balance["reserved"] == 0
    assert balance["available"] == 1
    kinds = [event["kind"] for event in ledger.events()]
    assert kinds.count("reserve") == 1
    assert kinds.count("spend") == 1
    assert len([event for event in ledger.events() if event["kind"] == "spend"]) == 1


class _CrashOnceStore(MemoryRunStore):
    """Persist a transition, then fail once to emulate a process crash."""

    def __init__(self, *, crash_after: int) -> None:
        super().__init__()
        self.crash_after = crash_after
        self.crashed = False

    async def record(
        self,
        run: RunRecord,
        *,
        events: Sequence[Mapping[str, Any]] = (),
        invocation: InvocationRecord | None = None,
    ) -> None:
        await super().record(run, events=events, invocation=invocation)
        if (
            invocation is not None
            and not self.crashed
            and len(self.invocations[run.run_id]) >= self.crash_after
        ):
            self.crashed = True
            raise RuntimeError("simulated crash")


@pytest.mark.asyncio
async def test_crash_and_resume_replays_ledger_without_double_charge() -> None:
    tasks = 20
    batch = compile_microtasks(
        (Microtask(f"m-{index}") for index in range(tasks)),
        handler="m.work",
        concurrency=4,
        name="budget_replay",
        timeout_seconds=30,
    )
    handler_calls: list[str] = []

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        handler_calls.append(payload["task_id"])
        await asyncio.sleep(0)
        return {"task_id": payload["task_id"]}

    store = _CrashOnceStore(crash_after=5)
    budget, ledger = _budget(cap_milli=tasks)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await batch.run(
            work,
            resources=WorkflowResources(budget=budget),
            run_store=store,
            run_id="replay-run",
        )
    await _cancel_swe_tasks()

    replayed = NativeBudgetLedger.replay(ledger.events())
    replayed_budget = RuntimeBudget(replayed, _FixedPolicy(), account="a")
    resumed = await batch.resume(
        work,
        "replay-run",
        resources=WorkflowResources(budget=replayed_budget),
        run_store=store,
    )

    assert resumed.status == "completed"
    balance = replayed.balance(account="a")
    assert balance["spent"] == tasks
    assert balance["reserved"] == 0
    spend_events = [
        event for event in replayed.events() if event["kind"] == "spend"
    ]
    assert len(spend_events) == tasks
    assert len({event["reservation_id"] for event in spend_events}) == tasks
    # Crash recovery may re-run unpersisted handlers (at-least-once), but the
    # stable reservation key prevents a second charge for the same invocation.
    assert len(handler_calls) >= tasks


def _provider_definition() -> object:
    return compile_definition(
        {
            "version": "1.0",
            "name": "budget_provider",
            "entrypoint": "decide",
            "limits": {"max_steps": 8, "max_depth": 1, "timeout_seconds": 30},
            "nodes": {
                "decide": {
                    "type": "decision",
                    "evaluator": "provider",
                    "provider": "mock.decision",
                    "prompt_key": "demo.assessment",
                    "input": {"topic": "$.topic"},
                    "choices": ["yes", "no"],
                    "routes": {"yes": "done", "no": "done"},
                    "cache_policy": "exact",
                },
                "done": {"type": "end", "status": "completed"},
            },
        },
        providers=frozenset({"mock.decision"}),
        prompt_keys=frozenset({"demo.assessment"}),
    )


@pytest.mark.asyncio
async def test_provider_and_cache_hit_use_the_same_budget_lifecycle() -> None:
    calls: list[str] = []

    async def provider(request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(request["prompt_key"])
        return {"choice": "yes"}

    provider.cache_params = scoped_cache_params(  # type: ignore[attr-defined]
        auth_scope="tenant-a",
        model="m1",
        policy_version="pol-1",
    )

    class CacheAwarePolicy:
        def reserve(self, request: BudgetRequest) -> int:
            return 1

        def settle(self, request: BudgetRequest, outcome: BudgetOutcome) -> int:
            return 0 if outcome.cache_state == "hit" else 1

    budget, ledger = _budget(cap_milli=2, policy=CacheAwarePolicy())
    cache = InMemoryDecisionCache()
    resources = WorkflowResources(
        providers={"mock.decision": provider},
        prompts={"demo.assessment": "choose {topic}"},
        prompt_keys=frozenset({"demo.assessment"}),
        cache=cache,
        budget=budget,
    )
    definition = _provider_definition()

    first = await run_workflow(
        definition, {}, {"topic": "alpha"}, resources=resources, run_id="provider-1"
    )
    second = await run_workflow(
        definition, {}, {"topic": "alpha"}, resources=resources, run_id="provider-2"
    )

    assert first.status == "completed"
    assert second.status == "completed"
    assert first.output["decide"]["cache"] == "miss"
    assert second.output["decide"]["cache"] == "hit"
    assert calls == ["demo.assessment"]
    assert ledger.balance(account="a")["spent"] == 1
    assert ledger.balance(account="a")["reserved"] == 0
    assert ledger.balance(account="a")["available"] == 1

