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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from swe.prompt_render import render_prompt
from swe.cache import DecisionCache, assert_cache_isolation, decision_cache_key
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
    semaphore: asyncio.Semaphore,
    provider_dispatch: ProviderDispatch,
    now_ms: NowMs,
    handles: Mapping[str, InvocationHandle] | None = None,
) -> None:
    bound = handles or {}
    for effect in effects:
        kind = effect["type"]
        handle = bound.get(str(effect["invocation_id"]))
        if kind == "invoke_handler":
            task = _run_handler(effect, handlers, nodes, semaphore, now_ms, handle)
        elif kind == "invoke_provider":
            task = _run_provider(effect, provider_dispatch, semaphore, now_ms, handle)
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


async def _run_handler(
    effect: Mapping[str, Any],
    handlers: Mapping[str, Handler],
    nodes: Mapping[str, Any],
    semaphore: asyncio.Semaphore,
    now_ms: NowMs,
    handle: InvocationHandle | None,
) -> HandlerOutcome:
    invocation_id = effect["invocation_id"]
    node_id = effect["node_id"]
    token = bind_current_invocation(handle)
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
    finally:
        reset_current_invocation(token)


async def _run_provider(
    effect: Mapping[str, Any],
    dispatch: ProviderDispatch,
    semaphore: asyncio.Semaphore,
    now_ms: NowMs,
    handle: InvocationHandle | None,
) -> HandlerOutcome:
    invocation_id = effect["invocation_id"]
    node_id = effect["node_id"]
    token = bind_current_invocation(handle)
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


__all__ = [
    "CANCEL_GRACE_SECONDS",
    "AbandonedWorkRegistry",
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
