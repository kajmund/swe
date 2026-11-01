"""Rust/Python canonical JSON, SHA-256, and expression parity."""

from __future__ import annotations

import pytest

pytest.importorskip("swe_native")

from swe.canonicalize import canonical_json_bytes, sha256_hex
from swe.expression import evaluate_expression
from swe.freeze import freeze_json
from swe.runtime import require_native
from swe.yaml_load import load_swd_json, load_swd_yaml
from swe_helpers import (
    EXAMPLE_HANDLERS,
    EXAMPLE_PROMPT_KEYS,
    EXAMPLE_PROVIDERS,
    FIXTURES,
)
from swe.definition import compile_definition

pytestmark = pytest.mark.swe_runtime


def test_canonical_bytes_match_python_oracle() -> None:
    native = require_native()
    samples = [
        {"b": 1, "a": 1},
        {"name": "é"},
        {"name": "e\u0301"},
        "a\r\nb",
        True,
        1.0,
        {"é": 1},
        {"child": ["a\r\nb"], "e\u0301": 1.0},
        None,
        [],
        {},
        {"nested": {"flag": True, "n": 1}},
    ]
    for sample in samples:
        assert native.canonical_json_bytes(sample) == canonical_json_bytes(sample)
        assert native.sha256_hex(sample) == sha256_hex(sample)


def test_yaml_json_sha_matches_across_languages() -> None:
    native = require_native()
    compiled_json = compile_definition(
        load_swd_json((FIXTURES / "provider_parallel.json").read_text(encoding="utf-8")),
        handlers=EXAMPLE_HANDLERS,
        providers=EXAMPLE_PROVIDERS,
        prompt_keys=EXAMPLE_PROMPT_KEYS,
    )
    compiled_yaml = compile_definition(
        load_swd_yaml((FIXTURES / "provider_parallel.yaml").read_text(encoding="utf-8")),
        handlers=EXAMPLE_HANDLERS,
        providers=EXAMPLE_PROVIDERS,
        prompt_keys=EXAMPLE_PROMPT_KEYS,
    )
    assert compiled_json.sha256 == compiled_yaml.sha256
    assert compiled_json.sha256 == sha256_hex(compiled_json.document)
    assert native.sha256_hex(_thaw_mapping(compiled_json.document)) == compiled_json.sha256


def test_expression_parity_including_nested_bool() -> None:
    native = require_native()
    cases = [
        ({"lt": [{"const": 3}, {"const": 5}]}, {}, True),
        ({"in": [{"const": True}, {"const": [1]}]}, {}, False),
        ({"in": [{"const": True}, {"const": [True]}]}, {}, True),
        ({"eq": [{"const": {"a": True}}, {"const": {"a": 1}}]}, {}, False),
        ({"eq": [{"const": [True]}, {"const": [1]}]}, {}, False),
        ({"var": "$.missing", "default": 7}, {}, 7),
        ({"exists": "$.nope"}, {"run": {}}, False),
        ({"div": [{"const": 4}, {"const": 2}]}, {}, 2.0),
    ]
    for expr, context, expected in cases:
        python = evaluate_expression(freeze_json(expr), freeze_json(context))
        rust = native.evaluate_expression(expr, context)
        assert rust == python == expected


def test_nan_infinity_and_key_collision_match() -> None:
    native = require_native()
    with pytest.raises(Exception, match="NaN"):
        canonical_json_bytes(float("nan"))
    with pytest.raises(ValueError, match="NaN and Infinity"):
        native.canonical_json_bytes(float("nan"))
    with pytest.raises(ValueError, match="NaN and Infinity"):
        native.canonical_json_bytes(float("inf"))
    colliding = {"é": 1, "e\u0301": 2}
    with pytest.raises(Exception, match="canonical key collision"):
        canonical_json_bytes(colliding)
    with pytest.raises(ValueError, match="canonical key collision"):
        native.canonical_json_bytes(colliding)


def test_expression_errors_match() -> None:
    native = require_native()
    with pytest.raises(ValueError, match="missing value"):
        native.evaluate_expression({"var": "$.run.steps_used"}, {})
    with pytest.raises(ValueError, match="requires numbers"):
        native.evaluate_expression({"lt": [{"const": True}, {"const": 1}]}, {})
    with pytest.raises(ValueError, match="division by zero"):
        native.evaluate_expression({"div": [{"const": 1}, {"const": 0}]}, {})
    with pytest.raises(ValueError, match="matching JSON types"):
        native.evaluate_expression({"eq": [{"const": 1}, {"const": "1"}]}, {})


async def test_event_loop_is_not_blocked_by_callback_boundary() -> None:
    import asyncio

    from swe.runtime import run_workflow
    from swe_runtime_helpers import compile_runtime

    progressed = asyncio.Event()
    compiled = compile_runtime(
        {
            "work": {"type": "task", "handler": "demo.work", "next": "done"},
            "done": {"type": "end", "status": "completed"},
        },
        name="probe",
        entrypoint="work",
        handlers=frozenset({"demo.work"}),
    )

    async def work(_payload: dict[str, object]) -> dict[str, bool]:
        await progressed.wait()
        return {"ok": True}

    async def probe() -> None:
        progressed.set()

    await asyncio.gather(
        run_workflow(compiled, {"demo.work": work}, {}),
        probe(),
    )


def _thaw_mapping(value: object) -> object:
    from swe.runtime_thaw import thaw_json

    return thaw_json(value)
