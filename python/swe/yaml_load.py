"""Optional YAML authoring loader. Runtime validation is JSON-only."""

from __future__ import annotations

import json
import math
from typing import Any

import yaml

from swe.errors import SwdParseError


def load_swd_text(raw: str | bytes) -> dict[str, Any]:
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return load_swd_json(text)
    return load_swd_yaml(text)


def load_swd_json(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SwdParseError(f"invalid JSON: {exc.msg}") from exc
    return json_object(value)


def load_swd_yaml(raw: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SwdParseError(f"invalid YAML: {exc}") from exc
    return json_object(value)


def json_object(value: object) -> dict[str, Any]:
    converted = json_safe(value)
    if not isinstance(converted, dict):
        raise SwdParseError("SWD document must be an object")
    return converted


def json_safe(value: object) -> object:
    if value is None or isinstance(value, bool) or isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SwdParseError("NaN and Infinity are not allowed")
        return value
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return { _json_key(key): json_safe(item) for key, item in value.items() }
    raise SwdParseError(f"unsupported YAML type {type(value).__name__}")


def _json_key(key: object) -> str:
    if not isinstance(key, str):
        raise SwdParseError("object keys must be strings")
    return key
