"""Committed 10k-operation quality gate for ActionOffer and exact cache."""

from __future__ import annotations

import pytest

from swe.cache import benchmark_exact_cache
from swe.offers import benchmark_action_offers

pytest.importorskip("swe_native")

pytestmark = pytest.mark.swe_runtime


async def test_ten_thousand_synthetic_offer_and_cache_operations() -> None:
    cache = await benchmark_exact_cache(
        operations=10_000,
        concurrency=64,
        min_throughput=100.0,
    )
    assert cache.operations == 10_000
    assert cache.warm.hits >= 10_000
    assert 1 <= cache.peak_in_flight <= 64
    assert cache.bytes_used <= cache.max_bytes
    assert cache.throughput_ops_per_second >= 100.0

    offers = await benchmark_action_offers(
        operations=10_000,
        concurrency=64,
        min_throughput=50.0,
    )
    assert offers.unique_offer_ids == 10_000
    assert offers.approved == 10_000
    assert offers.reused == 10_000
    assert 1 <= offers.peak_in_flight <= 64
    assert offers.throughput_ops_per_second >= 50.0
