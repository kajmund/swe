"""SWD 1.0 JSON Schema load and node-type constants."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

SCHEMA_FILE = Path(__file__).with_name("swd_v1.schema.json")
SWD_VERSION = "1.0"
NODE_TYPES = frozenset(
    {"task", "decision", "fork", "join", "map", "wait", "subworkflow", "end"}
)
CACHE_POLICIES = frozenset({"off", "exact"})
HANDLER_NAME_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
PROVIDER_NAME_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$"
PROMPT_KEY_PATTERN = HANDLER_NAME_PATTERN
NODE_ID_PATTERN = r"^[a-z][a-z0-9_]*$"
WORKFLOW_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"
CUSTOMER_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
JSON_PATH_PATTERN = r"^\$(\.[A-Za-z_][A-Za-z0-9_]*|\.[0-9]+)*$"


@cache
def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
