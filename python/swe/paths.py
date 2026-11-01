"""Restricted JSON paths used by SWD bindings and expressions."""

from __future__ import annotations

import re
from collections.abc import Mapping

from swe.errors import SwdExpressionError
from swe.schema import JSON_PATH_PATTERN

PATH_RE = re.compile(JSON_PATH_PATTERN)
MISSING = object()


def parse_json_path(path: str) -> list[str]:
    if not PATH_RE.fullmatch(path):
        raise SwdExpressionError(f"invalid JSON path {path!r}")
    if path == "$":
        return []
    return path[2:].split(".")


def resolve_path(context: object, path: str) -> object:
    current = context
    for segment in parse_json_path(path):
        current = _step(current, segment, path)
        if current is MISSING:
            return MISSING
    return current


def _step(current: object, segment: str, path: str) -> object:
    if isinstance(current, Mapping):
        if segment in current:
            return current[segment]
        return MISSING
    if isinstance(current, (list, tuple)) and segment.isdigit():
        index = int(segment)
        if 0 <= index < len(current):
            return current[index]
        return MISSING
    if current is None:
        return MISSING
    raise SwdExpressionError(f"cannot resolve {path!r} through {type(current).__name__}")
