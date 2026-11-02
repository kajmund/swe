"""Exact in-memory DecisionCache: hits, isolation, cancellation, off-mode."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from swe.cache import (
    InMemoryDecisionCache,
    decision_cache_key,
    scoped_cache_params,
)
from swe.definition import compile_definition
from swe.runtime import WorkflowResources, run_workflow
from swe.runtime_clock import FrozenClock
from swe.runtime_errors import SweHandlerError


def test_v1_decision_cache_key_is_stable() -> None:
    assert _key() == "9a90454ba4d09100657a703f0266066c217761dd1cc6156b3c9f7775dd6ea268"
    shuffled = _key(input_data={"topic": "alpha"})
    assert shuffled == _key()


def _key(**overrides: Any) -> str:
    payload = {
        "provider": "mock.decision",
        "prompt_key": "demo.assessment",
        "prompt": "choose {topic}",
        "input_data": {"topic": "alpha"},
        "choices": ("yes", "no"),
        "params": scoped_cache_params(
            auth_scope="tenant-a",
            policy_version="pol-1",
            model="m1",
            provider_version="1",
        ),
    }
    payload.update(overrides)
    return decision_cache_key(**payload)


async def test_exact_hit_and_miss_and_key_separation() -> None:
    cache = InMemoryDecisionCache()
    computes: list[str] = []

    async def compute(label: str) -> Mapping[str, Any]:
        computes.append(label)
        return {"choice": "yes", "label": label}

    first, hit = await cache.resolve(
        _key(),
        lambda: compute("first"),
        provider="mock.decision",
        prompt_key="demo.assessment",
    )
    second, second_hit = await cache.resolve(
        _key(),
        lambda: compute("again"),
        provider="mock.decision",
        prompt_key="demo.assessment",
    )
    other, _ = await cache.resolve(
        _key(input_data={"topic": "beta"}),
        lambda: compute("other"),
        provider="mock.decision",
        prompt_key="demo.assessment",
    )
    scoped = _key(
        params=scoped_cache_params(
            auth_scope="tenant-b",
            policy_version="pol-1",
            model="m1",
            provider_version="1",
        )
    )
    await cache.resolve(
        scoped,
        lambda: compute("scope"),
        provider="mock.decision",
        prompt_key="demo.assessment",
    )
    assert hit is False
    assert second_hit is True
    assert first["label"] == "first"
    assert second["label"] == "first"
    assert other["label"] == "other"
    assert computes == ["first", "other", "scope"]
    metrics = cache.metrics()
    assert metrics.hits == 1
    assert metrics.misses == 3
    assert metrics.computes == 3


async def test_ttl_and_deterministic_eviction() -> None:
    clock = FrozenClock(now=0)
    cache = InMemoryDecisionCache(max_entries=2, ttl_ms=10, clock=clock)

    async def compute(label: str) -> Mapping[str, Any]:
        return {"choice": "yes", "label": label}

    await cache.resolve("a", lambda: compute("a"), provider="p", prompt_key="k")
    clock.advance(1)
    await cache.resolve("b", lambda: compute("b"), provider="p", prompt_key="k")
    clock.advance(1)
    await cache.resolve("c", lambda: compute("c"), provider="p", prompt_key="k")
    metrics = cache.metrics()
    assert metrics.entries == 2
    assert metrics.evictions == 1
    clock.advance(10)
    missed, hit = await cache.resolve(
        "b",
        lambda: compute("b2"),
        provider="p",
        prompt_key="k",
    )
    assert hit is False
    assert missed["label"] == "b2"
    assert cache.metrics().expiries >= 1


async def test_single_flight_coalesces_high_concurrency() -> None:
    cache = InMemoryDecisionCache()
    started = asyncio.Event()
    release = asyncio.Event()
    computes = 0

    async def compute() -> Mapping[str, Any]:
        nonlocal computes
        computes += 1
        started.set()
        await release.wait()
        return {"choice": "yes", "n": computes}

    async def resolve() -> tuple[Mapping[str, Any], bool]:
        return await cache.resolve(
            "shared",
            compute,
            provider="p",
            prompt_key="k",
        )

    tasks = [asyncio.create_task(resolve()) for _ in range(64)]
    await started.wait()
    release.set()
    results = await asyncio.gather(*tasks)
    answers = [answer["n"] for answer, _hit in results]
    assert computes == 1
    assert answers == [1] * 64
    metrics = cache.metrics()
    assert metrics.computes == 1
    assert metrics.coalesced == 63
    assert metrics.misses == 1


async def test_failure_timeout_and_invalid_choice_are_not_cached() -> None:
    cache = InMemoryDecisionCache()

    async def boom() -> Mapping[str, Any]:
        raise SweHandlerError("provider failed")

    async def timeout() -> Mapping[str, Any]:
        raise TimeoutError("provider timed out")

    async def invalid() -> Mapping[str, Any]:
        return {"choice": ""}

    with pytest.raises(SweHandlerError, match="provider failed"):
        await cache.resolve("err", boom, provider="p", prompt_key="k")
    with pytest.raises(TimeoutError):
        await cache.resolve("to", timeout, provider="p", prompt_key="k")
    with pytest.raises(SweHandlerError, match="without a string choice"):
        await cache.resolve("bad", invalid, provider="p", prompt_key="k")

    computes = 0

    async def ok() -> Mapping[str, Any]:
        nonlocal computes
        computes += 1
        return {"choice": "yes"}

    await cache.resolve("err", ok, provider="p", prompt_key="k")
    await cache.resolve("to", ok, provider="p", prompt_key="k")
    assert computes == 2
    assert cache.metrics().errors >= 3
    assert cache.metrics().entries == 2


async def test_cancelled_waiter_does_not_corrupt_or_pin_a_flight() -> None:
    cache = InMemoryDecisionCache()
    started = asyncio.Event()
    release = asyncio.Event()
    computes = 0

    async def compute() -> Mapping[str, Any]:
        nonlocal computes
        computes += 1
        started.set()
        await release.wait()
        return {"choice": "yes"}

    keeper = asyncio.create_task(
        cache.resolve("k", compute, provider="p", prompt_key="k")
    )
    waiter = asyncio.create_task(
        cache.resolve("k", compute, provider="p", prompt_key="k")
    )
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    answer, hit = await keeper
    assert answer["choice"] == "yes"
    assert hit is False
    assert computes == 1
    cached, cached_hit = await cache.resolve(
        "k",
        compute,
        provider="p",
        prompt_key="k",
    )
    assert cached_hit is True
    assert cached["choice"] == "yes"

    lone_started = asyncio.Event()

    async def hanging() -> Mapping[str, Any]:
        lone_started.set()
        await asyncio.Event().wait()
        return {"choice": "yes"}

    lone = asyncio.create_task(
        cache.resolve("solo", hanging, provider="p", prompt_key="k")
    )
    await lone_started.wait()
    lone.cancel()
    with pytest.raises(asyncio.CancelledError):
        await lone
    await asyncio.sleep(0)
    assert cache.metrics().entries == 1
    assert cache.active_flight_count == 0


async def test_cache_policy_off_does_not_read_or_write() -> None:
    pytest.importorskip("swe_native")
    cache = _CountingCache()
    compiled = _provider_definition("off")
    calls = 0

    async def provider(_request: Mapping[str, Any]) -> str:
        nonlocal calls
        calls += 1
        return "yes"

    result = await run_workflow(
        compiled,
        {},
        {"topic": "alpha"},
        resources=WorkflowResources(
            providers={"mock.decision": provider},
            prompts={"demo.assessment": "choose {topic}"},
            prompt_keys=frozenset({"demo.assessment"}),
            cache=cache,
        ),
    )
    assert result.status == "completed"
    assert calls == 1
    assert cache.reads == 0
    assert cache.writes == 0


async def test_exact_policy_uses_l1_and_does_not_recompute() -> None:
    pytest.importorskip("swe_native")
    cache = InMemoryDecisionCache()
    compiled = _provider_definition("exact")
    calls = 0

    async def provider(request: Mapping[str, Any]) -> str:
        nonlocal calls
        calls += 1
        assert request["cache_policy"] == "exact"
        return "yes"

    provider.cache_params = scoped_cache_params(  # type: ignore[attr-defined]
        auth_scope="tenant-a",
        model="m1",
        policy_version="pol-1",
    )
    resources = WorkflowResources(
        providers={"mock.decision": provider},
        prompts={"demo.assessment": "choose {topic}"},
        prompt_keys=frozenset({"demo.assessment"}),
        cache=cache,
    )
    first = await run_workflow(compiled, {}, {"topic": "alpha"}, resources=resources)
    second = await run_workflow(compiled, {}, {"topic": "alpha"}, resources=resources)
    assert first.status == "completed"
    assert second.status == "completed"
    assert first.output["decide"]["cache"] == "miss"
    assert second.output["decide"]["cache"] == "hit"
    assert calls == 1


def _provider_definition(policy: str):
    return compile_definition(
        {
            "version": "1.0",
            "name": "cached",
            "entrypoint": "decide",
            "limits": {"max_steps": 8, "max_depth": 1, "timeout_seconds": 30},
            "nodes": {
                "decide": {
                    "type": "decision",
                    "evaluator": "provider",
                    "provider": "mock.decision",
                    "prompt_key": "demo.assessment",
                    "input": {"topic": "$.topic"},
                    "choices": ["yes", "no"],
                    "routes": {"yes": "done", "no": "done"},
                    "cache_policy": policy,
                },
                "done": {"type": "end", "status": "completed"},
            },
        },
        providers=frozenset({"mock.decision"}),
        prompt_keys=frozenset({"demo.assessment"}),
    )


class _CountingCache:
    def __init__(self) -> None:
        self.reads = 0
        self.writes = 0

    async def resolve(
        self,
        key: str,
        compute,
        *,
        provider: str,
        prompt_key: str,
    ) -> tuple[Mapping[str, Any], bool]:
        del key, provider, prompt_key
        self.reads += 1
        self.writes += 1
        return await compute(), False
