"""Exact decision-cache contract for provider decisions.

The engine owns the cache identity and the interface. Host storage adapters
implement `DecisionCache`; the engine imports no storage backend. Keeping the
key here means every caller and backend agrees on what "the same decision"
means.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

from swe.canonicalize import canonical_json_bytes


class DecisionCache(Protocol):
    """Cache for exact provider decisions, shared by L1 and durable stores."""

    async def resolve(
        self,
        key: str,
        compute: Callable[[], Awaitable[Mapping[str, Any]]],
        *,
        provider: str,
        prompt_key: str,
    ) -> tuple[Mapping[str, Any], bool]:
        """Return ``(answer, hit)``; concurrent identical keys compute once."""
        ...


def decision_cache_key(
    *,
    provider: str,
    prompt_key: str,
    prompt: str,
    input_data: Mapping[str, Any],
    choices: Sequence[str],
    params: Mapping[str, Any],
) -> str:
    """Stable cache identity. Prompt text carries the active prompt version."""
    payload = {
        "provider": provider,
        "prompt_key": prompt_key,
        "prompt": prompt,
        "input": dict(input_data),
        "choices": list(choices),
        "params": dict(params),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
