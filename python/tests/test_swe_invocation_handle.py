"""InvocationHandle remaining time, reason, and ContextVar binding."""

from __future__ import annotations

import asyncio

from swe import InvocationHandle, current_invocation, invocation_handle
from swe.invocation_handle import (
    AbandonedWorkRegistry,
    make_invocation_handle,
    request_cancel,
)


def test_remaining_seconds_clamps_at_zero() -> None:
    handle = make_invocation_handle(
        invocation_id="run:work:root:0:1",
        node_id="work",
        deadline_ms=5_000,
    )
    assert handle.remaining_seconds(0) == 5.0
    assert handle.remaining_seconds(2_500) == 2.5
    assert handle.remaining_seconds(5_000) == 0.0
    assert handle.remaining_seconds(9_000) == 0.0
    assert handle.reason() is None
    assert not handle.cancel.is_set()


async def test_reason_first_wins_and_event_is_set_before_task_cancel() -> None:
    handle = make_invocation_handle(
        invocation_id="run:work:root:0:1",
        node_id="work",
        deadline_ms=1_000,
    )

    async def work() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert handle.cancel.is_set()
            assert handle.reason() == "timeout"
            raise

    task = asyncio.create_task(work())
    await asyncio.sleep(0)
    request_cancel(handle, task, "timeout")
    request_cancel(handle, task, "cancel")
    assert handle.reason() == "timeout"
    assert handle.cancel.is_set()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()


def test_current_invocation_default_is_none() -> None:
    assert current_invocation() is None
    handle = make_invocation_handle(
        invocation_id="i", node_id="n", deadline_ms=1
    )
    assert isinstance(handle, InvocationHandle)


async def test_abandoned_registry_observability_and_cap() -> None:
    registry = AbandonedWorkRegistry(cap=1)

    async def never() -> None:
        await asyncio.Event().wait()

    first = asyncio.create_task(never(), name="swe:one")
    second = asyncio.create_task(never(), name="swe:two")
    await asyncio.sleep(0)
    registry.register("one", "a", first)
    assert registry.abandoned_count == 1
    assert registry.invocation_ids() == ("one",)
    snap = registry.snapshot()
    assert len(snap) == 1
    assert snap[0].invocation_id == "one"
    assert snap[0].age_seconds() >= 0
    assert registry.at_capacity()
    registry.register("two", "b", second)
    assert registry.abandoned_count == 2
    first.cancel()
    second.cancel()
    await asyncio.gather(first, second, return_exceptions=True)
    assert registry.abandoned_count == 0


async def test_abandoned_registry_keeps_distinct_tasks_with_same_invocation_id() -> None:
    registry = AbandonedWorkRegistry(cap=1)

    async def never() -> None:
        await asyncio.Event().wait()

    first = asyncio.create_task(never(), name="swe:one")
    second = asyncio.create_task(never(), name="swe:one-retry")
    await asyncio.sleep(0)
    registry.register("same", "a", first)
    registry.register("same", "a", first)
    assert registry.abandoned_count == 1
    registry.register("same", "a", second)
    assert registry.abandoned_count == 2
    assert registry.invocation_ids() == ("same", "same")
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert registry.abandoned_count == 1
    assert registry.invocation_ids() == ("same",)
    second.cancel()
    await asyncio.gather(second, return_exceptions=True)
    assert registry.abandoned_count == 0


async def test_abandoned_registry_deduplicates_before_result_observer(
    monkeypatch,
) -> None:
    registry = AbandonedWorkRegistry(cap=1)
    observed: list[asyncio.Task[object]] = []

    def observe(task: asyncio.Task[object]) -> None:
        observed.append(task)

    monkeypatch.setattr(invocation_handle, "consume_task_result", observe)
    task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    registry.register("same", "node", task)
    registry.register("same", "node", task)
    assert observed == [task]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
