"""Generic host journal for an external effect that cancel can race.

The mark is ``begin_external_effect``. It is not an OverGraph client. The
native journal schema stays v1, and the only ledger is ``swe-budget``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

pytest.importorskip("swe_native")
pytest.importorskip("swe_budget")

from swe.budget import BudgetRequest, NativeBudgetLedger, RuntimeBudget
from swe.effect_journal import begin_external_effect, blocks_resume
from swe.invocation_handle import current_invocation
from swe.run_store import MemoryRunStore, WorkflowResources
from swe.runtime import WorkflowRunner
from swe.runtime_errors import SwePreflightError
from swe_runtime_helpers import compile_runtime, swe_tasks

pytestmark = pytest.mark.swe_runtime

_CEILING = 100
_ACTUAL = 40


class _Tariff:
    def reserve(self, request: BudgetRequest) -> int:
        return _CEILING

    def settle(self, request: BudgetRequest, outcome: object) -> int:
        return _ACTUAL if getattr(outcome, "ok", False) else 0


def _flow() -> object:
    return compile_runtime(
        {
            "work": {"type": "task", "handler": "demo.work", "next": "done"},
            "done": {"type": "end", "status": "completed"},
        },
        name="effect",
        entrypoint="work",
        handlers=frozenset({"demo.work"}),
    )


def _budget() -> tuple[RuntimeBudget, NativeBudgetLedger]:
    ledger = NativeBudgetLedger()
    ledger.allocate(run_id="alloc", account="demo", amount_milli=1_000, key="demo:alloc")
    return RuntimeBudget(ledger, _Tariff(), account="demo"), ledger


def _kinds(events: list[Mapping[str, object]]) -> list[str]:
    return [str(event.get("kind")) for event in events]


def _ledger_amounts(ledger: NativeBudgetLedger, kind: str) -> list[int]:
    return [
        int(event["amount_milli"])
        for event in ledger.events()
        if event.get("kind") == kind
    ]


async def _stop_leftovers() -> None:
    leftover = swe_tasks()
    for task in leftover:
        task.cancel()
    if leftover:
        await asyncio.gather(*leftover, return_exceptions=True)


def test_recovery_blocks_open_external_attempts_and_replays_pure() -> None:
    import swe_native

    def started(delivery: str, action: str) -> dict[str, object]:
        key = swe_native.delivery_key("run", action, action)
        return {
            "kind": "AttemptStarted",
            "attempt_id": swe_native.attempt_id(key, 1),
            "delivery_key": key,
            "invocation_id": action,
            "action": action,
            "delivery": delivery,
            "started_at_ms": 1,
        }

    irreversible = swe_native.Journal()
    irreversible.append(started("external_non_idempotent", "write"))
    assert blocks_resume(list(irreversible.events()))
    plan = swe_native.Journal.load(irreversible.events()).plan_recovery()
    assert plan[0]["kind"] == "fail_stop"

    idempotent = swe_native.Journal()
    idempotent.append(started("external_idempotent", "retry"))
    assert blocks_resume(list(idempotent.events()))

    pure = swe_native.Journal()
    pure.append(started("pure", "compute"))
    assert blocks_resume(list(pure.events())) is False


def test_unknown_delivery_is_refused_before_dispatch() -> None:
    with pytest.raises(SwePreflightError, match="unknown effect delivery"):
        WorkflowRunner(
            _flow(),
            {"demo.work": _unused},
            resources=WorkflowResources(effect_deliveries={"work": "graph"}),
        )


async def _unused(_payload: Mapping[str, object]) -> Mapping[str, bool]:
    return {"ok": True}


@pytest.mark.asyncio
async def test_cancel_before_effect_releases_only_the_unused_reservation() -> None:
    store = MemoryRunStore()
    budget, ledger = _budget()
    cancel = asyncio.Event()
    entered = asyncio.Event()
    calls: list[str] = []

    async def work(_payload: Mapping[str, object]) -> Mapping[str, bool]:
        calls.append("entered")
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            calls.append("cancelled")
        try:
            await begin_external_effect()
            calls.append("started")
        except asyncio.CancelledError:
            calls.append("refused")
        raise asyncio.CancelledError

    async def trip() -> None:
        await entered.wait()
        cancel.set()

    compiled = _flow()
    result, _ = await asyncio.gather(
        WorkflowRunner(
            compiled,
            {"demo.work": work},
            resources=WorkflowResources(
                budget=budget,
                cancel_event=cancel,
                effect_deliveries={"work": "external_non_idempotent"},
            ),
            run_store=store,
        ).run({}, run_id="before-io"),
        trip(),
    )

    assert result.status == "cancelled"
    assert "entered" in calls
    assert "started" not in calls
    journal = await store.load_journal("before-io")
    assert _kinds(journal) == []
    assert _ledger_amounts(ledger, "spend") == []
    assert _ledger_amounts(ledger, "release") == [_CEILING]
    assert budget.pending() == ()
    assert ledger.balance(account="demo")["spent"] == 0
    assert ledger.balance(account="demo")["reserved"] == 0
    replay = await store.replay_results("before-io")
    assert replay == {}

    async def must_not_run(_payload: Mapping[str, object]) -> Mapping[str, bool]:
        calls.append("resume")
        return {"ok": True}

    resumed = await WorkflowRunner(
        compiled,
        {"demo.work": must_not_run},
        resources=WorkflowResources(
            budget=budget,
            effect_deliveries={"work": "external_non_idempotent"},
        ),
        run_store=store,
    ).resume("before-io")
    assert resumed.status == "cancelled"
    assert "resume" not in calls
    await _stop_leftovers()


@pytest.mark.asyncio
async def test_in_flight_effect_stays_pending_and_resume_does_not_redispatch() -> None:
    store = MemoryRunStore()
    budget, ledger = _budget()
    cancel = asyncio.Event()
    started = asyncio.Event()
    calls: list[str] = []

    async def work(_payload: Mapping[str, object]) -> Mapping[str, bool]:
        await begin_external_effect()
        calls.append("started")
        started.set()
        await asyncio.Event().wait()
        return {"wrote": True}

    async def trip() -> None:
        await started.wait()
        cancel.set()

    compiled = _flow()
    result, _ = await asyncio.gather(
        WorkflowRunner(
            compiled,
            {"demo.work": work},
            resources=WorkflowResources(
                budget=budget,
                cancel_event=cancel,
                effect_deliveries={"work": "external_non_idempotent"},
            ),
            run_store=store,
        ).run({}, run_id="in-flight"),
        trip(),
    )

    assert result.status == "cancelled"
    assert calls == ["started"]
    kinds = _kinds(await store.load_journal("in-flight"))
    assert kinds[:2] == ["AttemptStarted", "ChargeReserved"]
    assert "ChargePendingReconciliation" in kinds
    assert "ChargeSettled" not in kinds
    assert "ChargeReleased" not in kinds
    assert "ResultConfirmed" not in kinds
    assert _ledger_amounts(ledger, "spend") == []
    assert _ledger_amounts(ledger, "release") == []
    assert ledger.balance(account="demo")["spent"] == 0
    assert ledger.balance(account="demo")["reserved"] == _CEILING
    assert len(budget.pending()) == 1
    assert await store.replay_results("in-flight") == {}

    async def must_not_run(_payload: Mapping[str, object]) -> Mapping[str, bool]:
        calls.append("resume")
        return {"ok": True}

    resumed = await WorkflowRunner(
        compiled,
        {"demo.work": must_not_run},
        resources=WorkflowResources(
            budget=budget,
            effect_deliveries={"work": "external_non_idempotent"},
        ),
        run_store=store,
    ).resume("in-flight")
    assert resumed.status == "cancelled"
    assert calls == ["started"]
    assert _ledger_amounts(ledger, "release") == []
    await _stop_leftovers()


@pytest.mark.asyncio
async def test_runner_task_cancel_persists_cancelled_for_an_unknown_effect() -> None:
    store = MemoryRunStore()
    budget, ledger = _budget()
    started = asyncio.Event()

    async def work(_payload: Mapping[str, object]) -> Mapping[str, bool]:
        await begin_external_effect()
        started.set()
        await asyncio.Event().wait()
        return {"wrote": True}

    runner = WorkflowRunner(
        _flow(),
        {"demo.work": work},
        resources=WorkflowResources(
            budget=budget,
            effect_deliveries={"work": "external_non_idempotent"},
        ),
        run_store=store,
    )
    run_task = asyncio.create_task(runner.run({}, run_id="task-cancel"))
    await asyncio.wait_for(started.wait(), timeout=5)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    stored = await store.load_run("task-cancel")
    assert stored is not None
    assert stored.status == "cancelled"
    kinds = _kinds(await store.load_journal("task-cancel"))
    assert "AttemptStarted" in kinds
    assert "ChargePendingReconciliation" in kinds
    assert "ChargeSettled" not in kinds
    assert "ChargeReleased" not in kinds
    assert _ledger_amounts(ledger, "spend") == []
    assert _ledger_amounts(ledger, "release") == []
    assert ledger.balance(account="demo")["reserved"] == _CEILING

    async def must_not_run(_payload: Mapping[str, object]) -> Mapping[str, bool]:
        raise AssertionError("resume must not redispatch an unknown effect")

    resumed = await WorkflowRunner(
        _flow(),
        {"demo.work": must_not_run},
        resources=WorkflowResources(
            budget=budget,
            effect_deliveries={"work": "external_non_idempotent"},
        ),
        run_store=store,
    ).resume("task-cancel")
    assert resumed.status == "cancelled"
    await _stop_leftovers()


@pytest.mark.asyncio
async def test_crash_with_open_effect_fails_closed_on_resume() -> None:
    store = MemoryRunStore()
    budget, ledger = _budget()
    started = asyncio.Event()
    calls: list[str] = []

    async def work(_payload: Mapping[str, object]) -> Mapping[str, bool]:
        await begin_external_effect()
        calls.append("started")
        started.set()
        await asyncio.Event().wait()
        return {"wrote": True}

    run_task = asyncio.create_task(
        WorkflowRunner(
            _flow(),
            {"demo.work": work},
            resources=WorkflowResources(
                budget=budget,
                effect_deliveries={"work": "external_non_idempotent"},
            ),
            run_store=store,
        ).run({}, run_id="crash")
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    crashed = MemoryRunStore()
    crashed.runs["crash"] = store.runs["crash"]
    crashed.journals["crash"] = [dict(event) for event in store.journals["crash"]]
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert crashed.runs["crash"].status == "running"
    assert "ChargePendingReconciliation" not in _kinds(crashed.journals["crash"])

    async def must_not_run(_payload: Mapping[str, object]) -> Mapping[str, bool]:
        calls.append("resume")
        return {"ok": True}

    resumed = await WorkflowRunner(
        _flow(),
        {"demo.work": must_not_run},
        resources=WorkflowResources(
            budget=budget,
            effect_deliveries={"work": "external_non_idempotent"},
        ),
        run_store=crashed,
    ).resume("crash")
    assert resumed.status == "failed"
    assert resumed.error is not None
    assert resumed.error["path"] == "journal"
    assert "resume" not in calls
    assert _ledger_amounts(ledger, "release") == []
    assert ledger.balance(account="demo")["reserved"] == _CEILING
    assert ledger.balance(account="demo")["spent"] == 0
    await _stop_leftovers()


@pytest.mark.asyncio
async def test_confirmed_effect_settles_the_actual_charge_not_the_ceiling() -> None:
    store = MemoryRunStore()
    budget, ledger = _budget()
    calls: list[str] = []

    async def work(_payload: Mapping[str, object]) -> Mapping[str, bool]:
        handle = current_invocation()
        assert handle is not None
        assert handle.effect.delivery == "external_non_idempotent"
        await begin_external_effect()
        calls.append("started")
        return {"wrote": True}

    compiled = _flow()
    result = await WorkflowRunner(
        compiled,
        {"demo.work": work},
        resources=WorkflowResources(
            budget=budget,
            effect_deliveries={"work": "external_non_idempotent"},
        ),
        run_store=store,
    ).run({}, run_id="confirmed")

    assert result.status == "completed"
    assert calls == ["started"]
    kinds = _kinds(await store.load_journal("confirmed"))
    assert kinds == [
        "AttemptStarted",
        "ChargeReserved",
        "ResultConfirmed",
        "ChargeSettled",
    ]
    settled = [
        int(event["amount_milli"])
        for event in await store.load_journal("confirmed")
        if event["kind"] == "ChargeSettled"
    ]
    reserved = [
        int(event["amount_milli"])
        for event in await store.load_journal("confirmed")
        if event["kind"] == "ChargeReserved"
    ]
    assert reserved == [_CEILING]
    assert settled == [_ACTUAL]
    assert "ChargePendingReconciliation" not in kinds
    assert "ChargeReleased" not in kinds
    assert _ledger_amounts(ledger, "spend") == [_ACTUAL]
    assert _ledger_amounts(ledger, "release") == [_CEILING - _ACTUAL]
    assert ledger.balance(account="demo")["spent"] == _ACTUAL
    assert ledger.balance(account="demo")["reserved"] == 0

    async def must_not_run(_payload: Mapping[str, object]) -> Mapping[str, bool]:
        calls.append("resume")
        return {"ok": True}

    resumed = await WorkflowRunner(
        compiled,
        {"demo.work": must_not_run},
        resources=WorkflowResources(
            budget=budget,
            effect_deliveries={"work": "external_non_idempotent"},
        ),
        run_store=store,
    ).resume("confirmed")
    assert resumed.status == "completed"
    assert calls == ["started"]
    await _stop_leftovers()


@pytest.mark.asyncio
async def test_external_return_without_the_mark_releases_and_fails() -> None:
    store = MemoryRunStore()
    budget, ledger = _budget()

    async def work(_payload: Mapping[str, object]) -> Mapping[str, bool]:
        return {"wrote": True}

    result = await WorkflowRunner(
        _flow(),
        {"demo.work": work},
        resources=WorkflowResources(
            budget=budget,
            effect_deliveries={"work": "external_non_idempotent"},
        ),
        run_store=store,
    ).run({}, run_id="unmarked")

    assert result.status == "failed"
    assert result.error is not None
    assert "begin_external_effect" in result.error["message"]
    assert _kinds(await store.load_journal("unmarked")) == []
    assert _ledger_amounts(ledger, "spend") == []
    assert _ledger_amounts(ledger, "release") == [_CEILING]
    await _stop_leftovers()


@pytest.mark.asyncio
async def test_undeclared_cancel_still_keeps_the_reservation_pending() -> None:
    budget, ledger = _budget()
    cancel = asyncio.Event()
    entered = asyncio.Event()

    async def work(_payload: Mapping[str, object]) -> Mapping[str, bool]:
        entered.set()
        await asyncio.Event().wait()
        return {"ok": True}

    async def trip() -> None:
        await entered.wait()
        cancel.set()

    result, _ = await asyncio.gather(
        WorkflowRunner(
            _flow(),
            {"demo.work": work},
            resources=WorkflowResources(budget=budget, cancel_event=cancel),
        ).run({}, run_id="legacy-cancel"),
        trip(),
    )

    assert result.status == "cancelled"
    assert _ledger_amounts(ledger, "release") == []
    assert _ledger_amounts(ledger, "spend") == []
    assert ledger.balance(account="demo")["reserved"] == _CEILING
    assert len(budget.pending()) == 1
    await _stop_leftovers()
