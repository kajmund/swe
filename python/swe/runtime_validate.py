"""Validate handler output at the Python/Rust boundary."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator

from swe.runtime_errors import SweHandlerError
from swe.runtime_thaw import thaw_json


def require_json_value(value: object, *, node_id: str) -> Any:
    if not is_json_value(value):
        raise SweHandlerError(
            f"handler output for {node_id} is not JSON-serializable",
            path=f"nodes.{node_id}",
        )
    return thaw_json(value)


def is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (bool, str)):
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and is_json_value(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(is_json_value(item) for item in value)
    return False


def validate_output_schema(node: Mapping[str, Any], output: object, *, node_id: str) -> None:
    schema = node.get("output_schema")
    if schema is None:
        return
    error = next(Draft202012Validator(thaw_json(schema)).iter_errors(output), None)
    if error is None:
        return
    raise SweHandlerError(
        f"handler output for {node_id} failed output_schema: {error.message}",
        path=f"nodes.{node_id}.output_schema",
    )
