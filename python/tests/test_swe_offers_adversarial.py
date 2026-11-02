"""Adversarial ActionOffer coverage: claims, bindings, caches, crash windows."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from swe.canonicalize import sha256_hex
from swe.offers import (
    ActionOfferError,
    ActionOfferHost,
    MemoryOfferBudget,
    MemoryOfferStore,
    OfferRecord,
    OfferStatus,
    offer_delivery_key,
)
from swe.run_store import InvocationRecord, MemoryRunStore, RunRecord, WorkflowResources
from swe.runtime_clock import FrozenClock

from tests.test_swe_offers import _CrashOnceStore, _offer


async def _ok(_payload: Mapping[str, Any]) -> Mapping[str, bool]:
    return {"ok": True}


def _host(
    work,
    *,
    store: MemoryOfferStore | None = None,
    run_store: MemoryRunStore | None = None,
    budget: MemoryOfferBudget | None = None,
    clock: FrozenClock | None = None,
    **kwargs: Any,
) -> ActionOfferHost:
    return ActionOfferHost(
        {"demo.publish": work},
        store=store or MemoryOfferStore(),
        run_store=run_store if run_store is not None else MemoryRunStore(),
        budget=budget,
        clock=clock,
        policy_version="pol-1",
        context_version="ctx-1",
        **kwargs,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"doc": "a"},
        {"input": "document text", "run": "manual"},
        {"const": 1, "true": False},
        {"document-id": 1},
        {"Document": "X"},
        {"foo-bar": [1, {"n": 2}]},
        {"foo.bar": {"id": 1}},
        {"": 0},
        {"1abc": None},
        {"Å": {"nested": [{"ok": True}]}},
        {"doc": {"id": 1, "tags": ["a", {"k": "v"}]}},
    ],
)
def test_offer_input_key_variants_round_trip(payload: dict[str, Any]) -> None:
    first = _offer(input=payload)
    second = _offer(input=json.loads(json.dumps(payload)))
    assert first.offer_id == second.offer_id
    assert first.input_bound is True
    restored = json.loads(json.dumps(first.to_dict()))
    assert restored["input"] == json.loads(json.dumps(payload))


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ({"document-id": 1}, {"document-id": 2}),
        ({"document-id": {"id": 1}}, {"document-id": {"id": 2}}),
        ({"document-id": [1]}, {"document-id": [2]}),
        ({"foo-bar": True}, {"foo-bar": False}),
        ({"doc": "a"}, {"doc": "b"}),
        ({"input": 1, "run": 2}, {"input": 2, "run": 1}),
    ],
)
async def test_same_shape_different_values_stay_isolated(
    left: dict[str, Any],
    right: dict[str, Any],
) -> None:
    pytest.importorskip("swe_native")
    seen: list[Any] = []

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        seen.append(json.loads(json.dumps(dict(payload))))
        return {"ok": True}

    host = _host(work)
    first = await host.approve(_offer(input=left), auth_scope="tenant-a")
    second = await host.approve(_offer(input=right), auth_scope="tenant-a")
    assert first.run is not None and first.run.status == "completed"
    assert second.run is not None and second.run.status == "completed"
    assert seen == [left, right]


def test_const_alias_collision_is_rejected_at_propose() -> None:
    alias = f"c_{sha256_hex({'offer_const_key': 'document-id'})}"
    with pytest.raises(ActionOfferError, match="collides") as info:
        _offer(input={"document-id": 1, alias: 2})
    assert info.value.kind == "invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_seconds", 0),
        ("timeout_seconds", True),
        ("concurrency", 0),
        ("concurrency", -1),
        ("claim_lease_ms", 0),
        ("claim_lease_ms", False),
        ("definition_cache_max", 0),
        ("tariff_milli", -1),
        ("tariff_milli", True),
    ],
)
def test_host_rejects_invalid_bounds(field: str, value: object) -> None:
    with pytest.raises(ActionOfferError, match="integer") as info:
        _host(_ok, **{field: value})
    assert info.value.kind == "invalid"


async def test_expired_claim_takeover_is_exclusive_across_hosts() -> None:
    pytest.importorskip("swe_native")
    executions = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal executions
        executions += 1
        started.set()
        await release.wait()
        return {"published": payload["doc"]}

    store = MemoryOfferStore()
    clock = FrozenClock(now=100)
    offer = _offer()
    await store.save(
        OfferRecord(
            offer=offer,
            status=OfferStatus.APPROVING,
            delivery_key=offer_delivery_key(offer.offer_id, offer.action_ref),
            claimed_until_ms=10,
        )
    )
    hosts = [
        _host(work, store=store, clock=clock, budget=MemoryOfferBudget(), claim_lease_ms=1_000)
        for _ in range(2)
    ]
    tasks = [
        asyncio.create_task(host.approve(offer, auth_scope="tenant-a")) for host in hosts
    ]
    await started.wait()
    release.set()
    results = await asyncio.gather(*tasks)
    assert executions == 1
    assert {item.run.run_id for item in results if item.run is not None} == {
        results[0].run.run_id if results[0].run is not None else ""
    }
    assert sum(len(host.budget.settled) for host in hosts if host.budget is not None) == 1


async def test_lease_takeover_without_cas_token_does_not_double_dispatch() -> None:
    pytest.importorskip("swe_native")
    executions = 0

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal executions
        executions += 1
        return {"published": payload["doc"]}

    store = MemoryOfferStore()
    clock = FrozenClock(now=50)
    offer = _offer()
    await store.save(
        OfferRecord(
            offer=offer,
            status=OfferStatus.APPROVING,
            delivery_key=offer_delivery_key(offer.offer_id, offer.action_ref),
            claimed_until_ms=None,
        )
    )
    first = _host(work, store=store, clock=clock, budget=MemoryOfferBudget())
    second = _host(work, store=store, clock=clock, budget=first.budget)
    results = await asyncio.gather(
        first.approve(offer, auth_scope="tenant-a"),
        second.approve(offer, auth_scope="tenant-a"),
    )
    assert executions == 1
    assert all(item.run is not None and item.run.status == "completed" for item in results)
    assert first.budget is not None
    assert len(first.budget.settled) == 1


async def test_valid_lease_with_stored_effect_does_not_dispatch() -> None:
    pytest.importorskip("swe_native")
    executions = 0

    async def work(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal executions
        executions += 1
        return {"ok": True}

    store = MemoryOfferStore()
    clock = FrozenClock(now=0)
    offer = _offer()
    delivery = offer_delivery_key(offer.offer_id, offer.action_ref)
    store.effects[delivery] = {"ok": True}
    await store.save(
        OfferRecord(
            offer=offer,
            status=OfferStatus.APPROVING,
            delivery_key=delivery,
            claimed_until_ms=50,
        )
    )
    host = _host(work, store=store, clock=clock)
    with pytest.raises(ActionOfferError, match="already being approved") as info:
        await host.approve(offer, auth_scope="tenant-a")
    assert info.value.kind == "conflict"
    assert executions == 0


async def test_expired_offer_reconciles_stored_effect_without_new_handler() -> None:
    pytest.importorskip("swe_native")
    executions = 0

    async def work(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal executions
        executions += 1
        return {"fresh": True}

    store = MemoryOfferStore()
    clock = FrozenClock(now=100)
    offer = _offer(expires_at_ms=50)
    delivery = offer_delivery_key(offer.offer_id, offer.action_ref)
    store.effects[delivery] = {"recovered": True}
    host = _host(work, store=store, clock=clock, budget=MemoryOfferBudget())
    accepted = await host.approve(offer, auth_scope="tenant-a")
    assert accepted.run is not None
    assert accepted.run.status == "completed"
    assert accepted.run.output == {"execute": {"result": {"recovered": True}}}
    assert executions == 0
    assert host.budget is not None
    assert len(host.budget.settled) == 1


async def test_expired_offer_without_work_does_not_dispatch() -> None:
    pytest.importorskip("swe_native")
    executions = 0

    async def work(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal executions
        executions += 1
        return {"ok": True}

    host = _host(work, clock=FrozenClock(now=100))
    with pytest.raises(ActionOfferError, match="expired") as info:
        await host.approve(_offer(expires_at_ms=50), auth_scope="tenant-a")
    assert info.value.kind == "expired"
    assert executions == 0


async def test_stale_policy_does_not_dispatch_and_auth_still_applies() -> None:
    executions = 0

    async def work(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal executions
        executions += 1
        return {"ok": True}

    host = _host(work)
    with pytest.raises(ActionOfferError, match="policy_version") as stale:
        await host.approve(_offer(policy_version="pol-old"), auth_scope="tenant-a")
    assert stale.value.kind == "stale"
    with pytest.raises(ActionOfferError, match="not authorized") as auth:
        await host.approve(_offer(), auth_scope="tenant-b")
    assert auth.value.kind == "unauthorized"
    assert executions == 0


async def test_failed_run_is_not_redispatched() -> None:
    pytest.importorskip("swe_native")
    executions = 0
    cancel = asyncio.Event()
    started = asyncio.Event()

    async def wait(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal executions
        executions += 1
        started.set()
        await asyncio.Event().wait()
        return {"ok": True}

    host = _host(
        wait,
        budget=MemoryOfferBudget(),
        run_store=MemoryRunStore(),
    )
    host.resources = WorkflowResources(cancel_event=cancel)
    offer = _offer()

    async def trip() -> None:
        await started.wait()
        cancel.set()

    result, _ = await asyncio.gather(
        host.approve(offer, auth_scope="tenant-a"),
        trip(),
        return_exceptions=True,
    )
    assert isinstance(result, ActionOfferError)
    with pytest.raises(ActionOfferError, match="cancelled"):
        await host.approve(offer, auth_scope="tenant-a")
    assert executions == 1
    assert host.budget is not None
    assert host.budget.settled == {}


async def test_reject_and_revoke_lose_to_completed_takeover() -> None:
    pytest.importorskip("swe_native")
    store = MemoryOfferStore()
    clock = FrozenClock(now=20)
    offer = _offer()
    await store.save(
        OfferRecord(
            offer=offer,
            status=OfferStatus.APPROVING,
            delivery_key=offer_delivery_key(offer.offer_id, offer.action_ref),
            claimed_until_ms=1,
        )
    )
    host = _host(_ok, store=store, clock=clock, budget=MemoryOfferBudget())
    accepted = await host.approve(offer, auth_scope="tenant-a")
    assert accepted.run is not None
    assert accepted.run.status == "completed"
    with pytest.raises(ActionOfferError, match="cannot be rejected"):
        await host.reject(offer, auth_scope="tenant-a")
    with pytest.raises(ActionOfferError, match="cannot be revoked"):
        await host.revoke(offer.offer_id)


async def test_expired_running_run_without_handler_does_not_dispatch() -> None:
    pytest.importorskip("swe_native")
    executions = 0

    async def work(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal executions
        executions += 1
        return {"ok": True}

    clock = FrozenClock(now=10)
    host = _host(work, run_store=_CrashOnStartStore(), clock=clock)
    offer = _offer(expires_at_ms=50)
    with pytest.raises(RuntimeError, match="crash before handler"):
        await host.approve(offer, auth_scope="tenant-a")
    assert executions == 0
    clock.advance(50)
    with pytest.raises(ActionOfferError, match="expired") as info:
        await host.approve(offer, auth_scope="tenant-a")
    assert info.value.kind == "expired"
    assert executions == 0


async def test_expired_running_run_with_invocation_replays() -> None:
    pytest.importorskip("swe_native")
    executions = 0

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal executions
        executions += 1
        return {"published": payload["doc"]}

    clock = FrozenClock(now=10)
    host = _host(
        work,
        run_store=_CrashOnceStore(crash_after=1),
        clock=clock,
        budget=MemoryOfferBudget(),
    )
    offer = _offer(expires_at_ms=50)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await host.approve(offer, auth_scope="tenant-a")
    assert executions == 1
    clock.advance(50)
    recovered = await host.approve(offer, auth_scope="tenant-a")
    assert recovered.run is not None
    assert recovered.run.status == "completed"
    assert executions == 1
    assert host.budget is not None
    assert len(host.budget.settled) == 1


class _CrashOnStartStore:
    def __init__(self) -> None:
        self.inner = MemoryRunStore()
        self.crashed = False

    async def record(
        self,
        run: RunRecord,
        *,
        events: Sequence[Mapping[str, Any]] = (),
        invocation: InvocationRecord | None = None,
    ) -> None:
        await self.inner.record(run, events=events, invocation=invocation)
        if invocation is None and run.status == "running" and not self.crashed:
            self.crashed = True
            raise RuntimeError("crash before handler")

    async def load_run(self, run_id: str) -> RunRecord | None:
        return await self.inner.load_run(run_id)

    async def replay_results(self, run_id: str) -> Mapping[str, InvocationRecord]:
        return await self.inner.replay_results(run_id)

    async def events(self, run_id: str) -> list[dict[str, Any]]:
        return await self.inner.events(run_id)
