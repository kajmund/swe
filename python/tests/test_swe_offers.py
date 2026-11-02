"""ActionOffer 0.x: identity, no side effects before approve, host decisions."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from swe.offers import (
    ActionOfferError,
    ActionOfferHost,
    MemoryOfferBudget,
    MemoryOfferStore,
    OfferProvenance,
    OfferRecord,
    OfferStatus,
    offer_delivery_key,
    offer_identity,
    propose_action,
)
from swe.run_store import InvocationRecord, MemoryRunStore, RunRecord, WorkflowResources
from swe.runtime_clock import FrozenClock


def _offer(**overrides: Any):
    payload = {
        "action_ref": "demo.publish",
        "handler": "demo.publish",
        "input": {"doc": "a", "rev": 1},
        "policy_version": "pol-1",
        "context_version": "ctx-1",
        "auth_scope": "tenant-a",
        "summary": "Publish document a",
        "provenance": OfferProvenance("host", "tester", 10, "pattern"),
    }
    payload.update(overrides)
    return propose_action(**payload)


def test_offer_id_is_stable_across_key_order_and_process() -> None:
    first = _offer(input={"rev": 1, "doc": "a"})
    second = propose_action(
        action_ref=first.action_ref,
        handler=first.handler,
        input={"doc": "a", "rev": 1},
        policy_version=first.policy_version,
        context_version=first.context_version,
        auth_scope=first.auth_scope,
        summary="different summary",
        expires_at_ms=99,
        provenance=OfferProvenance("other", "someone", 99),
    )
    assert first.offer_id == second.offer_id
    assert first.offer_id == offer_identity(
        action_ref=first.action_ref,
        handler=first.handler,
        input={"rev": 1, "doc": "a"},
        input_fingerprint=first.input_fingerprint,
        policy_version=first.policy_version,
        context_version=first.context_version,
        auth_scope=first.auth_scope,
    )
    assert first.to_dict()["offer_id"] == first.from_value(first.to_dict()).offer_id


@pytest.mark.parametrize(
    "change",
    [
        {"input": {"doc": "b", "rev": 1}},
        {"policy_version": "pol-2"},
        {"auth_scope": "tenant-b"},
        {"action_ref": "demo.retract"},
        {"handler": "demo.retract"},
        {"context_version": "ctx-2"},
    ],
)
def test_offer_identity_changes_with_behavior_inputs(change: dict[str, Any]) -> None:
    assert _offer().offer_id != _offer(**change).offer_id


def test_propose_is_pure_and_does_not_touch_handlers() -> None:
    called = False

    async def boom(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal called
        called = True
        raise AssertionError("propose must not dispatch")

    host = ActionOfferHost(
        {"demo.publish": boom},
        policy_version="pol-1",
        context_version="ctx-1",
    )
    offer = _offer()
    assert offer.schema_version == "action_offer.v0"
    assert called is False
    assert host.handlers["demo.publish"] is boom


async def test_stale_expired_revoked_and_unauthorized_fail_before_side_effect() -> None:
    clock = FrozenClock(now=50)
    calls: list[Mapping[str, Any]] = []

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(dict(payload))
        return {"ok": True}

    host = ActionOfferHost(
        {"demo.publish": work},
        store=MemoryOfferStore(),
        run_store=MemoryRunStore(),
        budget=MemoryOfferBudget(),
        clock=clock,
        policy_version="pol-1",
        context_version="ctx-1",
    )
    expired = _offer(expires_at_ms=50)
    with pytest.raises(ActionOfferError, match="expired") as expired_info:
        await host.approve(expired, auth_scope="tenant-a")
    assert expired_info.value.kind == "expired"

    stale_policy = _offer(policy_version="pol-old")
    with pytest.raises(ActionOfferError, match="policy_version") as stale_info:
        await host.approve(stale_policy, auth_scope="tenant-a")
    assert stale_info.value.kind == "stale"

    with pytest.raises(ActionOfferError, match="not authorized") as auth_info:
        await host.approve(_offer(), auth_scope="tenant-b")
    assert auth_info.value.kind == "unauthorized"

    rejected = _offer(input={"doc": "r", "rev": 1})
    await host.reject(rejected, auth_scope="tenant-a")
    with pytest.raises(ActionOfferError, match="rejected") as rejected_info:
        await host.approve(rejected, auth_scope="tenant-a")
    assert rejected_info.value.kind == "rejected"

    to_revoke = _offer(input={"doc": "z", "rev": 1})
    await host.store.save(OfferRecord(offer=to_revoke, status=OfferStatus.PROPOSED))
    record = await host.revoke(to_revoke.offer_id)
    assert record.status is OfferStatus.REVOKED
    with pytest.raises(ActionOfferError, match="revoked") as revoked_info:
        await host.approve(to_revoke, auth_scope="tenant-a")
    assert revoked_info.value.kind == "revoked"
    assert calls == []
    assert host.budget.settled == {}


async def test_reject_is_idempotent_and_never_dispatches() -> None:
    calls: list[str] = []

    async def work(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append("ran")
        return {"ok": True}

    host = ActionOfferHost(
        {"demo.publish": work},
        policy_version="pol-1",
        context_version="ctx-1",
    )
    offer = _offer()
    first = await host.reject(offer, auth_scope="tenant-a")
    second = await host.reject(offer, auth_scope="tenant-a")
    assert first.status is OfferStatus.REJECTED
    assert second.status is OfferStatus.REJECTED
    assert calls == []


async def test_approve_uses_runtime_budget_cancel_and_journal() -> None:
    pytest.importorskip("swe_native")
    clock = FrozenClock(now=1)
    executions: list[str] = []
    budget = MemoryOfferBudget()
    store = MemoryRunStore()
    cancel = asyncio.Event()

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        executions.append(str(payload["doc"]))
        return {"published": payload["doc"]}

    host = ActionOfferHost(
        {"demo.publish": work},
        store=MemoryOfferStore(),
        run_store=store,
        budget=budget,
        resources=WorkflowResources(cancel_event=cancel),
        clock=clock,
        policy_version="pol-1",
        context_version="ctx-1",
    )
    offer = _offer()
    first = await host.approve(offer, auth_scope="tenant-a")
    second = await host.approve(offer, auth_scope="tenant-a")
    assert first.run is not None
    assert first.run.status == "completed"
    assert first.reused is False
    assert second.reused is True
    assert executions == ["a"]
    assert len(budget.settled) == 1
    assert second.delivery_key == first.delivery_key
    assert store.runs[first.run.run_id].status == "completed"


async def test_cancelled_approve_releases_budget_and_does_not_settle() -> None:
    pytest.importorskip("swe_native")
    budget = MemoryOfferBudget()
    cancel = asyncio.Event()
    started = asyncio.Event()

    async def wait(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        started.set()
        await asyncio.Event().wait()
        return {"ok": True}

    host = ActionOfferHost(
        {"demo.publish": wait},
        store=MemoryOfferStore(),
        run_store=MemoryRunStore(),
        budget=budget,
        resources=WorkflowResources(cancel_event=cancel),
        policy_version="pol-1",
        context_version="ctx-1",
    )

    async def trip() -> None:
        await started.wait()
        cancel.set()

    offer = _offer()
    result, _ = await asyncio.gather(
        host.approve(offer, auth_scope="tenant-a"),
        trip(),
        return_exceptions=True,
    )
    assert isinstance(result, ActionOfferError)
    assert budget.settled == {}
    assert offer_delivery_key(offer.offer_id, "demo.publish") in budget.released
    with pytest.raises(ActionOfferError, match="cancelled"):
        await host.approve(offer, auth_scope="tenant-a")


async def test_resume_replays_persisted_offer_handler() -> None:
    pytest.importorskip("swe_native")
    store = _CrashOnceStore(crash_after=1)
    executions: list[str] = []

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        executions.append(str(payload["doc"]))
        return {"published": payload["doc"]}

    host = ActionOfferHost(
        {"demo.publish": work},
        store=MemoryOfferStore(),
        run_store=store,
        budget=MemoryOfferBudget(),
        policy_version="pol-1",
        context_version="ctx-1",
    )
    offer = _offer()
    with pytest.raises(RuntimeError, match="simulated crash"):
        await host.approve(offer, auth_scope="tenant-a")
    resumed = await host.approve(offer, auth_scope="tenant-a")
    assert resumed.run is not None
    assert resumed.run.status == "completed"
    assert executions == ["a"]


async def test_unbound_fingerprint_offer_cannot_execute() -> None:
    offer = propose_action(
        action_ref="demo.publish",
        handler="demo.publish",
        input_fingerprint="abcd",
        policy_version="pol-1",
        context_version="ctx-1",
        auth_scope="tenant-a",
        summary="fingerprint only",
        provenance=OfferProvenance("host", "tester", 1),
    )
    host = ActionOfferHost(
        {"demo.publish": _ok},
        policy_version="pol-1",
        context_version="ctx-1",
    )
    with pytest.raises(ActionOfferError, match="unbound"):
        await host.approve(offer, auth_scope="tenant-a")


async def _ok(_payload: Mapping[str, Any]) -> Mapping[str, bool]:
    return {"ok": True}


class _CrashOnceStore:
    def __init__(self, *, crash_after: int) -> None:
        self.crash_after = crash_after
        self.crashed = False
        self.inner = MemoryRunStore()

    async def record(
        self,
        run: RunRecord,
        *,
        events: Sequence[Mapping[str, Any]] = (),
        invocation: InvocationRecord | None = None,
    ) -> None:
        await self.inner.record(run, events=events, invocation=invocation)
        if (
            invocation is not None
            and not self.crashed
            and len(self.inner.invocations[run.run_id]) >= self.crash_after
        ):
            self.crashed = True
            raise RuntimeError("simulated crash")

    async def load_run(self, run_id: str) -> RunRecord | None:
        return await self.inner.load_run(run_id)

    async def replay_results(self, run_id: str) -> Mapping[str, InvocationRecord]:
        return await self.inner.replay_results(run_id)

    async def events(self, run_id: str) -> list[dict[str, Any]]:
        return await self.inner.events(run_id)
