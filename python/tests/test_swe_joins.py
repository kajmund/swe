"""Fork/join branch-completion contract."""

from __future__ import annotations

import pytest

from swe.definition import compile_definition
from swe.errors import SwdValidationError
from swe_helpers import EXAMPLE_HANDLERS, EXAMPLE_PROMPT_KEYS, EXAMPLE_PROVIDERS


def _compile(document: object):
    return compile_definition(
        document,
        handlers=EXAMPLE_HANDLERS,
        providers=EXAMPLE_PROVIDERS,
        prompt_keys=EXAMPLE_PROMPT_KEYS,
    )


def _fanout() -> dict[str, object]:
    return {
        "version": "1.0",
        "name": "fanout",
        "entrypoint": "split",
        "limits": {"max_steps": 10, "max_depth": 2, "timeout_seconds": 30},
        "nodes": {
            "split": {
                "type": "fork",
                "branches": {"left": "task_a", "right": "task_b"},
                "join": "together",
            },
            "task_a": {"type": "task", "handler": "demo.execute_item"},
            "task_b": {"type": "task", "handler": "demo.execute_item"},
            "together": {"type": "join", "mode": "all", "next": "done"},
            "done": {"type": "end", "status": "completed"},
        },
    }


def test_fork_with_completing_branches_compiles() -> None:
    compiled = _compile(_fanout())
    assert compiled.entrypoint == "split"


def test_branch_cannot_terminate_before_join() -> None:
    document = _fanout()
    nodes = document["nodes"]
    assert isinstance(nodes, dict)
    nodes["task_a"]["next"] = "done"
    with pytest.raises(SwdValidationError, match="not terminate"):
        _compile(document)


def test_join_cannot_be_entered_without_owning_fork() -> None:
    document = _fanout()
    nodes = document["nodes"]
    assert isinstance(nodes, dict)
    document["entrypoint"] = "choose"
    nodes["choose"] = {
        "type": "decision",
        "evaluator": "expression",
        "condition": {"const": True},
        "routes": {"true": "split", "false": "together"},
    }
    with pytest.raises(SwdValidationError, match="only be entered from owning"):
        _compile(document)


def test_branch_cannot_escape_past_join() -> None:
    document = _fanout()
    nodes = document["nodes"]
    assert isinstance(nodes, dict)
    nodes["task_a"]["next"] = "together"
    nodes["task_b"]["next"] = "done"
    with pytest.raises(SwdValidationError, match="not terminate|escapes join"):
        _compile(document)


def test_join_cannot_have_two_owners() -> None:
    document = _fanout()
    nodes = document["nodes"]
    assert isinstance(nodes, dict)
    document["entrypoint"] = "first"
    nodes["first"] = {
        "type": "fork",
        "branches": {"p": "task_p", "q": "task_q"},
        "join": "together",
    }
    nodes["task_p"] = {"type": "task", "handler": "demo.execute_item"}
    nodes["task_q"] = {"type": "task", "handler": "demo.execute_item"}
    with pytest.raises(SwdValidationError, match="owned by both"):
        _compile(document)


def test_branch_start_cannot_be_entered_from_outside() -> None:
    document = _fanout()
    nodes = document["nodes"]
    assert isinstance(nodes, dict)
    document["entrypoint"] = "choose"
    nodes["choose"] = {
        "type": "decision",
        "evaluator": "expression",
        "condition": {"const": True},
        "routes": {"true": "split", "false": "task_a"},
    }
    with pytest.raises(SwdValidationError, match="may only be entered from"):
        _compile(document)


def test_fork_branches_cannot_share_nodes() -> None:
    document = _fanout()
    nodes = document["nodes"]
    assert isinstance(nodes, dict)
    nodes["task_a"]["next"] = "task_b"
    with pytest.raises(SwdValidationError, match="share nodes"):
        _compile(document)


def test_map_child_cannot_terminate_before_join() -> None:
    document = {
        "version": "1.0",
        "name": "map_needs",
        "entrypoint": "fanout",
        "limits": {"max_steps": 50, "max_depth": 4, "timeout_seconds": 60},
        "nodes": {
            "fanout": {
                "type": "map",
                "items": "$.needs",
                "child": "run_need",
                "concurrency": 8,
                "join": "after_needs",
            },
            "run_need": {
                "type": "task",
                "handler": "demo.execute_item",
                "next": "done",
            },
            "after_needs": {"type": "join", "mode": "all", "next": "done"},
            "done": {"type": "end", "status": "completed"},
        },
    }
    with pytest.raises(SwdValidationError, match="not terminate"):
        _compile(document)


def test_nested_fork_completes_inner_join_before_outer() -> None:
    document = {
        "version": "1.0",
        "name": "nested_forks",
        "entrypoint": "outer",
        "limits": {"max_steps": 20, "max_depth": 4, "timeout_seconds": 30},
        "nodes": {
            "outer": {
                "type": "fork",
                "branches": {"left": "inner", "right": "task_c"},
                "join": "outer_join",
            },
            "inner": {
                "type": "fork",
                "branches": {"a": "task_a", "b": "task_b"},
                "join": "inner_join",
            },
            "task_a": {"type": "task", "handler": "demo.execute_item"},
            "task_b": {"type": "task", "handler": "demo.execute_item"},
            "task_c": {"type": "task", "handler": "demo.execute_item"},
            "inner_join": {"type": "join", "mode": "all"},
            "outer_join": {"type": "join", "mode": "all", "next": "done"},
            "done": {"type": "end", "status": "completed"},
        },
    }
    compiled = _compile(document)
    assert compiled.entrypoint == "outer"


def test_inner_branch_cannot_skip_to_outer_join() -> None:
    document = {
        "version": "1.0",
        "name": "nested_skip",
        "entrypoint": "outer",
        "limits": {"max_steps": 20, "max_depth": 4, "timeout_seconds": 30},
        "nodes": {
            "outer": {
                "type": "fork",
                "branches": {"left": "inner", "right": "task_c"},
                "join": "outer_join",
            },
            "inner": {
                "type": "fork",
                "branches": {"a": "task_a", "b": "task_b"},
                "join": "inner_join",
            },
            "task_a": {
                "type": "task",
                "handler": "demo.execute_item",
                "next": "outer_join",
            },
            "task_b": {"type": "task", "handler": "demo.execute_item"},
            "task_c": {"type": "task", "handler": "demo.execute_item"},
            "inner_join": {"type": "join", "mode": "all"},
            "outer_join": {"type": "join", "mode": "all", "next": "done"},
            "done": {"type": "end", "status": "completed"},
        },
    }
    with pytest.raises(
        SwdValidationError,
        match="never completes at join|only be entered from owning",
    ):
        _compile(document)
