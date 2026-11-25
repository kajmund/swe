"""Host mark for an external effect, recorded in the native journal.

Journal schema v1 is unchanged. The runner writes it only for nodes listed in
``WorkflowResources.effect_deliveries``. Undeclared nodes keep the existing
cancel and budget behaviour. This module is not an OverGraph client and it
does not add a ledger.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from swe.invocation_handle import InvocationHandle, current_invocation
from swe.runtime_errors import SweHandlerError, SweRuntimeError

# Native ``JOURNAL_SCHEMA_VERSION``. The host API writes that log and no other.
EFFECT_JOURNAL_VERSION = 1

DELIVERIES = frozenset(
    {
        "pure",
        "replayable",
        "external_idempotent",
        "external_non_idempotent",
    }
)
EXTERNAL_DELIVERIES = frozenset({"external_idempotent", "external_non_idempotent"})

# Open attempts the runner must not re-dispatch. Pure ``recompute`` is absent
# on purpose: retry stays unsupported, and an unknown external effect fails
# closed instead of becoming a fresh success.
_BLOCKING_RECOVERY = frozenset(
    {"fail_stop", "replay_requires_result", "idempotent_retry"}
)


@dataclass
class _Attempt:
    attempt_id: str
    charge_id: str | None
    delivery: str
    closed: bool = False
    pending: bool = False


class EffectJournal:
    """In-process view over ``swe_native.Journal`` for one run."""

    def __init__(self) -> None:
        import swe_native

        self._native = swe_native
        self._journal = swe_native.Journal()
        self._attempts: dict[str, _Attempt] = {}

    def events(self) -> list[dict[str, Any]]:
        return [dict(event) for event in self._journal.events()]

    def open_external(self) -> bool:
        return any(
            not attempt.closed and attempt.delivery in EXTERNAL_DELIVERIES
            for attempt in self._attempts.values()
        )

    def open_ids(self) -> list[str]:
        return [
            invocation_id
            for invocation_id, attempt in self._attempts.items()
            if not attempt.closed
        ]

    def begin(
        self,
        *,
        run_id: str,
        invocation_id: str,
        node_id: str,
        action: str,
        delivery: str,
        now_ms: int,
        reserved_milli: int | None,
    ) -> None:
        if invocation_id in self._attempts:
            raise SweRuntimeError(
                "external effect already started",
                kind="handler",
                path=f"nodes.{node_id}",
            )
        key = self._native.delivery_key(run_id, invocation_id, action)
        attempt_id = self._native.attempt_id(key, 1)
        started_at_ms = int(now_ms)
        self._journal.append(
            {
                "kind": "AttemptStarted",
                "attempt_id": attempt_id,
                "delivery_key": key,
                "invocation_id": invocation_id,
                "action": action,
                "delivery": delivery,
                "started_at_ms": started_at_ms,
            }
        )
        charge_id = None
        if reserved_milli is not None and reserved_milli > 0:
            charge_id = f"{attempt_id}:charge"
            self._journal.append(
                {
                    "kind": "ChargeReserved",
                    "charge_id": charge_id,
                    "attempt_id": attempt_id,
                    "delivery_key": key,
                    "amount_milli": int(reserved_milli),
                    "tariff_milli": int(reserved_milli),
                    "reserved_at_ms": started_at_ms,
                }
            )
        self._attempts[invocation_id] = _Attempt(
            attempt_id=attempt_id,
            charge_id=charge_id,
            delivery=delivery,
        )

    def confirm(
        self,
        invocation_id: str,
        *,
        now_ms: int,
        value: Any,
        spent_milli: int | None,
    ) -> None:
        attempt = self._attempts.get(invocation_id)
        if attempt is None or attempt.closed or attempt.pending:
            return
        confirmed_at_ms = int(now_ms)
        self._journal.append(
            {
                "kind": "ResultConfirmed",
                "attempt_id": attempt.attempt_id,
                "outcome": {"kind": "ok", "value": value},
                "confirmed_at_ms": confirmed_at_ms,
            }
        )
        if attempt.charge_id is not None and spent_milli is not None:
            self._journal.append(
                {
                    "kind": "ChargeSettled",
                    "charge_id": attempt.charge_id,
                    "amount_milli": int(spent_milli),
                    "settled_at_ms": confirmed_at_ms,
                }
            )
        attempt.closed = True

    @classmethod
    def load(cls, events: list[dict[str, Any]]) -> EffectJournal:
        """Rebuild the in-process view from a persisted v1 event list.

        Resume must start from this log. A fresh journal would let the next
        ``save_journal`` replace a confirmed attempt.
        """

        import swe_native

        journal = cls.__new__(cls)
        journal._native = swe_native
        journal._journal = swe_native.Journal.load(list(events))
        journal._attempts = {}
        charge_of: dict[str, str] = {}
        pending_charges: set[str] = set()
        for event in events:
            kind = str(event.get("kind"))
            if kind == "AttemptStarted":
                journal._attempts[str(event["invocation_id"])] = _Attempt(
                    attempt_id=str(event["attempt_id"]),
                    charge_id=None,
                    delivery=str(event["delivery"]),
                )
            elif kind == "ChargeReserved":
                charge_of[str(event["attempt_id"])] = str(event["charge_id"])
            elif kind in {"ResultConfirmed", "AttemptAbandoned"}:
                attempt_id = str(event["attempt_id"])
                for attempt in journal._attempts.values():
                    if attempt.attempt_id == attempt_id:
                        attempt.closed = True
            elif kind == "ChargePendingReconciliation":
                pending_charges.add(str(event["charge_id"]))
        for attempt in journal._attempts.values():
            attempt.charge_id = charge_of.get(attempt.attempt_id)
            if attempt.charge_id is not None and attempt.charge_id in pending_charges:
                attempt.pending = True
        return journal

    def note_unknown(self, invocation_id: str, now_ms: int, reason: str) -> None:
        """Leave the attempt open and refuse to write its charge off as zero."""

        attempt = self._attempts.get(invocation_id)
        if attempt is None or attempt.closed or attempt.pending:
            return
        if attempt.charge_id is not None:
            self._journal.append(
                {
                    "kind": "ChargePendingReconciliation",
                    "charge_id": attempt.charge_id,
                    "reason": reason,
                    "at_ms": int(now_ms),
                }
            )
        attempt.pending = True


def confirmed_replay_gap(
    events: list[dict[str, Any]], known_ids: set[str],
) -> list[tuple[str, str, Any]] | None:
    """Confirmed attempts whose invocation record was never stored.

    Each item is ``(invocation_id, action, value)`` for an ok outcome the
    existing replay path can apply without calling the handler. ``None`` means
    the gap is not a plain ok result, so resume must fail closed instead of
    starting the effect again.
    """

    started: dict[str, dict[str, Any]] = {}
    gaps: list[tuple[str, str, Any]] = []
    for event in events:
        kind = str(event.get("kind"))
        if kind == "AttemptStarted":
            started[str(event["attempt_id"])] = event
            continue
        if kind != "ResultConfirmed":
            continue
        attempt = started.get(str(event.get("attempt_id")))
        if attempt is None:
            return None
        invocation_id = str(attempt["invocation_id"])
        if invocation_id in known_ids:
            continue
        outcome = event.get("outcome")
        if not isinstance(outcome, Mapping) or outcome.get("kind") != "ok":
            return None
        gaps.append(
            (invocation_id, str(attempt.get("action") or ""), outcome.get("value"))
        )
    return gaps


def blocks_resume(events: list[dict[str, Any]]) -> bool:
    """True when ``plan_recovery`` forbids re-dispatch."""

    import swe_native

    try:
        plan = swe_native.Journal.load(events).plan_recovery()
    except (TypeError, ValueError):
        return True
    return any(str(item.get("kind")) in _BLOCKING_RECOVERY for item in plan)


def external_unmarked(handle: InvocationHandle | None) -> bool:
    if handle is None:
        return False
    return (
        handle.effect.delivery in EXTERNAL_DELIVERIES and not handle.effect.started
    )


def should_release_unused(handle: InvocationHandle | None) -> bool:
    """Cancel before the effect mark may release only the unused reservation."""

    return external_unmarked(handle)


async def note_confirmed(handle: InvocationHandle | None, value: Any, reservation: Any) -> None:
    if handle is None or not handle.effect.started or handle.effect.on_confirmed is None:
        return
    await handle.effect.on_confirmed(value, reservation)


async def begin_external_effect() -> None:
    """Record ``AttemptStarted`` for the current invocation before external I/O.

    If the host cancel is already visible, this raises ``CancelledError`` and
    does not append ``AttemptStarted``. The runner then releases only the
    unused reservation. A call after a successful mark is refused.
    """

    handle = current_invocation()
    path = "" if handle is None else f"nodes.{handle.node_id}"
    if handle is None or handle.effect.begin is None:
        raise SweRuntimeError(
            "begin_external_effect requires a declared external delivery on this node",
            kind="preflight",
            path=path,
        )
    if handle.effect.delivery not in EXTERNAL_DELIVERIES:
        raise SweRuntimeError(
            "begin_external_effect requires an external delivery class",
            kind="preflight",
            path=path,
        )
    await handle.effect.begin()


class EffectNotStarted(SweHandlerError):
    """A declared external node returned without ``begin_external_effect``."""

    def __init__(self, node_id: str) -> None:
        super().__init__(
            "external delivery returned before begin_external_effect",
            path=f"nodes.{node_id}",
        )


__all__ = [
    "DELIVERIES",
    "EFFECT_JOURNAL_VERSION",
    "EXTERNAL_DELIVERIES",
    "EffectJournal",
    "EffectNotStarted",
    "begin_external_effect",
    "blocks_resume",
    "external_unmarked",
    "note_confirmed",
    "should_release_unused",
]
