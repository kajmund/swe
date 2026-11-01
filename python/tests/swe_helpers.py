"""Shared SWD example registries for tests."""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent
EXAMPLE_HANDLERS = frozenset(
    {
        "research.search_contracts",
        "research.search_legal_sources",
        "research.plan_follow_up",
        "research.inspect_context",
        "research.synthesize",
        "research.execute_need",
    }
)
EXAMPLE_PROVIDERS = frozenset({"jev"})
EXAMPLE_PROMPT_KEY = "research.assessment.user"
EXAMPLE_PROMPT_KEYS = frozenset({EXAMPLE_PROMPT_KEY})
