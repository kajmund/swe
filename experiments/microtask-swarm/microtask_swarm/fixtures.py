"""Frozen gold fixtures and task construction for the swarm pilot.

The fixture file is the only source of gold labels. Task input builders copy
only the fields a worker needs; they never copy gold objects, item ids, or
domain names into a prompt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from microtask_swarm.contracts import (
    ENTAILMENT_LABELS,
    Kind,
    OPERATION_CHOICES,
    TASK_KINDS,
    assert_input_is_typed,
)

_CONTRACT_VERSION = "microtask-swarm-gold-v1"
_ITEM_KEYS = {"item_id", "domain", "passage", "question", "tasks"}
_TASK_KEYS = {"gold"}
_TASK_INPUT_KEYS = {
    "relevance": {"query"},
    "entailment": {"hypothesis"},
    "atomicity": {"claim"},
    "extraction": set(),
    "operation_selection": {"context", "operations"},
}


class FixtureError(ValueError):
    """The frozen fixture set is malformed or has drifted."""


@dataclass(frozen=True, slots=True)
class GoldTask:
    kind: Kind
    input_data: Mapping[str, Any]
    gold: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Item:
    item_id: str
    domain: str
    passage: str
    question: str
    tasks: Mapping[Kind, GoldTask]


@dataclass(frozen=True, slots=True)
class GoldSuite:
    contract_version: str
    operation_menu: tuple[str, ...]
    items: tuple[Item, ...]
    sha256: str
    path: Path

    @property
    def item_count(self) -> int:
        return len(self.items)

    @property
    def microtask_count(self) -> int:
        return len(self.items) * len(TASK_KINDS)

    @property
    def domains(self) -> tuple[str, ...]:
        return tuple(sorted({item.domain for item in self.items}))


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """One unit of work, with gold kept outside the prompt payload."""

    task_id: str
    item_id: str
    domain: str
    kind: Kind
    input_data: Mapping[str, Any]
    gold: Mapping[str, Any]
    passage: str
    question: str


def load_fixtures(path: Path, *, expected_sha256: str | None = None) -> GoldSuite:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise FixtureError(
            f"fixture sha256 {digest} does not match pinned {expected_sha256}"
        )
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FixtureError(f"fixture is not valid JSON: {exc.msg}") from exc
    if not isinstance(document, Mapping):
        raise FixtureError("fixture root must be an object")
    if document.get("schema_version") != 1:
        raise FixtureError("fixture schema_version must be 1")
    if document.get("contract_version") != _CONTRACT_VERSION:
        raise FixtureError(f"fixture contract_version must be {_CONTRACT_VERSION!r}")

    menu = document.get("operation_menu")
    if not isinstance(menu, list) or tuple(menu) != OPERATION_CHOICES:
        raise FixtureError(
            f"operation_menu must equal the fixed contract menu {list(OPERATION_CHOICES)}"
        )

    raw_items = document.get("items")
    if not isinstance(raw_items, list):
        raise FixtureError("items must be an array")
    items: list[Item] = []
    seen_ids: set[str] = set()
    seen_domains: set[str] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            raise FixtureError("each item must be an object")
        if set(raw_item) != _ITEM_KEYS:
            raise FixtureError(
                f"item keys must be {sorted(_ITEM_KEYS)}, got {sorted(raw_item)}"
            )
        item_id = raw_item["item_id"]
        domain = raw_item["domain"]
        passage = raw_item["passage"]
        question = raw_item["question"]
        if not isinstance(item_id, str) or not item_id:
            raise FixtureError("item_id must be a non-empty string")
        if item_id in seen_ids:
            raise FixtureError(f"duplicate item_id {item_id!r}")
        if not isinstance(domain, str) or not domain:
            raise FixtureError(f"{item_id}: domain must be a non-empty string")
        if not isinstance(passage, str) or not passage:
            raise FixtureError(f"{item_id}: passage must be a non-empty string")
        if not isinstance(question, str) or not question:
            raise FixtureError(f"{item_id}: question must be a non-empty string")
        seen_ids.add(item_id)
        seen_domains.add(domain)

        raw_tasks = raw_item["tasks"]
        if not isinstance(raw_tasks, Mapping) or set(raw_tasks) != {
            str(kind) for kind in TASK_KINDS
        }:
            raise FixtureError(
                f"{item_id}: tasks must contain exactly {[str(k) for k in TASK_KINDS]}"
            )
        tasks: dict[Kind, GoldTask] = {}
        for kind in TASK_KINDS:
            raw_task = raw_tasks[str(kind)]
            if not isinstance(raw_task, Mapping):
                raise FixtureError(f"{item_id}.{kind}: task must be an object")
            allowed = _TASK_INPUT_KEYS[str(kind)] | _TASK_KEYS | {"operations"}
            if set(raw_task) - allowed:
                raise FixtureError(
                    f"{item_id}.{kind}: unexpected keys {sorted(set(raw_task) - allowed)}"
                )
            input_data = _build_input(kind, raw_task, passage, question, item_id)
            gold = raw_task.get("gold")
            if not isinstance(gold, Mapping):
                raise FixtureError(f"{item_id}.{kind}: gold must be an object")
            _validate_gold(kind, gold, passage, raw_task, item_id, menu)
            tasks[kind] = GoldTask(kind=kind, input_data=input_data, gold=dict(gold))
        items.append(
            Item(
                item_id=item_id,
                domain=domain,
                passage=passage,
                question=question,
                tasks=tasks,
            )
        )

    if len(items) < 2:
        raise FixtureError("at least two items are required")
    if len(seen_domains) < 2:
        raise FixtureError("fixtures must cover at least two domains")
    for item in items:
        _assert_no_gold_in_input(item, item.item_id)

    return GoldSuite(
        contract_version=_CONTRACT_VERSION,
        operation_menu=tuple(menu),
        items=tuple(items),
        sha256=digest,
        path=path,
    )


def _build_input(
    kind: Kind,
    raw_task: Mapping[str, Any],
    passage: str,
    question: str,
    item_id: str,
) -> dict[str, Any]:
    if kind is Kind.RELEVANCE:
        return {"passage": passage, "query": _require_str(raw_task, "query", item_id)}
    if kind is Kind.ENTAILMENT:
        return {
            "passage": passage,
            "hypothesis": _require_str(raw_task, "hypothesis", item_id),
        }
    if kind is Kind.ATOMICITY:
        return {"claim": _require_str(raw_task, "claim", item_id)}
    if kind is Kind.EXTRACTION:
        return {"passage": passage, "question": question}
    if kind is Kind.OPERATION:
        operations = raw_task.get("operations")
        if not isinstance(operations, list) or tuple(operations) != OPERATION_CHOICES:
            raise FixtureError(
                f"{item_id}.operation: operations must equal the fixed menu"
            )
        return {
            "context": _require_str(raw_task, "context", item_id),
            "operations": list(operations),
        }
    raise FixtureError(f"{item_id}: unknown kind {kind!r}")


def _require_str(raw: Mapping[str, Any], key: str, item_id: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FixtureError(f"{item_id}: {key} must be a non-empty string")
    return value


def _validate_gold(
    kind: Kind,
    gold: Mapping[str, Any],
    passage: str,
    raw_task: Mapping[str, Any],
    item_id: str,
    menu: Sequence[str],
) -> None:
    if kind is Kind.RELEVANCE:
        if set(gold) != {"relevant"} or not isinstance(gold["relevant"], bool):
            raise FixtureError(f"{item_id}.relevance: gold must be {{'relevant': bool}}")
    elif kind is Kind.ENTAILMENT:
        if set(gold) != {"label"} or gold["label"] not in ENTAILMENT_LABELS:
            raise FixtureError(
                f"{item_id}.entailment: gold label must be one of {ENTAILMENT_LABELS}"
            )
    elif kind is Kind.ATOMICITY:
        if set(gold) != {"atomic"} or not isinstance(gold["atomic"], bool):
            raise FixtureError(f"{item_id}.atomicity: gold must be {{'atomic': bool}}")
    elif kind is Kind.EXTRACTION:
        if set(gold) != {"quotes"}:
            raise FixtureError(f"{item_id}.extraction: gold must be {{'quotes': [...]}}")
        quotes = gold["quotes"]
        if not isinstance(quotes, list) or not quotes:
            raise FixtureError(f"{item_id}.extraction: gold quotes must be non-empty")
        for quote in quotes:
            if not isinstance(quote, str) or len(quote) < 10:
                raise FixtureError(f"{item_id}.extraction: gold quote is too short")
            if quote not in passage:
                raise FixtureError(
                    f"{item_id}.extraction: gold quote is not an exact passage substring"
                )
    elif kind is Kind.OPERATION:
        if set(gold) != {"operation"} or gold["operation"] not in menu:
            raise FixtureError(
                f"{item_id}.operation: gold operation must be in the fixed menu"
            )
        operations = raw_task.get("operations")
        if isinstance(operations, list) and gold["operation"] not in operations:
            raise FixtureError(
                f"{item_id}.operation: gold operation is absent from the menu input"
            )


def _assert_no_gold_in_input(item: Item, item_id: str) -> None:
    for kind, task in item.tasks.items():
        assert_input_is_typed(task.input_data)
        if item_id in json.dumps(task.input_data, ensure_ascii=False):
            raise FixtureError(f"{item_id}.{kind}: item id leaked into input")
        if item.domain in json.dumps(task.input_data, ensure_ascii=False):
            raise FixtureError(f"{item_id}.{kind}: domain leaked into input")


def build_task_specs(suite: GoldSuite, kind: Kind) -> tuple[TaskSpec, ...]:
    """Build per-item task specs for a distributed or single-call suite."""

    specs: list[TaskSpec] = []
    for item in suite.items:
        if kind is Kind.COMBINED:
            input_data = {
                "passage": item.passage,
                "question": item.question,
                "relevance": {
                    "query": item.tasks[Kind.RELEVANCE].input_data["query"]
                },
                "entailment": {
                    "hypothesis": item.tasks[Kind.ENTAILMENT].input_data["hypothesis"]
                },
                "atomicity": {
                    "claim": item.tasks[Kind.ATOMICITY].input_data["claim"]
                },
                "operation": {
                    "context": item.tasks[Kind.OPERATION].input_data["context"],
                    "operations": list(OPERATION_CHOICES),
                },
            }
            assert_input_is_typed(input_data)
            gold = {
                "relevance": item.tasks[Kind.RELEVANCE].gold,
                "entailment": item.tasks[Kind.ENTAILMENT].gold,
                "atomicity": item.tasks[Kind.ATOMICITY].gold,
                "extraction": item.tasks[Kind.EXTRACTION].gold,
                "operation": item.tasks[Kind.OPERATION].gold,
            }
            specs.append(
                TaskSpec(
                    task_id=f"{item.item_id}:combined",
                    item_id=item.item_id,
                    domain=item.domain,
                    kind=Kind.COMBINED,
                    input_data=input_data,
                    gold=gold,
                    passage=item.passage,
                    question=item.question,
                )
            )
            continue
        task = item.tasks[kind]
        specs.append(
            TaskSpec(
                task_id=f"{item.item_id}:{kind}",
                item_id=item.item_id,
                domain=item.domain,
                kind=kind,
                input_data=task.input_data,
                gold=task.gold,
                passage=item.passage,
                question=item.question,
            )
        )
    return tuple(specs)


def all_microtask_specs(suite: GoldSuite) -> tuple[TaskSpec, ...]:
    return tuple(
        spec
        for kind in TASK_KINDS
        for spec in build_task_specs(suite, kind)
    )


__all__ = [
    "FixtureError",
    "GoldSuite",
    "GoldTask",
    "Item",
    "TaskSpec",
    "all_microtask_specs",
    "build_task_specs",
    "load_fixtures",
]
