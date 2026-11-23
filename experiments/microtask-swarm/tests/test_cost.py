from __future__ import annotations

import unittest
from decimal import Decimal

from microtask_swarm.cost import (
    CostError,
    TokenUsage,
    account_calls,
    price_call,
)

from tests.support import load_test_config


def _tokens(**overrides):
    values = {
        "input": 1000,
        "output": 500,
        "reasoning": 200,
        "cache_read": 400,
        "cache_write": 0,
        "source": "provider",
    }
    values.update(overrides)
    return values


class CostTests(unittest.TestCase):
    def setUp(self) -> None:
        config = load_test_config()
        self.models = {model["id"]: model for model in config["models"]}
        self.gpt = self.models["gpt-oss-20b"]
        self.deepseek = self.models["deepseek-v4.1-flash"]

    def test_cache_is_priced_on_its_own_line(self) -> None:
        usage = TokenUsage(input=1000, output=500, reasoning=200, cache_read=400)
        priced = price_call(self.gpt, usage, period=None)
        self.assertEqual(priced["input"], Decimal("0.000042"))
        self.assertEqual(priced["cache_read"], Decimal("0.000014"))
        self.assertEqual(priced["output"], Decimal("0.00015"))

    def test_deepseek_requires_peak_or_off_peak(self) -> None:
        usage = TokenUsage(input=1000, output=500, reasoning=0, cache_read=0)
        self.assertEqual(price_call(self.deepseek, usage, period=None)["input"], None)
        peak = price_call(self.deepseek, usage, period="peak")
        off = price_call(self.deepseek, usage, period="off_peak")
        self.assertEqual(peak["input"], Decimal("0.0003"))
        self.assertEqual(off["input"], Decimal("0.00015"))
        self.assertEqual(peak["output"], Decimal("0.0006"))
        self.assertEqual(off["output"], Decimal("0.0003"))

    def test_reasoning_is_recorded_but_not_independently_priced(self) -> None:
        usage = TokenUsage(input=100, output=50, reasoning=999)
        priced = price_call(self.gpt, usage, period=None)
        self.assertNotIn("reasoning", priced)
        self.assertEqual(priced["output"], Decimal("0.000015"))

    def test_actual_requires_every_call_to_report_a_charge(self) -> None:
        calls = [
            {
                "model_id": "gpt-oss-20b",
                "role": "candidate",
                "attempt": 1,
                "latency_ms": 10,
                "tokens": _tokens(),
                "billed_usd": "0.01",
                "price_period": "peak",
            },
            {
                "model_id": "gpt-oss-20b",
                "role": "candidate",
                "attempt": 2,
                "latency_ms": 12,
                "tokens": _tokens(),
                "billed_usd": "0.02",
                "price_period": "peak",
            },
        ]
        cost = account_calls(calls, models=self.models, verified_evidence=2)
        self.assertEqual(cost["all_in"]["actual_usd"], "0.03")
        self.assertEqual(cost["all_in"]["retries"], 1)
        self.assertEqual(cost["all_in"]["calls"], 2)
        self.assertEqual(cost["usd_per_verified_evidence"], "0.015")

        calls[1]["billed_usd"] = None
        cost = account_calls(calls, models=self.models)
        self.assertEqual(cost["all_in"]["actual_usd"], "N/A")
        self.assertEqual(cost["usd_per_verified_evidence"], "N/A")

    def test_calculated_and_actual_are_distinct(self) -> None:
        calls = [
            {
                "model_id": "gpt-oss-20b",
                "role": "candidate",
                "attempt": 1,
                "latency_ms": 10,
                "tokens": _tokens(),
                "billed_usd": "9.99",
                "price_period": "peak",
            }
        ]
        cost = account_calls(calls, models=self.models)
        self.assertEqual(cost["all_in"]["actual_usd"], "9.99")
        self.assertNotEqual(cost["all_in"]["calculated_usd"], "9.99")

    def test_verifier_calls_are_included_in_all_in(self) -> None:
        calls = [
            {
                "model_id": "gpt-oss-20b",
                "role": "candidate",
                "attempt": 1,
                "latency_ms": 10,
                "tokens": _tokens(),
                "billed_usd": "0.01",
            },
            {
                "model_id": "gpt-oss-20b",
                "role": "verifier",
                "attempt": 1,
                "latency_ms": 5,
                "tokens": _tokens(),
                "billed_usd": "0.02",
            },
        ]
        cost = account_calls(calls, models=self.models)
        self.assertEqual(cost["candidate"]["calls"], 1)
        self.assertEqual(cost["verifier"]["calls"], 1)
        self.assertEqual(cost["all_in"]["calls"], 2)
        self.assertEqual(cost["all_in"]["actual_usd"], "0.03")

    def test_unknown_usage_keeps_all_in_calculated_na_but_reports_complete_subset(self) -> None:
        calls = [
            {
                "model_id": "gpt-oss-20b",
                "role": "candidate",
                "attempt": 1,
                "latency_ms": 10,
                "tokens": _tokens(),
            },
            {
                "model_id": "gpt-oss-20b",
                "role": "candidate",
                "attempt": 1,
                "latency_ms": 1,
                "tokens": {
                    "input": None,
                    "output": None,
                    "reasoning": None,
                    "cache_read": None,
                    "cache_write": None,
                    "source": "provider",
                },
            },
        ]
        cost = account_calls(calls, models=self.models)
        self.assertEqual(cost["all_in"]["calculated_usd"], "N/A")
        self.assertNotEqual(
            cost["all_in_complete_calls_calculated_usd"], "N/A"
        )
        self.assertEqual(cost["all_in"]["calls_with_unknown_usage"], 1)
        self.assertEqual(cost["all_in"]["calls_with_complete_usage"], 1)

    def test_cache_larger_than_input_is_rejected(self) -> None:
        usage = TokenUsage(input=10, output=1, cache_read=20)
        with self.assertRaises(CostError):
            price_call(self.gpt, usage, period=None)

    def test_unknown_model_is_rejected(self) -> None:
        calls = [
            {
                "model_id": "missing",
                "role": "candidate",
                "attempt": 1,
                "latency_ms": 1,
                "tokens": _tokens(),
            }
        ]
        with self.assertRaises(CostError):
            account_calls(calls, models=self.models)


if __name__ == "__main__":
    unittest.main()
