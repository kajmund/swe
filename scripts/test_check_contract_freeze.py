#!/usr/bin/env python3
"""The mutation matrix for the contract gate, run against throwaway git repos.

A gate is only worth what its negative tests prove. Each case builds a small
repository, commits a base, applies one change, commits a head, and runs the
real checker against it — so the matrix exercises the shipped script, not a mock.

The matrix is also a gate on itself: it asserts that the checker's exception
tables are exactly the reviewed ones, that every ignored pointer resolves in the
real fixtures, that every checker contract path is covered by a CI pattern that
requires the contract guard, and that the executed cases are exactly
``EXPECTED_CASES`` with the expected exit codes and failure reasons. Deleting a
negative case, relaxing an ignored pointer or losing CI coverage fails here.

The fixtures mirror the frozen contracts: the journal has the same 11 entries and
the same field layout as `contracts/journal-v1-events.json`, and the header
carries the embedded `schema_version` / `contract_version` / dependency versions.
That makes the "permitted example value" pointers exact and realistic.
"""

from __future__ import annotations

import copy
import fnmatch
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "check_contract_freeze.py"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"


SWD_VERSION = "1.0"
JOURNAL_SCHEMA_VERSION = 1
CONTRACT_VERSION = 1

# A SWD schema with real semantic values: required, enum, const and bounds.
# Only `/properties/version/const` is an identified version location.
SWD_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "name", "entrypoint", "nodes"],
    "properties": {
        "version": {"const": SWD_VERSION},
        "name": {"type": "string", "minLength": 1},
        "entrypoint": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
        "nodes": {
            "type": "object",
            "minProperties": 1,
            "propertyNames": {"enum": ["plan", "done"]},
        },
        "limits": {
            "type": "object",
            "required": ["max_steps", "timeout_seconds"],
            "properties": {
                "max_steps": {"type": "integer", "minimum": 1},
                "timeout_seconds": {"type": "integer", "minimum": 0},
            },
        },
    },
}

SWD_WORKFLOW = {
    "entrypoint": "plan",
    "limits": {"max_depth": 4, "max_steps": 2000, "timeout_seconds": 60},
    "name": "supplier_selection_reference",
    "nodes": {
        "done": {"status": "completed", "type": "end"},
        "plan": {
            "handler": "reference.plan_needs",
            "input": {"candidates": "$.input.candidates"},
            "next": "done",
            "type": "task",
        },
    },
    "version": SWD_VERSION,
}

# The same 11 entries and field layout as the frozen journal fixture.
JOURNAL_EVENTS = [
    {
        "action": "demo.action",
        "attempt_id": "dk1:1",
        "delivery": "pure",
        "delivery_key": "dk1",
        "invocation_id": "inv-1",
        "kind": "AttemptStarted",
        "started_at_ms": 1700000000000,
    },
    {
        "amount_milli": 1000,
        "attempt_id": "dk1:1",
        "charge_id": "ch-1",
        "delivery_key": "dk1",
        "kind": "ChargeReserved",
        "reserved_at_ms": 1700000000001,
        "tariff_milli": 1000,
    },
    {
        "attempt_id": "dk1:1",
        "confirmed_at_ms": 1700000000002,
        "kind": "ResultConfirmed",
        "outcome": {"kind": "ok", "value": {"answer": "a value"}},
    },
    {
        "amount_milli": 1000,
        "charge_id": "ch-1",
        "kind": "ChargeSettled",
        "settled_at_ms": 1700000000003,
    },
    {
        "action": "demo.external",
        "attempt_id": "dk2:1",
        "delivery": "external_idempotent",
        "delivery_key": "dk2",
        "invocation_id": "inv-2",
        "kind": "AttemptStarted",
        "started_at_ms": 1700000000004,
    },
    {
        "amount_milli": 2000,
        "attempt_id": "dk2:1",
        "charge_id": "ch-2",
        "delivery_key": "dk2",
        "kind": "ChargeReserved",
        "reserved_at_ms": 1700000000005,
        "tariff_milli": 2000,
    },
    {
        "at_ms": 1700000000006,
        "charge_id": "ch-2",
        "kind": "ChargePendingReconciliation",
        "reason": "external side effect unknown after crash",
    },
    {
        "abandoned_at_ms": 1700000000007,
        "attempt_id": "dk3:1",
        "kind": "AttemptAbandoned",
        "reason": "cancelled before dispatch",
    },
    {
        "action": "demo.released",
        "attempt_id": "dk4:1",
        "delivery": "pure",
        "delivery_key": "dk4",
        "invocation_id": "inv-4",
        "kind": "AttemptStarted",
        "started_at_ms": 1700000000008,
    },
    {
        "amount_milli": 300,
        "attempt_id": "dk4:1",
        "charge_id": "ch-4",
        "delivery_key": "dk4",
        "kind": "ChargeReserved",
        "reserved_at_ms": 1700000000009,
        "tariff_milli": 300,
    },
    {
        "charge_id": "ch-4",
        "kind": "ChargeReleased",
        "reason": "operation reported no cost",
        "released_at_ms": 1700000000010,
    },
]

JOURNAL_HEADER = {
    "contract_version": CONTRACT_VERSION,
    "definition_sha256": "aaaa",
    "dependency_versions": {"mock.supplier_facts": CONTRACT_VERSION},
    "run_id": "run-contract-v1",
    "schema_version": JOURNAL_SCHEMA_VERSION,
}

JOURNAL_SOURCE = (
    "pub const JOURNAL_SCHEMA_VERSION: u32 = 1;\n\n"
    "pub enum JournalEvent {\n"
    "    AttemptStarted { attempt_id: String },\n"
    "    ChargeSettled { charge_id: String, amount_milli: u64 },\n"
    "}\n"
)

REFERENCE_JOURNAL_SOURCE = (
    "pub const CONTRACT_VERSION: u32 = 1;\n\n"
    "fn keys() -> &'static [&'static str] {\n"
    '    &["started_at_ms", "settled_at_ms"]\n'
    "}\n"
)

NEEDS_SOURCE = (
    "pub struct NeedRef;\n\n"
    "pub fn resolve_need(\n"
    "    need: &NeedRef,\n"
    "    policy: &EvidencePolicy,\n"
    ") -> Result<(), NeedError> {\n"
    "    Ok(())\n"
    "}\n"
)

OPERATION_SPEC_SOURCE = (
    "pub struct OperationSpec {\n"
    "    pub operation: String,\n"
    "    pub status: u32,\n"
    "}\n\n"
    "pub fn gate() {}\n"
)

FILES = {
    "python/swe/schema.py": f'SWD_VERSION = "{SWD_VERSION}"\n',
    "python/swe/swd_v1.schema.json": dump(SWD_SCHEMA),
    "crates/swe-native/src/journal.rs": JOURNAL_SOURCE,
    "crates/swe-native/src/definition.rs": "pub struct CompiledDefinition;\n\npub fn parse() {}\n",
    "crates/swe-native/src/needs.rs": NEEDS_SOURCE,
    "crates/swe-native/src/evidence.rs": "pub struct Evidence;\n",
    "crates/swe-native/src/operations.rs": OPERATION_SPEC_SOURCE,
    "crates/swe-reference/src/journal.rs": REFERENCE_JOURNAL_SOURCE,
    "contracts/README.md": "# contracts\n",
    "contracts/swd-v1-workflow.json": dump(SWD_WORKFLOW),
    "contracts/journal-v1-events.json": dump(JOURNAL_EVENTS),
    "contracts/journal-v1-header.json": dump(JOURNAL_HEADER),
    "docs/swe-standalone/08-verification-log.md": "# log\n",
}
DECISION = "Kontraktsändring: test\n"

# Exact permitted example-value pointers, mirroring the gate's IGNORED_EXAMPLES.
# This is a deliberate hand copy: the matrix asserts they are identical, so
# relaxing the gate's table in the same PR fails here.
PERMITTED_EXAMPLES = {
    "contracts/journal-v1-events.json": [
        "/0/action",
        "/0/attempt_id",
        "/0/delivery_key",
        "/0/invocation_id",
        "/0/started_at_ms",
        "/1/amount_milli",
        "/1/attempt_id",
        "/1/charge_id",
        "/1/delivery_key",
        "/1/reserved_at_ms",
        "/1/tariff_milli",
        "/2/attempt_id",
        "/2/confirmed_at_ms",
        "/3/amount_milli",
        "/3/charge_id",
        "/3/settled_at_ms",
        "/4/action",
        "/4/attempt_id",
        "/4/delivery_key",
        "/4/invocation_id",
        "/4/started_at_ms",
        "/5/amount_milli",
        "/5/attempt_id",
        "/5/charge_id",
        "/5/delivery_key",
        "/5/reserved_at_ms",
        "/5/tariff_milli",
        "/6/at_ms",
        "/6/charge_id",
        "/6/reason",
        "/7/abandoned_at_ms",
        "/7/attempt_id",
        "/7/reason",
        "/8/action",
        "/8/attempt_id",
        "/8/delivery_key",
        "/8/invocation_id",
        "/8/started_at_ms",
        "/9/amount_milli",
        "/9/attempt_id",
        "/9/charge_id",
        "/9/delivery_key",
        "/9/reserved_at_ms",
        "/9/tariff_milli",
        "/10/charge_id",
        "/10/reason",
        "/10/released_at_ms",
    ],
    "contracts/journal-v1-header.json": ["/run_id", "/definition_sha256"],
}

# Exact version locations the gate normalises, mirroring its EMBEDDED table.
EXPECTED_EMBEDDED = {
    ("contracts/journal-v1-header.json", "/schema_version"): "JOURNAL_SCHEMA_VERSION",
    ("contracts/journal-v1-header.json", "/contract_version"): "CONTRACT_VERSION",
    (
        "contracts/journal-v1-header.json",
        "/dependency_versions/mock.supplier_facts",
    ): "CONTRACT_VERSION",
    ("python/swe/swd_v1.schema.json", "/properties/version/const"): "SWD_VERSION",
}


def journal_source(version: int = 1) -> str:
    return JOURNAL_SOURCE.replace(
        f"JOURNAL_SCHEMA_VERSION: u32 = {JOURNAL_SCHEMA_VERSION};",
        f"JOURNAL_SCHEMA_VERSION: u32 = {version};",
    )


def reference_source(version: int = 1) -> str:
    return REFERENCE_JOURNAL_SOURCE.replace(
        f"CONTRACT_VERSION: u32 = {CONTRACT_VERSION};",
        f"CONTRACT_VERSION: u32 = {version};",
    )


def frozen_events() -> list:
    return copy.deepcopy(JOURNAL_EVENTS)


def frozen_header() -> dict:
    return copy.deepcopy(JOURNAL_HEADER)


def pointer_parts(pointer: str) -> list[str]:
    return pointer.strip("/").split("/")


def get_pointer(value: object, pointer: str) -> object:
    current = value
    for part in pointer_parts(pointer):
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


def set_pointer(value: object, pointer: str, replacement: object) -> None:
    parts = pointer_parts(pointer)
    current = value
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    last = parts[-1]
    if isinstance(current, list):
        current[int(last)] = replacement
    else:
        current[last] = replacement


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(tmp: Path, *, contracts_at_base: bool, omit: set[str] | None = None) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "scripts").mkdir()
    shutil.copy(CHECKER, repo / "scripts" / "check_contract_freeze.py")
    omitted = omit or set()
    for path, content in FILES.items():
        if not contracts_at_base and path.startswith("contracts/"):
            continue
        if path in omitted:
            continue
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    if not contracts_at_base:
        for path, content in FILES.items():
            if path.startswith("contracts/"):
                target = repo / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
    return repo


def commit(repo: Path, changes: dict[str, str | None], message: str = "change") -> None:
    for path, content in changes.items():
        target = repo / path
        if content is None:
            if target.exists():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def run(repo: Path) -> tuple[int, str]:
    # Run the shipped checker against the throwaway repo; the root override is
    # what lets the gate be tested without copying it into the fixture.
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--base", "HEAD~1", "--head", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**subprocess.os.environ, "CONTRACT_FREEZE_ROOT": str(repo)},
    )
    # The failure reason is part of the contract: a case that fails for the
    # wrong reason is not a passing negative test.
    return result.returncode, result.stdout + result.stderr


CASES = []


def case(name: str, expect: int):
    def register(fn):
        CASES.append((name, expect, fn))
        return fn

    return register


@case("docs/frontend-only PR: no contract path touched", 0)
def _(repo: Path) -> None:
    commit(repo, {"docs/readme.md": "hello\n"})


@case("guard script deleted: fails", 1)
def _(repo: Path) -> None:
    commit(repo, {"scripts/check_contract_freeze.py": None})


@case("journal wire changed + SWD bumped: fails", 1)
def _(repo: Path) -> None:
    events = frozen_events()
    events[3] = {
        "amount": 500,
        "charge_id": "ch-1",
        "kind": "ChargeSettled",
        "settled_at_ms": 1700000000003,
    }
    commit(
        repo,
        {
            "contracts/journal-v1-events.json": dump(events),
            "python/swe/schema.py": 'SWD_VERSION = "1.1"\n',
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("SWD wire changed + journal bumped: fails", 1)
def _(repo: Path) -> None:
    commit(
        repo,
        {
            "contracts/swd-v1-workflow.json": dump(
                {"entrypoint": "planner", "nodes": {"planner": {"type": "task"}}}
            ),
            "crates/swe-native/src/journal.rs": journal_source(2),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("operations API changed + only journal bumped: fails", 1)
def _(repo: Path) -> None:
    commit(
        repo,
        {
            "crates/swe-native/src/operations.rs": "pub struct OperationSpec;\n\npub fn gate(strict: bool) {}\n",
            "crates/swe-native/src/journal.rs": journal_source(2),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("operations API + CONTRACT_VERSION bumped: passes without a journal bump", 0)
def _(repo: Path) -> None:
    header = frozen_header()
    header["contract_version"] = 2
    header["dependency_versions"]["mock.supplier_facts"] = 2
    commit(
        repo,
        {
            "crates/swe-native/src/operations.rs": "pub struct OperationSpec;\n\npub fn gate(strict: bool) {}\n",
            "crates/swe-reference/src/journal.rs": reference_source(2),
            "contracts/journal-v1-header.json": dump(header),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("comment/test only in journal.rs: no bump", 0)
def _(repo: Path) -> None:
    commit(
        repo,
        {
            "crates/swe-native/src/journal.rs": JOURNAL_SOURCE + "\n// clarified the comment\n",
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("header example value changed, form identical: no bump", 0)
def _(repo: Path) -> None:
    header = frozen_header()
    header["run_id"] = "other"
    header["definition_sha256"] = "bbbb"
    commit(
        repo,
        {
            "contracts/journal-v1-header.json": dump(header),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("header key renamed: journal bump required", 1)
def _(repo: Path) -> None:
    header = frozen_header()
    header["run"] = header.pop("run_id")
    commit(
        repo,
        {
            "contracts/journal-v1-header.json": dump(header),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("header key renamed and JOURNAL_SCHEMA_VERSION bumped: passes", 0)
def _(repo: Path) -> None:
    header = frozen_header()
    header["run"] = header.pop("run_id")
    header["schema_version"] = 2
    commit(
        repo,
        {
            "contracts/journal-v1-header.json": dump(header),
            "crates/swe-native/src/journal.rs": journal_source(2),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("public struct field renamed: CONTRACT_VERSION bump required", 1)
def _(repo: Path) -> None:
    commit(
        repo,
        {
            "crates/swe-native/src/operations.rs": (
                "pub struct OperationSpec {\n"
                "    pub op: String,\n"
                "    pub status: u32,\n"
                "}\n\n"
                "pub fn gate() {}\n"
            ),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("public struct field type changed: CONTRACT_VERSION bump required", 1)
def _(repo: Path) -> None:
    commit(
        repo,
        {
            "crates/swe-native/src/operations.rs": (
                "pub struct OperationSpec {\n"
                "    pub operation: String,\n"
                "    pub status: String,\n"
                "}\n\n"
                "pub fn gate() {}\n"
            ),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("reference journal wire key renamed: journal bump required", 1)
def _(repo: Path) -> None:
    commit(
        repo,
        {
            "crates/swe-reference/src/journal.rs": REFERENCE_JOURNAL_SOURCE.replace(
                "started_at_ms", "startedAtMs"
            ),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("no documented decision: fails", 1)
def _(repo: Path) -> None:
    commit(repo, {"crates/swe-native/src/operations.rs": "pub fn gate(strict: bool) {}\n"})


# ---------------------------------------------------------------------------
# The four closed bypasses, proven as negative cases (plus their positive
# counterparts so the fix is not merely "fail everything").
# ---------------------------------------------------------------------------


@case("frozen fixture moved out of contracts/: fails", 1)
def _(repo: Path) -> None:
    commit(
        repo,
        {
            "contracts/journal-v1-events.json": None,
            "archive/journal-v1-events.json": dump(frozen_events()),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("SWD schema required/enum changed: SWD bump required", 1)
def _(repo: Path) -> None:
    schema = copy.deepcopy(SWD_SCHEMA)
    schema["required"] = ["version", "name", "entrypoint", "nodes", "limits"]
    schema["properties"]["nodes"]["propertyNames"]["enum"] = ["plan", "done", "stop"]
    commit(
        repo,
        {
            "python/swe/swd_v1.schema.json": dump(schema),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("SWD schema required/enum changed + SWD bumped: passes", 0)
def _(repo: Path) -> None:
    schema = copy.deepcopy(SWD_SCHEMA)
    schema["required"] = ["version", "name", "entrypoint", "nodes", "limits"]
    schema["properties"]["nodes"]["propertyNames"]["enum"] = ["plan", "done", "stop"]
    commit(
        repo,
        {
            "python/swe/swd_v1.schema.json": dump(schema),
            "python/swe/schema.py": 'SWD_VERSION = "1.1"\n',
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("journal kind changed: journal bump required", 1)
def _(repo: Path) -> None:
    events = frozen_events()
    events[0]["kind"] = "AttemptStartedV2"
    commit(
        repo,
        {
            "contracts/journal-v1-events.json": dump(events),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("journal kind changed + journal bumped: passes", 0)
def _(repo: Path) -> None:
    events = frozen_events()
    events[0]["kind"] = "AttemptStartedV2"
    commit(
        repo,
        {
            "contracts/journal-v1-events.json": dump(events),
            "crates/swe-native/src/journal.rs": journal_source(2),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("multi-line parameter type changed: CONTRACT_VERSION bump required", 1)
def _(repo: Path) -> None:
    commit(
        repo,
        {
            "crates/swe-native/src/needs.rs": NEEDS_SOURCE.replace(
                "policy: &EvidencePolicy,", "policy: &BudgetPolicy,"
            ),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("multi-line parameter type changed + CONTRACT_VERSION bumped: passes", 0)
def _(repo: Path) -> None:
    header = frozen_header()
    header["contract_version"] = 2
    header["dependency_versions"]["mock.supplier_facts"] = 2
    commit(
        repo,
        {
            "crates/swe-native/src/needs.rs": NEEDS_SOURCE.replace(
                "policy: &EvidencePolicy,", "policy: &BudgetPolicy,"
            ),
            "crates/swe-reference/src/journal.rs": reference_source(2),
            "contracts/journal-v1-header.json": dump(header),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("SWD_VERSION removed alone: fails", 1)
def _(repo: Path) -> None:
    commit(
        repo,
        {
            "python/swe/schema.py": "# SWD_VERSION was removed\n",
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("JOURNAL_SCHEMA_VERSION removed alone: fails", 1)
def _(repo: Path) -> None:
    commit(
        repo,
        {
            "crates/swe-native/src/journal.rs": journal_source().replace(
                "pub const JOURNAL_SCHEMA_VERSION: u32 = 1;\n\n", ""
            ),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("CONTRACT_VERSION removed alone: fails", 1)
def _(repo: Path) -> None:
    commit(
        repo,
        {
            "crates/swe-reference/src/journal.rs": reference_source().replace(
                "pub const CONTRACT_VERSION: u32 = 1;\n\n", ""
            ),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("unknown scalar in journal event changed: fails", 1)
def _(repo: Path) -> None:
    events = frozen_events()
    events[4]["delivery"] = "replayable"
    commit(
        repo,
        {
            "contracts/journal-v1-events.json": dump(events),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("unknown scalar in SWD flow changed: fails", 1)
def _(repo: Path) -> None:
    workflow = copy.deepcopy(SWD_WORKFLOW)
    workflow["limits"]["timeout_seconds"] = 61
    commit(
        repo,
        {
            "contracts/swd-v1-workflow.json": dump(workflow),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("all permitted example values changed: no bump", 0)
def _(repo: Path) -> None:
    events = frozen_events()
    for pointer in PERMITTED_EXAMPLES["contracts/journal-v1-events.json"]:
        value = get_pointer(events, pointer)
        set_pointer(events, pointer, value + 7 if isinstance(value, int) else f"{value}-changed")
    header = frozen_header()
    for pointer in PERMITTED_EXAMPLES["contracts/journal-v1-header.json"]:
        value = get_pointer(header, pointer)
        set_pointer(header, pointer, f"{value}-changed")
    commit(
        repo,
        {
            "contracts/journal-v1-events.json": dump(events),
            "contracts/journal-v1-header.json": dump(header),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("CONTRACT_VERSION updates the journal header without a journal bump: passes", 0)
def _(repo: Path) -> None:
    header = frozen_header()
    header["contract_version"] = 2
    header["dependency_versions"]["mock.supplier_facts"] = 2
    commit(
        repo,
        {
            "crates/swe-reference/src/journal.rs": reference_source(2),
            "contracts/journal-v1-header.json": dump(header),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("embedded journal version that no longer matches its owner: fails", 1)
def _(repo: Path) -> None:
    header = frozen_header()
    header["schema_version"] = 2
    commit(
        repo,
        {
            "contracts/journal-v1-header.json": dump(header),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("contracts README edit needs no contract decision", 0)
def _(repo: Path) -> None:
    commit(repo, {"contracts/README.md": "# contracts\n\nEdited documentation.\n"})


@case("comment version spoof is not a bump: fails", 1)
def _(repo: Path) -> None:
    events = frozen_events()
    events[3] = {
        "amount": 500,
        "charge_id": "ch-1",
        "kind": "ChargeSettled",
        "settled_at_ms": 1700000000003,
    }
    # Raw text says the version was bumped; the declaration below still says 1.
    spoofed = "// JOURNAL_SCHEMA_VERSION = 2\n" + JOURNAL_SOURCE
    commit(
        repo,
        {
            "contracts/journal-v1-events.json": dump(events),
            "crates/swe-native/src/journal.rs": spoofed,
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("string version spoof is not a bump: fails", 1)
def _(repo: Path) -> None:
    events = frozen_events()
    events[3] = {
        "amount": 500,
        "charge_id": "ch-1",
        "kind": "ChargeSettled",
        "settled_at_ms": 1700000000003,
    }
    # The string literal contains a version assignment; it is not a declaration.
    spoofed = (
        'pub const SPOOF_NOTE: &str = "JOURNAL_SCHEMA_VERSION = 2";\n\n'
        + JOURNAL_SOURCE
    )
    commit(
        repo,
        {
            "contracts/journal-v1-events.json": dump(events),
            "crates/swe-native/src/journal.rs": spoofed,
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("function-local Python version spoof is not a bump: fails", 1)
def _(repo: Path) -> None:
    schema = copy.deepcopy(SWD_SCHEMA)
    schema["properties"]["name"]["minLength"] = 2
    commit(
        repo,
        {
            "python/swe/schema.py": (
                "def fake_version():\n"
                '    SWD_VERSION = "1.1"\n'
            ),
            "python/swe/swd_v1.schema.json": dump(schema),
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


@case("function-local Rust version spoof is not a bump: fails", 1)
def _(repo: Path) -> None:
    events = frozen_events()
    events[3]["amount_milli"] = 501
    spoofed = JOURNAL_SOURCE.replace(
        "pub const JOURNAL_SCHEMA_VERSION: u32 = 1;",
        "pub fn fake_version() {\n    pub const JOURNAL_SCHEMA_VERSION: u32 = 2;\n}",
    )
    commit(
        repo,
        {
            "contracts/journal-v1-events.json": dump(events),
            "crates/swe-native/src/journal.rs": spoofed,
            "docs/swe-standalone/08-verification-log.md": DECISION,
        },
    )


def bootstrap_cases() -> list[tuple[str, int, callable]]:
    def bootstrap_ok(repo: Path) -> None:
        commit(
            repo,
            {
                "python/swe/schema.py": 'SWD_VERSION = "2.0"\n',
                "contracts/journal-v1-events.json": dump(
                    [{"attempt_id": "a", "kind": "AttemptStarted"}]
                ),
                "docs/swe-standalone/08-verification-log.md": DECISION,
            },
        )

    def post_bootstrap_fails(repo: Path) -> None:
        commit(
            repo,
            {
                "contracts/journal-v1-events.json": dump(
                    [{"attempt_id": "a", "kind": "AttemptStarted"}]
                ),
                "docs/swe-standalone/08-verification-log.md": DECISION,
            },
        )

    def bootstrap_missing_head_version(repo: Path) -> None:
        # No schema.py at base and none at head: bootstrap must not excuse it.
        commit(
            repo,
            {
                "contracts/journal-v1-events.json": dump([{"kind": "AttemptStarted"}]),
                "docs/swe-standalone/08-verification-log.md": DECISION,
            },
        )

    def moved_version_file(repo: Path) -> None:
        # The version constant lives in a file that did not exist at the base
        # (a package move in the same PR). Bootstrap must not fail on that.
        commit(
            repo,
            {
                "python/swe/schema.py": 'SWD_VERSION = "1.0"\n',
                "docs/swe-standalone/08-verification-log.md": DECISION,
            },
        )

    return [
        ("bootstrap: version file absent at base AND head: fails", 1, bootstrap_missing_head_version),
        ("bootstrap: version file moved in this PR: allowed", 0, moved_version_file),
        ("bootstrap: contracts absent at base: version 1 allowed", 0, bootstrap_ok),
        ("post-bootstrap: same change with contracts at base: fails", 1, post_bootstrap_fails),
    ]


# Every case that must run, with the exit code and a distinctive reason. A
# deleted, renamed or reason-less negative case fails the matrix: a test that
# only checks "some exit code" cannot distinguish a gate failure from a crash.
EXPECTED_CASES = [
    ("docs/frontend-only PR: no contract path touched", 0, "no contract path changed"),
    ("guard script deleted: fails", 1, "the contract gate script was deleted"),
    ("journal wire changed + SWD bumped: fails", 1, "journal: wire form changed but its version stayed 1"),
    ("SWD wire changed + journal bumped: fails", 1, "swd: wire form changed but its version stayed 1.0"),
    ("operations API changed + only journal bumped: fails", 1, "contracts: wire form changed but its version stayed 1"),
    ("operations API + CONTRACT_VERSION bumped: passes without a journal bump", 0, "contract freeze OK"),
    ("comment/test only in journal.rs: no bump", 0, "affected types: none"),
    ("header example value changed, form identical: no bump", 0, "affected types: none"),
    ("header key renamed: journal bump required", 1, "journal: wire form changed but its version stayed 1"),
    ("header key renamed and JOURNAL_SCHEMA_VERSION bumped: passes", 0, "journal: version 1 -> 2"),
    ("public struct field renamed: CONTRACT_VERSION bump required", 1, "contracts: wire form changed but its version stayed 1"),
    ("public struct field type changed: CONTRACT_VERSION bump required", 1, "contracts: wire form changed but its version stayed 1"),
    ("reference journal wire key renamed: journal bump required", 1, "journal: wire form changed but its version stayed 1"),
    ("no documented decision: fails", 1, "without a documented decision"),
    ("frozen fixture moved out of contracts/: fails", 1, "journal: wire form changed but its version stayed 1"),
    ("SWD schema required/enum changed: SWD bump required", 1, "swd: wire form changed but its version stayed 1.0"),
    ("SWD schema required/enum changed + SWD bumped: passes", 0, "swd: version 1.0 -> 1.1"),
    ("journal kind changed: journal bump required", 1, "journal: wire form changed but its version stayed 1"),
    ("journal kind changed + journal bumped: passes", 0, "journal: version 1 -> 2"),
    ("multi-line parameter type changed: CONTRACT_VERSION bump required", 1, "contracts: wire form changed but its version stayed 1"),
    ("multi-line parameter type changed + CONTRACT_VERSION bumped: passes", 0, "contract freeze OK"),
    ("SWD_VERSION removed alone: fails", 1, "SWD_VERSION is missing at the head"),
    ("JOURNAL_SCHEMA_VERSION removed alone: fails", 1, "JOURNAL_SCHEMA_VERSION is missing at the head"),
    ("CONTRACT_VERSION removed alone: fails", 1, "CONTRACT_VERSION is missing at the head"),
    ("unknown scalar in journal event changed: fails", 1, "journal: wire form changed but its version stayed 1"),
    ("unknown scalar in SWD flow changed: fails", 1, "swd: wire form changed but its version stayed 1.0"),
    ("all permitted example values changed: no bump", 0, "affected types: none"),
    ("CONTRACT_VERSION updates the journal header without a journal bump: passes", 0, "affected types: none"),
    ("embedded journal version that no longer matches its owner: fails", 1, "journal: wire form changed but its version stayed 1"),
    ("contracts README edit needs no contract decision", 0, "no contract path changed"),
    ("comment version spoof is not a bump: fails", 1, "journal: wire form changed but its version stayed 1"),
    ("string version spoof is not a bump: fails", 1, "journal: wire form changed but its version stayed 1"),
    ("function-local Python version spoof is not a bump: fails", 1, "SWD_VERSION is missing at the head"),
    ("function-local Rust version spoof is not a bump: fails", 1, "JOURNAL_SCHEMA_VERSION is missing at the head"),
    ("bootstrap: version file absent at base AND head: fails", 1, "SWD_VERSION is missing at the head"),
    ("bootstrap: version file moved in this PR: allowed", 0, "allowed by the bootstrap exception"),
    ("bootstrap: contracts absent at base: version 1 allowed", 0, "allowed by the bootstrap exception"),
    ("post-bootstrap: same change with contracts at base: fails", 1, "journal: wire form changed but its version stayed 1"),
]


def load_checker():
    """Import the shipped checker so its tables can be compared literally."""
    spec = importlib.util.spec_from_file_location("check_contract_freeze", CHECKER)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load the contract checker from {CHECKER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_example_tables(checker) -> list[str]:
    """The gate's exception tables must be exactly the reviewed ones."""
    failures: list[str] = []

    expected_ignored = {
        path: frozenset(pointers) for path, pointers in PERMITTED_EXAMPLES.items()
    }
    if checker.IGNORED_EXAMPLES != expected_ignored:
        failures.append(
            "IGNORED_EXAMPLES in scripts/check_contract_freeze.py changed; update "
            "the mirror in this file deliberately (an extra pointer hides a "
            "fixture value from the gate)"
        )
    if checker.EMBEDDED != EXPECTED_EMBEDDED:
        failures.append(
            "EMBEDDED in scripts/check_contract_freeze.py changed; update the "
            "mirror in this file deliberately"
        )

    for path, pointers in sorted(checker.IGNORED_EXAMPLES.items()):
        value = json.loads((ROOT / path).read_text())
        for pointer in sorted(pointers):
            try:
                resolved = get_pointer(value, pointer)
            except (KeyError, IndexError, TypeError) as error:
                failures.append(f"ignored pointer {path}#{pointer} does not resolve: {error!r}")
                continue
            if not isinstance(resolved, (str, int, float, bool)):
                failures.append(f"ignored pointer {path}#{pointer} is not a scalar example")

    for path, pointer in sorted(checker.EMBEDDED):
        value = json.loads((ROOT / path).read_text())
        try:
            get_pointer(value, pointer)
        except (KeyError, IndexError, TypeError) as error:
            failures.append(f"embedded pointer {path}#{pointer} does not resolve: {error!r}")

    return failures


def ci_contract_globs() -> list[str]:
    """Globs whose CI `case` branch makes the contract guard required.

    Parsed from `.github/workflows/ci.yml` so a checker contract path that CI
    would never flag fails here instead of silently skipping the guard.
    """
    if not CI_WORKFLOW.is_file():
        raise SystemExit(
            f"cannot find {CI_WORKFLOW}; the contract matrix needs it for the "
            "contract-surface parity check"
        )
    lines = CI_WORKFLOW.read_text().splitlines()
    groups: dict[str, list[str] | None] = {"contract_surface": None, "guard_script": None}
    for index, line in enumerate(lines):
        marker = line.strip()
        if not marker.endswith("=true"):
            continue
        name = marker.split("=", 1)[0]
        if name not in groups:
            continue
        pattern_line = ""
        for previous in range(index - 1, -1, -1):
            if lines[previous].strip():
                pattern_line = lines[previous].strip()
                break
        if not pattern_line.endswith(")"):
            raise SystemExit(
                f"cannot parse the CI case pattern before {marker!r}: {pattern_line!r}"
            )
        groups[name] = pattern_line[:-1].split("|")
    if groups["contract_surface"] is None:
        raise SystemExit(
            "cannot find the `contract_surface=true` case in .github/workflows/ci.yml; "
            "the contract-surface parity check would otherwise pass silently"
        )
    return [pattern for patterns in groups.values() if patterns for pattern in patterns]


def check_ci_coverage(checker) -> list[str]:
    """Every checker contract path must trigger the CI contract guard."""
    globs = ci_contract_globs()
    missing: list[str] = []
    for entry in checker.CONTRACT_PATHS:
        sample = entry + "example.txt" if entry.endswith("/") else entry
        if not any(fnmatch.fnmatchcase(sample, pattern) for pattern in globs):
            missing.append(f"{entry} (sample {sample!r})")
    if not missing:
        return []
    return [
        "these checker contract paths are not covered by a CI pattern that "
        "requires the contract guard: " + ", ".join(missing)
    ]


def main() -> int:
    failures = 0
    checker = load_checker()

    print("self-checks:")
    self_check_failures = [*check_example_tables(checker), *check_ci_coverage(checker)]
    cases = [*CASES, *bootstrap_cases()]
    actual_names = [name for name, _expect, _mutate in cases]
    expected_names = [name for name, _code, _reason in EXPECTED_CASES]
    if actual_names != expected_names:
        self_check_failures.append(
            "the executed case list differs from EXPECTED_CASES: deleting or "
            f"renaming a case must be deliberate. Got {actual_names!r}"
        )
    if [expect for _name, expect, _mutate in cases] != [
        code for _name, code, _reason in EXPECTED_CASES
    ]:
        self_check_failures.append(
            "a registered case changed its expected exit code without updating EXPECTED_CASES"
        )
    for failure in self_check_failures:
        print(f"FAIL  self-check: {failure}")
    failures += len(self_check_failures)
    if not self_check_failures:
        print("PASS  gate tables, fixture pointers and CI contract-surface parity match")

    for (name, expect, mutate), (expected_name, expected_code, reason) in zip(
        cases, EXPECTED_CASES
    ):
        with tempfile.TemporaryDirectory() as tmp:
            omit = (
                {"python/swe/schema.py"}
                if "version file moved" in name or "absent at base AND head" in name
                else None
            )
            repo = make_repo(
                Path(tmp),
                contracts_at_base="post-bootstrap" in name or "bootstrap" not in name,
                omit=omit,
            )
            mutate(repo)
            code, output = run(repo)
        reason_ok = reason in output
        ok = code == expected_code and reason_ok
        failures += 0 if ok else 1
        detail = "" if reason_ok else f"  (missing reason {reason!r})"
        print(
            f"{'PASS' if ok else 'FAIL'}  expected {expected_code} got {code}  "
            f"{expected_name}{detail}"
        )
        if not ok:
            print(output)
    total = len(EXPECTED_CASES)
    print(f"\n{total} cases, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
