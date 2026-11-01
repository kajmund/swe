"""map with 0, 1, and 100 items; next without a join still waits."""

from __future__ import annotations

import pytest

pytest.importorskip("swe_native")

from swe.runtime import run_workflow
from swe_runtime_helpers import compile_runtime

pytestmark = pytest.mark.swe_runtime


def _map_definition(*, concurrency: int = 8) -> object:
    return compile_runtime(
        {
            "fanout": {
                "type": "map",
                "items": "$.needs",
                "child": "run_need",
                "concurrency": concurrency,
                "next": "done",
            },
            "run_need": {"type": "task", "handler": "demo.need"},
            "done": {"type": "end", "status": "completed"},
        },
        name="map_items",
        entrypoint="fanout",
        limits={"max_steps": 400, "max_depth": 2, "timeout_seconds": 30},
        handlers=frozenset({"demo.need"}),
    )


async def _need(payload: dict[str, object]) -> dict[str, object]:
    return {"item": payload["item"], "index": payload["index"]}


async def test_map_zero_items_completes() -> None:
    result = await run_workflow(_map_definition(), {"demo.need": _need}, {"needs": []})
    assert result.status == "completed"
    assert result.output["fanout"]["result"] == []


async def test_map_one_item() -> None:
    result = await run_workflow(_map_definition(), {"demo.need": _need}, {"needs": ["only"]})
    assert result.status == "completed"
    assert result.output["fanout"]["result"] == [{"item": "only", "index": 0}]


async def test_map_one_hundred_stable_order() -> None:
    needs = list(range(100))
    result = await run_workflow(
        _map_definition(concurrency=8),
        {"demo.need": _need},
        {"needs": needs},
    )
    assert result.status == "completed"
    rows = result.output["fanout"]["result"]
    assert [row["item"] for row in rows] == needs
    assert [row["index"] for row in rows] == needs
    assert len(rows) == 100
