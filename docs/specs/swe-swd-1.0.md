# Socialism Workflow Engine (SWE) / SWD 1.0

**Status:** First implementation slice — schema, parser, contracts  
**Runtime:** not in this PR (no scheduler, no OverGraph writes, no research cutover)

## Goal

One small local workflow engine for research, expert/model routing, parallel agents, tools, and later crawl/ingest. Declarative JSON (YAML is authoring syntax). Rust core + Python async handlers in later PRs. OverGraph holds run history and the Jev cache, one catalog directory per customer and top-level feature.

Inventory of current `main`: [swe-inventory.md](swe-inventory.md).

## This PR

Python reference implementation under `backend/app/swe/`:

- JSON Schema: `backend/app/swe/swd_v1.schema.json`
- Parse JSON or YAML → JSON object
- Graph validation (refs, join ownership, reachability, non-terminal loops)
- Handler / provider registry checks
- Typed expression AST (`3 < 5` is true; missing/null/type mismatch fail; no `eval`)
- Canonical JSON + SHA-256 (Python oracle until Rust matches it)
- Trusted `WorkflowHostContext` → `workflows/customer_<id>/<feature>/`

Unknown node types and unknown registered handlers fail before any execution. YAML and JSON of the same document share a SHA-256.

## Node types (v1)

`task`, `decision` (expression or named provider), `fork`, `join` (`mode: all`, `on_failure: fail`), `map`, `wait`, `subworkflow`, `end`.

Loops are a `decision` routing backwards plus `limits.max_steps` / `max_depth`. `wait` is schema-complete; the runtime may land after the core scheduler.

## Expressions

JSON objects only. Operators: `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `and`, `or`, `not`, `in`, `add`, `sub`, `mul`, `div`, `exists`, plus `var` / `const`. `var` may set `default`. Missing paths without `default` are errors, never silent `false`. Booleans are not numbers.

## Isolation

Customer id and feature id come from host context, never from YAML. Allowed features: `research`, `expert_routing`, `model_routing`, `group_chat`, `webcrawler`. Nested model choice under research is stored in the **research** catalog.

## Canonical JSON (PR 1 oracle)

`canonical_json_bytes`: UTF-8, NFC, LF, sorted keys, compact separators, no NaN/Inf, bools distinct from ints, integer-valued floats stored as ints. Later Rust must match these bytes; then Rust is normative.

## Follow-up PRs

1. **This PR** — inventory, schema, parser/validator, tests, docs
2. Rust/PyO3 runtime: `task`, `decision(expression)`, `fork`, `join`, `map`, `end` + Python asyncio dispatch
3. OverGraph adapter: isolation, `immediate` WAL, atomic transitions, recovery, run events
4. Jev provider + SHA-256 cache L1/L2 + single-flight, reuse `EvidenceScreenCache` contracts
5. Research integration and differential tests; delete replaced orchestration
6. Expert/model/subagent adapters. `wait`, UI, webcrawl later

Out of v1: distributed schedulers, brokers, arbitrary Python in YAML, auto-deletion, SWE-owned agent memory, a second Jev/research engine.

## Example

Fan-out/join + expression + Jev provider: `backend/tests/swe/research_parallel.yaml`. That file is engine semantics, not a replacement for `execute_attempt_research`.
