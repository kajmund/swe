"""Host-visible invocation deadline and cancel handle.

Handlers keep the ``async def handler(payload)`` signature. Hosts read
``current_invocation()`` from a ContextVar bound around every handler and
provider call. SWE sets ``handle.cancel`` and ``reason`` before
``task.cancel()`` so the Event is visible before ``CancelledError``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

CancelReason: TypeAlias = Literal["timeout", "cancel"]


@dataclass
class EffectCell:
    """Opt-in external-effect mark for one invocation.

    ``delivery`` is empty unless the host declared one for this node. ``begin``
    is installed by the runner and records journal schema v1 ``AttemptStarted``
    before external I/O. ``started`` stays false until that call returns.
    """

    delivery: str | None = None
    action: str = ""
    started: bool = False
    begin: Callable[[], Awaitable[None]] | None = None
    on_confirmed: Callable[[Any, Any], Awaitable[None]] | None = None


@dataclass
class _ReasonCell:
    value: CancelReason | None = None


@dataclass(frozen=True)
class InvocationHandle:
    """Bound for one handler or provider invocation.

    ``deadline_ms`` is ``min(action deadline, workflow deadline)``. ``reason``
    is ``timeout`` for an action or workflow deadline and ``cancel`` for a host
    ``cancel_event``. It stays ``None`` until SWE signals stop.
    """

    invocation_id: str
    node_id: str
    deadline_ms: int
    cancel: asyncio.Event = field(compare=False)
    _reason: _ReasonCell = field(default_factory=_ReasonCell, repr=False, compare=False)
    effect: EffectCell = field(default_factory=EffectCell, compare=False, repr=False)

    def reason(self) -> CancelReason | None:
        return self._reason.value

    def remaining_seconds(self, now_ms: int) -> float:
        remaining_ms = int(self.deadline_ms) - int(now_ms)
        if remaining_ms <= 0:
            return 0.0
        return remaining_ms / 1000.0

    def _signal(self, reason: CancelReason) -> None:
        if self._reason.value is None:
            self._reason.value = reason
        self.cancel.set()


_CURRENT: ContextVar[InvocationHandle | None] = ContextVar(
    "swe_current_invocation", default=None
)


def current_invocation() -> InvocationHandle | None:
    """Return the handle bound around the active handler or provider call."""

    return _CURRENT.get()


def bind_current_invocation(
    handle: InvocationHandle | None,
) -> Token[InvocationHandle | None]:
    """Bind ``handle`` until :func:`reset_current_invocation`."""

    return _CURRENT.set(handle)


def reset_current_invocation(token: Token[InvocationHandle | None]) -> None:
    _CURRENT.reset(token)


def invocation_deadline_ms(
    *,
    now_ms: int,
    workflow_deadline_ms: int,
    action_timeout_seconds: int | None,
) -> int:
    """Return ``min(action deadline, workflow deadline)`` as an epoch-ms value."""

    if action_timeout_seconds is None:
        return int(workflow_deadline_ms)
    action_deadline = int(now_ms) + int(action_timeout_seconds) * 1000
    return min(action_deadline, int(workflow_deadline_ms))


def make_invocation_handle(
    *,
    invocation_id: str,
    node_id: str,
    deadline_ms: int,
) -> InvocationHandle:
    return InvocationHandle(
        invocation_id=invocation_id,
        node_id=node_id,
        deadline_ms=int(deadline_ms),
        cancel=asyncio.Event(),
    )


def request_cancel(
    handle: InvocationHandle | None,
    task: asyncio.Task[Any] | None,
    reason: CancelReason,
) -> None:
    """Publish cancel on the handle, then cancel the asyncio task.

    Order is part of the contract: ``handle.cancel`` and ``reason`` are visible
    before ``CancelledError`` is injected.
    """

    if handle is not None:
        handle._signal(reason)
    if task is not None and not task.done():
        task.cancel()


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

    Entries are keyed by ``id(task)`` so two attempts of the same invocation
    id both occupy a slot. Detaching a task never waits for its I/O. New
    dispatch fails closed when ``abandoned_count >= cap``. A task that later
    finishes frees its slot.
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
        key = id(task)
        if key in self._items:
            return
        consume_task_result(task)
        if task.done():
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


__all__ = [
    "AbandonedWork",
    "AbandonedWorkRegistry",
    "CancelReason",
    "EffectCell",
    "InvocationHandle",
    "bind_current_invocation",
    "consume_task_result",
    "current_invocation",
    "invocation_deadline_ms",
    "make_invocation_handle",
    "request_cancel",
    "reset_current_invocation",
]
