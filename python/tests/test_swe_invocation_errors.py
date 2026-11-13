"""Structured InvocationRecord.error: mapping plus legacy string."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from types import MappingProxyType

import pytest

pytest.importorskip("swe_native")

from swe.invocation_error import invocation_error_from_exception, normalize_invocation_error
from swe.runtime import WorkflowRunner, WorkflowResources, require_native
from swe.runtime_clock import FrozenClock
from swe.runtime_errors import SweHandlerError, SweRuntimeError
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


# --- Review regressions -----------------------------------------------------

_KERNEL_KINDS = frozenset(
    {
        "preflight",
        "expression",
        "path",
        "type",
        "limit",
        "timeout",
        "idempotency",
        "unknown_invocation",
        "conflict",
        "cancelled",
        "handler",
        "unsupported",
        "internal",
    }
)


class _JsonRoundTripStore(MemoryRunStore):
    """A RunStore that persists InvocationRecord.error as a JSON value."""

    async def record(self, run, *, events=(), invocation=None):  # type: ignore[no-untyped-def]
        if invocation is not None:
            invocation = InvocationRecord(
                invocation_id=invocation.invocation_id,
                node_id=invocation.node_id,
                ok=invocation.ok,
                value=invocation.value,
                error=json.loads(json.dumps(invocation.error)),
            )
        await super().record(run, events=events, invocation=invocation)


async def _journal_only(
    source: MemoryRunStore, run_id: str, compiled: object
) -> MemoryRunStore:
    """Simulate a crash right after the invocation record was persisted."""

    store = _JsonRoundTripStore()
    await store.record(
        RunRecord(
            run_id=run_id,
            definition_sha256=compiled.sha256,  # type: ignore[attr-defined]
            status="running",
            input={},
            output=None,
            error=None,
            started_at_ms=0,
            updated_at_ms=0,
            finished_at_ms=None,
            last_seq=0,
        )
    )
    for invocation in (await source.replay_results(run_id)).values():
        await store.record(
            (await store.load_run(run_id)),  # type: ignore[arg-type]
            invocation=invocation,
        )
    return store


def _run_failed(result: object) -> dict[str, object]:
    events = [e for e in result.events if e["type"] == "RunFailed"]  # type: ignore[attr-defined]
    assert len(events) == 1
    return dict(events[0]["payload"])


async def _assert_live_replay_parity(handler: object, run_id: str) -> dict[str, object]:
    """Run once, then resume from the journal alone; both must agree."""

    compiled = _task_flow()
    store = _JsonRoundTripStore()
    runner = WorkflowRunner(
        compiled,
        {"demo.work": handler},  # type: ignore[dict-item]
        resources=WorkflowResources(),
        run_store=store,
        clock=FrozenClock(1),
    )
    first = await runner.run({}, run_id=run_id)
    assert first.status == "failed"
    assert first.error is not None
    recorded = list((await store.replay_results(run_id)).values())
    assert len(recorded) == 1
    record_error = recorded[0].error
    assert isinstance(record_error, dict)
    json.dumps(record_error)

    calls: list[object] = []

    async def must_not_run(payload: dict[str, object]) -> dict[str, str]:
        calls.append(payload)
        raise AssertionError("replay must not invoke the handler")

    replay_store = await _journal_only(store, run_id, compiled)
    replayer = WorkflowRunner(
        compiled,
        {"demo.work": must_not_run},
        resources=WorkflowResources(),
        run_store=replay_store,
        clock=FrozenClock(1),
    )
    resumed = await replayer.resume(run_id)
    assert calls == []
    assert resumed.status == "failed"
    assert resumed.error == first.error
    assert _run_failed(resumed) == _run_failed(first)
    return record_error


@pytest.mark.parametrize("legacy", ["", "   "])
async def test_legacy_empty_string_error_replays_as_replayed_failure(legacy: str) -> None:
    # P1-a: an empty legacy message must still resume.
    assert normalize_invocation_error(legacy)["message"] == "replayed failure"
    compiled = _task_flow()
    native = require_native()
    started = native.Machine().start(
        thaw_json(compiled.document), compiled.sha256, "legacy-empty", {}, 0
    )
    invocation_id = started["effects"][0]["invocation_id"]
    store = MemoryRunStore()
    await store.record(
        RunRecord(
            run_id="legacy-empty",
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
        invocation=InvocationRecord(invocation_id, "work", False, None, legacy),
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
    resumed = await runner.resume("legacy-empty")
    assert resumed.status == "failed"
    assert resumed.error is not None
    assert resumed.error["kind"] == "handler"
    assert resumed.error["message"] == "replayed failure"


@pytest.mark.parametrize(
    "make_error",
    [
        lambda: SweHandlerError(""),
        lambda: SweHandlerError("   ", kind="timeout"),
        lambda: SweRuntimeError(""),
    ],
    ids=["handler-error", "handler-error-whitespace", "base-runtime-error"],
)
async def test_empty_handler_message_fails_cleanly_and_replays(make_error: object) -> None:
    # P1-b: both raise sites (_run_handler and the task-exception branch).
    async def boom(_payload: dict[str, object]) -> dict[str, str]:
        raise make_error()  # type: ignore[operator]

    record_error = await _assert_live_replay_parity(boom, "empty-message")
    assert record_error["message"].strip()


@pytest.mark.parametrize("kind", [" timeout", "timeout ", "\ttimeout\n"])
@pytest.mark.parametrize("error_type", [SweHandlerError, SweRuntimeError])
async def test_padded_kind_gives_the_same_kernel_kind_live_and_on_replay(
    kind: str, error_type: type
) -> None:
    # P2-a: normalize once, the same way, on both paths. SweRuntimeError takes
    # the task-exception branch of _report_task; SweHandlerError does not.
    async def boom(_payload: dict[str, object]) -> dict[str, str]:
        raise error_type("deadline", kind=kind, path="nodes.work")

    record_error = await _assert_live_replay_parity(boom, "padded-kind")
    assert record_error["kind"] == "timeout"


async def test_live_record_error_is_the_normalized_mapping() -> None:
    async def boom(_payload: dict[str, object]) -> dict[str, str]:
        raise SweHandlerError("x", kind=" host_kind ", payload={"a": [1, 2]})

    record_error = await _assert_live_replay_parity(boom, "normalized-record")
    assert record_error == normalize_invocation_error(record_error)
    assert record_error == {
        "kind": "host_kind",
        "message": "x",
        "path": "",
        "payload": {"a": [1, 2]},
    }


def _cyclic() -> dict[str, object]:
    value: dict[str, object] = {}
    value["self"] = value
    return value


def _deep() -> dict[str, object]:
    value: dict[str, object] = {}
    for _ in range(100_000):
        value = {"x": value}
    return value


@pytest.mark.parametrize(
    "payload",
    [
        {"x": float("nan")},
        {"x": object()},
        {1: "non-string key"},
        _cyclic(),
        _deep(),
    ],
    ids=["nan", "object", "int-key", "cycle", "deep"],
)
async def test_invalid_payload_degrades_to_a_handler_error(payload: object) -> None:
    # P3-a: payload is validated as JSON before the kernel fail.
    normalized = normalize_invocation_error(
        {"kind": "timeout", "message": "deadline", "payload": payload}
    )
    assert normalized["kind"] == "handler"
    assert "payload" not in normalized
    json.dumps(normalized)

    async def boom(_payload: dict[str, object]) -> dict[str, str]:
        raise SweHandlerError("deadline", kind="timeout", payload=payload)  # type: ignore[arg-type]

    record_error = await _assert_live_replay_parity(boom, "bad-payload")
    assert record_error["kind"] == "handler"
    assert "payload" not in record_error


def test_payload_is_thawed_to_plain_json() -> None:
    normalized = normalize_invocation_error(
        {"kind": "handler", "message": "m", "payload": MappingProxyType({"a": (1, 2)})}
    )
    assert normalized["payload"] == {"a": [1, 2]}
    assert type(normalized["payload"]) is dict


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "legacy",
        None,
        {"kind": " timeout", "message": "m"},
        {"message": "m"},
        {"kind": "", "message": "m"},
        {"kind": "host_kind", "message": "m", "payload": {"x": float("nan")}},
    ],
)
def test_normalize_is_idempotent(value: object) -> None:
    once = normalize_invocation_error(value)
    assert normalize_invocation_error(once) == once


class _CustomMapping(Mapping[str, object]):
    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def _native_fail(error: object) -> dict[str, object]:
    compiled = _task_flow()
    machine = require_native().Machine()
    started = machine.start(thaw_json(compiled.document), compiled.sha256, "parity", {}, 0)
    machine.fail(started["effects"][0]["invocation_id"], error, 1)
    result = machine.error()
    assert result is not None
    return dict(result)


@pytest.mark.parametrize(
    ("value", "kernel_kind"),
    [
        ({"message": "m"}, "handler"),
        ({"kind": None, "message": "m"}, "handler"),
        ({"kind": "", "message": "m"}, "handler"),
        ({"kind": "  ", "message": "m"}, "handler"),
        ({"kind": " timeout", "message": "m"}, "timeout"),
        ({"kind": "host_kind", "message": "m"}, "handler"),
        (MappingProxyType({"kind": "timeout", "message": "m"}), "timeout"),
        (_CustomMapping({"kind": "timeout", "message": "m", "path": "p"}), "timeout"),
        ({"kind": "timeout", "message": "m", "path": None}, "timeout"),
    ],
)
def test_python_and_rust_accept_the_same_mappings(value: object, kernel_kind: str) -> None:
    # P3-b: same accept outcome and same kernel kind on both sides.
    normalized = normalize_invocation_error(value)
    expected = normalized["kind"] if normalized["kind"] in _KERNEL_KINDS else "handler"
    assert expected == kernel_kind
    assert _native_fail(value)["kind"] == kernel_kind
    assert _native_fail(normalized)["kind"] == kernel_kind


@pytest.mark.parametrize(
    ("value", "error_type"),
    [
        ({"kind": "timeout"}, ValueError),
        ({"kind": "timeout", "message": None}, ValueError),
        ({"kind": "timeout", "message": ""}, ValueError),
        ({"kind": "timeout", "message": "  "}, ValueError),
        ({"kind": "timeout", "message": 7}, TypeError),
        ({"kind": 7, "message": "m"}, TypeError),
        ({"kind": "timeout", "message": "m", "path": 7}, TypeError),
        (7, TypeError),
    ],
)
def test_python_and_rust_reject_the_same_mappings(value: object, error_type: type) -> None:
    with pytest.raises(error_type):
        normalize_invocation_error(value)
    with pytest.raises(error_type):
        _native_fail(value)
