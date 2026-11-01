"""Reserved SWD features fail closed before a run starts."""

from __future__ import annotations

import pytest

pytest.importorskip("swe_native")

from swe.definition import compile_definition
from swe.runtime import WorkflowResources, run_workflow
from swe.runtime_errors import SwePreflightError, SweUnsupportedError
from swe.yaml_load import load_swd_json
from swe_helpers import (
    EXAMPLE_HANDLERS,
    EXAMPLE_PROMPT_KEY,
    EXAMPLE_PROMPT_KEYS,
    EXAMPLE_PROVIDERS,
    FIXTURES,
)
from swe_runtime_helpers import compile_runtime

pytestmark = pytest.mark.swe_runtime

PROVIDER_FIXTURE = {
    "version": "1.0",
    "name": "provider_fixture",
    "entrypoint": "evaluate",
    "limits": {"max_steps": 10, "max_depth": 2, "timeout_seconds": 30},
    "nodes": {
        "evaluate": {
            "type": "decision",
            "evaluator": "provider",
            "provider": "fake",
            "prompt_key": "demo.assessment",
            "choices": ["yes", "no"],
            "routes": {"yes": "done", "no": "done"},
        },
        "done": {"type": "end", "status": "completed"},
    },
}


async def test_research_fixture_requires_registered_provider() -> None:
    compiled = compile_definition(
        load_swd_json((FIXTURES / "provider_parallel.json").read_text(encoding="utf-8")),
        handlers=EXAMPLE_HANDLERS,
        providers=EXAMPLE_PROVIDERS,
        prompt_keys=EXAMPLE_PROMPT_KEYS,
    )
    handlers = {name: _ok for name in EXAMPLE_HANDLERS}
    with pytest.raises(SwePreflightError, match="decision provider"):
        await run_workflow(compiled, handlers, {})


async def test_provider_decision_requires_active_prompt() -> None:
    compiled = compile_definition(
        PROVIDER_FIXTURE,
        providers=frozenset({"fake"}),
        prompt_keys=EXAMPLE_PROMPT_KEYS,
    )
    resources = WorkflowResources(
        providers={"fake": _provider},
        prompt_keys=EXAMPLE_PROMPT_KEYS,
    )
    with pytest.raises(SwePreflightError, match="active prompt"):
        await run_workflow(compiled, {}, {}, resources=resources)


async def test_execution_rejects_provider_node_without_prompt_catalog() -> None:
    """Execution is fail-closed too, not only compile.

    A compiled definition can be constructed through another path, so the
    driver holds the same line: a prompt that is not in the active catalog is
    never rendered, even when its text is present.
    """
    compiled = compile_definition(
        PROVIDER_FIXTURE,
        providers=frozenset({"fake"}),
        prompt_keys=EXAMPLE_PROMPT_KEYS,
    )
    resources = WorkflowResources(
        providers={"fake": _provider},
        prompts={EXAMPLE_PROMPT_KEY: "decide"},
    )
    with pytest.raises(SwePreflightError, match="requires a prompt_keys allowlist"):
        await run_workflow(compiled, {}, {}, resources=resources)


async def test_execution_rejects_prompt_outside_active_catalog() -> None:
    """A declared prompt that the host did not permit is not rendered."""
    compiled = compile_definition(
        PROVIDER_FIXTURE,
        providers=frozenset({"fake"}),
        prompt_keys=EXAMPLE_PROMPT_KEYS,
    )
    resources = WorkflowResources(
        providers={"fake": _provider},
        prompts={EXAMPLE_PROMPT_KEY: "decide"},
        prompt_keys=frozenset({"other.prompt"}),
    )
    with pytest.raises(SwePreflightError, match="not in the active prompt catalog"):
        await run_workflow(compiled, {}, {}, resources=resources)


async def test_execution_renders_when_prompt_is_declared_and_permitted() -> None:
    """The happy path still runs: declared catalog plus permitted prompt."""
    compiled = compile_definition(
        PROVIDER_FIXTURE,
        providers=frozenset({"fake"}),
        prompt_keys=EXAMPLE_PROMPT_KEYS,
    )
    resources = WorkflowResources(
        providers={"fake": _provider},
        prompts={EXAMPLE_PROMPT_KEY: "decide"},
        prompt_keys=EXAMPLE_PROMPT_KEYS,
    )
    result = await run_workflow(compiled, {}, {}, resources=resources)
    assert result.status == "completed"


async def test_wait_is_unsupported() -> None:
    compiled = compile_runtime(
        {
            "pause": {"type": "wait", "next": "done"},
            "done": {"type": "end", "status": "completed"},
        },
        name="waiter",
        entrypoint="pause",
    )
    with pytest.raises(SweUnsupportedError, match="wait"):
        await run_workflow(compiled, {}, {})


async def test_subworkflow_is_unsupported() -> None:
    compiled = compile_runtime(
        {
            "child": {"type": "subworkflow", "workflow": "other", "next": "done"},
            "done": {"type": "end", "status": "completed"},
        },
        name="nested",
        entrypoint="child",
    )
    with pytest.raises(SweUnsupportedError, match="subworkflow"):
        await run_workflow(compiled, {}, {})


async def test_exact_cache_and_retry_are_unsupported() -> None:
    cache = compile_runtime(
        {
            "work": {
                "type": "task",
                "handler": "demo.work",
                "cache_policy": "exact",
                "next": "done",
            },
            "done": {"type": "end", "status": "completed"},
        },
        name="cached",
        entrypoint="work",
        handlers=frozenset({"demo.work"}),
    )
    with pytest.raises(SweUnsupportedError, match="cache_policy"):
        await run_workflow(cache, {"demo.work": _ok}, {})

    retry = compile_runtime(
        {
            "work": {
                "type": "task",
                "handler": "demo.work",
                "retry": {"max_attempts": 2},
                "next": "done",
            },
            "done": {"type": "end", "status": "completed"},
        },
        name="retried",
        entrypoint="work",
        handlers=frozenset({"demo.work"}),
    )
    with pytest.raises(SweUnsupportedError, match="retry"):
        await run_workflow(retry, {"demo.work": _ok}, {})


async def _ok(_payload: dict[str, object]) -> dict[str, bool]:
    return {"ok": True}


async def _provider(_request: dict[str, object]) -> str:
    return "yes"
