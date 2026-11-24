from __future__ import annotations

import unittest

from microtask_swarm.budget import BudgetError, SweycoinMeter, replay_events


class BudgetTests(unittest.TestCase):
    def test_reserve_settle_and_release(self) -> None:
        meter = SweycoinMeter(1000)
        first = meter.reserve("task:a:1", 300)
        second = meter.reserve("task:a:2", 200)
        self.assertEqual(meter.available_milli, 500)
        self.assertEqual(meter.settle(first, 250), 250)
        self.assertEqual(meter.spent_milli, 250)
        self.assertEqual(meter.reserved_milli, 200)
        meter.release(second)
        self.assertEqual(meter.available_milli, 750)
        self.assertEqual(meter.spent_milli, 250)

    def test_insufficient_funds_is_rejected(self) -> None:
        meter = SweycoinMeter(100)
        with self.assertRaises(BudgetError):
            meter.reserve("task:a:1", 101)
        self.assertEqual(meter.available_milli, 100)

    def test_settle_cannot_exceed_reservation_or_repeat(self) -> None:
        meter = SweycoinMeter(1000)
        reservation = meter.reserve("task:a:1", 100)
        with self.assertRaises(BudgetError):
            meter.settle(reservation, 101)
        meter.settle(reservation, 100)
        with self.assertRaises(BudgetError):
            meter.settle(reservation, 1)

    def test_idempotency_key_rejects_different_parameters(self) -> None:
        meter = SweycoinMeter(1000)
        first = meter.reserve("task:a:1", 100)
        second = meter.reserve("task:a:1", 100)
        self.assertEqual(first, second)
        with self.assertRaises(BudgetError):
            meter.reserve("task:a:1", 200)

    def test_retries_are_separate_charges(self) -> None:
        meter = SweycoinMeter(1000)
        first = meter.reserve("task:a:attempt-1", 250)
        meter.settle(first, 250)
        second = meter.reserve("task:a:attempt-2", 250)
        meter.settle(second, 250)
        self.assertEqual(meter.spent_milli, 500)

    def test_charge_is_budget_checked_and_idempotent(self) -> None:
        meter = SweycoinMeter(500)
        meter.charge("verifier:1", 500)
        meter.charge("verifier:1", 500)
        self.assertEqual(meter.spent_milli, 500)
        with self.assertRaises(BudgetError):
            meter.charge("verifier:2", 1)

    def test_float_amounts_are_rejected(self) -> None:
        meter = SweycoinMeter(1000)
        with self.assertRaises(BudgetError):
            meter.reserve("task:a:1", 1.5)  # type: ignore[arg-type]

    def test_replay_reconstructs_state(self) -> None:
        meter = SweycoinMeter(1000)
        reservation = meter.reserve("task:a:1", 300)
        meter.settle(reservation, 250)
        replay = replay_events(meter.events, allocated_milli=1000)
        self.assertEqual(replay.spent_milli, meter.spent_milli)
        self.assertEqual(replay.reserved_milli, meter.reserved_milli)
        self.assertEqual(replay.available_milli, meter.available_milli)


if __name__ == "__main__":
    unittest.main()
