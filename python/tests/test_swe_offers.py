"""ActionOffer 0.x: identity, no side effects before approve, host decisions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from swe.offers import (
    ActionOffer,
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
        input_bound=True,
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


async def test_concurrent_approves_dispatch_once_and_reuse_the_result() -> None:
    pytest.importorskip("swe_native")
    started = asyncio.Event()
    release = asyncio.Event()
    executions = 0

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal executions
        executions += 1
        started.set()
        await release.wait()
        return {"published": payload["doc"]}

    host = ActionOfferHost(
        {"demo.publish": work},
        store=MemoryOfferStore(),
        run_store=MemoryRunStore(),
        budget=MemoryOfferBudget(),
        policy_version="pol-1",
        context_version="ctx-1",
    )
    offer = _offer()
    tasks = [
        asyncio.create_task(host.approve(offer, auth_scope="tenant-a"))
        for _ in range(32)
    ]
    await started.wait()
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)
    owners = [item for item in results if not item.reused]
    waiters = [item for item in results if item.reused]
    assert executions == 1
    assert len(owners) == 1
    assert len(waiters) == 31
    run_ids = {item.run.run_id for item in results if item.run is not None}
    outputs = {json.dumps(item.run.output, sort_keys=True) for item in results if item.run is not None}
    assert run_ids == {owners[0].run.run_id if owners[0].run is not None else ""}
    assert outputs == {json.dumps(owners[0].run.output, sort_keys=True)}
    assert len(host.budget.settled) == 1


async def test_recovery_settles_budget_after_settle_crash() -> None:
    pytest.importorskip("swe_native")
    executions: list[str] = []

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        executions.append(str(payload["doc"]))
        return {"published": payload["doc"]}

    budget = _FailFirstSettle()
    host = ActionOfferHost(
        {"demo.publish": work},
        store=MemoryOfferStore(),
        run_store=MemoryRunStore(),
        budget=budget,
        policy_version="pol-1",
        context_version="ctx-1",
    )
    offer = _offer()
    with pytest.raises(RuntimeError, match="settle crashed"):
        await host.approve(offer, auth_scope="tenant-a")
    assert executions == ["a"]
    assert budget.settled == {}
    recovered = await host.approve(offer, auth_scope="tenant-a")
    assert recovered.reused is True
    assert recovered.run is not None
    assert recovered.run.status == "completed"
    assert executions == ["a"]
    key = offer_delivery_key(offer.offer_id, "demo.publish")
    assert budget.settled[key] == 1


async def test_expired_offer_still_settles_a_completed_run() -> None:
    pytest.importorskip("swe_native")
    clock = FrozenClock(now=10)
    executions: list[str] = []

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        executions.append(str(payload["doc"]))
        return {"published": payload["doc"]}

    budget = _FailFirstSettle()
    host = ActionOfferHost(
        {"demo.publish": work},
        store=MemoryOfferStore(),
        run_store=MemoryRunStore(),
        budget=budget,
        clock=clock,
        policy_version="pol-1",
        context_version="ctx-1",
    )
    offer = _offer(expires_at_ms=20)
    with pytest.raises(RuntimeError, match="settle crashed"):
        await host.approve(offer, auth_scope="tenant-a")
    assert executions == ["a"]
    assert budget.settled == {}
    clock.advance(20)
    recovered = await host.approve(offer, auth_scope="tenant-a")
    assert recovered.reused is True
    assert recovered.run is not None
    assert recovered.run.status == "completed"
    assert executions == ["a"]
    key = offer_delivery_key(offer.offer_id, "demo.publish")
    assert budget.settled[key] == 1
    with pytest.raises(ActionOfferError, match="not authorized") as auth_info:
        await host.approve(offer, auth_scope="tenant-b")
    assert auth_info.value.kind == "unauthorized"


async def test_empty_bound_input_dispatches() -> None:
    pytest.importorskip("swe_native")
    seen: list[Mapping[str, Any]] = []

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        seen.append(dict(payload))
        return {"ok": True}

    offer = propose_action(
        action_ref="demo.publish",
        handler="demo.publish",
        input={},
        policy_version="pol-1",
        context_version="ctx-1",
        auth_scope="tenant-a",
        summary="empty bound input",
        provenance=OfferProvenance("host", "tester", 1),
    )
    assert offer.input_bound is True
    host = ActionOfferHost(
        {"demo.publish": work},
        policy_version="pol-1",
        context_version="ctx-1",
    )
    result = await host.approve(offer, auth_scope="tenant-a")
    assert result.run is not None
    assert result.run.status == "completed"
    assert seen == [{}]


def test_empty_bound_input_is_not_fingerprint_only() -> None:
    bound = propose_action(
        action_ref="demo.publish",
        handler="demo.publish",
        input={},
        policy_version="pol-1",
        context_version="ctx-1",
        auth_scope="tenant-a",
        summary="bound empty",
        provenance=OfferProvenance("host", "tester", 1),
    )
    unbound = propose_action(
        action_ref="demo.publish",
        handler="demo.publish",
        input_fingerprint=bound.input_fingerprint,
        policy_version="pol-1",
        context_version="ctx-1",
        auth_scope="tenant-a",
        summary="fingerprint only",
        provenance=OfferProvenance("host", "tester", 1),
    )
    assert bound.input_bound is True
    assert unbound.input_bound is False
    assert bound.offer_id != unbound.offer_id


async def test_input_and_run_keys_keep_their_bound_values() -> None:
    pytest.importorskip("swe_native")
    seen: list[Mapping[str, Any]] = []

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        seen.append({"input": payload["input"], "run": payload["run"]})
        return {"ok": True}

    host = ActionOfferHost(
        {"demo.publish": work},
        policy_version="pol-1",
        context_version="ctx-1",
    )
    result = await host.approve(
        _offer(input={"input": "document text", "run": "manual"}),
        auth_scope="tenant-a",
    )
    assert result.run is not None
    assert result.run.status == "completed"
    assert seen == [{"input": "document text", "run": "manual"}]


async def test_claim_wait_rechecks_freshness_before_new_dispatch() -> None:
    pytest.importorskip("swe_native")
    clock = FrozenClock(now=0)
    executions: list[str] = []

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        executions.append(str(payload["doc"]))
        return {"published": payload["doc"]}

    store = MemoryOfferStore()
    host = ActionOfferHost(
        {"demo.publish": work},
        store=store,
        run_store=MemoryRunStore(),
        clock=clock,
        policy_version="pol-1",
        context_version="ctx-1",
    )
    offer = _offer(expires_at_ms=50)
    await store.save(
        OfferRecord(
            offer=offer,
            status=OfferStatus.APPROVING,
            delivery_key=offer_delivery_key(offer.offer_id, offer.action_ref),
            claimed_until_ms=100,
        )
    )
    original_wait = store.wait

    async def wait_and_expire(*args: Any, **kwargs: Any):
        clock.advance(100)
        return await original_wait(*args, **kwargs)

    store.wait = wait_and_expire  # type: ignore[method-assign]
    with pytest.raises(ActionOfferError, match="expired") as info:
        await host.approve(offer, auth_scope="tenant-a")
    assert info.value.kind == "expired"
    assert executions == []


async def test_constant_bound_values_are_not_shared_across_offers() -> None:
    pytest.importorskip("swe_native")
    seen: list[Any] = []

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        seen.append(payload["document-id"])
        return {"id": payload["document-id"]}

    host = ActionOfferHost(
        {"demo.publish": work},
        policy_version="pol-1",
        context_version="ctx-1",
    )
    first = await host.approve(_offer(input={"document-id": 1}), auth_scope="tenant-a")
    second = await host.approve(_offer(input={"document-id": 2}), auth_scope="tenant-a")
    assert first.run is not None and first.run.status == "completed"
    assert second.run is not None and second.run.status == "completed"
    assert seen == [1, 2]
    assert first.run.output != second.run.output


async def test_nested_constant_bindings_compile_and_dispatch() -> None:
    pytest.importorskip("swe_native")
    seen: list[Any] = []

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        seen.append(json.loads(json.dumps(payload["document-id"])))
        return {"ok": True}

    host = ActionOfferHost(
        {"demo.publish": work},
        policy_version="pol-1",
        context_version="ctx-1",
    )
    object_result = await host.approve(
        _offer(input={"document-id": {"id": 1}}),
        auth_scope="tenant-a",
    )
    list_result = await host.approve(
        _offer(input={"document-id": [1]}),
        auth_scope="tenant-a",
    )
    assert object_result.run is not None and object_result.run.status == "completed"
    assert list_result.run is not None and list_result.run.status == "completed"
    assert seen == [{"id": 1}, [1]]


async def test_const_definitions_are_not_retained_and_identifier_cache_is_bounded() -> None:
    pytest.importorskip("swe_native")

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"ok": payload.get("document-id", payload.get("doc"))}

    host = ActionOfferHost(
        {"demo.publish": work},
        policy_version="pol-1",
        context_version="ctx-1",
        definition_cache_max=2,
    )
    for index in range(8):
        result = await host.approve(
            _offer(input={"document-id": index}),
            auth_scope="tenant-a",
        )
        assert result.run is not None
        assert result.run.status == "completed"
    assert host._definitions == {}

    first = await host.approve(_offer(input={"doc": "a"}), auth_scope="tenant-a")
    first_key = next(iter(host._definitions))
    second = await host.approve(_offer(input={"rev": 1}), auth_scope="tenant-a")
    await host.approve(_offer(input={"doc": "b"}), auth_scope="tenant-a")
    third = await host.approve(_offer(input={"ok": True}), auth_scope="tenant-a")
    assert first.run is not None and second.run is not None and third.run is not None
    assert len(host._definitions) == 2
    assert first_key in host._definitions
    assert any(key[1] != first_key[1] for key in host._definitions)


def test_nested_offer_and_record_are_json_serializable() -> None:
    offer = _offer(
        input={
            "document": {"id": 1, "tags": ["a", {"k": "v"}]},
            "items": [{"n": 1}, {"n": 2}],
        }
    )
    payload = json.loads(json.dumps(offer.to_dict()))
    restored = ActionOffer.from_value(payload)
    assert restored.offer_id == offer.offer_id
    assert restored.input_bound is True
    assert payload["input"]["document"]["tags"][1]["k"] == "v"
    assert payload["input"]["items"][1]["n"] == 2
    record = OfferRecord(
        offer=offer,
        status=OfferStatus.APPROVED,
        result={"out": {"nested": [{"ok": True}]}},
    )
    record_payload = json.loads(json.dumps(record.to_dict()))
    assert record_payload["result"]["out"]["nested"][0]["ok"] is True
    assert record_payload["offer"]["input"]["document"]["id"] == 1


async def test_concurrent_approve_reject_revoke_have_a_single_decision() -> None:
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
    host = ActionOfferHost(
        {"demo.publish": work},
        store=store,
        run_store=MemoryRunStore(),
        budget=MemoryOfferBudget(),
        policy_version="pol-1",
        context_version="ctx-1",
    )
    offer = _offer()
    await store.save(OfferRecord(offer=offer, status=OfferStatus.PROPOSED))

    async def release_if_started() -> None:
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
        except TimeoutError:
            return
        finally:
            release.set()

    approve, rejected, revoked, _ = await asyncio.gather(
        host.approve(offer, auth_scope="tenant-a"),
        host.reject(offer, auth_scope="tenant-a"),
        host.revoke(offer.offer_id),
        release_if_started(),
        return_exceptions=True,
    )
    record = await store.load(offer.offer_id)
    assert record is not None
    match record.status:
        case OfferStatus.APPROVED:
            assert not isinstance(approve, BaseException)
            assert executions == 1
            assert isinstance(rejected, ActionOfferError)
            assert isinstance(revoked, ActionOfferError)
        case OfferStatus.REJECTED:
            assert executions == 0
            assert isinstance(approve, ActionOfferError)
            assert approve.kind == "rejected"
            assert not isinstance(rejected, BaseException)
        case OfferStatus.REVOKED:
            assert executions == 0
            assert isinstance(approve, ActionOfferError)
            assert approve.kind in {"revoked", "rejected"}
        case _ as other:
            raise AssertionError(f"non-terminal offer status {other}")


async def test_in_flight_approve_blocks_reject_and_still_finishes_once() -> None:
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

    host = ActionOfferHost(
        {"demo.publish": work},
        store=MemoryOfferStore(),
        run_store=MemoryRunStore(),
        budget=MemoryOfferBudget(),
        policy_version="pol-1",
        context_version="ctx-1",
    )
    offer = _offer()
    approve_task = asyncio.create_task(host.approve(offer, auth_scope="tenant-a"))
    await started.wait()
    with pytest.raises(ActionOfferError, match="already being approved") as info:
        await host.reject(offer, auth_scope="tenant-a")
    assert info.value.kind == "conflict"
    with pytest.raises(ActionOfferError, match="already being approved"):
        await host.revoke(offer.offer_id)
    release.set()
    accepted = await approve_task
    assert accepted.run is not None
    assert accepted.run.status == "completed"
    assert executions == 1


async def test_two_hosts_serialize_approve_and_do_not_rerun_after_crash() -> None:
    pytest.importorskip("swe_native")
    executions = 0

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal executions
        executions += 1
        return {"published": payload["doc"]}

    store = MemoryOfferStore()
    run_store = _CrashOnceStore(crash_after=1)
    budget = MemoryOfferBudget()
    first = ActionOfferHost(
        {"demo.publish": work},
        store=store,
        run_store=run_store,
        budget=budget,
        policy_version="pol-1",
        context_version="ctx-1",
    )
    second = ActionOfferHost(
        {"demo.publish": work},
        store=store,
        run_store=run_store,
        budget=budget,
        policy_version="pol-1",
        context_version="ctx-1",
    )
    offer = _offer()
    with pytest.raises(RuntimeError, match="simulated crash"):
        await first.approve(offer, auth_scope="tenant-a")
    assert executions == 1
    recovered = await second.approve(offer, auth_scope="tenant-a")
    assert recovered.run is not None
    assert recovered.run.status == "completed"
    assert executions == 1
    assert len(budget.settled) == 1


async def test_two_hosts_reject_then_approve_never_starts() -> None:
    pytest.importorskip("swe_native")
    executions = 0

    async def work(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal executions
        executions += 1
        return {"ok": True}

    store = MemoryOfferStore()
    first = ActionOfferHost(
        {"demo.publish": work},
        store=store,
        run_store=MemoryRunStore(),
        policy_version="pol-1",
        context_version="ctx-1",
    )
    second = ActionOfferHost(
        {"demo.publish": work},
        store=store,
        run_store=MemoryRunStore(),
        policy_version="pol-1",
        context_version="ctx-1",
    )
    offer = _offer()
    rejected = await first.reject(offer, auth_scope="tenant-a")
    assert rejected.status is OfferStatus.REJECTED
    with pytest.raises(ActionOfferError, match="rejected"):
        await second.approve(offer, auth_scope="tenant-a")
    with pytest.raises(ActionOfferError, match="rejected"):
        await first.approve(offer, auth_scope="tenant-a")
    assert executions == 0


async def test_two_hosts_concurrent_approve_dispatch_once() -> None:
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
    run_store = MemoryRunStore()
    budget = MemoryOfferBudget()
    hosts = [
        ActionOfferHost(
            {"demo.publish": work},
            store=store,
            run_store=run_store,
            budget=budget,
            policy_version="pol-1",
            context_version="ctx-1",
        )
        for _ in range(2)
    ]
    offer = _offer()
    tasks = [
        asyncio.create_task(hosts[index].approve(offer, auth_scope="tenant-a"))
        for index in range(2)
    ]
    await started.wait()
    release.set()
    results = await asyncio.gather(*tasks)
    assert executions == 1
    assert {item.run.run_id for item in results if item.run is not None} == {
        results[0].run.run_id if results[0].run is not None else ""
    }
    assert len(budget.settled) == 1


async def test_two_hosts_retry_after_reserve_crash_starts_once() -> None:
    pytest.importorskip("swe_native")
    executions = 0

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal executions
        executions += 1
        return {"published": payload["doc"]}

    store = MemoryOfferStore()
    budget = _FailFirstReserve()
    first = ActionOfferHost(
        {"demo.publish": work},
        store=store,
        run_store=MemoryRunStore(),
        budget=budget,
        policy_version="pol-1",
        context_version="ctx-1",
    )
    second = ActionOfferHost(
        {"demo.publish": work},
        store=store,
        run_store=MemoryRunStore(),
        budget=budget,
        policy_version="pol-1",
        context_version="ctx-1",
    )
    offer = _offer()
    with pytest.raises(RuntimeError, match="reserve crashed"):
        await first.approve(offer, auth_scope="tenant-a")
    assert executions == 0
    record = await store.load(offer.offer_id)
    assert record is None or record.status is OfferStatus.PROPOSED
    accepted = await second.approve(offer, auth_scope="tenant-a")
    assert accepted.run is not None
    assert accepted.run.status == "completed"
    assert executions == 1


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
    assert offer.input_bound is False


async def _ok(_payload: Mapping[str, Any]) -> Mapping[str, bool]:
    return {"ok": True}


class _FailFirstReserve(MemoryOfferBudget):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def reserve(self, *, account: str, amount_milli: int, key: str) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("reserve crashed")
        await super().reserve(account=account, amount_milli=amount_milli, key=key)


class _FailFirstSettle(MemoryOfferBudget):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def settle(self, *, key: str, amount_milli: int) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("settle crashed")
        await super().settle(key=key, amount_milli=amount_milli)


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
