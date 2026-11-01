"""Shared SWD example registries for tests."""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent
EXAMPLE_HANDLERS = frozenset(
    {
        "demo.fetch_catalog",
        "demo.fetch_pricing",
        "demo.refine",
        "demo.inspect",
        "demo.summarize",
        "demo.execute_item",
    }
)
EXAMPLE_PROVIDERS = frozenset({"mock.decision"})
EXAMPLE_PROMPT_KEY = "demo.assessment"
EXAMPLE_PROMPT_KEYS = frozenset({EXAMPLE_PROMPT_KEY})
