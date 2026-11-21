"""asyncio handler and provider dispatch. Never wait while the Rust lock is held.

`invoke_handler` calls a registered task handler. `invoke_provider` renders the
decision prompt from the active prompt map, calls the registered provider, and
validates the returned choice against the node's declared choices. With
`cache_policy: exact` the answer goes through the shared L1/L2 decision cache
and the cache state is part of the completion payload.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from swe.cache import DecisionCache, assert_cache_isolation, decision_cache_key
from swe.invocation_error import invocation_error_from_exception
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


@dataclass(frozen=True)
class AbandonedWork:
    """One detached handler or provider task that ignored cancel."""

    invocation_id: str
    node_id: str
    abandoned_at_mono: float

    def age_seconds(self, now_mono: float | None = None) -> float:
        now = time.monotonic() if now_mono is None else now_mono
        return max(0.0, now - self.abandoned_at_mono)


class AbandonedWorkRegistry:
    """Bounded, observable set of abandoned handler tasks.

    Detaching a task never waits for its I/O. New dispatch fails closed when
    ``abandoned_count >= cap``. A task that later finishes frees its slot.
    Entries are keyed by the asyncio task identity so two attempts that share
    a deterministic invocation id cannot overwrite each other.
    """

    def __init__(self, cap: int) -> None:
        if cap < 1:
            raise ValueError("abandoned work cap must be at least 1")
        self.cap = cap
        self._items: dict[int, tuple[AbandonedWork, asyncio.Task[Any]]] = {}

    @property
    def abandoned_count(self) -> int:
        self._drop_done()
        return len(self._items)

    def invocation_ids(self) -> tuple[str, ...]:
        self._drop_done()
        return tuple(item[0].invocation_id for item in self._items.values())

    def snapshot(self) -> tuple[AbandonedWork, ...]:
        self._drop_done()
        return tuple(item[0] for item in self._items.values())

    def at_capacity(self) -> bool:
        return self.abandoned_count >= self.cap

    def register(
        self,
        invocation_id: str,
        node_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        consume_task_result(task)
        key = id(task)
        if task.done():
            self._items.pop(key, None)
            return
        work = AbandonedWork(invocation_id, node_id, time.monotonic())
        self._items[key] = (work, task)

        def _release(done: asyncio.Task[Any]) -> None:
            current = self._items.get(key)
            if current is not None and current[1] is done:
                self._items.pop(key, None)
            consume_task_result(done)

        task.add_done_callback(_release)

    def _drop_done(self) -> None:
        for key, (_, task) in list(self._items.items()):
            if task.done():
                consume_task_result(task)
                self._items.pop(key, None)


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


def consume_task_result(task: asyncio.Task[Any]) -> None:
    """Observe a task result so abandoned work is not an unhandled warning."""

    def _consume(done: asyncio.Task[Any]) -> None:
        if done.cancelled():
            return
        try:
            done.exception()
        except (asyncio.CancelledError, asyncio.InvalidStateError):
            return

    if task.done():
        _consume(task)
        return
    task.add_done_callback(_consume)


@dataclass(frozen=True)
class ProviderDispatch:
    """Provider registry plus the resources provider decisions need."""

    providers: Mapping[str, Provider]
    prompts: Mapping[str, str] | None = None
    cache: DecisionCache | None = None


def spawn_invocations(
    effects: list[Mapping[str, Any]],
    *,
    handlers: Mapping[str, Handler],
    nodes: Mapping[str, Any],
    pending: dict[str, asyncio.Task[HandlerOutcome]],
    semaphore: CapacityLimiter,
    provider_dispatch: ProviderDispatch,
    now_ms: NowMs,
) -> None:
    for effect in effects:
        kind = effect["type"]
        if kind == "invoke_handler":
            task = _run_handler(effect, handlers, nodes, semaphore, now_ms)
        elif kind == "invoke_provider":
            task = _run_provider(effect, provider_dispatch, semaphore, now_ms)
        else:
            continue
        invocation_id = effect["invocation_id"]
        pending[invocation_id] = asyncio.create_task(
            task,
            name=f"swe:{invocation_id}",
        )


def cancel_invocations(
    effects: list[Mapping[str, Any]],
    pending: dict[str, asyncio.Task[HandlerOutcome]],
) -> None:
    for effect in effects:
        if effect["type"] != "cancel_invocation":
            continue
        task = pending.get(effect["invocation_id"])
        if task is not None and not task.done():
            task.cancel()


def cancel_all(pending: dict[str, asyncio.Task[HandlerOutcome]]) -> None:
    for task in pending.values():
        if not task.done():
            task.cancel()


async def reap_owned(
    pending: dict[str, asyncio.Task[HandlerOutcome]],
    cancel_task: asyncio.Task[bool] | None,
    abandoned: AbandonedWorkRegistry,
    invocation_nodes: Mapping[str, str] | None = None,
) -> None:
    """Cancel leftover work, wait a short grace, then detach.

    Must not await abandoned tasks. The caller's ``finally`` uses this so a
    handler that ignores cancel cannot hang ``run`` / ``resume``.
    """

    nodes = dict(invocation_nodes or {})
    owned = list(pending.items())
    pending.clear()
    still: list[tuple[str, asyncio.Task[HandlerOutcome]]] = []
    for invocation_id, task in owned:
        if task.done():
            consume_task_result(task)
            continue
        task.cancel()
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


async def _run_handler(
    effect: Mapping[str, Any],
    handlers: Mapping[str, Handler],
    nodes: Mapping[str, Any],
    semaphore: CapacityLimiter,
    now_ms: NowMs,
) -> HandlerOutcome:
    invocation_id = effect["invocation_id"]
    node_id = effect["node_id"]
    try:
        async with semaphore:
            output = await handlers[effect["handler"]](effect["input"])
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


async def _run_provider(
    effect: Mapping[str, Any],
    dispatch: ProviderDispatch,
    semaphore: CapacityLimiter,
    now_ms: NowMs,
) -> HandlerOutcome:
    invocation_id = effect["invocation_id"]
    node_id = effect["node_id"]
    try:
        request = _provider_request(effect, dispatch.prompts)
        provider = dispatch.providers[effect["provider"]]
        async with semaphore:
            answer, cache_state = await _call_provider(provider, request, dispatch.cache)
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
) -> tuple[Mapping[str, Any], str | None]:
    if request["cache_policy"] != "exact":
        return await _invoke_provider(provider, request), None
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
    answer, hit = await cache.resolve(
        key,
        lambda: _invoke_provider(provider, request),
        provider=request["provider"],
        prompt_key=request["prompt_key"],
    )
    return answer, "hit" if hit else "miss"


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
