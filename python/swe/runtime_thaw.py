"""Thaw a compiled SWD document into plain JSON-compatible values."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def thaw_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_json(item) for item in value]
    return value
