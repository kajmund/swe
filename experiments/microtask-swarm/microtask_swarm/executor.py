"""Bounded dispatcher for typed microtasks.

This is a host-layer dispatcher: it never invents workflow semantics. When the
SWE Python package is importable, dependencies are compiled by
``swe.microtasks``; otherwise the identical deterministic layering fallback is
used. Provider calls always run under one global semaphore, so requested
concurrency is an upper bound on in-flight requests.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from microtask_swarm.budget import BudgetError, SweycoinMeter
from microtask_swarm.contracts import (
    ContractViolation,
    Kind,
    parse_json_output,
    render_prompt,
    verify_output,
)
from microtask_swarm.dag import LayerPlan, ScheduledTask, compile_layers
from microtask_swarm.fixtures import TaskSpec
from microtask_swarm.results import CallRecord, TaskResult


class ProviderError(RuntimeError):
    """A provider call failed before a typed output was produced."""


@dataclass(frozen=True, slots=True)
class ChatRequest:
    request_id: str
    task_id: str
    kind: Kind
    model_id: str
    system: str
    user: str
    schema: Mapping[str, Any]
    max_output_tokens: int
    passage: str
    input_data: Mapping[str, Any]
    attempt: int
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    content: str
    tokens: Mapping[str, Any]
    billed_usd: str | None = None
    price_period: str | None = None


class Provider(Protocol):
    async def chat(self, request: ChatRequest) -> ProviderResponse: ...


@dataclass(frozen=True, slots=True)
class Candidate:
    model_id: str
    output: Any = None
    verification: Any = None
    calls: tuple[CallRecord, ...] = ()
    error: str | None = None
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class Execution:
    results: Mapping[str, TaskResult]
    wall_ms: float
    peak_in_flight: int
    layers: tuple[tuple[str, ...], ...]
    layer_source: str
    layer_fallback_reason: str | None
    call_count: int
    retry_count: int
    cancelled_count: int
    early_cancelled_tasks: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "wall_ms": round(self.wall_ms, 3),
            "peak_in_flight": self.peak_in_flight,
            "layers": [list(layer) for layer in self.layers],
            "layer_source": self.layer_source,
            "layer_fallback_reason": self.layer_fallback_reason,
            "call_count": self.call_count,
            "retry_count": self.retry_count,
            "cancelled_count": self.cancelled_count,
            "early_cancelled_tasks": self.early_cancelled_tasks,
        }


def _empty_tokens() -> dict[str, Any]:
    return {
        "input": None,
        "output": None,
        "reasoning": None,
        "cache_read": None,
        "cache_write": None,
        "source": "provider",
        "cache_read_assumed_miss": False,
    }


def _normalize_tokens(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    tokens = _empty_tokens()
    if raw is not None:
        for key in ("input", "output", "reasoning", "cache_read", "cache_write"):
            tokens[key] = raw.get(key)
        tokens["source"] = str(raw.get("source", "provider"))
        tokens["cache_read_assumed_miss"] = bool(
            raw.get("cache_read_assumed_miss", False)
        )
    return tokens


class SwarmExecutor:
    def __init__(
        self,
        *,
        provider: Provider,
        models: Mapping[str, Mapping[str, Any]],
        contracts: Mapping[str, Mapping[str, Any]],
        concurrency: int,
        max_attempts: int,
        timeout_seconds: float,
        meter: SweycoinMeter,
        retry_backoff_ms: float = 0.0,
        default_price_period: str | None = "peak",
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.provider = provider
        self.models = models
        self.contracts = contracts
        self.concurrency = concurrency
        self.max_attempts = max_attempts
        self.timeout_seconds = timeout_seconds
        self.meter = meter
        self.retry_backoff_ms = retry_backoff_ms
        self.default_price_period = default_price_period
        self._semaphore = asyncio.Semaphore(concurrency)
        self._active = 0
        self._peak = 0

    async def run(
        self,
        specs: Sequence[TaskSpec],
        *,
        mode: str,
        model_ids: Sequence[str],
    ) -> Execution:
        if mode not in {"single_call", "distributed_independent", "heterogeneous_competition"}:
            raise ValueError(f"unknown mode {mode!r}")
        if not model_ids:
            raise ValueError("at least one model is required")
        for model_id in model_ids:
            if model_id not in self.models:
                raise ValueError(f"unknown model {model_id!r}")
            if not self.models[model_id].get("available", False):
                raise ValueError(f"model {model_id!r} is not available")

        scheduled = tuple(
            ScheduledTask(task_id=spec.task_id, payload={"kind": str(spec.kind)})
            for spec in specs
        )
        plan: LayerPlan = compile_layers(scheduled, concurrency=self.concurrency)
        by_id = {spec.task_id: spec for spec in specs}
        self._active = 0
        self._peak = 0
        started = time.perf_counter()
        results: dict[str, TaskResult] = {}
        if mode == "heterogeneous_competition":
            for layer in plan.layers:
                outcomes = await asyncio.gather(
                    *(
                        self._run_competition(by_id[task_id], model_ids)
                        for task_id in layer
                    )
                )
                for outcome in outcomes:
                    results[outcome.task_id] = outcome
        else:
            for layer in plan.layers:
                outcomes = await asyncio.gather(
                    *(
                        self._run_single(by_id[task_id], model_ids[0])
                        for task_id in layer
                    )
                )
                for outcome in outcomes:
                    results[outcome.task_id] = outcome
        wall_ms = (time.perf_counter() - started) * 1000.0
        all_calls = [call for result in results.values() for call in result.calls]
        return Execution(
            results=results,
            wall_ms=wall_ms,
            peak_in_flight=self._peak,
            layers=plan.layers,
            layer_source=plan.source,
            layer_fallback_reason=plan.fallback_reason,
            call_count=len(all_calls),
            retry_count=sum(1 for call in all_calls if call.attempt > 1),
            cancelled_count=sum(1 for call in all_calls if call.cancelled),
            early_cancelled_tasks=sum(
                1 for result in results.values() if result.early_cancelled
            ),
        )

    async def _run_single(self, spec: TaskSpec, model_id: str) -> TaskResult:
        candidate = await self._call_with_retries(spec, model_id)
        if candidate.cancelled:
            return TaskResult(
                task_id=spec.task_id,
                kind=str(spec.kind),
                error="cancelled",
                calls=candidate.calls,
            )
        if candidate.verification is not None and candidate.verification.ok:
            return TaskResult(
                task_id=spec.task_id,
                kind=str(spec.kind),
                output=candidate.output,
                verification=candidate.verification,
                selected_models=(model_id,),
                calls=candidate.calls,
            )
        return TaskResult(
            task_id=spec.task_id,
            kind=str(spec.kind),
            output=candidate.output,
            verification=candidate.verification,
            error=candidate.error or "contract verification failed",
            selected_models=(),
            calls=candidate.calls,
        )

    async def _run_competition(
        self, spec: TaskSpec, model_ids: Sequence[str]
    ) -> TaskResult:
        tasks = {
            model_id: asyncio.create_task(
                self._call_with_retries(spec, model_id),
                name=f"swarm:{spec.task_id}:{model_id}",
            )
            for model_id in model_ids
        }
        pending: set[asyncio.Task[Candidate]] = set(tasks.values())
        completed: list[Candidate] = []
        selected: Candidate | None = None
        cancelled_count = 0
        model_order = {model_id: index for index, model_id in enumerate(model_ids)}
        while pending and selected is None:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            batch = sorted(
                (task.result() for task in done),
                key=lambda candidate: model_order.get(candidate.model_id, len(model_order)),
            )
            for candidate in batch:
                completed.append(candidate)
                if (
                    selected is None
                    and candidate.verification is not None
                    and candidate.verification.ok
                    and not candidate.cancelled
                ):
                    selected = candidate
        if selected is not None and pending:
            for task in pending:
                task.cancel()
            cancelled = await asyncio.gather(*pending, return_exceptions=True)
            for item in cancelled:
                if isinstance(item, Candidate):
                    completed.append(item)
                    if item.cancelled:
                        cancelled_count += 1
                else:
                    cancelled_count += 1
            pending.clear()
        if selected is None:
            first_output = next(
                (candidate for candidate in completed if candidate.output is not None),
                None,
            )
            calls = tuple(call for candidate in completed for call in candidate.calls)
            return TaskResult(
                task_id=spec.task_id,
                kind=str(spec.kind),
                output=first_output.output if first_output is not None else None,
                verification=(
                    first_output.verification if first_output is not None else None
                ),
                error="no candidate passed independent verification",
                selected_models=(),
                calls=calls,
                cancelled_candidates=cancelled_count,
                early_cancelled=selected is not None,
            )
        calls = tuple(call for candidate in completed for call in candidate.calls)
        return TaskResult(
            task_id=spec.task_id,
            kind=str(spec.kind),
            output=selected.output,
            verification=selected.verification,
            selected_models=(selected.model_id,),
            calls=calls,
            cancelled_candidates=cancelled_count,
            early_cancelled=cancelled_count > 0,
        )

    async def _call_with_retries(self, spec: TaskSpec, model_id: str) -> Candidate:
        calls: list[CallRecord] = []
        last_error = "call failed"
        last_output: Any = None
        last_verification: Any = None
        for attempt in range(1, self.max_attempts + 1):
            key = f"{spec.task_id}:{model_id}:attempt-{attempt}"
            tariff = self._tariff(spec.kind)
            try:
                reservation_id = self.meter.reserve(key, tariff)
            except BudgetError as exc:
                calls.append(
                    CallRecord(
                        model_id=model_id,
                        role="candidate",
                        attempt=attempt,
                        latency_ms=0.0,
                        tokens=_empty_tokens(),
                        error=str(exc),
                    )
                )
                return Candidate(
                    model_id=model_id, calls=tuple(calls), error=str(exc)
                )

            try:
                system, user = render_prompt(spec.kind, spec.input_data)
                request = ChatRequest(
                    request_id=f"{spec.task_id}:{model_id}",
                    task_id=spec.task_id,
                    kind=spec.kind,
                    model_id=model_id,
                    system=system,
                    user=user,
                    schema=self._schema(spec.kind),
                    max_output_tokens=self._max_tokens(spec.kind, model_id),
                    passage=spec.passage,
                    input_data=spec.input_data,
                    attempt=attempt,
                    timeout_seconds=self.timeout_seconds,
                )
            except Exception as exc:
                self.meter.release(reservation_id)
                calls.append(
                    CallRecord(
                        model_id=model_id,
                        role="candidate",
                        attempt=attempt,
                        latency_ms=0.0,
                        tokens=_empty_tokens(),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                return Candidate(
                    model_id=model_id,
                    calls=tuple(calls),
                    error=f"{type(exc).__name__}: {exc}",
                )
            started = time.perf_counter()
            try:
                async with self._semaphore:
                    self._active += 1
                    self._peak = max(self._peak, self._active)
                    try:
                        response = await asyncio.wait_for(
                            self.provider.chat(request),
                            self.timeout_seconds,
                        )
                    finally:
                        self._active -= 1
            except asyncio.CancelledError:
                latency = (time.perf_counter() - started) * 1000.0
                try:
                    self.meter.settle(reservation_id, tariff)
                except BudgetError:
                    self.meter.release(reservation_id)
                calls.append(
                    CallRecord(
                        model_id=model_id,
                        role="candidate",
                        attempt=attempt,
                        latency_ms=latency,
                        tokens=_empty_tokens(),
                        error="cancelled",
                        cancelled=True,
                    )
                )
                return Candidate(
                    model_id=model_id,
                    calls=tuple(calls),
                    error="cancelled",
                    cancelled=True,
                )
            except Exception as exc:
                latency = (time.perf_counter() - started) * 1000.0
                self._settle_attempt(reservation_id, tariff)
                last_error = f"{type(exc).__name__}: {exc}"
                calls.append(
                    CallRecord(
                        model_id=model_id,
                        role="candidate",
                        attempt=attempt,
                        latency_ms=latency,
                        tokens=_empty_tokens(),
                        error=last_error,
                    )
                )
                if attempt < self.max_attempts:
                    await self._backoff()
                    continue
                return Candidate(
                    model_id=model_id, calls=tuple(calls), error=last_error
                )

            latency = (time.perf_counter() - started) * 1000.0
            tokens = _normalize_tokens(response.tokens)
            output: Any = None
            verification: Any = None
            try:
                output = parse_json_output(response.content)
                verification = verify_output(
                    spec.kind,
                    output,
                    passage=spec.passage,
                    max_quotes=int(self.contracts.get("extraction", {}).get("max_quotes", 3)),
                    min_quote_chars=int(
                        self.contracts.get("extraction", {}).get("min_quote_chars", 10)
                    ),
                )
                if not verification.ok:
                    raise ContractViolation(
                        "independent verification failed: "
                        + "; ".join(verification.errors)
                    )
            except ContractViolation as exc:
                last_output = output
                last_verification = verification
                self._settle_attempt(reservation_id, tariff)
                last_error = f"{type(exc).__name__}: {exc}"
                calls.append(
                    CallRecord(
                        model_id=model_id,
                        role="candidate",
                        attempt=attempt,
                        latency_ms=latency,
                        tokens=tokens,
                        billed_usd=response.billed_usd,
                        price_period=response.price_period or self.default_price_period,
                        error=last_error,
                    )
                )
                if attempt < self.max_attempts:
                    await self._backoff()
                    continue
                return Candidate(
                    model_id=model_id,
                    output=last_output,
                    verification=last_verification,
                    calls=tuple(calls),
                    error=last_error,
                )

            self._settle_attempt(reservation_id, tariff)
            calls.append(
                CallRecord(
                    model_id=model_id,
                    role="candidate",
                    attempt=attempt,
                    latency_ms=latency,
                    tokens=tokens,
                    billed_usd=response.billed_usd,
                    price_period=response.price_period or self.default_price_period,
                    output=output,
                )
            )
            return Candidate(
                model_id=model_id,
                output=output,
                verification=verification,
                calls=tuple(calls),
            )
        return Candidate(model_id=model_id, calls=tuple(calls), error=last_error)

    def _settle_attempt(self, reservation_id: int, tariff: int) -> None:
        try:
            self.meter.settle(reservation_id, tariff)
        except BudgetError:
            self.meter.release(reservation_id)

    async def _backoff(self) -> None:
        if self.retry_backoff_ms > 0:
            await asyncio.sleep(self.retry_backoff_ms / 1000.0)

    def _contract(self, kind: Kind) -> Mapping[str, Any]:
        key = str(kind)
        contract = self.contracts.get(key)
        if not isinstance(contract, Mapping):
            raise ValueError(f"no contract config for {key!r}")
        return contract

    def _tariff(self, kind: Kind) -> int:
        return int(self._contract(kind).get("sweycoins_milli", 0))

    def _max_tokens(self, kind: Kind, model_id: str) -> int:
        base = int(self._contract(kind).get("max_output_tokens", 64))
        model = self.models.get(model_id, {})
        headroom = int(model.get("output_token_headroom", 0))
        return base + max(0, headroom)

    def _schema(self, kind: Kind) -> Mapping[str, Any]:
        from microtask_swarm.contracts import output_schema

        return output_schema(kind)


__all__ = [
    "Candidate",
    "ChatRequest",
    "Execution",
    "Provider",
    "ProviderError",
    "ProviderResponse",
    "SwarmExecutor",
]
