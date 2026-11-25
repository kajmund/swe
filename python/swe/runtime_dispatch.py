"""asyncio handler and provider dispatch. Never wait while the Rust lock is held.

`invoke_handler` calls a registered task handler. `invoke_provider` renders the
decision prompt from the active prompt map, calls the registered provider, and
validates the returned choice against the node's declared choices. With
`cache_policy: exact` the answer goes through the shared L1/L2 decision cache
and the cache state is part of the completion payload.

Every handler and provider call is bound to an :class:`InvocationHandle` via
``current_invocation()``. Hosts abort sockets from that handle; SWE does not
force-close blocking I/O. After cancel, unfinished work is detached into a
bounded abandoned-work registry instead of being awaited on the success path.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from swe.budget import (
    BudgetOutcome,
    BudgetRequest,
    BudgetReservation,
    RuntimeBudget,
)
from swe.cache import (
    CacheComputeOwner,
    DecisionCache,
    OwnedOrphanDecisionCache,
    assert_cache_isolation,
    decision_cache_key,
)
from swe.effect_journal import (
    EffectNotStarted,
    note_confirmed,
    should_release_unused,
)
from swe.invocation_error import invocation_error_from_exception
from swe.invocation_handle import (
    AbandonedWorkRegistry,
    CancelReason,
    InvocationHandle,
    bind_current_invocation,
    consume_task_result,
    current_invocation,
    request_cancel,
    reset_current_invocation,
)
from swe.prompt_render import render_prompt
from swe.runtime_errors import SweHandlerError
from swe.runtime_validate import require_json_value, validate_output_schema

Handler = Callable[[Mapping[str, Any]], Awaitable[Any]]
Provider = Callable[[Mapping[str, Any]], Awaitable[Any]]
NowMs = Callable[[], int]

# After SWE has persisted terminal status, wait this long for cooperative
# cancel before detaching the task into the abandoned-work registry.
CANCEL_GRACE_SECONDS = 0.1


@dataclass(frozen=True)
class HandlerOutcome:
    invocation_id: str
    node_id: str
    ok: bool
    value: Any
    cancelled: bool = False
    completed_at_ms: int | None = None


@dataclass
class _CapacityWaiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[None]
    state: str = "queued"


class CapacityLimiter:
    """Loop-neutral async capacity shared by every run on one runner.

    A permit stays held while a cancellation-resistant handler remains inside
    its dispatch boundary. Waiters use futures from their own event loops;
    the synchronous lock protects reservation transfer without binding the
    limiter itself to the first loop that experiences contention.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._available = capacity
        self._lock = threading.Lock()
        self._waiters: deque[_CapacityWaiter] = deque()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *_exc: object) -> None:
        self.release()

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        waiter = _CapacityWaiter(loop, loop.create_future())
        with self._lock:
            if self._available > 0 and not self._waiters:
                self._available -= 1
                return
            self._waiters.append(waiter)
        try:
            await waiter.future
        except BaseException:
            release_permit = False
            with self._lock:
                if waiter.state == "queued":
                    waiter.state = "cancelled"
                    try:
                        self._waiters.remove(waiter)
                    except ValueError:
                        pass
                elif waiter.state == "transferring":
                    # The releaser will observe cancellation and transfer the
                    # same permit to another waiter.
                    waiter.state = "cancelled"
                elif waiter.state == "granted":
                    waiter.state = "cancelled"
                    release_permit = True
            if release_permit:
                self.release()
            raise

    def release(self) -> None:
        while True:
            with self._lock:
                waiter = self._next_waiter_locked()
                if waiter is None:
                    if self._available >= self.capacity:
                        raise RuntimeError("capacity limiter released too many times")
                    self._available += 1
                    return
                waiter.state = "transferring"
            try:
                waiter.loop.call_soon_threadsafe(
                    _complete_capacity_waiter,
                    waiter.future,
                )
            except RuntimeError:
                scheduled = False
            else:
                scheduled = True
            with self._lock:
                if scheduled and waiter.state != "cancelled":
                    waiter.state = "granted"
                    return
                # A loop may close, or its task may be cancelled, while its
                # permit is transferring. Give that permit to the next live
                # waiter instead of leaking or duplicating it.
                waiter.state = "cancelled"

    def _next_waiter_locked(self) -> _CapacityWaiter | None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.state != "queued" or waiter.loop.is_closed():
                continue
            return waiter
        return None


def _complete_capacity_waiter(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


@dataclass(frozen=True)
class ProviderDispatch:
    """Provider registry plus the resources provider decisions need."""

    providers: Mapping[str, Provider]
    prompts: Mapping[str, str] | None = None
    cache: DecisionCache | None = None
    cache_owner: CacheComputeOwner = field(default_factory=CacheComputeOwner)


def spawn_invocations(
    effects: list[Mapping[str, Any]],
    *,
    handlers: Mapping[str, Handler],
    nodes: Mapping[str, Any],
    pending: dict[str, asyncio.Task[HandlerOutcome]],
    semaphore: CapacityLimiter,
    provider_dispatch: ProviderDispatch,
    now_ms: NowMs,
    handles: Mapping[str, InvocationHandle] | None = None,
    budget: RuntimeBudget | None = None,
    budget_slots: dict[str, list[BudgetReservation | None]] | None = None,
    run_id: str = "",
) -> None:
    bound = handles or {}
    for effect in effects:
        kind = effect["type"]
        handle = bound.get(str(effect["invocation_id"]))
        invocation_id = str(effect["invocation_id"])
        slot: list[BudgetReservation | None] | None = None
        if kind in {"invoke_handler", "invoke_provider"} and budget_slots is not None:
            slot = []
            budget_slots[invocation_id] = slot
        if kind == "invoke_handler":
            task = _run_handler(
                effect,
                handlers,
                nodes,
                semaphore,
                now_ms,
                handle,
                budget,
                run_id,
                slot,
            )
        elif kind == "invoke_provider":
            task = _run_provider(
                effect,
                provider_dispatch,
                semaphore,
                now_ms,
                handle,
                budget,
                run_id,
                slot,
            )
        else:
            continue
        pending[invocation_id] = asyncio.create_task(
            task,
            name=f"swe:{invocation_id}",
        )


def cancel_invocations(
    effects: list[Mapping[str, Any]],
    pending: dict[str, asyncio.Task[HandlerOutcome]],
    handles: Mapping[str, InvocationHandle] | None = None,
    reason: CancelReason = "cancel",
) -> None:
    bound = handles or {}
    for effect in effects:
        if effect["type"] != "cancel_invocation":
            continue
        invocation_id = str(effect["invocation_id"])
        request_cancel(bound.get(invocation_id), pending.get(invocation_id), reason)


def cancel_all(
    pending: dict[str, asyncio.Task[HandlerOutcome]],
    handles: Mapping[str, InvocationHandle] | None = None,
    reason: CancelReason = "cancel",
) -> None:
    bound = handles or {}
    for invocation_id, task in pending.items():
        request_cancel(bound.get(invocation_id), task, reason)


async def reap_owned(
    pending: dict[str, asyncio.Task[HandlerOutcome]],
    cancel_task: asyncio.Task[bool] | None,
    abandoned: AbandonedWorkRegistry,
    invocation_nodes: Mapping[str, str] | None = None,
    handles: Mapping[str, InvocationHandle] | None = None,
    reason: CancelReason = "cancel",
) -> None:
    """Cancel leftover work, wait a short grace, then detach.

    Must not await abandoned tasks. The caller's ``finally`` uses this so a
    handler that ignores cancel cannot hang ``run`` / ``resume``. Signal the
    invocation handle before ``task.cancel()`` so a cancelled ``run`` still
    publishes ``handle.cancel`` for host socket abort.
    """

    nodes = dict(invocation_nodes or {})
    bound = dict(handles or {})
    owned = list(pending.items())
    pending.clear()
    still: list[tuple[str, asyncio.Task[HandlerOutcome]]] = []
    for invocation_id, task in owned:
        if task.done():
            consume_task_result(task)
            continue
        request_cancel(bound.get(invocation_id), task, reason)
        still.append((invocation_id, task))
    extra: list[asyncio.Task[Any]] = []
    if cancel_task is not None and not cancel_task.done():
        cancel_task.cancel()
        extra.append(cancel_task)
    wait = [task for _, task in still] + extra
    if wait:
        await asyncio.wait(wait, timeout=CANCEL_GRACE_SECONDS)
    for invocation_id, task in still:
        if task.done():
            consume_task_result(task)
            continue
        abandoned.register(invocation_id, nodes.get(invocation_id, ""), task)
    for task in extra:
        consume_task_result(task)


def _outcome(
    invocation_id: str,
    node_id: str,
    *,
    now_ms: NowMs,
    ok: bool = False,
    value: Any = None,
    cancelled: bool = False,
) -> HandlerOutcome:
    return HandlerOutcome(
        invocation_id,
        node_id,
        ok,
        value,
        cancelled=cancelled,
        completed_at_ms=now_ms(),
    )


def _budget_request(
    effect: Mapping[str, Any],
    *,
    run_id: str,
    kind: str,
    name: str,
) -> BudgetRequest:
    return BudgetRequest(
        run_id=str(run_id),
        invocation_id=str(effect["invocation_id"]),
        node_id=str(effect["node_id"]),
        kind=kind,
        name=name,
        input=dict(effect["input"]),
    )


def _settle(
    budget: RuntimeBudget | None,
    reservation: BudgetReservation | None,
    outcome: BudgetOutcome,
) -> None:
    if budget is not None and reservation is not None:
        budget.settle(reservation, outcome)


async def _account_cancelled(
    budget: RuntimeBudget | None,
    reservation: BudgetReservation | None,
    handle: InvocationHandle | None,
) -> None:
    """Release an unused external reservation; keep a started one pending.

    Undeclared nodes always ``mark_pending``. A declared external node that
    has not called ``begin_external_effect`` has not started I/O, so only the
    unused reservation is released. After the mark, the charge stays pending.
    """

    if budget is None or reservation is None:
        return
    if should_release_unused(handle):
        budget.release(reservation)
        return
    budget.mark_pending(reservation)


def _budget_flags(handle: InvocationHandle | None) -> tuple[bool, bool]:
    if handle is None:
        return False, False
    return handle.cancel.is_set(), handle.reason() == "timeout"


def _budget_outcome(
    handle: InvocationHandle | None,
    *,
    ok: bool,
    value: Any = None,
    error: Mapping[str, Any] | None = None,
    cache_state: str | None = None,
) -> BudgetOutcome:
    cancelled, timed_out = _budget_flags(handle)
    return BudgetOutcome(
        ok=ok,
        cancelled=cancelled,
        timed_out=timed_out,
        cache_state=cache_state,
        value=value,
        error=error,
    )


async def _run_handler(
    effect: Mapping[str, Any],
    handlers: Mapping[str, Handler],
    nodes: Mapping[str, Any],
    semaphore: CapacityLimiter,
    now_ms: NowMs,
    handle: InvocationHandle | None,
    budget: RuntimeBudget | None = None,
    run_id: str = "",
    reservation_slot: list[BudgetReservation | None] | None = None,
) -> HandlerOutcome:
    invocation_id = effect["invocation_id"]
    node_id = effect["node_id"]
    token = bind_current_invocation(handle)
    try:
        async with semaphore:
            reservation = None
            if budget is not None:
                request = _budget_request(
                    effect,
                    run_id=run_id,
                    kind="handler",
                    name=str(effect["handler"]),
                )
                reservation = budget.reserve(request)
                if reservation_slot is not None and reservation is not None:
                    reservation_slot.append(reservation)
            try:
                output = await handlers[effect["handler"]](effect["input"])
            except asyncio.CancelledError:
                await _account_cancelled(budget, reservation, handle)
                raise
            except SweHandlerError as exc:
                _settle(
                    budget,
                    reservation,
                    _budget_outcome(
                        handle,
                        ok=False,
                        error=invocation_error_from_exception(exc),
                    ),
                )
                raise
            except Exception as exc:  # noqa: BLE001 - settle, then report failure
                _settle(
                    budget,
                    reservation,
                    _budget_outcome(
                        handle,
                        ok=False,
                        error=invocation_error_from_exception(exc),
                    ),
                )
                raise
            else:
                if should_release_unused(handle):
                    if budget is not None and reservation is not None:
                        budget.release(reservation)
                        reservation = None
                    raise EffectNotStarted(str(node_id))
                _settle(
                    budget,
                    reservation,
                    _budget_outcome(handle, ok=True, value=output),
                )
                await note_confirmed(handle, output, reservation)
        output = require_json_value(output, node_id=node_id)
        validate_output_schema(nodes[node_id], output, node_id=node_id)
        return _outcome(invocation_id, node_id, now_ms=now_ms, ok=True, value=output)
    except asyncio.CancelledError:
        return _outcome(invocation_id, node_id, now_ms=now_ms, cancelled=True)
    except SweHandlerError as exc:
        return _outcome(
            invocation_id,
            node_id,
            now_ms=now_ms,
            value=invocation_error_from_exception(exc),
        )
    except Exception as exc:  # noqa: BLE001 - handler boundary returns typed failures
        return _outcome(
            invocation_id,
            node_id,
            now_ms=now_ms,
            value=invocation_error_from_exception(exc),
        )
    finally:
        reset_current_invocation(token)


async def _run_provider(
    effect: Mapping[str, Any],
    dispatch: ProviderDispatch,
    semaphore: CapacityLimiter,
    now_ms: NowMs,
    handle: InvocationHandle | None,
    budget: RuntimeBudget | None = None,
    run_id: str = "",
    reservation_slot: list[BudgetReservation | None] | None = None,
) -> HandlerOutcome:
    invocation_id = effect["invocation_id"]
    node_id = effect["node_id"]
    token = bind_current_invocation(handle)
    try:
        request = _provider_request(effect, dispatch.prompts)
        provider = dispatch.providers[effect["provider"]]
        reservation = None
        if budget is not None:
            budget_request = _budget_request(
                effect,
                run_id=run_id,
                kind="provider",
                name=str(effect["provider"]),
            )
            reservation = budget.reserve(budget_request)
            if reservation_slot is not None and reservation is not None:
                reservation_slot.append(reservation)
        try:
            answer, cache_state = await _call_provider(
                provider,
                request,
                dispatch.cache,
                semaphore,
                dispatch.cache_owner,
            )
        except asyncio.CancelledError:
            await _account_cancelled(budget, reservation, handle)
            raise
        except SweHandlerError as exc:
            _settle(
                budget,
                reservation,
                _budget_outcome(
                    handle,
                    ok=False,
                    error=invocation_error_from_exception(exc),
                ),
            )
            raise
        except Exception as exc:  # noqa: BLE001 - settle, then report failure
            _settle(
                budget,
                reservation,
                _budget_outcome(
                    handle,
                    ok=False,
                    error=invocation_error_from_exception(exc),
                ),
            )
            raise
        else:
            if should_release_unused(handle):
                if budget is not None and reservation is not None:
                    budget.release(reservation)
                    reservation = None
                raise EffectNotStarted(str(node_id))
            _settle(
                budget,
                reservation,
                _budget_outcome(
                    handle,
                    ok=True,
                    cache_state=cache_state,
                    value=dict(answer),
                ),
            )
            await note_confirmed(handle, dict(answer), reservation)
        output = _provider_output(effect, answer, cache_state)
        return _outcome(invocation_id, node_id, now_ms=now_ms, ok=True, value=output)
    except asyncio.CancelledError:
        return _outcome(invocation_id, node_id, now_ms=now_ms, cancelled=True)
    except SweHandlerError as exc:
        return _outcome(
            invocation_id,
            node_id,
            now_ms=now_ms,
            value=invocation_error_from_exception(exc),
        )
    except Exception as exc:  # noqa: BLE001 - provider boundary returns typed failures
        return _outcome(
            invocation_id,
            node_id,
            now_ms=now_ms,
            value=invocation_error_from_exception(exc),
        )
    finally:
        reset_current_invocation(token)


def _provider_request(
    effect: Mapping[str, Any],
    prompts: Mapping[str, str] | None,
) -> dict[str, Any]:
    prompt_key = str(effect["prompt_key"])
    if prompts is None:
        raise SweHandlerError(
            f"provider decision requires an active prompt map for {prompt_key!r}",
            path=f"nodes.{effect['node_id']}.prompt_key",
        )
    input_data = dict(effect["input"])
    try:
        prompt = render_prompt(dict(prompts), prompt_key, **input_data)
    except RuntimeError as exc:
        raise SweHandlerError(str(exc), path=f"nodes.{effect['node_id']}.prompt_key") from exc
    return {
        "provider": str(effect["provider"]),
        "prompt_key": prompt_key,
        "prompt": prompt,
        "input": input_data,
        "choices": tuple(str(choice) for choice in effect["choices"]),
        "cache_policy": str(effect["cache_policy"]),
    }


async def _call_provider(
    provider: Provider,
    request: Mapping[str, Any],
    cache: DecisionCache | None,
    semaphore: CapacityLimiter,
    cache_owner: CacheComputeOwner,
) -> tuple[Mapping[str, Any], str | None]:
    if request["cache_policy"] != "exact":
        return await _invoke_provider_with_capacity(provider, request, semaphore), None
    if cache is None:
        raise SweHandlerError(
            f"cache_policy exact requires a provider cache for {request['provider']!r}",
        )
    params = dict(getattr(provider, "cache_params", {}))
    assert_cache_isolation(params, path="provider.cache_params")
    key = decision_cache_key(
        provider=request["provider"],
        prompt_key=request["prompt_key"],
        prompt=request["prompt"],
        input_data=request["input"],
        choices=request["choices"],
        params=params,
    )
    compute = lambda: _invoke_provider_with_capacity(provider, request, semaphore)
    if isinstance(cache, OwnedOrphanDecisionCache):
        answer, hit = await cache.resolve_owned(
            key,
            compute,
            provider=request["provider"],
            prompt_key=request["prompt_key"],
            owner=cache_owner,
        )
    else:
        answer, hit = await cache.resolve(
            key,
            compute,
            provider=request["provider"],
            prompt_key=request["prompt_key"],
        )
    return answer, "hit" if hit else "miss"


async def _invoke_provider_with_capacity(
    provider: Provider,
    request: Mapping[str, Any],
    semaphore: CapacityLimiter,
) -> Mapping[str, Any]:
    # The task that performs the external compute owns the permit.  For exact
    # cache misses that is the shared cache task, so it keeps capacity while it
    # outlives a cancelled waiter and coalesced waiters consume no extra slots.
    async with semaphore:
        return await _invoke_provider(provider, request)


async def _invoke_provider(
    provider: Provider,
    request: Mapping[str, Any],
) -> Mapping[str, Any]:
    raw = await provider(request)
    if isinstance(raw, str):
        answer: dict[str, Any] = {"choice": raw}
    elif isinstance(raw, Mapping):
        answer = dict(raw)
    else:
        raise SweHandlerError(
            f"provider {request['provider']!r} returned a non-object decision",
        )
    choice = answer.get("choice")
    if not isinstance(choice, str):
        raise SweHandlerError(
            f"provider {request['provider']!r} returned no string choice",
        )
    if choice not in request["choices"]:
        raise SweHandlerError(
            f"provider {request['provider']!r} returned choice {choice!r} "
            "outside the declared choices",
        )
    return answer


def _provider_output(
    effect: Mapping[str, Any],
    answer: Mapping[str, Any],
    cache_state: str | None,
) -> Mapping[str, Any]:
    output = dict(answer)
    output["choice"] = str(answer["choice"])
    if cache_state is not None:
        output["cache"] = cache_state
    return require_json_value(output, node_id=effect["node_id"])


async def identity_handler(_input: Mapping[str, Any]) -> Mapping[str, Any]:
    return dict(_input)


def sync_handler(func: Callable[[Mapping[str, Any]], Any]) -> Handler:
    async def wrapped(payload: Mapping[str, Any]) -> Any:
        return func(payload)

    return wrapped


__all__ = [
    "CANCEL_GRACE_SECONDS",
    "AbandonedWorkRegistry",
    "CapacityLimiter",
    "Handler",
    "HandlerOutcome",
    "InvocationHandle",
    "Provider",
    "ProviderDispatch",
    "cancel_all",
    "cancel_invocations",
    "current_invocation",
    "identity_handler",
    "reap_owned",
    "spawn_invocations",
    "sync_handler",
]
