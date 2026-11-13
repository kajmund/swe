"""Thin asyncio adapter around the Rust SWE machine.

Python compiles SWD, owns the event loop, and runs registered async handlers and
decision providers. Rust owns routing, join barriers, and typed events. The
adapter never calls into Rust while awaiting a handler or a provider.

Deadline: the driver waits with the remaining monotonic timeout and calls
`Machine.tick(now_ms)` when it expires. Tick fails the run and emits
`CancelInvocation` for open handlers. Handlers that swallow
`CancelledError` are not force-stopped; terminal status stays protected and
late results do not route again.

Persistence: `WorkflowRunner(..., run_store=store)` writes every transition to
a durable store as one transaction. `WorkflowRunner.resume` / `resume_run`
replays the deterministic machine under the same run id, start time and input,
completing invocations whose result is already persisted instead of calling
their handler again. `run_workflow` keeps the phase-2 signature and is an
unpersisted runner.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from swe.definition import CompiledDefinition
from swe.invocation_error import live_invocation_error, normalize_invocation_error
from swe.runtime_clock import FrozenClock, WorkflowClock
from swe.runtime_dispatch import (
    Handler,
    HandlerOutcome,
    ProviderDispatch,
    cancel_all,
    cancel_invocations,
    reap_owned,
    spawn_invocations,
)
from swe.runtime_errors import SweRuntimeError
from swe.runtime_preflight import (
    assert_handlers_registered,
    assert_phase2_supported,
    assert_providers_registered,
)
from swe.run_store import (
    InvocationRecord,
    RunRecord,
    RunStore,
    WorkflowResources,
)
from swe.runtime_thaw import thaw_json


@dataclass
class _Driver:
    machine: Any
    handlers: Mapping[str, Handler]
    provider_dispatch: ProviderDispatch
    nodes: Mapping[str, Any]
    clock: WorkflowClock | FrozenClock
    cancel_event: asyncio.Event | None
    pending: dict[str, asyncio.Task[HandlerOutcome]]
    semaphore: asyncio.Semaphore
    run_id: str
    definition_sha256: str
    input_data: Mapping[str, Any]
    started_at_ms: int
    run_store: RunStore | None
    replay: dict[str, InvocationRecord]
    persisted_events: int
    last_seq: int
    invocation_nodes: dict[str, str] = field(default_factory=dict)
    cancel_task: asyncio.Task[bool] | None = None


try:
    import swe_native
except ImportError as exc:  # pragma: no cover - exercised by importorskip tests
    swe_native = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str
    output: Any
    events: list[dict[str, Any]]
    error: dict[str, Any] | None


@dataclass(frozen=True)
class _RunStart:
    machine: Any
    document: Mapping[str, Any]
    clock: WorkflowClock | FrozenClock
    run_id: str
    input_data: Mapping[str, Any]
    started_at_ms: int
    replay: dict[str, InvocationRecord]
    persisted_events: int
    last_seq: int


def require_native() -> Any:
    if swe_native is None:
        raise SweRuntimeError(
            "swe_native is not built; install the swe-native wheel (maturin build --manifest-path crates/swe-native/Cargo.toml) or run `make swe-native` in the host repository",
            kind="preflight",
        ) from _IMPORT_ERROR
    return swe_native


class WorkflowRunner:
    """Run configuration: handlers, resources and an optional durable store."""

    def __init__(
        self,
        compiled: CompiledDefinition,
        handlers: Mapping[str, Handler],
        *,
        resources: WorkflowResources | None = None,
        run_store: RunStore | None = None,
        clock: WorkflowClock | FrozenClock | None = None,
        concurrency: int = 32,
    ) -> None:
        self.compiled = compiled
        self.handlers = handlers
        self.resources = resources or WorkflowResources()
        self.run_store = run_store
        self.clock = clock
        self.concurrency = concurrency

    async def run(
        self,
        input_data: Mapping[str, Any] | None = None,
        *,
        run_id: str | None = None,
    ) -> RunResult:
        """Start a new run; with a store every transition is persisted."""
        native = require_native()
        document = thaw_json(self.compiled.document)
        assert_phase2_supported(document)
        assert_handlers_registered(document, self.handlers)
        assert_providers_registered(
            document,
            self.resources.providers,
            self.resources.prompts,
            self.resources.cache,
            prompt_keys=self.resources.prompt_keys,
        )
        machine = native.Machine()
        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        clock = self.clock or WorkflowClock()
        payload = thaw_json(input_data or {})
        started_at_ms = clock.now_ms()
        outcome = machine.start(document, self.compiled.sha256, run_id, payload, started_at_ms)
        driver = _build_driver(self, _RunStart(
            machine=machine,
            document=document,
            clock=clock,
            run_id=run_id,
            input_data=payload,
            started_at_ms=started_at_ms,
            replay={},
            persisted_events=0,
            last_seq=0,
        ))
        try:
            await _persist(driver)
            return await self._execute(driver, outcome)
        finally:
            await reap_owned(driver.pending, driver.cancel_task)
            driver.cancel_task = None

    async def resume(self, run_id: str) -> RunResult:
        """Continue a persisted run after a crash; terminal runs return as-is."""
        store = self.run_store
        if store is None:
            raise SweRuntimeError("resume requires a run store", kind="resume")
        stored = await store.load_run(run_id)
        if stored is None:
            raise SweRuntimeError(f"no persisted run {run_id!r}", kind="resume")
        if stored.definition_sha256 != self.compiled.sha256:
            raise SweRuntimeError(
                f"persisted run {run_id!r} was started from another definition",
                kind="resume",
            )
        if stored.status != "running":
            return RunResult(
                run_id=run_id,
                status=stored.status,
                output=stored.output,
                events=await store.events(run_id),
                error=None if stored.error is None else dict(stored.error),
            )
        native = require_native()
        document = thaw_json(self.compiled.document)
        assert_phase2_supported(document)
        assert_handlers_registered(document, self.handlers)
        assert_providers_registered(
            document,
            self.resources.providers,
            self.resources.prompts,
            self.resources.cache,
            prompt_keys=self.resources.prompt_keys,
        )
        machine = native.Machine()
        clock = self.clock or WorkflowClock()
        events = await store.events(run_id)
        outcome = machine.start(
            document,
            self.compiled.sha256,
            run_id,
            dict(stored.input),
            stored.started_at_ms,
        )
        driver = _build_driver(self, _RunStart(
            machine=machine,
            document=document,
            clock=clock,
            run_id=run_id,
            input_data=dict(stored.input),
            started_at_ms=stored.started_at_ms,
            replay=dict(await store.replay_results(run_id)),
            persisted_events=len(events),
            last_seq=stored.last_seq,
        ))
        try:
            return await self._execute(driver, outcome)
        finally:
            await reap_owned(driver.pending, driver.cancel_task)
            driver.cancel_task = None

    async def _execute(self, driver: _Driver, outcome: Mapping[str, Any]) -> RunResult:
        await _drive(driver, outcome)
        return _run_result(driver)


def _build_driver(runner: WorkflowRunner, start: _RunStart) -> _Driver:
    return _Driver(
        machine=start.machine,
        handlers=runner.handlers,
        provider_dispatch=ProviderDispatch(
            providers=runner.resources.providers,
            prompts=runner.resources.prompts,
            cache=runner.resources.cache,
        ),
        nodes=start.document["nodes"],
        clock=start.clock,
        cancel_event=runner.resources.cancel_event,
        pending={},
        semaphore=asyncio.Semaphore(runner.concurrency),
        run_id=start.run_id,
        definition_sha256=runner.compiled.sha256,
        input_data=start.input_data,
        started_at_ms=start.started_at_ms,
        run_store=runner.run_store,
        replay=start.replay,
        persisted_events=start.persisted_events,
        last_seq=start.last_seq,
    )


async def run_workflow(
    compiled: CompiledDefinition,
    handlers: Mapping[str, Handler],
    input_data: Mapping[str, Any] | None = None,
    *,
    resources: WorkflowResources | None = None,
    run_id: str | None = None,
    clock: WorkflowClock | FrozenClock | None = None,
    concurrency: int = 32,
) -> RunResult:
    """Unpersisted run. Use `WorkflowRunner(run_store=...)` for durable runs."""
    runner = WorkflowRunner(
        compiled,
        handlers,
        resources=resources,
        clock=clock,
        concurrency=concurrency,
    )
    return await runner.run(input_data, run_id=run_id)


async def resume_run(
    compiled: CompiledDefinition,
    handlers: Mapping[str, Handler],
    *,
    resources: WorkflowResources | None = None,
    run_store: RunStore,
    run_id: str,
    clock: WorkflowClock | FrozenClock | None = None,
    concurrency: int = 32,
) -> RunResult:
    """Continue a persisted run after a crash; terminal runs return as-is."""
    runner = WorkflowRunner(
        compiled,
        handlers,
        resources=resources,
        run_store=run_store,
        clock=clock,
        concurrency=concurrency,
    )
    return await runner.resume(run_id)


async def _drive(driver: _Driver, outcome: Mapping[str, Any]) -> None:
    await _apply_effects(driver, outcome["effects"])
    while driver.machine.status() == "running":
        if _cancelled(driver):
            await _cancel_run(driver)
            break
        if not driver.pending:
            if _remaining_seconds(driver) <= 0:
                await _timeout_run(driver)
            break
        await _apply_one(driver)
    if driver.run_store is not None:
        await _persist(driver)


async def _cancel_run(driver: _Driver) -> None:
    outcome = driver.machine.cancel(driver.clock.now_ms())
    await _persist(driver)
    await _apply_effects(driver, outcome["effects"])


async def _persist(driver: _Driver, invocation: InvocationRecord | None = None) -> None:
    if driver.run_store is None:
        return
    machine = driver.machine
    events = list(machine.events())
    new_events = events[driver.persisted_events:]
    driver.persisted_events = len(events)
    if new_events:
        driver.last_seq = int(new_events[-1]["seq"])
    status = machine.status()
    now = driver.clock.now_ms()
    await driver.run_store.record(
        RunRecord(
            run_id=driver.run_id,
            definition_sha256=driver.definition_sha256,
            status=status,
            input=driver.input_data,
            output=machine.output(),
            error=machine.error(),
            started_at_ms=driver.started_at_ms,
            updated_at_ms=now,
            finished_at_ms=now if status != "running" else None,
            last_seq=driver.last_seq,
        ),
        events=new_events,
        invocation=invocation,
    )


async def _apply_effects(driver: _Driver, effects: Sequence[Mapping[str, Any]]) -> None:
    queue = list(effects)
    while queue:
        produced: list[Mapping[str, Any]] = []
        for effect in queue:
            produced.extend(await _apply_effect(driver, effect))
        queue = produced


async def _apply_effect(
    driver: _Driver, effect: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    if effect["type"] == "cancel_invocation":
        cancel_invocations([effect], driver.pending)
        return []
    if driver.machine.status() != "running":
        return []
    invocation_id = effect["invocation_id"]
    replayed = driver.replay.pop(invocation_id, None)
    if replayed is None:
        driver.invocation_nodes[invocation_id] = str(effect["node_id"])
        spawn_invocations(
            [effect],
            handlers=driver.handlers,
            nodes=driver.nodes,
            pending=driver.pending,
            semaphore=driver.semaphore,
            provider_dispatch=driver.provider_dispatch,
        )
        return []
    outcome = _apply_replay(driver, replayed)
    await _persist(driver)
    return list(outcome["effects"])


def _apply_replay(driver: _Driver, record: InvocationRecord) -> dict[str, Any]:
    now = driver.clock.now_ms()
    if record.ok:
        return driver.machine.complete(record.invocation_id, record.value, now)
    error = normalize_invocation_error(record.error)
    return driver.machine.fail(record.invocation_id, error, now)


def _cancelled(driver: _Driver) -> bool:
    return driver.cancel_event is not None and driver.cancel_event.is_set()


def _remaining_seconds(driver: _Driver) -> float:
    deadline = driver.machine.deadline_ms()
    now = driver.clock.now_ms()
    if now >= deadline:
        return 0.0
    return (deadline - now) / 1000.0


async def _timeout_run(driver: _Driver) -> None:
    now = max(driver.clock.now_ms(), driver.machine.deadline_ms())
    outcome = driver.machine.tick(now)
    await _persist(driver)
    await _apply_effects(driver, outcome["effects"])


async def _apply_one(driver: _Driver) -> None:
    remaining = _remaining_seconds(driver)
    if remaining <= 0:
        await _timeout_run(driver)
        return
    watch = set(driver.pending.values())
    if driver.cancel_event is not None:
        if driver.cancel_task is None or driver.cancel_task.done():
            driver.cancel_task = asyncio.create_task(driver.cancel_event.wait(), name="swe:cancel")
        watch.add(driver.cancel_task)
    finished, _ = await asyncio.wait(
        watch,
        return_when=asyncio.FIRST_COMPLETED,
        timeout=remaining,
    )
    if _cancelled(driver):
        return
    if not finished:
        await _timeout_run(driver)
        return
    tasks = [task for task in finished if task.get_name() != "swe:cancel"]
    if not tasks:
        return
    task = tasks[0]
    invocation_id = task.get_name().removeprefix("swe:")
    driver.pending.pop(invocation_id, None)
    if task.cancelled():
        return
    outcome, invocation = _report_task(driver, invocation_id, task)
    if invocation is None:
        return
    await _persist(driver, invocation)
    await _apply_effects(driver, outcome["effects"])
    if outcome["status"] != "running":
        cancel_all(driver.pending)


def _report_task(
    driver: _Driver,
    invocation_id: str,
    task: asyncio.Task[HandlerOutcome],
) -> tuple[dict[str, Any], InvocationRecord | None]:
    error = task.exception()
    if error is not None:
        payload = live_invocation_error(error)
        outcome = driver.machine.fail(invocation_id, payload, driver.clock.now_ms())
        node_id = driver.invocation_nodes.pop(invocation_id, "")
        return outcome, InvocationRecord(invocation_id, node_id, False, None, payload)
    result = task.result()
    if result.cancelled:
        driver.invocation_nodes.pop(invocation_id, None)
        return {"status": "running", "effects": []}, None
    mapped_node_id = driver.invocation_nodes.pop(invocation_id, "")
    node_id = result.node_id or mapped_node_id
    if result.ok:
        outcome = driver.machine.complete(invocation_id, result.value, driver.clock.now_ms())
        return outcome, InvocationRecord(invocation_id, node_id, True, result.value, None)
    payload = live_invocation_error(result.value)
    outcome = driver.machine.fail(invocation_id, payload, driver.clock.now_ms())
    return outcome, InvocationRecord(invocation_id, node_id, False, None, payload)


def _run_result(driver: _Driver) -> RunResult:
    return RunResult(
        run_id=driver.run_id,
        status=driver.machine.status(),
        output=driver.machine.output(),
        events=list(driver.machine.events()),
        error=driver.machine.error(),
    )
