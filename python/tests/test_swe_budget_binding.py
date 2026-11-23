"""Native ledger binding and the generic runtime budget state machine."""

from __future__ import annotations

import pytest

pytest.importorskip("swe_native")

from swe.budget import (
    BudgetOutcome,
    BudgetOverrun,
    BudgetRequest,
    BudgetStateError,
    BudgetUnknownCost,
    NativeBudgetLedger,
    RuntimeBudget,
    ReservationState,
)

pytestmark = pytest.mark.swe_runtime


class _Policy:
    def __init__(self, reserve_milli: int, settle_milli: int) -> None:
        self.reserve_milli = reserve_milli
        self.settle_milli = settle_milli

    def reserve(self, request: BudgetRequest) -> int:
        return self.reserve_milli

    def settle(self, request: BudgetRequest, outcome: BudgetOutcome) -> int:
        return self.settle_milli


def _request(invocation_id: str = "inv-1") -> BudgetRequest:
    return BudgetRequest(
        run_id="run-1",
        invocation_id=invocation_id,
        node_id="work",
        kind="handler",
        name="demo.work",
        input={},
    )


def _funded(amount_milli: int = 1_000) -> NativeBudgetLedger:
    ledger = NativeBudgetLedger()
    ledger.allocate(
        run_id="run-1",
        account="a",
        amount_milli=amount_milli,
        key="alloc",
    )
    return ledger


def test_native_ledger_round_trip_and_replay() -> None:
    ledger = _funded()
    reservation = ledger.reserve(
        run_id="run-1", account="a", amount_milli=300, key="reserve-1"
    )
    ledger.settle(reservation_id=reservation, spent_milli=200, key="settle-1")

    balance = ledger.balance(account="a")
    assert balance == {
        "allocated": 1_000,
        "incoming": 0,
        "outgoing": 0,
        "spent": 200,
        "reserved": 0,
        "available": 800,
    }
    assert ledger.reserve(
        run_id="run-1", account="a", amount_milli=300, key="reserve-1"
    ) == reservation

    replayed = NativeBudgetLedger.replay(ledger.events())
    assert replayed.balance(account="a") == balance
    assert list(replayed.events()) == list(ledger.events())


def test_runtime_budget_reserves_before_and_settles_once() -> None:
    ledger = _funded(100)
    budget = RuntimeBudget(ledger, _Policy(reserve_milli=10, settle_milli=8), account="a")
    reservation = budget.reserve(_request())
    assert reservation is not None
    assert reservation.reserved_milli == 10
    assert ledger.balance(account="a")["reserved"] == 10
    assert ledger.balance(account="a")["available"] == 90

    budget.settle(reservation, BudgetOutcome(ok=True, value={"ok": True}))
    assert reservation.state is ReservationState.SETTLED
    assert ledger.balance(account="a")["spent"] == 8
    assert ledger.balance(account="a")["reserved"] == 0
    assert ledger.balance(account="a")["available"] == 92

    with pytest.raises(BudgetStateError):
        budget.settle(reservation, BudgetOutcome(ok=True))
    budget.release(reservation)  # idempotent no-op after settle
    assert ledger.balance(account="a")["spent"] == 8


def test_runtime_budget_retry_reuses_the_same_key_without_double_charge() -> None:
    ledger = _funded(100)
    budget = RuntimeBudget(ledger, _Policy(reserve_milli=10, settle_milli=10), account="a")

    first = budget.reserve(_request())
    assert first is not None
    budget.settle(first, BudgetOutcome(ok=True, value={"attempt": 1}))

    # A host-level retry of the same logical invocation: same key, no new charge.
    retry = budget.reserve(_request())
    assert retry is not None
    assert retry.reservation_id == first.reservation_id
    budget.settle(retry, BudgetOutcome(ok=True, value={"attempt": 2}))

    assert ledger.balance(account="a")["spent"] == 10
    assert ledger.balance(account="a")["reserved"] == 0
    assert [
        event["kind"] for event in ledger.events() if event["kind"] == "spend"
    ] == ["spend"]


def test_unknown_cost_fails_closed_before_reservation() -> None:
    class UnknownPolicy(_Policy):
        def reserve(self, request: BudgetRequest) -> int:
            raise BudgetUnknownCost("provider has no price")

    ledger = _funded(100)
    budget = RuntimeBudget(ledger, UnknownPolicy(10, 10), account="a")
    with pytest.raises(BudgetUnknownCost):
        budget.reserve(_request())
    assert ledger.balance(account="a")["reserved"] == 0
    assert ledger.balance(account="a")["available"] == 100


def test_overrun_keeps_the_reservation_open_for_reconciliation() -> None:
    ledger = _funded(100)
    budget = RuntimeBudget(ledger, _Policy(reserve_milli=10, settle_milli=11), account="a")
    reservation = budget.reserve(_request())
    assert reservation is not None

    with pytest.raises(BudgetOverrun):
        budget.settle(reservation, BudgetOutcome(ok=True))
    assert ledger.balance(account="a")["reserved"] == 10
    assert ledger.balance(account="a")["spent"] == 0

    budget.reconcile(reservation, spent_milli=10)
    assert ledger.balance(account="a")["reserved"] == 0
    assert ledger.balance(account="a")["spent"] == 10
