"""Characterization test: Python dispatch has no budget-ledger hook.

Audit: ``docs/analysis/2026-11-23-rea-budget-audit.md``.

This is deliberately **not** a claim that SWD 1.0 promises budget enforcement.
The frozen schema has no budget field, and
``docs/specs/swe-runtime.md:91`` says budget settlement is not on the Python
runtime path. The test pins the current host boundary so a later integration
cannot silently assume that ``WorkflowRunner`` reserves or settles Sweycoin.

If a budget hook is added to dispatch or ``WorkflowResources``, these tests
must fail and be updated together with the policy document.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
from dataclasses import fields
from typing import Any

import pytest

from swe.run_store import WorkflowResources
from swe.runtime_dispatch import (
    CapacityLimiter,
    ProviderDispatch,
    spawn_invocations,
)


class HostBudget:
    """A host-side cap that dispatch is not allowed to know about today."""

    def __init__(self, cap_milli: int) -> None:
        self.cap_milli = cap_milli
        self.spent_milli = 0
        self.reserve_calls = 0
        self.settle_calls = 0

    def reserve(self, amount_milli: int) -> None:
        self.reserve_calls += 1
        if self.spent_milli + amount_milli > self.cap_milli:
            raise RuntimeError("host budget exceeded")

    def settle(self, amount_milli: int) -> None:
        self.settle_calls += 1
        self.spent_milli += amount_milli


@pytest.mark.asyncio
async def test_dispatch_runs_paid_handler_without_consulting_host_budget() -> None:
    """A would-be paid handler runs even when the host cap is zero.

    SWE has no parameter that lets the caller hand a ledger to dispatch, so the
    host must enforce the cap inside its handler or a wrapper. This test
    demonstrates the consequence, not a contract breach.
    """

    budget = HostBudget(cap_milli=0)
    charged_milli: list[int] = []

    async def paid_handler(_payload: dict[str, Any]) -> dict[str, bool]:
        # The host policy would reserve here. SWE never calls ``budget``.
        charged_milli.append(1_000)
        return {"ok": True}

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
        handlers={"paid.work": paid_handler},
        nodes=nodes,
        pending=pending,
        semaphore=CapacityLimiter(1),
        provider_dispatch=ProviderDispatch(providers={}),
        now_ms=lambda: 0,
    )
    outcomes = await asyncio.gather(*pending.values())

    assert outcomes[0].ok is True
    assert charged_milli == [1_000]
    assert sum(charged_milli) > budget.cap_milli
    assert budget.reserve_calls == 0
    assert budget.settle_calls == 0
    assert budget.spent_milli == 0


def test_workflow_resources_and_dispatch_expose_no_budget_hook() -> None:
    """The absence is structural, not just a missing call in one path."""

    resource_fields = {field.name for field in fields(WorkflowResources)}
    assert not resource_fields.intersection({"budget", "ledger", "spend", "cost"})

    parameters = inspect.signature(spawn_invocations).parameters
    assert not set(parameters).intersection({"budget", "ledger", "spend", "cost"})


def test_core_ledger_is_not_exposed_as_a_python_module() -> None:
    """The Rust ``swe-budget`` crate has no Python binding at this commit."""

    assert importlib.util.find_spec("swe_budget") is None


def test_offer_path_has_an_optional_budget_but_dispatch_does_not() -> None:
    """ActionOffer is the only in-tree budget consumer, and it is opt-in."""

    from swe.offers import ActionOfferHost

    offer_parameters = inspect.signature(ActionOfferHost.__init__).parameters
    assert "budget" in offer_parameters

    dispatch_parameters = inspect.signature(spawn_invocations).parameters
    assert not set(dispatch_parameters).intersection({"budget", "ledger"})
