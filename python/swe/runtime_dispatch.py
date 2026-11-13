"""asyncio handler and provider dispatch. Never wait while the Rust lock is held.

`invoke_handler` calls a registered task handler. `invoke_provider` renders the
decision prompt from the active prompt map, calls the registered provider, and
validates the returned choice against the node's declared choices. With
`cache_policy: exact` the answer goes through the shared L1/L2 decision cache
and the cache state is part of the completion payload.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from swe.prompt_render import render_prompt
from swe.cache import DecisionCache, assert_cache_isolation, decision_cache_key
from swe.invocation_error import invocation_error_from_exception
from swe.runtime_errors import SweHandlerError
from swe.runtime_validate import require_json_value, validate_output_schema

Handler = Callable[[Mapping[str, Any]], Awaitable[Any]]
Provider = Callable[[Mapping[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class HandlerOutcome:
    invocation_id: str
    node_id: str
    ok: bool
    value: Any
    cancelled: bool = False


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
) -> None:
    for effect in effects:
        kind = effect["type"]
        if kind == "invoke_handler":
            task = _run_handler(effect, handlers, nodes, semaphore)
        elif kind == "invoke_provider":
            task = _run_provider(effect, provider_dispatch, semaphore)
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
) -> None:
    owned: list[asyncio.Task[Any]] = list(pending.values())
    if cancel_task is not None:
        owned.append(cancel_task)
    pending.clear()
    for task in owned:
        if not task.done():
            task.cancel()
    if owned:
        await asyncio.gather(*owned, return_exceptions=True)


async def _run_handler(
    effect: Mapping[str, Any],
    handlers: Mapping[str, Handler],
    nodes: Mapping[str, Any],
    semaphore: asyncio.Semaphore,
) -> HandlerOutcome:
    invocation_id = effect["invocation_id"]
    node_id = effect["node_id"]
    try:
        async with semaphore:
            output = await handlers[effect["handler"]](effect["input"])
        output = require_json_value(output, node_id=node_id)
        validate_output_schema(nodes[node_id], output, node_id=node_id)
        return HandlerOutcome(invocation_id, node_id, True, output)
    except asyncio.CancelledError:
        return HandlerOutcome(invocation_id, node_id, False, None, cancelled=True)
    except SweHandlerError as exc:
        return HandlerOutcome(
            invocation_id, node_id, False, invocation_error_from_exception(exc)
        )


async def _run_provider(
    effect: Mapping[str, Any],
    dispatch: ProviderDispatch,
    semaphore: asyncio.Semaphore,
) -> HandlerOutcome:
    invocation_id = effect["invocation_id"]
    node_id = effect["node_id"]
    try:
        request = _provider_request(effect, dispatch.prompts)
        provider = dispatch.providers[effect["provider"]]
        async with semaphore:
            answer, cache_state = await _call_provider(provider, request, dispatch.cache)
        output = _provider_output(effect, answer, cache_state)
        return HandlerOutcome(invocation_id, node_id, True, output)
    except asyncio.CancelledError:
        return HandlerOutcome(invocation_id, node_id, False, None, cancelled=True)
    except SweHandlerError as exc:
        return HandlerOutcome(
            invocation_id, node_id, False, invocation_error_from_exception(exc)
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
