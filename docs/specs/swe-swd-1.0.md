# SWE Workflow Definition (SWD) 1.0

**Status:** frozen v1 wire contract.

SWD is a declarative JSON workflow format. YAML is accepted as authoring syntax
and is converted to the same canonical JSON representation before hashing.
The normative schema is [`python/swe/swd_v1.schema.json`](../../python/swe/swd_v1.schema.json),
and a complete frozen document is
[`contracts/swd-v1-workflow.json`](../../contracts/swd-v1-workflow.json).

## Document

A document has exactly these top-level fields:

- `version`: `"1.0"`;
- `name`: a stable workflow name;
- `entrypoint`: the first node id;
- `limits`: positive `max_steps`, `max_depth`, and `timeout_seconds` values;
- `nodes`: a non-empty map of node ids to node definitions.

Unknown fields are rejected. Definitions are normalized to NFC, frozen, written
as canonical JSON, and identified by SHA-256.

## Nodes

SWD 1.0 supports:

- `task`: emits an invocation for a host-supplied handler;
- `decision` with `evaluator: expression`: evaluates a deterministic expression;
- `decision` with `evaluator: provider`: emits a host provider invocation and
  validates the returned choice;
- `fork` and its owning `join`: deterministic parallel branches;
- `map` and its owning `join`: bounded fan-out over a JSON array;
- `end`: terminal `completed`, `failed`, `cancelled`, or `incomplete` status.

Graph validation rejects unreachable nodes, dangling routes, non-terminal
cycles, a join without exactly one owner, overlapping branch ownership, and
registries that do not contain every referenced handler/provider/prompt key.

## Data binding and expressions

Bindings are JSON-path reads (`$.input.value`, `$.node.result`) or explicit
`{"const": ...}` values. Expressions are single-key objects over `var`, `const`,
comparison, boolean, membership, arithmetic, `not`, and `exists`. Evaluation is
pure and uses JSON value semantics; arbitrary code is never evaluated.

## Host boundary

The document declares handler names, provider names, prompt keys, and choices.
The host injects their implementations and content. SWD contains no storage
layout, credentials, model implementation, network client, or domain policy.

## Compatibility

The schema version is `SWD_VERSION = "1.0"`. A wire change requires a version
change and a contract decision under `docs/swe-standalone/`; CI enforces this
with `scripts/check_contract_freeze.py`. The schema's legacy `$id` is retained
as a stable identifier for v1 and is not dereferenced at runtime.
