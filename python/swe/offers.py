"""ActionOffer 0.x: typed, immutable proposals for existing SWE execution.

An offer is a serializable suggestion, never a side effect and never a second
workflow engine. `propose_action` is a pure function. Approve, reject, and
revoke are explicit host operations. A accepted offer is revalidated and then
dispatched through `WorkflowRunner`, `RunStore`, cancellation, and an optional
idempotent budget ledger.

This module is outside the frozen SWD, journal, event, provider, and Rust wire
contracts. It has no Research, Socialism, legal, or product-AINA dependency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import re
import sys
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, assert_never

from swe.cache import benchmark_exact_cache
from swe.canonicalize import sha256_hex
from swe.definition import CompiledDefinition, compile_definition
from swe.freeze import freeze_json
from swe.run_store import MemoryRunStore, RunStore, WorkflowResources
from swe.runtime import RunResult, WorkflowRunner
from swe.runtime_clock import FrozenClock, WorkflowClock
from swe.runtime_dispatch import Handler
from swe.runtime_errors import SweRuntimeError
from swe.runtime_thaw import thaw_json
from swe.runtime_validate import is_json_value
from swe.schema import HANDLER_NAME_PATTERN, SWD_VERSION

ACTION_OFFER_SCHEMA_VERSION = "action_offer.v0"
OFFER_WORKFLOW_NAME = "action_offer"
HANDLER_NAME_RE = re.compile(HANDLER_NAME_PATTERN)
BINDING_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_TARIFF_MILLI = 1
DEFAULT_DEFINITION_CACHE = 256


class OfferStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVING = "approving"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"


CLAIMABLE_OFFER_STATUSES = frozenset({None, OfferStatus.PROPOSED})
REJECTABLE_OFFER_STATUSES = frozenset({None, OfferStatus.PROPOSED})
REVOKABLE_OFFER_STATUSES = frozenset(
    {None, OfferStatus.PROPOSED, OfferStatus.REJECTED}
)


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionOfferError(SweRuntimeError):
    """Typed refusal for an ActionOffer host operation."""


@dataclass(frozen=True, slots=True)
class OfferProvenance:
    source: str
    created_by: str
    created_at_ms: int
    pattern: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "created_by": self.created_by,
            "created_at_ms": self.created_at_ms,
            "pattern": self.pattern,
        }

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | OfferProvenance) -> OfferProvenance:
        if isinstance(value, cls):
            return value
        pattern = value.get("pattern")
        if pattern is not None and not isinstance(pattern, str):
            raise ActionOfferError("provenance.pattern must be a string", kind="invalid")
        return cls(
            source=_require_text(value.get("source"), "provenance.source"),
            created_by=_require_text(value.get("created_by"), "provenance.created_by"),
            created_at_ms=_require_int(value.get("created_at_ms"), "provenance.created_at_ms"),
            pattern=pattern,
        )


@dataclass(frozen=True, slots=True)
class ActionOffer:
    """Immutable, serializable proposal for one SWE handler invocation."""

    schema_version: str
    offer_id: str
    action_ref: str
    handler: str
    input: Mapping[str, Any]
    input_bound: bool
    input_fingerprint: str
    policy_version: str
    context_version: str
    auth_scope: str
    summary: str
    risk_level: RiskLevel
    risk_reasons: tuple[str, ...]
    expires_at_ms: int | None
    revalidate_after_ms: int | None
    provenance: OfferProvenance

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "offer_id": self.offer_id,
            "action_ref": self.action_ref,
            "handler": self.handler,
            "input": thaw_json(self.input) if self.input_bound else None,
            "input_bound": self.input_bound,
            "input_fingerprint": self.input_fingerprint,
            "policy_version": self.policy_version,
            "context_version": self.context_version,
            "auth_scope": self.auth_scope,
            "summary": self.summary,
            "risk": {
                "level": str(self.risk_level),
                "reasons": list(self.risk_reasons),
            },
            "approval": {"required": True},
            "expires_at_ms": self.expires_at_ms,
            "revalidate_after_ms": self.revalidate_after_ms,
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | ActionOffer) -> ActionOffer:
        if isinstance(value, cls):
            return value
        offer = propose_action(
            action_ref=_require_text(value.get("action_ref"), "action_ref"),
            handler=_require_text(value.get("handler"), "handler"),
            input=_input_from_value(value),
            input_fingerprint=(
                value.get("input_fingerprint")
                if isinstance(value.get("input_fingerprint"), str)
                else None
            ),
            policy_version=_require_text(value.get("policy_version"), "policy_version"),
            context_version=_require_text(value.get("context_version"), "context_version"),
            auth_scope=_require_text(value.get("auth_scope"), "auth_scope"),
            summary=_require_text(value.get("summary"), "summary"),
            risk_level=_parse_risk_level(
                value.get("risk", {}).get("level")
                if isinstance(value.get("risk"), Mapping)
                else value.get("risk_level")
            ),
            risk_reasons=_parse_reasons(
                value.get("risk", {}).get("reasons")
                if isinstance(value.get("risk"), Mapping)
                else value.get("risk_reasons")
            ),
            expires_at_ms=_optional_int(value.get("expires_at_ms"), "expires_at_ms"),
            revalidate_after_ms=_optional_int(
                value.get("revalidate_after_ms"), "revalidate_after_ms"
            ),
            provenance=_require_provenance(value.get("provenance")),
        )
        recorded = value.get("offer_id")
        if isinstance(recorded, str) and recorded != offer.offer_id:
            raise ActionOfferError("offer_id does not match canonical identity", kind="invalid")
        return offer


@dataclass(frozen=True, slots=True)
class OfferRecord:
    offer: ActionOffer
    status: OfferStatus
    run_id: str | None = None
    delivery_key: str | None = None
    result: Mapping[str, Any] | None = None
    decided_at_ms: int | None = None
    claimed_until_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "offer": self.offer.to_dict(),
            "status": str(self.status),
            "run_id": self.run_id,
            "delivery_key": self.delivery_key,
            "result": None if self.result is None else thaw_json(self.result),
            "decided_at_ms": self.decided_at_ms,
            "claimed_until_ms": self.claimed_until_ms,
        }


@dataclass(frozen=True, slots=True)
class OfferAcceptance:
    offer: ActionOffer
    status: OfferStatus
    run: RunResult | None
    delivery_key: str
    reused: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer.offer_id,
            "status": str(self.status),
            "delivery_key": self.delivery_key,
            "reused": self.reused,
            "run_id": None if self.run is None else self.run.run_id,
            "run_status": None if self.run is None else self.run.status,
            "output": None if self.run is None else self.run.output,
        }


class OfferStore(Protocol):
    """Host-owned ledger of offer decisions. Create/surface does not use it.

    Decision writes must go through `transition` so approve, reject, and revoke
    share one atomic compare-and-set. `run_effect` is the persistent
    exactly-once boundary for handler side effects.
    """

    async def load(self, offer_id: str) -> OfferRecord | None: ...

    async def save(self, record: OfferRecord) -> None: ...

    async def transition(
        self,
        offer_id: str,
        *,
        expected: Collection[OfferStatus | None],
        record: OfferRecord,
        expected_claimed_until_ms: Collection[int | None] | None = None,
    ) -> OfferRecord | None: ...

    async def wait(
        self,
        offer_id: str,
        *,
        not_in: Collection[OfferStatus],
        timeout_s: float,
    ) -> OfferRecord | None: ...

    async def load_effect(self, key: str) -> Mapping[str, Any] | None: ...

    async def run_effect(
        self,
        key: str,
        compute: Callable[[], Awaitable[Mapping[str, Any]]],
    ) -> tuple[Mapping[str, Any], bool]: ...


class OfferBudget(Protocol):
    """Idempotent reserve/settle/release adapter around the existing ledger."""

    async def reserve(self, *, account: str, amount_milli: int, key: str) -> None: ...

    async def settle(self, *, key: str, amount_milli: int) -> None: ...

    async def release(self, *, key: str) -> None: ...


class MemoryOfferStore:
    """In-process offer ledger used by tests and the reference host.

    One condition lock serializes load/save/transition/effect claims, so two
    `ActionOfferHost` instances that share this store see the same atomic
    decision order. Durable adapters must provide the same exclusivity.
    """

    def __init__(self) -> None:
        self.records: dict[str, OfferRecord] = {}
        self.effects: dict[str, Mapping[str, Any]] = {}
        self._condition = asyncio.Condition()
        self._effect_flights: dict[str, asyncio.Future[Mapping[str, Any]]] = {}

    async def load(self, offer_id: str) -> OfferRecord | None:
        async with self._condition:
            return self.records.get(offer_id)

    async def save(self, record: OfferRecord) -> None:
        async with self._condition:
            self.records[record.offer.offer_id] = record
            self._condition.notify_all()

    async def transition(
        self,
        offer_id: str,
        *,
        expected: Collection[OfferStatus | None],
        record: OfferRecord,
        expected_claimed_until_ms: Collection[int | None] | None = None,
    ) -> OfferRecord | None:
        allowed = frozenset(expected)
        allowed_leases = (
            None if expected_claimed_until_ms is None else frozenset(expected_claimed_until_ms)
        )
        async with self._condition:
            current = self.records.get(offer_id)
            status = None if current is None else current.status
            if status not in allowed:
                return None
            if allowed_leases is not None:
                current_lease = None if current is None else current.claimed_until_ms
                if current_lease not in allowed_leases:
                    return None
            self.records[offer_id] = record
            self._condition.notify_all()
            return record

    async def wait(
        self,
        offer_id: str,
        *,
        not_in: Collection[OfferStatus],
        timeout_s: float,
    ) -> OfferRecord | None:
        blocked = frozenset(not_in)
        timeout = max(0.0, timeout_s)

        def _ready() -> bool:
            current = self.records.get(offer_id)
            return current is None or current.status not in blocked

        async with self._condition:
            if _ready():
                return self.records.get(offer_id)
            try:
                await asyncio.wait_for(self._condition.wait_for(_ready), timeout=timeout)
            except TimeoutError:
                pass
            return self.records.get(offer_id)

    async def load_effect(self, key: str) -> Mapping[str, Any] | None:
        async with self._condition:
            stored = self.effects.get(key)
            return None if stored is None else dict(stored)

    async def run_effect(
        self,
        key: str,
        compute: Callable[[], Awaitable[Mapping[str, Any]]],
    ) -> tuple[Mapping[str, Any], bool]:
        async with self._condition:
            stored = self.effects.get(key)
            if stored is not None:
                return dict(stored), True
            flight = self._effect_flights.get(key)
            if flight is None:
                flight = asyncio.get_running_loop().create_future()
                flight.add_done_callback(_consume_future_exception)
                self._effect_flights[key] = flight
                owner = True
            else:
                owner = False
        if not owner:
            return dict(await asyncio.shield(flight)), True
        try:
            raw = await compute()
            if not isinstance(raw, Mapping) or not is_json_value(raw):
                raise ActionOfferError(
                    "offer handler must return a finite JSON object",
                    kind="invalid",
                )
            result = dict(raw)
            async with self._condition:
                existing = self.effects.get(key)
                if existing is None:
                    self.effects[key] = result
                    existing = result
                if not flight.done():
                    flight.set_result(dict(existing))
                return dict(existing), existing is not result
        except BaseException as error:
            async with self._condition:
                if not flight.done():
                    flight.set_exception(error)
            raise
        finally:
            async with self._condition:
                if self._effect_flights.get(key) is flight:
                    self._effect_flights.pop(key, None)


class MemoryOfferBudget:
    """In-process idempotent budget used to prove the approve charge path."""

    def __init__(self) -> None:
        self.reserved: dict[str, int] = {}
        self.settled: dict[str, int] = {}
        self.released: set[str] = set()
        self.reserve_calls = 0
        self.settle_calls = 0
        self.release_calls = 0
        self._lock = asyncio.Lock()

    async def reserve(self, *, account: str, amount_milli: int, key: str) -> None:
        del account
        async with self._lock:
            self.reserve_calls += 1
            if key in self.settled or key in self.reserved:
                return
            self.released.discard(key)
            self.reserved[key] = amount_milli

    async def settle(self, *, key: str, amount_milli: int) -> None:
        async with self._lock:
            self.settle_calls += 1
            if key in self.settled:
                return
            reserved = self.reserved.pop(key, None)
            if reserved is None:
                raise ActionOfferError(f"budget key {key!r} was never reserved", kind="budget")
            if amount_milli > reserved:
                raise ActionOfferError(
                    f"budget key {key!r} settled for {amount_milli} over {reserved}",
                    kind="budget",
                )
            self.settled[key] = amount_milli

    async def release(self, *, key: str) -> None:
        async with self._lock:
            self.release_calls += 1
            if key in self.settled or key in self.released:
                return
            self.reserved.pop(key, None)
            self.released.add(key)


def propose_action(
    *,
    action_ref: str,
    handler: str,
    input: Mapping[str, Any] | None = None,
    input_fingerprint: str | None = None,
    policy_version: str,
    context_version: str,
    auth_scope: str,
    summary: str,
    risk_level: RiskLevel | str = RiskLevel.LOW,
    risk_reasons: Sequence[str] = (),
    expires_at_ms: int | None = None,
    revalidate_after_ms: int | None = None,
    provenance: OfferProvenance | Mapping[str, Any],
    schema_version: str = ACTION_OFFER_SCHEMA_VERSION,
) -> ActionOffer:
    """Build an offer. This function performs no I/O and no dispatch."""
    if schema_version != ACTION_OFFER_SCHEMA_VERSION:
        raise ActionOfferError(
            f"unsupported ActionOffer schema {schema_version!r}",
            kind="invalid",
        )
    action = _require_text(action_ref, "action_ref")
    handler_name = _require_handler(handler)
    bound, fingerprint, input_bound = _bound_input(input, input_fingerprint)
    _task_bindings(bound)
    policy = _require_text(policy_version, "policy_version")
    context = _require_text(context_version, "context_version")
    scope = _require_text(auth_scope, "auth_scope")
    text = _require_text(summary, "summary")
    level = _parse_risk_level(risk_level)
    reasons = _parse_reasons(risk_reasons)
    expires = _optional_int(expires_at_ms, "expires_at_ms")
    revalidate = _optional_int(revalidate_after_ms, "revalidate_after_ms")
    proven = OfferProvenance.from_value(provenance)
    offer_id = offer_identity(
        action_ref=action,
        handler=handler_name,
        input=bound,
        input_fingerprint=fingerprint,
        input_bound=input_bound,
        policy_version=policy,
        context_version=context,
        auth_scope=scope,
    )
    return ActionOffer(
        schema_version=schema_version,
        offer_id=offer_id,
        action_ref=action,
        handler=handler_name,
        input=freeze_json(bound),
        input_bound=input_bound,
        input_fingerprint=fingerprint,
        policy_version=policy,
        context_version=context,
        auth_scope=scope,
        summary=text,
        risk_level=level,
        risk_reasons=reasons,
        expires_at_ms=expires,
        revalidate_after_ms=revalidate,
        provenance=proven,
    )


def offer_identity(
    *,
    action_ref: str,
    handler: str,
    input: Mapping[str, Any],
    input_fingerprint: str,
    input_bound: bool = True,
    policy_version: str,
    context_version: str,
    auth_scope: str,
    schema_version: str = ACTION_OFFER_SCHEMA_VERSION,
) -> str:
    """Deterministic offer id. Field order and process identity do not matter."""
    payload = {
        "schema_version": schema_version,
        "action_ref": action_ref,
        "handler": handler,
        "input": dict(input),
        "input_fingerprint": input_fingerprint,
        "input_bound": input_bound,
        "policy_version": policy_version,
        "context_version": context_version,
        "auth_scope": auth_scope,
    }
    return sha256_hex(payload)


def offer_delivery_key(offer_id: str, action_ref: str) -> str:
    """Stable logical delivery identity for an approved offer."""
    return sha256_hex({"offer_id": offer_id, "action": action_ref})


def offer_run_id(offer_id: str) -> str:
    return f"offer-{offer_id}"


class ActionOfferHost:
    """Explicit approve/reject/revoke operations over existing SWE dispatch."""

    def __init__(
        self,
        handlers: Mapping[str, Handler],
        *,
        store: OfferStore | None = None,
        run_store: RunStore | None = None,
        budget: OfferBudget | None = None,
        resources: WorkflowResources | None = None,
        clock: WorkflowClock | FrozenClock | None = None,
        policy_version: str,
        context_version: str,
        tariff_milli: int = DEFAULT_TARIFF_MILLI,
        budget_account: str = "offers",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        concurrency: int = 1,
        claim_lease_ms: int | None = None,
        definition_cache_max: int = DEFAULT_DEFINITION_CACHE,
    ) -> None:
        self.handlers = dict(handlers)
        self.store = store or MemoryOfferStore()
        self.run_store = run_store
        self.budget = budget
        self.resources = resources or WorkflowResources()
        self.clock: WorkflowClock | FrozenClock = clock or WorkflowClock()
        self.policy_version = policy_version
        self.context_version = context_version
        self.tariff_milli = _require_non_negative_int(tariff_milli, "tariff_milli")
        self.budget_account = budget_account
        self.timeout_seconds = _require_positive_int(timeout_seconds, "timeout_seconds")
        self.concurrency = _require_positive_int(concurrency, "concurrency")
        if claim_lease_ms is None:
            self.claim_lease_ms = self.timeout_seconds * 1000
        else:
            self.claim_lease_ms = _require_positive_int(claim_lease_ms, "claim_lease_ms")
        self._definition_cache_max = _require_positive_int(
            definition_cache_max, "definition_cache_max"
        )
        self._definitions: OrderedDict[tuple[str, str], CompiledDefinition] = OrderedDict()
        self._claim_lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Future[OfferAcceptance]] = {}

    def _new_flight(self) -> asyncio.Future[OfferAcceptance]:
        flight: asyncio.Future[OfferAcceptance] = asyncio.get_running_loop().create_future()
        flight.add_done_callback(_consume_future_exception)
        return flight

    async def approve(
        self,
        offer: ActionOffer | Mapping[str, Any],
        *,
        auth_scope: str,
    ) -> OfferAcceptance:
        resolved = ActionOffer.from_value(offer)
        now = self.clock.now_ms()
        self._assert_actor(resolved, auth_scope)
        if not await self._has_replayable_work(resolved):
            self._assert_fresh(resolved, auth_scope=auth_scope, now_ms=now)
        delivery = offer_delivery_key(resolved.offer_id, resolved.action_ref)
        owner, flight = await self._claim(resolved.offer_id)
        if not owner:
            accepted = await asyncio.shield(flight)
            return OfferAcceptance(
                offer=resolved,
                status=accepted.status,
                run=accepted.run,
                delivery_key=accepted.delivery_key,
                reused=True,
            )
        try:
            accepted = await self._approve_or_follow(resolved, delivery, now)
            if not flight.done():
                flight.set_result(accepted)
            return accepted
        except BaseException as error:
            if not flight.done():
                flight.set_exception(error)
            raise
        finally:
            await self._release_claim(resolved.offer_id, flight)

    async def reject(
        self,
        offer: ActionOffer | Mapping[str, Any],
        *,
        auth_scope: str,
    ) -> OfferRecord:
        resolved = ActionOffer.from_value(offer)
        now = self.clock.now_ms()
        self._assert_actor(resolved, auth_scope)
        record = OfferRecord(
            offer=resolved,
            status=OfferStatus.REJECTED,
            decided_at_ms=now,
        )
        written = await self.store.transition(
            resolved.offer_id,
            expected=REJECTABLE_OFFER_STATUSES,
            record=record,
        )
        if written is not None:
            return written
        existing = await self.store.load(resolved.offer_id)
        if existing is None:
            raise ActionOfferError("offer decision changed during reject", kind="conflict")
        return self._rejected_conflict(existing)

    async def revoke(self, offer_id: str) -> OfferRecord:
        existing = await self.store.load(offer_id)
        if existing is None:
            raise ActionOfferError(f"unknown offer {offer_id!r}", kind="invalid")
        if existing.status is OfferStatus.REVOKED:
            return existing
        record = OfferRecord(
            offer=existing.offer,
            status=OfferStatus.REVOKED,
            decided_at_ms=self.clock.now_ms(),
        )
        written = await self.store.transition(
            offer_id,
            expected=REVOKABLE_OFFER_STATUSES,
            record=record,
        )
        if written is not None:
            return written
        current = await self.store.load(offer_id)
        if current is None:
            raise ActionOfferError(f"unknown offer {offer_id!r}", kind="invalid")
        if current.status is OfferStatus.REVOKED:
            return current
        match current.status:
            case OfferStatus.APPROVED:
                raise ActionOfferError("approved offer cannot be revoked", kind="conflict")
            case OfferStatus.APPROVING:
                raise ActionOfferError("offer is already being approved", kind="conflict")
            case OfferStatus.PROPOSED | OfferStatus.REJECTED:
                raise ActionOfferError("offer decision changed during revoke", kind="conflict")
            case OfferStatus.REVOKED:
                return current
            case _ as unreachable:
                assert_never(unreachable)

    async def _claim(
        self, offer_id: str
    ) -> tuple[bool, asyncio.Future[OfferAcceptance]]:
        async with self._claim_lock:
            existing = await self.store.load(offer_id)
            if existing is not None and existing.status is OfferStatus.APPROVED:
                flight = self._completed_claim(existing)
                return False, flight
            flight = self._inflight.get(offer_id)
            if flight is None:
                flight = self._new_flight()
                self._inflight[offer_id] = flight
                return True, flight
            return False, flight

    def _completed_claim(self, existing: OfferRecord) -> asyncio.Future[OfferAcceptance]:
        flight = self._new_flight()
        flight.set_result(
            self._reuse(
                existing.offer,
                existing,
                existing.delivery_key or offer_delivery_key(
                    existing.offer.offer_id, existing.offer.action_ref
                ),
            )
        )
        return flight

    async def _release_claim(
        self,
        offer_id: str,
        flight: asyncio.Future[OfferAcceptance],
    ) -> None:
        async with self._claim_lock:
            if self._inflight.get(offer_id) is flight:
                self._inflight.pop(offer_id, None)

    async def _approve_or_follow(
        self,
        resolved: ActionOffer,
        delivery: str,
        now: int,
    ) -> OfferAcceptance:
        claimed_until_ms = now + self.claim_lease_ms
        claim = OfferRecord(
            offer=resolved,
            status=OfferStatus.APPROVING,
            delivery_key=delivery,
            decided_at_ms=now,
            claimed_until_ms=claimed_until_ms,
        )
        written = await self.store.transition(
            resolved.offer_id,
            expected=CLAIMABLE_OFFER_STATUSES,
            record=claim,
        )
        if written is None:
            current = await self.store.load(resolved.offer_id)
            return await self._follow_existing(resolved, delivery, current)
        try:
            return await self._approve_claimed(
                resolved, delivery, claimed_until_ms=claimed_until_ms
            )
        except BaseException:
            await self._release_unstarted_claim(
                resolved, delivery, claimed_until_ms=claimed_until_ms
            )
            raise

    async def _follow_existing(
        self,
        resolved: ActionOffer,
        delivery: str,
        current: OfferRecord | None,
    ) -> OfferAcceptance:
        if current is None:
            return await self._approve_or_follow(resolved, delivery, self.clock.now_ms())
        match current.status:
            case OfferStatus.APPROVED:
                return self._reuse(resolved, current, delivery)
            case OfferStatus.REJECTED:
                raise ActionOfferError("offer was rejected", kind="rejected")
            case OfferStatus.REVOKED:
                raise ActionOfferError("offer was revoked", kind="revoked")
            case OfferStatus.PROPOSED:
                return await self._approve_or_follow(resolved, delivery, self.clock.now_ms())
            case OfferStatus.APPROVING:
                finished = await self._try_finish_existing(resolved, delivery)
                if finished is not None:
                    return finished
                timeout_s = self._lease_remaining_s(current)
                if timeout_s > 0:
                    await self.store.wait(
                        resolved.offer_id,
                        not_in={OfferStatus.APPROVING},
                        timeout_s=timeout_s,
                    )
                    finished = await self._try_finish_existing(resolved, delivery)
                    if finished is not None:
                        return finished
                latest = await self.store.load(resolved.offer_id)
                if latest is not None and latest.status is OfferStatus.APPROVING:
                    taken = await self._takeover_claim(resolved, delivery, latest)
                    if taken is not None:
                        try:
                            return await self._approve_claimed(
                                resolved,
                                delivery,
                                claimed_until_ms=taken.claimed_until_ms or self.clock.now_ms(),
                            )
                        except BaseException:
                            await self._release_unstarted_claim(
                                resolved,
                                delivery,
                                claimed_until_ms=taken.claimed_until_ms,
                            )
                            raise
                    refreshed = await self.store.load(resolved.offer_id)
                    if (
                        refreshed is not None
                        and refreshed.status is OfferStatus.APPROVING
                        and (
                            not self._lease_expired(refreshed)
                            or refreshed.claimed_until_ms == latest.claimed_until_ms
                        )
                    ):
                        raise ActionOfferError(
                            "offer is already being approved", kind="conflict"
                        )
                    return await self._follow_existing(resolved, delivery, refreshed)
                return await self._follow_existing(resolved, delivery, latest)
            case _ as unreachable:
                assert_never(unreachable)

    async def _takeover_claim(
        self,
        resolved: ActionOffer,
        delivery: str,
        current: OfferRecord,
    ) -> OfferRecord | None:
        if not self._lease_expired(current):
            return None
        now = self.clock.now_ms()
        claimed_until_ms = now + self.claim_lease_ms
        return await self.store.transition(
            resolved.offer_id,
            expected={OfferStatus.APPROVING},
            expected_claimed_until_ms={current.claimed_until_ms},
            record=OfferRecord(
                offer=resolved,
                status=OfferStatus.APPROVING,
                delivery_key=delivery,
                decided_at_ms=now,
                claimed_until_ms=claimed_until_ms,
            ),
        )

    def _lease_expired(self, record: OfferRecord) -> bool:
        return self._lease_remaining_s(record) <= 0

    def _lease_remaining_s(self, record: OfferRecord) -> float:
        if record.claimed_until_ms is None:
            return 0.0
        remaining_ms = record.claimed_until_ms - self.clock.now_ms()
        if remaining_ms <= 0:
            return 0.0
        return min(self.timeout_seconds, remaining_ms / 1000)

    async def _try_finish_existing(
        self,
        resolved: ActionOffer,
        delivery: str,
    ) -> OfferAcceptance | None:
        existing = await self.store.load(resolved.offer_id)
        if existing is not None and existing.status is OfferStatus.APPROVED:
            return self._reuse(resolved, existing, delivery)
        persisted = await self._load_persisted_run(resolved)
        if persisted is not None and persisted.status == "completed":
            return await self._finish_completed(resolved, delivery, persisted, reused=True)
        if persisted is not None and persisted.status != "running":
            raise ActionOfferError(
                f"offer {resolved.offer_id} already ended with {persisted.status!r}",
                kind=persisted.status,
            )
        return None

    async def _release_unstarted_claim(
        self,
        resolved: ActionOffer,
        delivery: str,
        *,
        claimed_until_ms: int | None,
    ) -> None:
        persisted = await self._load_persisted_run(resolved)
        effect = await self.store.load_effect(delivery)
        if persisted is not None or effect is not None:
            return
        await self.store.transition(
            resolved.offer_id,
            expected={OfferStatus.APPROVING},
            expected_claimed_until_ms={claimed_until_ms},
            record=OfferRecord(offer=resolved, status=OfferStatus.PROPOSED),
        )

    def _rejected_conflict(self, existing: OfferRecord) -> OfferRecord:
        match existing.status:
            case OfferStatus.REJECTED:
                return existing
            case OfferStatus.APPROVED:
                raise ActionOfferError("approved offer cannot be rejected", kind="conflict")
            case OfferStatus.REVOKED:
                raise ActionOfferError("revoked offer cannot be rejected", kind="revoked")
            case OfferStatus.APPROVING:
                raise ActionOfferError("offer is already being approved", kind="conflict")
            case OfferStatus.PROPOSED:
                raise ActionOfferError("offer decision changed during reject", kind="conflict")
            case _ as unreachable:
                assert_never(unreachable)

    async def _approve_claimed(
        self,
        resolved: ActionOffer,
        delivery: str,
        *,
        claimed_until_ms: int,
    ) -> OfferAcceptance:
        existing = await self.store.load(resolved.offer_id)
        if existing is not None and existing.status is OfferStatus.APPROVED:
            return self._reuse(resolved, existing, delivery)
        persisted = await self._load_persisted_run(resolved)
        if persisted is not None and persisted.status == "completed":
            return await self._finish_completed(resolved, delivery, persisted, reused=True)
        if persisted is not None and persisted.status != "running":
            raise ActionOfferError(
                f"offer {resolved.offer_id} already ended with {persisted.status!r}",
                kind=persisted.status,
            )
        if not await self._has_replayable_work(resolved):
            self._assert_fresh(
                resolved,
                auth_scope=resolved.auth_scope,
                now_ms=self.clock.now_ms(),
            )
        if not resolved.input_bound:
            raise ActionOfferError(
                "unbound offer cannot be approved without matching input",
                kind="unbound",
            )
        if not await self._still_owns_claim(resolved.offer_id, claimed_until_ms):
            current = await self.store.load(resolved.offer_id)
            return await self._follow_existing(resolved, delivery, current)
        if self.budget is not None:
            await self.budget.reserve(
                account=self.budget_account,
                amount_milli=self.tariff_milli,
                key=delivery,
            )
        try:
            if not await self._still_owns_claim(resolved.offer_id, claimed_until_ms):
                current = await self.store.load(resolved.offer_id)
                if self.budget is not None:
                    await self.budget.release(key=delivery)
                return await self._follow_existing(resolved, delivery, current)
            run = await self._dispatch(resolved, persisted, delivery)
        except BaseException:
            if self.budget is not None:
                await self.budget.release(key=delivery)
            raise
        if run.status == "completed":
            return await self._finish_completed(resolved, delivery, run, reused=False)
        if self.budget is not None:
            await self.budget.release(key=delivery)
        raise ActionOfferError(
            f"approved offer {resolved.offer_id} ended with {run.status!r}",
            kind=run.status,
        )

    async def _still_owns_claim(self, offer_id: str, claimed_until_ms: int) -> bool:
        current = await self.store.load(offer_id)
        return (
            current is not None
            and current.status is OfferStatus.APPROVING
            and current.claimed_until_ms == claimed_until_ms
        )

    async def _finish_completed(
        self,
        resolved: ActionOffer,
        delivery: str,
        run: RunResult,
        *,
        reused: bool,
    ) -> OfferAcceptance:
        await self._reconcile_budget(delivery)
        acceptance = OfferAcceptance(
            offer=resolved,
            status=OfferStatus.APPROVED,
            run=run,
            delivery_key=delivery,
            reused=reused,
        )
        await self._remember(resolved, OfferStatus.APPROVED, acceptance, self.clock.now_ms())
        return acceptance

    async def _reconcile_budget(self, delivery: str) -> None:
        if self.budget is None:
            return
        await self.budget.reserve(
            account=self.budget_account,
            amount_milli=self.tariff_milli,
            key=delivery,
        )
        await self.budget.settle(key=delivery, amount_milli=self.tariff_milli)

    def _assert_fresh(
        self,
        offer: ActionOffer,
        *,
        auth_scope: str,
        now_ms: int,
    ) -> None:
        self._assert_actor(offer, auth_scope)
        if offer.policy_version != self.policy_version:
            raise ActionOfferError("offer policy_version is stale", kind="stale")
        if offer.context_version != self.context_version:
            raise ActionOfferError("offer context_version is stale", kind="stale")
        if offer.expires_at_ms is not None and now_ms >= offer.expires_at_ms:
            raise ActionOfferError("offer has expired", kind="expired")
        if offer.revalidate_after_ms is not None and now_ms >= offer.revalidate_after_ms:
            raise ActionOfferError("offer must be revalidated", kind="stale")

    def _assert_actor(self, offer: ActionOffer, auth_scope: str) -> None:
        if auth_scope != offer.auth_scope:
            raise ActionOfferError("caller is not authorized for this offer", kind="unauthorized")

    async def _has_replayable_work(self, offer: ActionOffer) -> bool:
        existing = await self.store.load(offer.offer_id)
        if existing is not None and existing.status is OfferStatus.APPROVED:
            return True
        persisted = await self._load_persisted_run(offer)
        if persisted is not None and persisted.status == "completed":
            return True
        delivery = offer_delivery_key(offer.offer_id, offer.action_ref)
        if await self.store.load_effect(delivery) is not None:
            return True
        return await self._has_persisted_invocation(offer)

    async def _has_persisted_invocation(self, offer: ActionOffer) -> bool:
        store = self.run_store
        if store is None:
            return False
        replay = await store.replay_results(offer_run_id(offer.offer_id))
        return bool(replay)

    def _reuse(
        self,
        offer: ActionOffer,
        existing: OfferRecord,
        delivery: str,
    ) -> OfferAcceptance:
        run = None
        if existing.result is not None:
            payload = dict(existing.result)
            run = RunResult(
                run_id=str(payload.get("run_id") or existing.run_id or offer_run_id(offer.offer_id)),
                status=str(payload.get("status") or "completed"),
                output=payload.get("output"),
                events=list(payload.get("events") or []),
                error=payload.get("error"),
            )
        return OfferAcceptance(
            offer=offer,
            status=OfferStatus.APPROVED,
            run=run,
            delivery_key=existing.delivery_key or delivery,
            reused=True,
        )

    async def _load_persisted_run(self, offer: ActionOffer) -> RunResult | None:
        store = self.run_store
        if store is None:
            return None
        run_id = offer_run_id(offer.offer_id)
        stored = await store.load_run(run_id)
        if stored is None:
            return None
        if stored.status == "running":
            return RunResult(
                run_id=run_id,
                status="running",
                output=stored.output,
                events=await store.events(run_id),
                error=None if stored.error is None else dict(stored.error),
            )
        return RunResult(
            run_id=run_id,
            status=stored.status,
            output=stored.output,
            events=await store.events(run_id),
            error=None if stored.error is None else dict(stored.error),
        )

    async def _dispatch(
        self,
        offer: ActionOffer,
        persisted: RunResult | None,
        delivery: str,
    ) -> RunResult:
        compiled = self._definition(offer)
        runner = WorkflowRunner(
            compiled,
            self._effect_handlers(delivery, offer),
            resources=self.resources,
            run_store=self.run_store,
            clock=self.clock,
            concurrency=self.concurrency,
        )
        run_id = offer_run_id(offer.offer_id)
        if persisted is not None and persisted.status == "running":
            return await runner.resume(run_id)
        return await runner.run(thaw_json(offer.input), run_id=run_id)

    def _effect_handlers(self, delivery: str, offer: ActionOffer) -> dict[str, Handler]:
        _bindings, names = _task_bindings(offer.input)
        return {
            name: self._wrap_effect(delivery, handler, names)
            for name, handler in self.handlers.items()
        }

    def _wrap_effect(
        self,
        delivery: str,
        handler: Handler,
        names: Mapping[str, str],
    ) -> Handler:
        async def wrapped(payload: Mapping[str, Any]) -> Any:
            restored = {names.get(key, key): value for key, value in payload.items()}
            result, _reused = await self.store.run_effect(delivery, lambda: handler(restored))
            return result

        return wrapped

    def _definition(self, offer: ActionOffer) -> CompiledDefinition:
        bindings, _names = _task_bindings(offer.input)
        key = (offer.handler, sha256_hex(bindings))
        compiled = self._definitions.get(key)
        if compiled is not None:
            self._definitions.move_to_end(key)
            return compiled
        compiled = compile_definition(
            {
                "version": SWD_VERSION,
                "name": OFFER_WORKFLOW_NAME,
                "entrypoint": "execute",
                "limits": {
                    "max_steps": 8,
                    "max_depth": 1,
                    "timeout_seconds": self.timeout_seconds,
                },
                "nodes": {
                    "execute": {
                        "type": "task",
                        "handler": offer.handler,
                        "input": bindings,
                        "next": "done",
                    },
                    "done": {"type": "end", "status": "completed"},
                },
            },
            handlers=frozenset({offer.handler}),
        )
        self._store_definition(key, compiled, cacheable=not _has_const_binding(bindings))
        return compiled

    def _store_definition(
        self,
        key: tuple[str, str],
        compiled: CompiledDefinition,
        *,
        cacheable: bool,
    ) -> None:
        if not cacheable:
            return
        self._definitions[key] = compiled
        self._definitions.move_to_end(key)
        while len(self._definitions) > self._definition_cache_max:
            self._definitions.popitem(last=False)

    async def _remember(
        self,
        offer: ActionOffer,
        status: OfferStatus,
        acceptance: OfferAcceptance,
        now_ms: int,
    ) -> None:
        result = None
        if acceptance.run is not None:
            result = {
                "run_id": acceptance.run.run_id,
                "status": acceptance.run.status,
                "output": acceptance.run.output,
                "events": list(acceptance.run.events),
                "error": acceptance.run.error,
            }
        record = OfferRecord(
            offer=offer,
            status=status,
            run_id=None if acceptance.run is None else acceptance.run.run_id,
            delivery_key=acceptance.delivery_key,
            result=result,
            decided_at_ms=now_ms,
        )
        written = await self.store.transition(
            offer.offer_id,
            expected={OfferStatus.APPROVING, OfferStatus.APPROVED, None, OfferStatus.PROPOSED},
            record=record,
        )
        if written is None:
            current = await self.store.load(offer.offer_id)
            if current is None or current.status is not OfferStatus.APPROVED:
                raise ActionOfferError(
                    "offer decision changed while recording approval",
                    kind="conflict",
                )


def _consume_future_exception(flight: asyncio.Future[Any]) -> None:
    if flight.cancelled():
        return
    flight.exception()


def _const_binding_name(key: str) -> str:
    return f"c_{sha256_hex({'offer_const_key': key})}"


def _task_bindings(input_data: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    thawed = thaw_json(input_data)
    bindings: dict[str, Any] = {}
    names: dict[str, str] = {}
    for key, value in thawed.items():
        if BINDING_KEY_RE.fullmatch(key):
            bind_name = key
            bind_value: Any = f"$.input.{key}"
        else:
            bind_name = _const_binding_name(key)
            bind_value = {"const": value}
        if bind_name in bindings:
            raise ActionOfferError(
                f"offer input key {key!r} collides with another binding name",
                kind="invalid",
            )
        bindings[bind_name] = bind_value
        names[bind_name] = key
    return bindings, names


def _has_const_binding(bindings: Mapping[str, Any]) -> bool:
    return any(isinstance(value, Mapping) and "const" in value for value in bindings.values())


def _bound_input(
    input_data: Mapping[str, Any] | None,
    input_fingerprint: str | None,
) -> tuple[dict[str, Any], str, bool]:
    if input_data is None and not input_fingerprint:
        raise ActionOfferError("offer requires bound input or an input fingerprint", kind="invalid")
    if input_data is None:
        return {}, str(input_fingerprint), False
    bound = dict(input_data)
    if not is_json_value(bound):
        raise ActionOfferError("offer input must be a finite JSON object", kind="invalid")
    fingerprint = sha256_hex(bound)
    if input_fingerprint is not None and fingerprint != input_fingerprint:
        raise ActionOfferError("input fingerprint does not match bound input", kind="invalid")
    return bound, fingerprint, True


def _input_from_value(value: Mapping[str, Any]) -> Mapping[str, Any] | None:
    flag = value.get("input_bound")
    raw = value.get("input")
    if flag is False:
        return None
    if isinstance(raw, Mapping):
        return raw
    if flag is True:
        return {}
    return None


def _require_provenance(value: object) -> OfferProvenance:
    if not isinstance(value, (Mapping, OfferProvenance)):
        raise ActionOfferError("provenance is required", kind="invalid")
    return OfferProvenance.from_value(value)


def _require_handler(value: object) -> str:
    name = _require_text(value, "handler")
    if not HANDLER_NAME_RE.fullmatch(name):
        raise ActionOfferError(f"invalid handler name {name!r}", kind="invalid", path="handler")
    return name


def _require_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionOfferError(f"{path} must be a non-empty string", kind="invalid", path=path)
    if "\x00" in value:
        raise ActionOfferError(f"{path} cannot contain NUL", kind="invalid", path=path)
    return value


def _require_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ActionOfferError(f"{path} must be an integer", kind="invalid", path=path)
    return value


def _require_positive_int(value: object, path: str) -> int:
    number = _require_int(value, path)
    if number < 1:
        raise ActionOfferError(f"{path} must be a positive integer", kind="invalid", path=path)
    return number


def _require_non_negative_int(value: object, path: str) -> int:
    number = _require_int(value, path)
    if number < 0:
        raise ActionOfferError(
            f"{path} must be a non-negative integer", kind="invalid", path=path
        )
    return number


def _optional_int(value: object, path: str) -> int | None:
    if value is None:
        return None
    return _require_int(value, path)


def _parse_risk_level(value: object) -> RiskLevel:
    if isinstance(value, RiskLevel):
        return value
    if not isinstance(value, str):
        raise ActionOfferError("risk_level must be low, medium, or high", kind="invalid")
    try:
        return RiskLevel(value)
    except ValueError as exc:
        raise ActionOfferError(
            f"unknown risk_level {value!r}",
            kind="invalid",
        ) from exc


def _parse_reasons(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise ActionOfferError("risk_reasons must be a string list", kind="invalid")
    reasons = tuple(_require_text(item, "risk_reasons") for item in value)
    return reasons


def _percentile_ms(values: Sequence[int], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1)
    return ordered[index] / 1_000_000


@dataclass(frozen=True, slots=True)
class OfferBenchmark:
    operations: int
    concurrency: int
    wall_ms: float
    throughput_ops_per_second: float
    peak_in_flight: int
    p50_latency_ms: float
    p95_latency_ms: float
    unique_offer_ids: int
    approved: int
    reused: int
    python_version: str
    platform: str
    cpu_count: int | None
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "operations": self.operations,
            "concurrency": self.concurrency,
            "wall_ms": round(self.wall_ms, 3),
            "throughput_ops_per_second": round(self.throughput_ops_per_second, 3),
            "peak_in_flight": self.peak_in_flight,
            "p50_latency_ms": round(self.p50_latency_ms, 6),
            "p95_latency_ms": round(self.p95_latency_ms, 6),
            "unique_offer_ids": self.unique_offer_ids,
            "approved": self.approved,
            "reused": self.reused,
            "environment": {
                "python": self.python_version,
                "platform": self.platform,
                "cpu_count": self.cpu_count,
            },
            "generated_at": self.generated_at,
        }


async def benchmark_action_offers(
    *,
    operations: int = 10_000,
    concurrency: int = 64,
    min_throughput: float = 0.0,
) -> OfferBenchmark:
    """Synthetic offer propose/approve gate without model or network calls."""
    if isinstance(operations, bool) or operations < 1:
        raise ActionOfferError("operations must be a positive integer", kind="invalid")
    if isinstance(concurrency, bool) or concurrency < 1:
        raise ActionOfferError("concurrency must be a positive integer", kind="invalid")

    async def work(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        await asyncio.sleep(0)
        return {"ordinal": payload["ordinal"], "ok": True}

    store = MemoryOfferStore()
    run_store = MemoryRunStore()
    budget = MemoryOfferBudget()
    host = ActionOfferHost(
        {"bench.work": work},
        store=store,
        run_store=run_store,
        budget=budget,
        policy_version="bench-policy",
        context_version="bench-context",
        concurrency=concurrency,
    )
    offers = [
        propose_action(
            action_ref="bench.work",
            handler="bench.work",
            input={"ordinal": index},
            policy_version="bench-policy",
            context_version="bench-context",
            auth_scope="bench-scope",
            summary=f"synthetic offer {index}",
            provenance=OfferProvenance("benchmark", "swe", 0, "synthetic"),
        )
        for index in range(operations)
    ]
    unique_ids = {offer.offer_id for offer in offers}
    if len(unique_ids) != operations:
        raise ActionOfferError(
            f"quality gate failed: expected {operations} offer ids, got {len(unique_ids)}",
            kind="invalid",
        )
    active = 0
    peak = 0
    latencies: list[int] = []
    approved = 0
    reused = 0
    semaphore = asyncio.Semaphore(concurrency)
    started = time.perf_counter_ns()

    async def approve_one(offer: ActionOffer) -> None:
        nonlocal active, peak, approved, reused
        async with semaphore:
            active += 1
            peak = max(peak, active)
            mark = time.perf_counter_ns()
            try:
                acceptance = await host.approve(offer, auth_scope="bench-scope")
                if acceptance.reused:
                    reused += 1
                else:
                    approved += 1
            finally:
                latencies.append(time.perf_counter_ns() - mark)
                active -= 1

    await asyncio.gather(*(approve_one(offer) for offer in offers))
    await asyncio.gather(*(approve_one(offer) for offer in offers))
    wall_ns = time.perf_counter_ns() - started
    wall_seconds = max(wall_ns / 1_000_000_000, 1e-9)
    total_ops = operations * 2
    throughput = total_ops / wall_seconds
    if approved != operations:
        raise ActionOfferError(
            f"quality gate failed: expected {operations} approvals, got {approved}",
            kind="invalid",
        )
    if reused != operations:
        raise ActionOfferError(
            f"quality gate failed: expected {operations} reused approvals, got {reused}",
            kind="invalid",
        )
    if peak > concurrency:
        raise ActionOfferError(
            f"quality gate failed: peak in-flight {peak} exceeded {concurrency}",
            kind="invalid",
        )
    if len(budget.settled) != operations:
        raise ActionOfferError(
            "quality gate failed: double approve charged the budget twice",
            kind="budget",
        )
    if throughput < min_throughput:
        raise ActionOfferError(
            f"quality gate failed: {throughput:.3f} ops/s is below {min_throughput:.3f} ops/s",
            kind="invalid",
        )
    return OfferBenchmark(
        operations=operations,
        concurrency=concurrency,
        wall_ms=wall_ns / 1_000_000,
        throughput_ops_per_second=throughput,
        peak_in_flight=peak,
        p50_latency_ms=_percentile_ms(latencies, 0.50),
        p95_latency_ms=_percentile_ms(latencies, 0.95),
        unique_offer_ids=len(unique_ids),
        approved=approved,
        reused=reused,
        python_version=platform.python_version(),
        platform=platform.platform(),
        cpu_count=os.cpu_count(),
        generated_at=datetime.now(UTC).isoformat(),
    )


def benchmark_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SWE ActionOffer/cache quality gate")
    parser.add_argument("--operations", type=int, default=10_000)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--min-throughput", type=float, default=0.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        cache_report = asyncio.run(
            benchmark_exact_cache(
                operations=args.operations,
                concurrency=args.concurrency,
                min_throughput=args.min_throughput,
            )
        )
        offer_report = asyncio.run(
            benchmark_action_offers(
                operations=args.operations,
                concurrency=args.concurrency,
                min_throughput=args.min_throughput,
            )
        )
    except (ActionOfferError, SweRuntimeError, RuntimeError) as error:
        print(f"offer/cache benchmark failed: {error}", file=sys.stderr)
        return 1
    payload = {
        "schema_version": 1,
        "cache": cache_report.to_dict(),
        "offers": offer_report.to_dict(),
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


__all__ = [
    "ACTION_OFFER_SCHEMA_VERSION",
    "CLAIMABLE_OFFER_STATUSES",
    "REJECTABLE_OFFER_STATUSES",
    "REVOKABLE_OFFER_STATUSES",
    "ActionOffer",
    "ActionOfferError",
    "ActionOfferHost",
    "MemoryOfferBudget",
    "MemoryOfferStore",
    "OfferAcceptance",
    "OfferBenchmark",
    "OfferBudget",
    "OfferProvenance",
    "OfferRecord",
    "OfferStatus",
    "OfferStore",
    "RiskLevel",
    "benchmark_action_offers",
    "benchmark_main",
    "offer_delivery_key",
    "offer_identity",
    "offer_run_id",
    "propose_action",
]


if __name__ == "__main__":
    raise SystemExit(benchmark_main())
