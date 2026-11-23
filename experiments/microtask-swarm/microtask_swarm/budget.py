"""Small host-side Sweycoin meter with the core ledger's integer semantics.

The SWE core ledger lives in Rust (``crates/swe-budget``) and is not exported by
the Python extension in this environment. This meter mirrors the invariants the
experiment needs: integer milli-Sweycoins, checked reserve, settle-at-most-once,
release of the unspent remainder, idempotency keys, and no float amounts. It is
a host accounting adapter for the pilot, not a replacement core ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


class BudgetError(ValueError):
    """A Sweycoin reservation or settlement violates an invariant."""


@dataclass(frozen=True, slots=True)
class Reservation:
    reservation_id: int
    key: str
    amount_milli: int
    settled_milli: int | None = None
    released: bool = False


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    key: str
    amount_milli: int
    reservation_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "amount_milli": self.amount_milli,
            "reservation_id": self.reservation_id,
        }


class SweycoinMeter:
    def __init__(self, allocated_milli: int) -> None:
        if isinstance(allocated_milli, bool) or not isinstance(allocated_milli, int):
            raise BudgetError("allocated_milli must be an integer")
        if allocated_milli < 0:
            raise BudgetError("allocated_milli must be non-negative")
        self._allocated = allocated_milli
        self._spent = 0
        self._reserved = 0
        self._reservations: dict[int, Reservation] = {}
        self._keys: dict[str, tuple[str, int]] = {}
        self._events: list[Event] = []
        self._next_id = 1

    @property
    def allocated_milli(self) -> int:
        return self._allocated

    @property
    def spent_milli(self) -> int:
        return self._spent

    @property
    def reserved_milli(self) -> int:
        return self._reserved

    @property
    def available_milli(self) -> int:
        return self._allocated - self._spent - self._reserved

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def _check_amount(self, amount_milli: int) -> int:
        if isinstance(amount_milli, bool) or not isinstance(amount_milli, int):
            raise BudgetError("amount must be an integer milli-Sweycoin value")
        if amount_milli < 0:
            raise BudgetError("amount must be non-negative")
        return amount_milli

    def _remember(self, key: str, kind: str, amount: int) -> None:
        previous = self._keys.get(key)
        if previous is not None and previous != (kind, amount):
            raise BudgetError(f"idempotency key {key!r} reused with different parameters")
        self._keys[key] = (kind, amount)

    def reserve(self, key: str, amount_milli: int) -> int:
        amount = self._check_amount(amount_milli)
        previous = self._keys.get(key)
        if previous is not None:
            if previous != ("reserve", amount):
                raise BudgetError(
                    f"idempotency key {key!r} reused with different parameters"
                )
            for reservation in self._reservations.values():
                if reservation.key == key:
                    return reservation.reservation_id
        if amount > self.available_milli:
            raise BudgetError(
                f"insufficient funds: available {self.available_milli}, requested {amount}"
            )
        reservation_id = self._next_id
        self._next_id += 1
        self._keys[key] = ("reserve", amount)
        self._reservations[reservation_id] = Reservation(
            reservation_id=reservation_id,
            key=key,
            amount_milli=amount,
        )
        self._reserved += amount
        self._events.append(Event("reserve", key, amount, reservation_id))
        return reservation_id

    def settle(self, reservation_id: int, amount_milli: int) -> int:
        amount = self._check_amount(amount_milli)
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            raise BudgetError(f"unknown reservation {reservation_id}")
        if reservation.settled_milli is not None or reservation.released:
            raise BudgetError(f"reservation {reservation_id} is already closed")
        if amount > reservation.amount_milli:
            raise BudgetError(
                f"overspend: reserved {reservation.amount_milli}, requested {amount}"
            )
        closed = Reservation(
            reservation_id=reservation.reservation_id,
            key=reservation.key,
            amount_milli=reservation.amount_milli,
            settled_milli=amount,
        )
        self._reservations[reservation_id] = closed
        self._reserved -= reservation.amount_milli
        self._spent += amount
        self._events.append(Event("settle", reservation.key, amount, reservation_id))
        return amount

    def release(self, reservation_id: int) -> None:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            raise BudgetError(f"unknown reservation {reservation_id}")
        if reservation.settled_milli is not None or reservation.released:
            raise BudgetError(f"reservation {reservation_id} is already closed")
        self._reservations[reservation_id] = Reservation(
            reservation_id=reservation.reservation_id,
            key=reservation.key,
            amount_milli=reservation.amount_milli,
            released=True,
        )
        self._reserved -= reservation.amount_milli
        self._events.append(
            Event("release", reservation.key, 0, reservation_id)
        )

    def charge(self, key: str, amount_milli: int) -> None:
        """Charge a non-reserved action, such as a deterministic verifier."""

        amount = self._check_amount(amount_milli)
        previous = self._keys.get(key)
        if previous is not None:
            if previous != ("charge", amount):
                raise BudgetError(
                    f"idempotency key {key!r} reused with different parameters"
                )
            return
        if amount > self.available_milli:
            raise BudgetError(
                f"insufficient funds: available {self.available_milli}, requested {amount}"
            )
        self._keys[key] = ("charge", amount)
        self._spent += amount
        self._events.append(Event("charge", key, amount))

    def to_dict(self) -> dict[str, Any]:
        return {
            "allocated_milli": self._allocated,
            "spent_milli": self._spent,
            "reserved_milli": self._reserved,
            "available_milli": self.available_milli,
            "event_count": len(self._events),
            "reservations": len(self._reservations),
        }


def replay_events(
    events: Iterable[Mapping[str, Any] | Event], *, allocated_milli: int
) -> SweycoinMeter:
    """Rebuild a meter from its event log for tests and reports."""

    meter = SweycoinMeter(allocated_milli)
    for raw in events:
        event = raw if isinstance(raw, Event) else Event(**raw)
        if event.kind == "reserve":
            meter.reserve(event.key, event.amount_milli)
        elif event.kind == "settle":
            if event.reservation_id is None:
                raise BudgetError("settle event needs a reservation id")
            meter.settle(event.reservation_id, event.amount_milli)
        elif event.kind == "release":
            if event.reservation_id is None:
                raise BudgetError("release event needs a reservation id")
            meter.release(event.reservation_id)
        elif event.kind == "charge":
            meter.charge(event.key, event.amount_milli)
        else:
            raise BudgetError(f"unknown event kind {event.kind!r}")
    return meter


__all__ = [
    "BudgetError",
    "Event",
    "Reservation",
    "SweycoinMeter",
    "replay_events",
]
