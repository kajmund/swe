"""Runtime budget integration: reserve before dispatch, settle/release once."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

pytest.importorskip("swe_native")
pytest.importorskip("swe_budget")

from swe.budget import (
    BudgetExceeded,
    BudgetOutcome,
    BudgetRequest,
    BudgetStateError,
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
from swe.runtime import WorkflowRunner, run_workflow
from swe_runtime_helpers import swe_tasks

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
    assert budget.open_count == 0
    assert budget.retained_terminal_count <= budget.terminal_capacity

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

    store = _CrashOnceStore(crash_after=tasks)
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


class _RaisingSettlePolicy(_FixedPolicy):
    def __init__(self) -> None:
        super().__init__(reserve_milli=10, failure_milli=10)

    def settle(self, request: BudgetRequest, outcome: BudgetOutcome) -> int:
        raise RuntimeError("pricing broke")


class _InvalidSettlePolicy(_FixedPolicy):
    def __init__(self) -> None:
        super().__init__(reserve_milli=10, failure_milli=10)

    def settle(self, request: BudgetRequest, outcome: BudgetOutcome) -> int:
        return -1


class _OverrunSettlePolicy(_FixedPolicy):
    def __init__(self) -> None:
        super().__init__(reserve_milli=10, failure_milli=10)

    def settle(self, request: BudgetRequest, outcome: BudgetOutcome) -> int:
        return self.reserve_milli + 1


def _one_task_batch():
    return compile_microtasks(
        [Microtask("only")],
        handler="m.work",
        concurrency=1,
        timeout_seconds=30,
    )


@pytest.mark.parametrize(
    ("policy_factory", "reconciled_milli"),
    [
        (_RaisingSettlePolicy, 7),
        (_InvalidSettlePolicy, 5),
        (_OverrunSettlePolicy, 10),
    ],
)
@pytest.mark.asyncio
async def test_failed_settlement_is_pending_and_reconcilable_by_invocation_id(
    policy_factory: type[_FixedPolicy],
    reconciled_milli: int,
) -> None:
    """A policy exception, invalid amount, and overrun keep the hold visible.

    The host does not receive the reservation object from dispatch; it must be
    able to find the hold through the public budget object and reconcile it by
    invocation id without releasing the funds early or settling twice.
    """

    budget, ledger = _budget(cap_milli=10, policy=policy_factory())
    batch = _one_task_batch()

    async def work(_payload: Mapping[str, Any]) -> Mapping[str, bool]:
        return {"ok": True}

    result = await batch.run(
        work,
        resources=WorkflowResources(budget=budget),
        run_id="settle-failure",
    )

    assert result.status == "failed"
    balance = ledger.balance(account="a")
    assert balance["reserved"] == 10
    assert balance["spent"] == 0
    assert balance["available"] == 0

    pending = budget.pending()
    assert len(pending) == 1
    reservation = pending[0]
    invocation_id = reservation.request.invocation_id
    assert budget.reservation_for(invocation_id) is reservation
    assert budget.unresolved() == (reservation,)

    budget.reconcile_invocation(invocation_id, spent_milli=reconciled_milli)
    balance = ledger.balance(account="a")
    assert balance["spent"] == reconciled_milli
    assert balance["reserved"] == 0
    assert balance["available"] == 10 - reconciled_milli
    assert budget.pending() == ()
    assert budget.unresolved() == ()

    # Repeating reconciliation is a no-op, and a second settle is rejected.
    budget.reconcile_invocation(invocation_id, spent_milli=reconciled_milli)
    assert len([event for event in ledger.events() if event["kind"] == "spend"]) == 1
    with pytest.raises(BudgetStateError):
        budget.settle(reservation, BudgetOutcome(ok=True))


def test_unresolved_exposes_a_reserved_hold_and_requires_pending_to_reconcile() -> None:
    budget, ledger = _budget(cap_milli=10)
    reservation = budget.reserve(_request("inv-direct"))
    assert reservation is not None
    assert budget.unresolved() == (reservation,)
    assert budget.pending() == ()

    # An in-flight hold cannot be reconciled behind the runtime's back.
    with pytest.raises(BudgetStateError):
        budget.reconcile_invocation("inv-direct", release=True)

    budget.mark_pending(reservation)
    assert budget.pending() == (reservation,)
    budget.reconcile_invocation("inv-direct", release=True)
    assert budget.unresolved() == ()
    assert ledger.balance(account="a")["reserved"] == 0
    assert ledger.balance(account="a")["available"] == 10


def _one_node_flow() -> object:
    return compile_definition(
        {
            "version": "1.0",
            "name": "budget_retry",
            "entrypoint": "work",
            "limits": {"max_steps": 8, "max_depth": 1, "timeout_seconds": 30},
            "nodes": {
                "work": {"type": "task", "handler": "m.work", "next": "done"},
                "done": {"type": "end", "status": "completed"},
            },
        },
        handlers=frozenset({"m.work"}),
    )


@pytest.mark.asyncio
async def test_terminal_delivery_is_not_redispatched_on_host_retry() -> None:
    calls: list[int] = []

    async def work(_payload: Mapping[str, Any]) -> Mapping[str, bool]:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    budget, ledger = _budget(cap_milli=5)
    runner = WorkflowRunner(
        _one_node_flow(),
        {"m.work": work},
        resources=WorkflowResources(budget=budget),
        run_store=MemoryRunStore(),
    )
    first = await runner.run({}, run_id="retry-terminal")
    assert first.status == "failed"
    assert calls == [0]
    assert budget.pending() == ()
    assert budget.open_count == 0
    assert budget.retained_terminal_count == 1

    # Same run/invocation id = same logical delivery. It is already settled at 0
    # (the failed attempt), so a second dispatch must fail closed before the
    # handler runs, not conflict after the handler succeeds.
    second = await runner.run({}, run_id="retry-terminal")
    assert second.status == "failed"
    assert second.error is not None
    assert "already settled" in second.error["message"].lower()
    assert calls == [0]
    assert ledger.balance(account="a")["spent"] == 0
    assert budget.pending() == ()


class _CrashBeforePersistStore(MemoryRunStore):
    """Persist the run start, then crash before the first invocation record."""

    def __init__(self) -> None:
        super().__init__()
        self.crashed = False

    async def record(
        self,
        run: RunRecord,
        *,
        events: Sequence[Mapping[str, Any]] = (),
        invocation: InvocationRecord | None = None,
    ) -> None:
        if invocation is not None and not self.crashed:
            self.crashed = True
            raise RuntimeError("simulated crash before persist")
        await super().record(run, events=events, invocation=invocation)


@pytest.mark.asyncio
async def test_crash_between_settle_and_persist_restores_terminal_guard() -> None:
    calls: list[int] = []

    async def work(_payload: Mapping[str, Any]) -> Mapping[str, bool]:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    budget, ledger = _budget(cap_milli=5)
    store = _CrashBeforePersistStore()
    runner = WorkflowRunner(
        _one_node_flow(),
        {"m.work": work},
        resources=WorkflowResources(budget=budget),
        run_store=store,
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        await runner.run({}, run_id="crash-terminal")
    await _cancel_swe_tasks()
    assert calls == [0]

    replayed = NativeBudgetLedger.replay(ledger.events())
    resumed_budget = RuntimeBudget(replayed, _FixedPolicy(), account="a")
    resumed = await WorkflowRunner(
        _one_node_flow(),
        {"m.work": work},
        resources=WorkflowResources(budget=resumed_budget),
        run_store=store,
    ).resume("crash-terminal")

    assert resumed.status == "failed"
    assert resumed.error is not None
    assert "already settled" in resumed.error["message"].lower()
    assert calls == [0]
    assert replayed.balance(account="a")["spent"] == 0
    assert resumed_budget.pending() == ()


def test_restore_reattaches_an_open_ledger_reservation() -> None:
    ledger = NativeBudgetLedger()
    ledger.allocate(run_id="run-restore", account="a", amount_milli=5, key="alloc")
    ledger.reserve(
        run_id="run-restore",
        account="a",
        amount_milli=1,
        key="run-restore:inv-restore:reserve",
    )

    replayed = NativeBudgetLedger.replay(ledger.events())
    budget = RuntimeBudget(replayed, _FixedPolicy(), account="a")
    assert budget.pending() == ()
    assert budget.restore(run_id="run-restore") == 1

    pending = budget.pending()
    assert len(pending) == 1
    assert pending[0].request.invocation_id == "inv-restore"
    assert replayed.balance(account="a")["reserved"] == 1

    budget.reconcile_invocation("inv-restore", spent_milli=1)
    assert replayed.balance(account="a")["spent"] == 1
    assert replayed.balance(account="a")["reserved"] == 0


@pytest.mark.asyncio
async def test_cancellation_resistant_abandon_marks_pending_for_recovery() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def stubborn(_payload: Mapping[str, Any]) -> Mapping[str, bool]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            finished.set()
            return {"ok": True}
        return {"ok": False}

    budget, ledger = _budget(cap_milli=1)
    runner = WorkflowRunner(
        _one_node_flow(),
        {"m.work": stubborn},
        resources=WorkflowResources(
            budget=budget, action_timeouts={"work": 1}
        ),
    )
    result = await asyncio.wait_for(
        runner.run({}, run_id="abandon-budget"), timeout=5
    )
    assert result.status == "failed"
    assert started.is_set()

    pending = budget.pending()
    assert len(pending) == 1
    assert ledger.balance(account="a")["reserved"] == 1
    invocation_id = pending[0].request.invocation_id

    budget.reconcile_invocation(invocation_id, release=True)
    assert ledger.balance(account="a")["reserved"] == 0

    release.set()
    await asyncio.wait_for(finished.wait(), timeout=2)
    await asyncio.sleep(0)
    # The late return tries to settle a released reservation; it is rejected
    # inside the abandoned task and never charges a second time.
    assert ledger.balance(account="a")["spent"] == 0
    await _cancel_swe_tasks()
    assert swe_tasks() == []


def test_terminal_retention_is_bounded_and_drops_input() -> None:
    ledger = NativeBudgetLedger()
    ledger.allocate(run_id="r", account="a", amount_milli=10, key="alloc")
    budget = RuntimeBudget(
        ledger,
        _FixedPolicy(),
        account="a",
        terminal_capacity=2,
    )
    reservations = []
    for index in range(3):
        reservation = budget.reserve(
            BudgetRequest(
                run_id="r",
                invocation_id=f"inv-{index}",
                node_id="work",
                kind="handler",
                name="m.work",
                input={"secret": index},
            )
        )
        assert reservation is not None
        budget.settle(reservation, BudgetOutcome(ok=True, value={"i": index}))
        reservations.append(reservation)

    assert budget.open_count == 0
    assert budget.retained_terminal_count == 2
    assert budget.reservation_for("inv-0") is None
    assert budget.reservation_for("inv-2") is reservations[2]
    assert reservations[0].request.input == {}
    assert ledger.balance(account="a")["spent"] == 3

