"""Generic runtime budget contract backed by the Rust ``swe-budget`` ledger.

The runtime never invents a second ledger. ``RuntimeBudget`` composes a host
``BudgetLedger`` (the PyO3 binding ``swe_budget.Ledger`` in the reference
case) with a domain-neutral ``BudgetPolicy`` that supplies two integers:

- ``reserve(request)``: a conservative ceiling for one invocation, or ``0``
  for a call the host has decided is free. Raising ``BudgetUnknownCost`` (or
  returning anything that is not a non-negative integer) fails closed before
  the handler is called.
- ``settle(request, outcome)``: the actual cost, at most the reserved ceiling.
  Raising or returning an amount above the ceiling fails closed and leaves the
  reservation open for host reconciliation; reserved funds are never reused.

Cancellation and action timeout call ``mark_pending``. The reservation stays
on the ledger until the host either reconciles it or a late handler outcome
settles it. This is deliberate: SWE cannot know whether an external provider
billed a cancelled call.

Retries are not introduced here. If a host retries an invocation, it must reuse
the same ``run_id`` and ``invocation_id`` so the ledger's idempotency keys
prevent a second reservation or charge. A retry may reuse a ``PENDING`` hold;
a terminal ``SETTLED``/``RELEASED`` delivery is refused before the handler. If
the ledger already holds a settlement under the stable settle key, that
settlement is authoritative and a different amount fails closed.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

from swe.runtime_errors import SweRuntimeError


class BudgetError(SweRuntimeError):
    """Base error for the runtime budget boundary."""

    def __init__(self, message: str, *, kind: str = "budget", path: str = "") -> None:
        super().__init__(message, kind=kind, path=path)


class BudgetUnknownCost(BudgetError):
    """The policy could not produce a conservative ceiling; nothing dispatched."""


class BudgetExceeded(BudgetError):
    """The ledger refused the reservation (insufficient funds or bad key)."""


class BudgetOverrun(BudgetError):
    """The actual cost exceeds the ceiling; the reservation stays open."""


class BudgetStateError(BudgetError):
    """A reservation was settled/released twice or used after a terminal state."""


class ReservationState(StrEnum):
    RESERVED = "reserved"
    PENDING = "pending"
    SETTLED = "settled"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class BudgetRequest:
    """One handler or provider invocation presented to the policy."""

    run_id: str
    invocation_id: str
    node_id: str
    kind: str
    name: str
    input: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BudgetOutcome:
    """What the dispatch boundary observed after the call completed."""

    ok: bool
    cancelled: bool = False
    timed_out: bool = False
    cache_state: str | None = None
    value: Any = None
    error: Mapping[str, Any] | None = None


@dataclass
class BudgetReservation:
    """In-process state for one open ledger reservation."""

    request: BudgetRequest
    base_key: str
    reservation_id: int
    reserved_milli: int
    state: ReservationState = ReservationState.RESERVED
    spent_milli: int = 0


class BudgetLedger(Protocol):
    """The subset of the Rust ledger the runtime needs.

    ``swe_budget.Ledger`` is wrapped by :class:`NativeBudgetLedger`; a host may
    provide its own durable adapter with the same integer semantics.
    """

    def reserve(
        self, *, run_id: str, account: str, amount_milli: int, key: str
    ) -> int: ...

    def settle(self, *, reservation_id: int, spent_milli: int, key: str) -> None: ...

    def release(self, *, reservation_id: int, key: str) -> None: ...

    def balance(self, *, account: str) -> Mapping[str, int]: ...

    def events(self) -> Sequence[Mapping[str, Any]]: ...


class BudgetPolicy(Protocol):
    """Host policy. It owns prices, provider identity and cost estimates."""

    def reserve(self, request: BudgetRequest) -> int: ...

    def settle(self, request: BudgetRequest, outcome: BudgetOutcome) -> int: ...


def _require_amount(value: object, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BudgetUnknownCost(
            f"{path} must return an integer milli-unit amount, got {type(value).__name__}",
            path=path,
        )
    if value < 0:
        raise BudgetUnknownCost(f"{path} must be non-negative", path=path)
    return value


def _key(request: BudgetRequest, suffix: str) -> str:
    return f"{request.run_id}:{request.invocation_id}:{suffix}"


DEFAULT_TERMINAL_CAPACITY = 1_024


class RuntimeBudget:
    """Compose a ledger and a policy; enforce the runtime reservation protocol."""

    def __init__(
        self,
        ledger: BudgetLedger,
        policy: BudgetPolicy,
        *,
        account: str = "default",
        terminal_capacity: int = DEFAULT_TERMINAL_CAPACITY,
    ) -> None:
        if not isinstance(account, str) or not account.strip():
            raise BudgetError("account must be a non-empty string", path="account")
        if (
            isinstance(terminal_capacity, bool)
            or not isinstance(terminal_capacity, int)
            or terminal_capacity < 1
        ):
            raise BudgetError(
                "terminal_capacity must be a positive integer",
                path="terminal_capacity",
            )
        for name in ("reserve", "settle", "release", "balance", "events"):
            if not callable(getattr(ledger, name, None)):
                raise BudgetError(
                    f"budget ledger is missing {name}()", path="ledger"
                )
        for name in ("reserve", "settle"):
            if not callable(getattr(policy, name, None)):
                raise BudgetError(
                    f"budget policy is missing {name}()", path="policy"
                )
        self.ledger = ledger
        self.policy = policy
        self.account = account
        self.terminal_capacity = terminal_capacity
        # Open holds only: RESERVED or PENDING. Terminal holds move to the
        # bounded _completed window and drop request.input.
        self._by_invocation: dict[str, BudgetReservation] = {}
        self._completed: OrderedDict[str, BudgetReservation] = OrderedDict()

    @property
    def account_name(self) -> str:
        return self.account

    @property
    def open_count(self) -> int:
        return len(self._by_invocation)

    @property
    def retained_terminal_count(self) -> int:
        return len(self._completed)

    def reserve(self, request: BudgetRequest) -> BudgetReservation | None:
        """Reserve before dispatch. ``None`` means the policy declared it free."""

        existing = self.reservation_for(request.invocation_id)
        if existing is not None:
            if existing.request.run_id != request.run_id:
                raise BudgetStateError(
                    f"invocation id {request.invocation_id!r} already belongs to "
                    f"run {existing.request.run_id!r}",
                    path=f"nodes.{request.node_id}",
                )
            if existing.state in {
                ReservationState.SETTLED,
                ReservationState.RELEASED,
            }:
                raise BudgetStateError(
                    f"logical delivery {request.invocation_id!r} is already "
                    f"{existing.state.value}; refusing to reserve it again",
                    path=f"nodes.{request.node_id}",
                )
            if existing.state is ReservationState.PENDING:
                # A prior attempt was cancelled, abandoned, or failed to settle.
                # Reuse its ceiling; a retry must not create a second reservation.
                return existing
            raise BudgetStateError(
                f"invocation {request.invocation_id!r} already has an active "
                "reservation; concurrent duplicate dispatch is not allowed",
                path=f"nodes.{request.node_id}",
            )

        try:
            amount = self.policy.reserve(request)
        except BudgetError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed at the policy edge
            raise BudgetUnknownCost(
                f"budget policy could not price {request.name!r}: {exc}",
                path=f"nodes.{request.node_id}",
            ) from exc
        amount = _require_amount(amount, path="policy.reserve")
        if amount == 0:
            return None
        base = f"{request.run_id}:{request.invocation_id}"
        try:
            reservation_id = self.ledger.reserve(
                run_id=request.run_id,
                account=self.account,
                amount_milli=amount,
                key=_key(request, "reserve"),
            )
        except BudgetError as exc:
            raise BudgetExceeded(
                f"budget reservation failed for {request.name!r}: {exc}",
                path=f"nodes.{request.node_id}",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - ledger errors are budget errors
            raise BudgetExceeded(
                f"budget reservation failed for {request.name!r}: {exc}",
                path=f"nodes.{request.node_id}",
            ) from exc
        reservation = BudgetReservation(
            request=request,
            base_key=base,
            reservation_id=int(reservation_id),
            reserved_milli=amount,
        )
        self._by_invocation[request.invocation_id] = reservation
        return reservation

    def settle(
        self,
        reservation: BudgetReservation,
        outcome: BudgetOutcome,
    ) -> None:
        """Settle exactly once with the policy's actual cost.

        Any failure leaves the reservation open and marks it pending. The host
        can then find it through :meth:`pending` / :meth:`unresolved` and
        finish it with :meth:`reconcile_invocation`; reserved funds are never
        released by the failure path.
        """

        self._require_open(reservation, "settle")
        try:
            actual = self.policy.settle(reservation.request, outcome)
        except BudgetError:
            self._mark_unresolved(reservation)
            raise
        except Exception as exc:  # noqa: BLE001 - keep the reservation open
            self._mark_unresolved(reservation)
            raise BudgetStateError(
                f"budget policy could not settle {reservation.request.name!r}: {exc}",
                path=f"nodes.{reservation.request.node_id}",
            ) from exc
        try:
            actual = _require_amount(actual, path="policy.settle")
        except BudgetError:
            self._mark_unresolved(reservation)
            raise
        if actual > reservation.reserved_milli:
            self._mark_unresolved(reservation)
            raise BudgetOverrun(
                f"actual cost {actual} exceeds reservation "
                f"{reservation.reserved_milli} for {reservation.request.name!r}; "
                "the reservation stays open for reconciliation",
                path=f"nodes.{reservation.request.node_id}",
            )
        try:
            self._settle_amount(reservation, actual)
        except BudgetError as exc:
            recorded = self._recorded_settlement(reservation)
            if recorded is not None:
                state, recorded_milli = recorded
                reservation.state = state
                reservation.spent_milli = recorded_milli
                self._finish_terminal(reservation)
                raise BudgetStateError(
                    f"logical delivery {reservation.request.invocation_id!r} is "
                    f"already {state.value} at {recorded_milli} milli; refusing "
                    f"a different settlement ({actual})",
                    path=f"nodes.{reservation.request.node_id}",
                ) from exc
            self._mark_unresolved(reservation)
            raise

    def release(self, reservation: BudgetReservation) -> None:
        """Release an unsettled reservation. Idempotent after settle/release."""

        if reservation.state in {ReservationState.SETTLED, ReservationState.RELEASED}:
            return
        try:
            self.ledger.release(
                reservation_id=reservation.reservation_id,
                key=_key(reservation.request, "release"),
            )
        except BudgetError as exc:
            raise BudgetStateError(
                f"budget release failed for {reservation.request.name!r}: {exc}",
                path=f"nodes.{reservation.request.node_id}",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - ledger errors are budget errors
            raise BudgetStateError(
                f"budget release failed for {reservation.request.name!r}: {exc}",
                path=f"nodes.{reservation.request.node_id}",
            ) from exc
        reservation.state = ReservationState.RELEASED
        self._finish_terminal(reservation)

    def mark_pending(self, reservation: BudgetReservation) -> None:
        """Keep funds reserved after cancel/timeout or a failed settlement.

        Marking pending never releases funds. It only makes the held
        reservation visible to :meth:`pending` / :meth:`unresolved` so the host
        can reconcile it later.
        """

        if reservation.state is ReservationState.RESERVED:
            reservation.state = ReservationState.PENDING

    def reconcile(
        self,
        reservation: BudgetReservation,
        *,
        spent_milli: int | None = None,
        release: bool = False,
    ) -> None:
        """Host decision for a pending/late charge.

        ``release=True`` refunds the reservation. Otherwise ``spent_milli`` is
        settled, bounded by the original reservation. Exactly one of the two
        outcomes is applied; repeated calls are idempotent. Only a pending
        reservation can be reconciled: a still-running ``RESERVED`` hold must
        first be marked pending (or be allowed to settle normally), so a host
        cannot settle an in-flight call early and then race the runtime.
        """

        if reservation.state in {ReservationState.SETTLED, ReservationState.RELEASED}:
            return
        if reservation.state is not ReservationState.PENDING:
            raise BudgetStateError(
                "reconcile requires a pending reservation; "
                "a reserved hold must settle normally or be marked pending first",
                path=f"nodes.{reservation.request.node_id}",
            )
        if release:
            self.release(reservation)
            return
        if spent_milli is None:
            raise BudgetStateError(
                "reconcile requires spent_milli or release=True",
                path=f"nodes.{reservation.request.node_id}",
            )
        amount = _require_amount(spent_milli, path="reconcile.spent_milli")
        if amount > reservation.reserved_milli:
            raise BudgetOverrun(
                f"reconciled cost {amount} exceeds reservation "
                f"{reservation.reserved_milli} for {reservation.request.name!r}",
                path=f"nodes.{reservation.request.node_id}",
            )
        self._settle_amount(reservation, amount)

    def reconcile_invocation(
        self,
        invocation_id: str,
        *,
        spent_milli: int | None = None,
        release: bool = False,
    ) -> None:
        """Reconcile by invocation id, without retaining the reservation object.

        During workflow dispatch the host never sees the object returned by
        :meth:`reserve`. This is the public recovery path for a pending hold:
        it looks the reservation up by the invocation id exposed on
        :class:`BudgetRequest`, then applies :meth:`reconcile` exactly once.
        """

        reservation = self.reservation_for(invocation_id)
        if reservation is None:
            raise BudgetStateError(
                f"unknown budget reservation for invocation {invocation_id!r}",
                path="budget",
            )
        self.reconcile(reservation, spent_milli=spent_milli, release=release)

    def reservation_for(self, invocation_id: str) -> BudgetReservation | None:
        """Return the known reservation, including the bounded terminal window."""

        return self._by_invocation.get(invocation_id) or self._completed.get(
            invocation_id
        )

    def pending(self) -> tuple[BudgetReservation, ...]:
        """Holds awaiting a host decision: cancel/timeout or failed settlement."""

        return tuple(
            reservation
            for reservation in self._by_invocation.values()
            if reservation.state is ReservationState.PENDING
        )

    def unresolved(self) -> tuple[BudgetReservation, ...]:
        """Every non-terminal hold, including in-flight and pending settlements."""

        return tuple(
            reservation
            for reservation in self._by_invocation.values()
            if reservation.state
            in {ReservationState.RESERVED, ReservationState.PENDING}
        )

    def mark_pending_invocation(self, invocation_id: str) -> None:
        """Mark an open hold pending; used when the runtime abandons its task."""

        reservation = self._by_invocation.get(invocation_id)
        if reservation is not None:
            self.mark_pending(reservation)

    def restore(self, *, run_id: str | None = None) -> int:
        """Rehydrate reservations from the ledger event log after replay.

        Open reservations become ``PENDING`` so the host can reconcile them.
        Terminal reservations are restored into the bounded terminal window so
        a re-dispatch of a settled logical delivery fails closed before the
        handler runs. Returns the number of open holds restored.
        """

        records: dict[int, dict[str, Any]] = {}
        for event in self.ledger.events():
            if str(event.get("account", "")) != self.account:
                continue
            event_run = str(event.get("run_id", ""))
            if run_id is not None and event_run != run_id:
                continue
            kind = str(event.get("kind", ""))
            reservation_id = event.get("reservation_id")
            if not isinstance(reservation_id, int):
                continue
            if kind == "reserve":
                key = str(event.get("key", ""))
                prefix = f"{event_run}:"
                suffix = ":reserve"
                if not (key.startswith(prefix) and key.endswith(suffix)):
                    continue
                invocation_id = key[len(prefix) : -len(suffix)]
                if not invocation_id:
                    continue
                records[reservation_id] = {
                    "run_id": event_run,
                    "invocation_id": invocation_id,
                    "amount": int(event.get("amount_milli", 0)),
                    "state": ReservationState.PENDING,
                    "spent": 0,
                }
            elif kind == "spend" and reservation_id in records:
                records[reservation_id]["state"] = ReservationState.SETTLED
                records[reservation_id]["spent"] = int(
                    event.get("amount_milli", 0)
                )
            elif kind == "release" and reservation_id in records:
                if records[reservation_id]["state"] is not ReservationState.SETTLED:
                    records[reservation_id]["state"] = ReservationState.RELEASED

        restored = 0
        for reservation_id, record in records.items():
            invocation_id = record["invocation_id"]
            if self.reservation_for(invocation_id) is not None:
                continue
            request = BudgetRequest(
                run_id=record["run_id"],
                invocation_id=invocation_id,
                node_id="",
                kind="restored",
                name="ledger",
                input={},
            )
            reservation = BudgetReservation(
                request=request,
                base_key=f"{record['run_id']}:{invocation_id}",
                reservation_id=reservation_id,
                reserved_milli=record["amount"],
                state=record["state"],
                spent_milli=record["spent"],
            )
            if record["state"] is ReservationState.PENDING:
                self._by_invocation[invocation_id] = reservation
                restored += 1
            else:
                self._remember_terminal(reservation)
        return restored

    def balance(self) -> Mapping[str, int]:
        return self.ledger.balance(account=self.account)

    def events(self) -> Sequence[Mapping[str, Any]]:
        return self.ledger.events()

    def _finish_terminal(self, reservation: BudgetReservation) -> None:
        self._remember_terminal(reservation)

    def _remember_terminal(self, reservation: BudgetReservation) -> None:
        """Drop input and keep only a bounded recent terminal window."""

        invocation_id = reservation.request.invocation_id
        reservation.request = replace(reservation.request, input={})
        self._by_invocation.pop(invocation_id, None)
        self._completed[invocation_id] = reservation
        self._completed.move_to_end(invocation_id)
        while len(self._completed) > self.terminal_capacity:
            self._completed.popitem(last=False)

    def _recorded_settlement(
        self, reservation: BudgetReservation
    ) -> tuple[ReservationState, int] | None:
        settle_key = _key(reservation.request, "settle")
        for event in self.ledger.events():
            if event.get("key") == settle_key and event.get("kind") == "spend":
                return ReservationState.SETTLED, int(event.get("amount_milli", 0))
        release_key = _key(reservation.request, "release")
        for event in self.ledger.events():
            if event.get("key") == release_key and event.get("kind") == "release":
                return ReservationState.RELEASED, 0
        return None

    def _settle_amount(self, reservation: BudgetReservation, spent_milli: int) -> None:
        try:
            self.ledger.settle(
                reservation_id=reservation.reservation_id,
                spent_milli=spent_milli,
                key=_key(reservation.request, "settle"),
            )
        except BudgetError as exc:
            raise BudgetStateError(
                f"budget settle failed for {reservation.request.name!r}: {exc}",
                path=f"nodes.{reservation.request.node_id}",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - ledger errors are budget errors
            raise BudgetStateError(
                f"budget settle failed for {reservation.request.name!r}: {exc}",
                path=f"nodes.{reservation.request.node_id}",
            ) from exc
        reservation.state = ReservationState.SETTLED
        reservation.spent_milli = spent_milli
        self._finish_terminal(reservation)

    @staticmethod
    def _mark_unresolved(reservation: BudgetReservation) -> None:
        """Expose a failed settlement without releasing its reserved funds."""

        if reservation.state is ReservationState.RESERVED:
            reservation.state = ReservationState.PENDING

    @staticmethod
    def _require_open(reservation: BudgetReservation, action: str) -> None:
        if reservation.state not in {
            ReservationState.RESERVED,
            ReservationState.PENDING,
        }:
            raise BudgetStateError(
                f"cannot {action} reservation in state {reservation.state.value!r}",
                path=f"nodes.{reservation.request.node_id}",
            )


class NativeBudgetLedger:
    """Adapter for ``swe_budget.Ledger`` (the Rust ``swe-budget`` crate)."""

    def __init__(self, ledger: Any | None = None) -> None:
        if ledger is None:
            import swe_budget

            ledger = swe_budget.Ledger()
        self._ledger = ledger

    def allocate(
        self,
        *,
        run_id: str,
        account: str,
        amount_milli: int,
        key: str,
    ) -> None:
        try:
            self._ledger.allocate(run_id, account, amount_milli, key)
        except ValueError as exc:
            raise BudgetError(str(exc)) from exc

    def reserve(
        self, *, run_id: str, account: str, amount_milli: int, key: str
    ) -> int:
        try:
            return int(self._ledger.reserve(run_id, account, amount_milli, key))
        except ValueError as exc:
            raise BudgetExceeded(str(exc)) from exc

    def settle(self, *, reservation_id: int, spent_milli: int, key: str) -> None:
        try:
            self._ledger.settle(reservation_id, spent_milli, key)
        except ValueError as exc:
            raise BudgetStateError(str(exc)) from exc

    def release(self, *, reservation_id: int, key: str) -> None:
        try:
            self._ledger.release(reservation_id, key)
        except ValueError as exc:
            raise BudgetStateError(str(exc)) from exc

    def balance(self, *, account: str) -> Mapping[str, int]:
        return dict(self._ledger.balance(account))

    def events(self) -> list[Mapping[str, Any]]:
        return list(self._ledger.events())

    @classmethod
    def replay(cls, events: Sequence[Mapping[str, Any]]) -> NativeBudgetLedger:
        import swe_budget

        return cls(swe_budget.Ledger.replay(list(events)))


__all__ = [
    "BudgetError",
    "BudgetExceeded",
    "BudgetLedger",
    "BudgetOutcome",
    "BudgetOverrun",
    "BudgetPolicy",
    "BudgetRequest",
    "BudgetReservation",
    "BudgetStateError",
    "BudgetUnknownCost",
    "NativeBudgetLedger",
    "ReservationState",
    "RuntimeBudget",
]
