"""Durable run-store contract shared by the SWE adapter and its implementations.

`swe.runtime` writes one `RunRecord` per transition and replays
`InvocationRecord`s on resume. `app.services.overgraph.workflows` implements the
contract, so the runtime stays independent of any persistence backend.

`WorkflowResources` is the other half of the same contract: the host-supplied
resources a run may use. It lives here so the durable-run types and the
injected resources are declared together.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from swe.cache import DecisionCache


@dataclass(frozen=True)
class RunRecord:
    """Durable run snapshot as of one transition boundary."""

    run_id: str
    definition_sha256: str
    status: str
    input: Mapping[str, Any]
    output: Any
    error: Mapping[str, Any] | None
    started_at_ms: int
    updated_at_ms: int
    finished_at_ms: int | None
    last_seq: int


@dataclass(frozen=True)
class InvocationRecord:
    """Persisted handler outcome; replayed instead of running the handler again."""

    invocation_id: str
    node_id: str
    ok: bool
    value: Any
    error: str | None


class RunStore(Protocol):
    """Durable run sink, implemented by `app.services.overgraph.workflows`."""

    async def record(
        self,
        run: RunRecord,
        *,
        events: Sequence[Mapping[str, Any]] = (),
        invocation: InvocationRecord | None = None,
    ) -> None: ...

    async def load_run(self, run_id: str) -> RunRecord | None: ...

    async def replay_results(self, run_id: str) -> Mapping[str, InvocationRecord]: ...

    async def events(self, run_id: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class WorkflowResources:
    """Optional run resources shared by the runner and provider dispatch.

    ``prompt_keys`` is the active prompt catalog. It is required once the
    definition contains a provider decision, exactly like the compile-time
    allowlist: execution never renders a prompt that is not declared.
    """

    providers: Mapping[str, Callable[..., Awaitable[Any]]] = field(
        default_factory=dict
    )
    prompts: Mapping[str, str] | None = None
    prompt_keys: Collection[str] | None = None
    cache: DecisionCache | None = None
    cancel_event: asyncio.Event | None = None
