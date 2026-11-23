"""Generic runtime budget contract backed by the Rust ``swe-budget`` ledger.

The runtime never invents a second ledger. ``RuntimeBudget`` composes a host
``BudgetLedger`` (the PyO3 binding ``swe_native.Ledger`` in the reference
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
prevent a second reservation or charge.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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

    ``swe_native.Ledger`` is wrapped by :class:`NativeBudgetLedger`; a host may
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


class RuntimeBudget:
    """Compose a ledger and a policy; enforce the runtime reservation protocol."""

    def __init__(
        self,
        ledger: BudgetLedger,
        policy: BudgetPolicy,
        *,
        account: str = "default",
    ) -> None:
        if not isinstance(account, str) or not account.strip():
            raise BudgetError("account must be a non-empty string", path="account")
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
        self._by_invocation: dict[str, BudgetReservation] = {}

    @property
    def account_name(self) -> str:
        return self.account

    def reserve(self, request: BudgetRequest) -> BudgetReservation | None:
        """Reserve before dispatch. ``None`` means the policy declared it free."""

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
        """Settle exactly once with the policy's actual cost."""

        self._require_open(reservation, "settle")
        try:
            actual = self.policy.settle(reservation.request, outcome)
        except BudgetError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the reservation open
            raise BudgetStateError(
                f"budget policy could not settle {reservation.request.name!r}: {exc}",
                path=f"nodes.{reservation.request.node_id}",
            ) from exc
        actual = _require_amount(actual, path="policy.settle")
        if actual > reservation.reserved_milli:
            raise BudgetOverrun(
                f"actual cost {actual} exceeds reservation "
                f"{reservation.reserved_milli} for {reservation.request.name!r}; "
                "the reservation stays open for reconciliation",
                path=f"nodes.{reservation.request.node_id}",
            )
        self._settle_amount(reservation, actual)

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

    def mark_pending(self, reservation: BudgetReservation) -> None:
        """Keep funds reserved after cancel/timeout; never release early."""

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
        outcomes is applied; repeated calls are idempotent.
        """

        if reservation.state in {ReservationState.SETTLED, ReservationState.RELEASED}:
            return
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

    def pending(self) -> tuple[BudgetReservation, ...]:
        return tuple(
            reservation
            for reservation in self._by_invocation.values()
            if reservation.state is ReservationState.PENDING
        )

    def balance(self) -> Mapping[str, int]:
        return self.ledger.balance(account=self.account)

    def events(self) -> Sequence[Mapping[str, Any]]:
        return self.ledger.events()

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
    """Adapter for ``swe_native.Ledger`` (the Rust ``swe-budget`` crate)."""

    def __init__(self, ledger: Any | None = None) -> None:
        if ledger is None:
            import swe_native

            ledger = swe_native.Ledger()
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
        import swe_native

        return cls(swe_native.Ledger.replay(list(events)))


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
