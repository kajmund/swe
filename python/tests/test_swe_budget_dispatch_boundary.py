"""Pin the opt-in runtime-budget boundary at ``spawn_invocations``.

Audit: ``docs/analysis/2026-11-25-rea-budget-audit.md``.

The original characterization test (2026-11-23, commit ``63e7393``) pinned an
*absent* budget hook: ``WorkflowResources`` had no ``budget`` field and
``spawn_invocations`` had no budget parameter. That finding was correct at its
baseline ``eff1c7e`` and is now obsolete: PR #11 introduced
``WorkflowResources.budget`` and the PyO3 ``swe_budget.Ledger`` binding.
PR #15 added an experiments-only adaptive-tariff comparison; production still
requires a host-supplied ``BudgetPolicy``.

This test pins the *new* boundary instead:

- the resource is opt-in; without it dispatch runs the handler and appends no
  reservation to ``budget_slots``;
- with it, dispatch reserves before the handler and settles exactly once;
- the handler observes the hold while it runs.

It uses a small in-memory ``BudgetLedger`` protocol implementation, so it needs
neither the compiled ``swe_native`` nor ``swe_budget`` wheels and makes no paid
calls. End-to-end lifecycle coverage (cancel, timeout, replay, overrun) lives in
``test_swe_budget_runtime.py`` and ``test_swe_budget_binding.py``.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from swe.budget import BudgetOutcome, BudgetRequest, RuntimeBudget
from swe.run_store import WorkflowResources
from swe.runtime_dispatch import (
    CapacityLimiter,
    ProviderDispatch,
    spawn_invocations,
)


class RecordingLedger:
    """Minimal ``BudgetLedger`` protocol implementation for one dispatch."""

    def __init__(self, allocated_milli: int = 1_000) -> None:
        self.allocated_milli = allocated_milli
        self.calls: list[tuple[str, int]] = []
        self.spent_milli = 0
        self.reserved_milli = 0
        self._next_id = 1
        self._holds: dict[int, dict[str, Any]] = {}

    def reserve(self, *, run_id: str, account: str, amount_milli: int, key: str) -> int:
        self.calls.append(("reserve", amount_milli))
        reservation_id = self._next_id
        self._next_id += 1
        self._holds[reservation_id] = {
            "amount": amount_milli,
            "spent": 0,
            "status": "open",
        }
        self.reserved_milli += amount_milli
        return reservation_id

    def settle(self, *, reservation_id: int, spent_milli: int, key: str) -> None:
        self.calls.append(("settle", spent_milli))
        hold = self._holds[reservation_id]
        hold["status"] = "settled"
        hold["spent"] = spent_milli
        self.reserved_milli -= hold["amount"]
        self.spent_milli += spent_milli

    def release(self, *, reservation_id: int, key: str) -> None:
        self.calls.append(("release", 0))
        hold = self._holds[reservation_id]
        hold["status"] = "released"
        self.reserved_milli -= hold["amount"]

    def lookup(self, *, key: str) -> Mapping[str, Any] | None:
        return None

    def balance(self, *, account: str) -> Mapping[str, int]:
        return {
            "allocated": self.allocated_milli,
            "incoming": 0,
            "outgoing": 0,
            "spent": self.spent_milli,
            "reserved": self.reserved_milli,
            "available": self.allocated_milli - self.spent_milli - self.reserved_milli,
        }

    def events(self) -> Sequence[Mapping[str, Any]]:
        return ()


class FixedPolicy:
    def __init__(self, *, reserve_milli: int, settle_milli: int) -> None:
        self.reserve_milli = reserve_milli
        self.settle_milli = settle_milli

    def reserve(self, request: BudgetRequest) -> int:
        return self.reserve_milli

    def settle(self, request: BudgetRequest, outcome: BudgetOutcome) -> int:
        return self.settle_milli


async def _dispatch(
    handler: Any,
    *,
    ledger: RecordingLedger | None = None,
    budget_slots: dict[str, list[Any]] | None = None,
) -> tuple[list[Any], dict[str, list[Any]]]:
    """Run one handler effect, with or without an attached ``RuntimeBudget``."""

    budget = None
    if ledger is not None:
        budget = RuntimeBudget(
            ledger,
            FixedPolicy(reserve_milli=10, settle_milli=8),
            account="a",
        )
    slots: dict[str, list[Any]] = {} if budget_slots is None else budget_slots
    effect = {
        "type": "invoke_handler",
        "invocation_id": "inv-budget-1",
        "node_id": "paid-node",
        "handler": "paid.work",
        "input": {},
    }
    nodes = {"paid-node": {"type": "task", "handler": "paid.work"}}
    pending: dict[str, asyncio.Task[Any]] = {}
    spawn_invocations(
        [effect],
        handlers={"paid.work": handler},
        nodes=nodes,
        pending=pending,
        semaphore=CapacityLimiter(1),
        provider_dispatch=ProviderDispatch(providers={}),
        now_ms=lambda: 0,
        budget=budget,
        budget_slots=slots,
        run_id="run-1",
    )
    outcomes = list(await asyncio.gather(*pending.values()))
    return outcomes, slots


@pytest.mark.asyncio
async def test_dispatch_without_budget_runs_handler_without_a_reservation() -> None:
    """The resource is opt-in: no ledger is attached and no hold is recorded."""

    calls: list[int] = []

    async def handler(_payload: Mapping[str, Any]) -> Mapping[str, bool]:
        calls.append(1)
        return {"ok": True}

    outcomes, slots = await _dispatch(handler, budget_slots={})

    assert outcomes[0].ok is True
    assert calls == [1]
    # spawn_invocations creates one slot per handler/provider effect; with no
    # RuntimeBudget it stays empty, so no reservation was appended.
    assert slots == {"inv-budget-1": []}


@pytest.mark.asyncio
async def test_dispatch_with_budget_reserves_before_handler_and_settles_once() -> None:
    """The handler runs under an open hold; dispatch settles it once."""

    ledger = RecordingLedger()
    observed: list[dict[str, int]] = []

    async def handler(_payload: Mapping[str, Any]) -> Mapping[str, bool]:
        observed.append(dict(ledger.balance(account="a")))
        return {"ok": True}

    outcomes, slots = await _dispatch(handler, ledger=ledger, budget_slots={})

    assert outcomes[0].ok is True
    assert observed == [
        {
            "allocated": 1_000,
            "incoming": 0,
            "outgoing": 0,
            "spent": 0,
            "reserved": 10,
            "available": 990,
        }
    ]
    assert len(slots["inv-budget-1"]) == 1
    assert ledger.calls == [("reserve", 10), ("settle", 8)]
    assert ledger.spent_milli == 8
    assert ledger.reserved_milli == 0


def test_workflow_resources_exposes_budget_as_an_optional_field() -> None:
    """The structural absence recorded on 2026-11-23 is gone."""

    assert WorkflowResources().budget is None
    parameters = inspect.signature(spawn_invocations).parameters
    assert "budget" in parameters
    assert "budget_slots" in parameters
