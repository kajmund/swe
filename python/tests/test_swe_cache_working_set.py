"""Characterize working-set reuse that the hot-key benchmark cannot show.

Audit: ``docs/analysis/2026-11-25-rea-budget-audit.md`` (finding A).

``benchmark_exact_cache`` primes one key and then repeats that same key, so its
"10,000 warm hits" measure hot-key throughput, not reuse over a working set
(``python/swe/cache.py:724,752-754``; ``docs/specs/swe-exact-cache.md``). This
test makes the difference concrete with the reference LRU L1: a working set
that fits is fully reusable, while a sequential working set larger than the L1
capacity gets no second-pass hits at all. A frozen step clock makes the LRU
order independent of platform clock resolution.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from swe.cache import InMemoryDecisionCache
from swe.runtime_clock import FrozenClock


async def _run_pass(
    cache: InMemoryDecisionCache,
    clock: FrozenClock,
    working_set: int,
) -> None:
    async def compute(index: int) -> Mapping[str, Any]:
        return {"choice": "yes", "ordinal": index}

    for index in range(working_set):
        clock.advance(1)
        await cache.resolve(
            f"working-set:{index}",
            lambda idx=index: compute(idx),
            provider="bench.provider",
            prompt_key="bench.prompt",
        )


async def test_working_set_that_fits_l1_is_fully_reused() -> None:
    working_set = 64
    clock = FrozenClock(now=0)
    cache = InMemoryDecisionCache(max_entries=working_set, clock=clock)

    await _run_pass(cache, clock, working_set)
    after_cold = cache.metrics()
    await _run_pass(cache, clock, working_set)
    after_warm = cache.metrics()

    assert after_cold.hits == 0
    assert after_cold.computes == working_set
    assert after_warm.hits == working_set
    assert after_warm.evictions == 0
    assert after_warm.entries == working_set


async def test_sequential_working_set_larger_than_l1_gets_no_warm_hits() -> None:
    max_entries = 64
    working_set = 256
    clock = FrozenClock(now=0)
    cache = InMemoryDecisionCache(max_entries=max_entries, clock=clock)

    await _run_pass(cache, clock, working_set)
    await _run_pass(cache, clock, working_set)
    metrics = cache.metrics()

    # Sequential LRU: each miss in the second pass evicts the entry that the
    # later part of the first pass would have reused. The hot-key benchmark
    # never exercises this shape, so its warm count must not be read as a
    # working-set hit rate.
    assert metrics.hits == 0
    assert metrics.computes == 2 * working_set
    assert metrics.evictions == 2 * working_set - max_entries
    assert metrics.entries == max_entries
