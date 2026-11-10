"""Structured InvocationRecord.error: mapping plus legacy string."""

from __future__ import annotations

import pytest

pytest.importorskip("swe_native")

from swe.invocation_error import invocation_error_from_exception, normalize_invocation_error
from swe.runtime import WorkflowRunner, WorkflowResources, require_native
from swe.runtime_clock import FrozenClock
from swe.runtime_errors import SweHandlerError
from swe.runtime_thaw import thaw_json
from swe.run_store import InvocationRecord, MemoryRunStore, RunRecord
from swe_runtime_helpers import compile_runtime

pytestmark = pytest.mark.swe_runtime


def _task_flow() -> object:
    return compile_runtime(
        {
            "work": {"type": "task", "handler": "demo.work", "next": "done"},
            "done": {"type": "end", "status": "completed"},
        },
        name="once",
        entrypoint="work",
        limits={"max_steps": 8, "max_depth": 1, "timeout_seconds": 1},
        handlers=frozenset({"demo.work"}),
    )


def test_normalize_legacy_string() -> None:
    assert normalize_invocation_error("boom") == {
        "kind": "handler",
        "message": "boom",
        "path": "",
    }
    assert normalize_invocation_error(None)["message"] == "replayed failure"


def test_normalize_mapping_keeps_kind_path_and_payload() -> None:
    payload = normalize_invocation_error(
        {
            "kind": "timeout",
            "message": "deadline",
            "path": "nodes.work",
            "payload": {"retryable": False},
        }
    )
    assert payload["kind"] == "timeout"
    assert payload["message"] == "deadline"
    assert payload["path"] == "nodes.work"
    assert payload["payload"] == {"retryable": False}


def test_unknown_kind_stays_on_the_mapping() -> None:
    payload = normalize_invocation_error({"kind": "host_kind", "message": "x"})
    assert payload["kind"] == "host_kind"


def test_exception_uses_swe_runtime_fields() -> None:
    exc = SweHandlerError(
        "need failed",
        kind="timeout",
        path="nodes.work",
        payload={"code": 1},
    )
    payload = invocation_error_from_exception(exc)
    assert payload["kind"] == "timeout"
    assert payload["path"] == "nodes.work"
    assert payload["payload"] == {"code": 1}


def test_machine_fail_accepts_mapping_and_legacy_string() -> None:
    compiled = _task_flow()
    native = require_native()
    machine = native.Machine()
    outcome = machine.start(
        thaw_json(compiled.document),
        compiled.sha256,
        "fail-map",
        {},
        0,
    )
    invocation_id = outcome["effects"][0]["invocation_id"]
    machine.fail(
        invocation_id,
        {"kind": "timeout", "message": "deadline", "path": "nodes.work"},
        1,
    )
    error = machine.error()
    assert error is not None
    assert error["kind"] == "timeout"
    assert error["message"] == "deadline"
    assert error["path"] == "nodes.work"

    string_machine = native.Machine()
    started = string_machine.start(
        thaw_json(compiled.document),
        compiled.sha256,
        "fail-str",
        {},
        0,
    )
    string_id = started["effects"][0]["invocation_id"]
    string_machine.fail(string_id, "legacy boom", 1)
    string_error = string_machine.error()
    assert string_error is not None
    assert string_error["kind"] == "handler"
    assert string_error["message"] == "legacy boom"


def test_unknown_host_kind_maps_to_handler_in_the_kernel() -> None:
    compiled = _task_flow()
    native = require_native()
    machine = native.Machine()
    outcome = machine.start(
        thaw_json(compiled.document),
        compiled.sha256,
        "fail-host",
        {},
        0,
    )
    invocation_id = outcome["effects"][0]["invocation_id"]
    machine.fail(
        invocation_id,
        {"kind": "host_kind", "message": "opaque", "path": ""},
        1,
    )
    error = machine.error()
    assert error is not None
    assert error["kind"] == "handler"
    assert error["message"] == "opaque"


async def test_handler_mapping_is_persisted() -> None:
    store = MemoryRunStore()
    compiled = _task_flow()

    async def boom(_payload: dict[str, object]) -> dict[str, str]:
        raise SweHandlerError("deadline", kind="timeout", path="nodes.work")

    runner = WorkflowRunner(
        compiled,
        {"demo.work": boom},
        resources=WorkflowResources(),
        run_store=store,
    )
    first = await runner.run({}, run_id="inv-err-1")
    assert first.status == "failed"
    assert first.error is not None
    assert first.error["kind"] == "timeout"
    assert first.error["path"] == "nodes.work"
    recorded = await store.replay_results("inv-err-1")
    assert len(recorded) == 1
    invocation = next(iter(recorded.values()))
    assert invocation.ok is False
    assert isinstance(invocation.error, dict)
    assert invocation.error["kind"] == "timeout"
    assert invocation.error["message"] == "deadline"
    assert invocation.error["path"] == "nodes.work"

    resumed = await runner.resume("inv-err-1")
    assert resumed.status == "failed"
    assert resumed.error is not None
    assert resumed.error["kind"] == "timeout"


async def test_legacy_string_invocation_error_replays_on_resume() -> None:
    compiled = _task_flow()
    store = MemoryRunStore()
    native = require_native()
    probe = native.Machine()
    started = probe.start(
        thaw_json(compiled.document),
        compiled.sha256,
        "legacy-live",
        {},
        0,
    )
    invocation_id = started["effects"][0]["invocation_id"]
    await store.record(
        RunRecord(
            run_id="legacy-live",
            definition_sha256=compiled.sha256,
            status="running",
            input={},
            output=None,
            error=None,
            started_at_ms=0,
            updated_at_ms=0,
            finished_at_ms=None,
            last_seq=0,
        ),
        invocation=InvocationRecord(
            invocation_id=invocation_id,
            node_id="work",
            ok=False,
            value=None,
            error="legacy boom",
        ),
    )

    async def must_not_run(_payload: dict[str, object]) -> dict[str, str]:
        raise AssertionError("legacy replay must not invoke the handler")

    runner = WorkflowRunner(
        compiled,
        {"demo.work": must_not_run},
        resources=WorkflowResources(),
        run_store=store,
        clock=FrozenClock(1),
    )
    replayed = await runner.resume("legacy-live")
    assert replayed.status == "failed"
    assert replayed.error is not None
    assert replayed.error["kind"] == "handler"
    assert "legacy boom" in replayed.error["message"]
