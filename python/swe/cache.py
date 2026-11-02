"""Exact decision-cache contract for provider decisions.

The engine owns the cache identity and the interface. Host storage adapters
implement `DecisionCache`; the engine imports no storage backend. Keeping the
key here means every caller and backend agrees on what "the same decision"
means.

`decision_cache_key` is the frozen v1 identity. It does not grow first-class
auth, policy, model, or provider-version fields. Hosts that need those
dimensions to separate entries must put them in `cache_params` (see
`scoped_cache_params`) or use a dedicated `InMemoryDecisionCache` instance.
A v2 key is an explicit later version decision, not a silent change here.

`InMemoryDecisionCache` is the reference L1: bounded, TTL-aware, exact,
single-flight, and storage-neutral. Durable L2 remains a host adapter.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, assert_never

from swe.canonicalize import canonical_json_bytes
from swe.freeze import freeze_json
from swe.runtime_clock import FrozenClock, WorkflowClock
from swe.runtime_errors import SweHandlerError
from swe.runtime_validate import is_json_value

DEFAULT_MAX_ENTRIES = 256
DEFAULT_MAX_BYTES = 8_388_608
DEFAULT_BENCHMARK_OPERATIONS = 10_000
DEFAULT_BENCHMARK_CONCURRENCY = 64


class DecisionCache(Protocol):
    """Cache for exact provider decisions, shared by L1 and durable stores."""

    async def resolve(
        self,
        key: str,
        compute: Callable[[], Awaitable[Mapping[str, Any]]],
        *,
        provider: str,
        prompt_key: str,
    ) -> tuple[Mapping[str, Any], bool]:
        """Return ``(answer, hit)``; concurrent identical keys compute once."""
        ...


def decision_cache_key(
    *,
    provider: str,
    prompt_key: str,
    prompt: str,
    input_data: Mapping[str, Any],
    choices: Sequence[str],
    params: Mapping[str, Any],
) -> str:
    """Stable cache identity. Prompt text carries the active prompt version."""
    payload = {
        "provider": provider,
        "prompt_key": prompt_key,
        "prompt": prompt,
        "input": dict(input_data),
        "choices": list(choices),
        "params": dict(params),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def scoped_cache_params(
    params: Mapping[str, Any] | None = None,
    *,
    auth_scope: str | None = None,
    policy_version: str | None = None,
    model: str | None = None,
    provider_version: str | None = None,
) -> dict[str, Any]:
    """Put behavior-determining scope into the frozen v1 ``params`` bag.

    This does not change `decision_cache_key`. It is the supported v1 way to
    keep authorization, policy, model, and provider version out of each
    other's entries.
    """
    out = dict(params or {})
    if auth_scope is not None:
        out["auth_scope"] = auth_scope
    if policy_version is not None:
        out["policy_version"] = policy_version
    if model is not None:
        out["model"] = model
    if provider_version is not None:
        out["provider_version"] = provider_version
    return out


class _DropReason(StrEnum):
    EXPIRY = "expiry"
    EVICTION = "eviction"


@dataclass(frozen=True, slots=True)
class CacheMetrics:
    hits: int
    misses: int
    coalesced: int
    computes: int
    errors: int
    evictions: int
    expiries: int
    entries: int
    bytes_used: int
    peak_in_flight: int
    peak_entries: int
    p50_latency_ms: float
    p95_latency_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "coalesced": self.coalesced,
            "computes": self.computes,
            "errors": self.errors,
            "evictions": self.evictions,
            "expiries": self.expiries,
            "entries": self.entries,
            "bytes_used": self.bytes_used,
            "peak_in_flight": self.peak_in_flight,
            "peak_entries": self.peak_entries,
            "p50_latency_ms": self.p50_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
        }


@dataclass
class _CacheEntry:
    key: str
    value: Mapping[str, Any]
    inserted_at_ms: int
    expires_at_ms: int | None
    size_bytes: int


@dataclass
class _Flight:
    task: asyncio.Task[Mapping[str, Any]]
    waiters: int = 0


class InMemoryDecisionCache:
    """Bounded in-memory L1 for exact, validated provider successes."""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        ttl_ms: int | None = None,
        clock: WorkflowClock | FrozenClock | None = None,
        namespace: str = "",
    ) -> None:
        if isinstance(max_entries, bool) or max_entries < 1:
            raise SweHandlerError("max_entries must be a positive integer")
        if isinstance(max_bytes, bool) or max_bytes < 1:
            raise SweHandlerError("max_bytes must be a positive integer")
        if ttl_ms is not None and (isinstance(ttl_ms, bool) or ttl_ms < 1):
            raise SweHandlerError("ttl_ms must be a positive integer when set")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.ttl_ms = ttl_ms
        self.namespace = namespace
        self._clock: WorkflowClock | FrozenClock = clock or WorkflowClock()
        self._entries: dict[str, _CacheEntry] = {}
        self._flights: dict[str, _Flight] = {}
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0
        self._coalesced = 0
        self._computes = 0
        self._errors = 0
        self._evictions = 0
        self._expiries = 0
        self._bytes_used = 0
        self._in_flight = 0
        self._peak_in_flight = 0
        self._peak_entries = 0
        self._latencies_ns: list[int] = []

    @property
    def active_flight_count(self) -> int:
        return len(self._flights)

    def metrics(self) -> CacheMetrics:
        return CacheMetrics(
            hits=self._hits,
            misses=self._misses,
            coalesced=self._coalesced,
            computes=self._computes,
            errors=self._errors,
            evictions=self._evictions,
            expiries=self._expiries,
            entries=len(self._entries),
            bytes_used=self._bytes_used,
            peak_in_flight=self._peak_in_flight,
            peak_entries=self._peak_entries,
            p50_latency_ms=_percentile_ms(self._latencies_ns, 0.50),
            p95_latency_ms=_percentile_ms(self._latencies_ns, 0.95),
        )

    async def resolve(
        self,
        key: str,
        compute: Callable[[], Awaitable[Mapping[str, Any]]],
        *,
        provider: str,
        prompt_key: str,
    ) -> tuple[Mapping[str, Any], bool]:
        del provider, prompt_key
        started = time.perf_counter_ns()
        storage_key = self._storage_key(key)
        try:
            answer, hit = await self._resolve(storage_key, compute)
            return dict(answer), hit
        except Exception:
            self._errors += 1
            raise
        finally:
            self._latencies_ns.append(time.perf_counter_ns() - started)

    async def _resolve(
        self,
        key: str,
        compute: Callable[[], Awaitable[Mapping[str, Any]]],
    ) -> tuple[Mapping[str, Any], bool]:
        async with self._lock:
            now = self._clock.now_ms()
            self._purge_expired(now)
            cached = self._fresh(key, now)
            if cached is not None:
                self._hits += 1
                return cached.value, True
            flight = self._flights.get(key)
            if flight is None:
                self._misses += 1
                self._computes += 1
                task = asyncio.create_task(
                    self._compute(key, compute),
                    name=f"swe-cache:{key[:12]}",
                )
                flight = _Flight(task=task, waiters=1)
                self._flights[key] = flight
                coalesced = False
            else:
                flight.waiters += 1
                self._coalesced += 1
                coalesced = True
            self._in_flight += 1
            self._peak_in_flight = max(self._peak_in_flight, self._in_flight)
        try:
            answer = await asyncio.shield(flight.task)
            return answer, coalesced
        except asyncio.CancelledError:
            raise
        finally:
            async with self._lock:
                self._in_flight = max(0, self._in_flight - 1)
                current = self._flights.get(key)
                if current is flight:
                    current.waiters = max(0, current.waiters - 1)
                    if current.waiters == 0 and not current.task.done():
                        current.task.cancel()
                        self._flights.pop(key, None)

    async def _compute(
        self,
        key: str,
        compute: Callable[[], Awaitable[Mapping[str, Any]]],
    ) -> Mapping[str, Any]:
        try:
            raw = await compute()
            answer = _validated_success(raw)
            async with self._lock:
                now = self._clock.now_ms()
                self._purge_expired(now)
                self._store(key, answer, now)
            return answer
        except asyncio.CancelledError:
            raise
        finally:
            async with self._lock:
                flight = self._flights.get(key)
                if flight is not None and flight.task is asyncio.current_task():
                    self._flights.pop(key, None)

    def _storage_key(self, key: str) -> str:
        if not self.namespace:
            return key
        return f"{self.namespace}:{key}"

    def _fresh(self, key: str, now: int) -> _CacheEntry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at_ms is not None and now >= entry.expires_at_ms:
            self._drop(key, _DropReason.EXPIRY)
            return None
        return entry

    def _store(self, key: str, answer: Mapping[str, Any], now: int) -> None:
        frozen = freeze_json(dict(answer))
        size = len(key.encode("utf-8")) + len(canonical_json_bytes(answer))
        if size > self.max_bytes:
            raise SweHandlerError("provider answer exceeds the L1 memory bound")
        expires_at_ms = None if self.ttl_ms is None else now + self.ttl_ms
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._bytes_used -= previous.size_bytes
        while self._entries and (
            len(self._entries) >= self.max_entries
            or self._bytes_used + size > self.max_bytes
        ):
            victim = min(
                self._entries.values(),
                key=lambda entry: (entry.inserted_at_ms, entry.key),
            )
            self._drop(victim.key, _DropReason.EVICTION)
        self._entries[key] = _CacheEntry(
            key=key,
            value=frozen,
            inserted_at_ms=now,
            expires_at_ms=expires_at_ms,
            size_bytes=size,
        )
        self._bytes_used += size
        self._peak_entries = max(self._peak_entries, len(self._entries))
        if self._bytes_used > self.max_bytes or len(self._entries) > self.max_entries:
            raise SweHandlerError("exact cache exceeded its memory bound")

    def _purge_expired(self, now: int) -> None:
        expired = [
            key
            for key, entry in self._entries.items()
            if entry.expires_at_ms is not None and now >= entry.expires_at_ms
        ]
        for key in sorted(expired):
            self._drop(key, _DropReason.EXPIRY)

    def _drop(self, key: str, reason: _DropReason) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        self._bytes_used = max(0, self._bytes_used - entry.size_bytes)
        match reason:
            case _DropReason.EXPIRY:
                self._expiries += 1
            case _DropReason.EVICTION:
                self._evictions += 1
            case _ as unreachable:
                assert_never(unreachable)


def _validated_success(raw: object) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise SweHandlerError("exact cache rejects a non-object provider answer")
    if not is_json_value(raw):
        raise SweHandlerError("exact cache rejects a non-JSON provider answer")
    choice = raw.get("choice")
    if not isinstance(choice, str) or not choice:
        raise SweHandlerError("exact cache rejects an answer without a string choice")
    return dict(raw)


def _percentile_ms(values: Sequence[int], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1)
    return ordered[index] / 1_000_000


@dataclass(frozen=True, slots=True)
class CacheBenchmark:
    operations: int
    concurrency: int
    cold: CacheMetrics
    warm: CacheMetrics
    wall_ms: float
    throughput_ops_per_second: float
    peak_in_flight: int
    peak_entries: int
    bytes_used: int
    max_bytes: int
    python_version: str
    platform: str
    cpu_count: int | None
    generated_at: str
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": 1,
            "operations": self.operations,
            "concurrency": self.concurrency,
            "cold": self.cold.to_dict(),
            "warm": self.warm.to_dict(),
            "wall_ms": round(self.wall_ms, 3),
            "throughput_ops_per_second": round(self.throughput_ops_per_second, 3),
            "peak_in_flight": self.peak_in_flight,
            "peak_entries": self.peak_entries,
            "bytes_used": self.bytes_used,
            "max_bytes": self.max_bytes,
            "environment": {
                "python": self.python_version,
                "platform": self.platform,
                "cpu_count": self.cpu_count,
            },
            "generated_at": self.generated_at,
        }
        payload.update(self.extra)
        return payload


async def benchmark_exact_cache(
    *,
    operations: int = DEFAULT_BENCHMARK_OPERATIONS,
    concurrency: int = DEFAULT_BENCHMARK_CONCURRENCY,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    min_throughput: float = 0.0,
) -> CacheBenchmark:
    """Run the synthetic exact-cache gate without model or network calls."""
    if isinstance(operations, bool) or operations < 1:
        raise SweHandlerError("operations must be a positive integer")
    if isinstance(concurrency, bool) or concurrency < 1:
        raise SweHandlerError("concurrency must be a positive integer")
    cache = InMemoryDecisionCache(max_entries=max_entries, max_bytes=max_bytes)
    computes = 0

    async def compute_for(index: int) -> Mapping[str, Any]:
        nonlocal computes
        computes += 1
        await asyncio.sleep(0)
        return {"choice": "yes", "ordinal": index}

    async def resolve_one(index: int, *, unique: bool) -> Mapping[str, Any]:
        key = decision_cache_key(
            provider="bench.provider",
            prompt_key="bench.prompt",
            prompt="choose {ordinal}",
            input_data={"ordinal": index if unique else 0},
            choices=("yes", "no"),
            params=scoped_cache_params(model="bench-v1", policy_version="p1"),
        )
        answer, _hit = await cache.resolve(
            key,
            lambda idx=index, is_unique=unique: compute_for(idx if is_unique else 0),
            provider="bench.provider",
            prompt_key="bench.prompt",
        )
        return answer

    started = time.perf_counter_ns()
    semaphore = asyncio.Semaphore(concurrency)

    async def run_wave(unique: bool) -> None:
        async def bound(index: int) -> Mapping[str, Any]:
            async with semaphore:
                return await resolve_one(index, unique=unique)

        await asyncio.gather(*(bound(index) for index in range(operations)))

    await run_wave(unique=True)
    cold = cache.metrics()
    await resolve_one(0, unique=False)
    primed = cache.metrics()
    await run_wave(unique=False)
    final = cache.metrics()
    warm_hits = final.hits - primed.hits
    wall_ns = time.perf_counter_ns() - started
    wall_seconds = max(wall_ns / 1_000_000_000, 1e-9)
    throughput = (operations * 2) / wall_seconds
    if cold.computes < 1:
        raise SweHandlerError("quality gate failed: cold wave computed nothing")
    if warm_hits < operations:
        raise SweHandlerError(
            f"quality gate failed: warm wave expected {operations} hits, "
            f"got {warm_hits}"
        )
    if cache.metrics().peak_in_flight > concurrency:
        raise SweHandlerError(
            "quality gate failed: peak in-flight "
            f"{cache.metrics().peak_in_flight} exceeded {concurrency}"
        )
    if cache.metrics().bytes_used > max_bytes:
        raise SweHandlerError("quality gate failed: L1 exceeded its memory bound")
    if throughput < min_throughput:
        raise SweHandlerError(
            f"quality gate failed: {throughput:.3f} ops/s is below "
            f"{min_throughput:.3f} ops/s"
        )
    return CacheBenchmark(
        operations=operations,
        concurrency=concurrency,
        cold=cold,
        warm=final,
        wall_ms=wall_ns / 1_000_000,
        throughput_ops_per_second=throughput,
        peak_in_flight=cache.metrics().peak_in_flight,
        peak_entries=cache.metrics().peak_entries,
        bytes_used=cache.metrics().bytes_used,
        max_bytes=max_bytes,
        python_version=platform.python_version(),
        platform=platform.platform(),
        cpu_count=os.cpu_count(),
        generated_at=datetime.now(UTC).isoformat(),
        extra={"computes": computes},
    )


def benchmark_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SWE exact-cache quality gate")
    parser.add_argument("--operations", type=int, default=DEFAULT_BENCHMARK_OPERATIONS)
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_BENCHMARK_CONCURRENCY
    )
    parser.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--min-throughput", type=float, default=0.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(
            benchmark_exact_cache(
                operations=args.operations,
                concurrency=args.concurrency,
                max_entries=args.max_entries,
                max_bytes=args.max_bytes,
                min_throughput=args.min_throughput,
            )
        )
    except (SweHandlerError, RuntimeError) as error:
        print(f"exact-cache benchmark failed: {error}", file=sys.stderr)
        return 1
    payload = _canonical_report(report.to_dict())
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


def _canonical_report(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_ENTRIES",
    "CacheBenchmark",
    "CacheMetrics",
    "DecisionCache",
    "InMemoryDecisionCache",
    "benchmark_exact_cache",
    "benchmark_main",
    "decision_cache_key",
    "scoped_cache_params",
]


if __name__ == "__main__":
    raise SystemExit(benchmark_main())
