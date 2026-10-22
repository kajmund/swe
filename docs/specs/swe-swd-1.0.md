# Socialism Workflow Engine (SWE) / SWD 1.0

**Status:** Schema/parser (PR #398), in-memory Rust/PyO3 runtime (phase 2),
OverGraph run store + crash-durable resume (phase 3), provider decisions +
exact Jev cache (phase 4)  
**Runtime:** `crates/swe-native` + `app.swe.runtime`. Durable runs in
`app.services.overgraph.workflows`, Jev cache in
`app.services.overgraph.jev_cache`. No research cutover yet (phase 5).

## Goal

One small local workflow engine for research, expert/model routing, parallel agents, tools, and later crawl/ingest. Declarative JSON (YAML is authoring syntax). Rust core + Python async handlers in later PRs. OverGraph holds run history and the Jev cache, one catalog directory per customer and top-level feature.

Inventory of current `main`: [swe-inventory.md](swe-inventory.md). Runtime:
[swe-runtime.md](swe-runtime.md). Durable runs: [swe-overgraph.md](swe-overgraph.md).
Providers: [swe-provider.md](swe-provider.md).

## This PR

Python reference implementation under `backend/app/swe/`:

- JSON Schema: `backend/app/swe/swd_v1.schema.json`
- Parse JSON or YAML → JSON object
- Graph validation (refs, join ownership, reachability, non-terminal loops)
- Handler / provider registry checks
- Typed expression AST (`3 < 5` is true; missing/null/type mismatch fail; no `eval`)
- Provider decisions reference `prompt_key` in the prompt catalog (`PROMPT_KEY_SET` at compile; `require_active_prompts` / `render_prompt` at runtime)
- Canonical JSON + SHA-256 (Python oracle until Rust matches it); object keys are NFC-normalized and colliding keys fail
- Compiled `document` is the deep-frozen canonical form, so hash and executed definition match
- Fork/map branches must complete at the owning join; omitted `next` on a branch node completes that branch into the join
- A nested fork/map is one step in an outer branch; inner branches are not outer members
- Trusted `WorkflowHostContext` → `workflows/customer_<id>/<feature>/`

Unknown node types and unknown registered handlers fail before any execution. YAML and JSON of the same document share a SHA-256.

## Node types (v1)

`task`, `decision` (expression or named provider), `fork`, `join` (`mode: all`, `on_failure: fail`), `map`, `wait`, `subworkflow`, `end`.

A `fork`/`map` does not skip to its `join`. Join is reachable only from nodes in that instance's branches. A branch may not terminate at `end`, skip its join, or continue past the join. Nested fan-out continues from its own join (or map `next`). Expression `decision` routes are exactly `true` and `false`. `in` uses the same JSON type rules as `eq` (booleans are not numbers).

Loops are a `decision` routing backwards plus `limits.max_steps` / `max_depth`. `wait` is schema-complete; the runtime may land after the core scheduler.

## Expressions

JSON objects only. Operators: `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `and`, `or`, `not`, `in`, `add`, `sub`, `mul`, `div`, `exists`, plus `var` / `const`. `var` may set `default`. Missing paths without `default` are errors, never silent `false`. Booleans are not numbers, including inside objects and arrays (`eq`/`ne`/`in` use recursive JSON equality). The evaluator accepts the frozen compiled form (`Mapping` / `tuple`); it does not thaw a mutable copy.

## Isolation

Customer id and feature id come from host context, never from YAML. Allowed features: `research`, `expert_routing`, `model_routing`, `group_chat`, `webcrawler`. Nested model choice under research is stored in the **research** catalog.

## Canonical JSON (PR 1 oracle)

`canonical_json_bytes`: UTF-8, NFC, LF, sorted keys, compact separators, no NaN/Inf, bools distinct from ints, integer-valued floats stored as ints. Later Rust must match these bytes; then Rust is normative.

## Follow-up PRs

1. **Done** — inventory, schema, parser/validator, tests, docs
2. **Done** — Rust/PyO3 runtime: `task`, `decision(expression)`, `fork`, `join`, `map`, `end` + Python asyncio dispatch — [swe-runtime.md](swe-runtime.md)
3. **Done** — OverGraph adapter: isolation, `immediate` WAL, atomic transitions, recovery, run events — [swe-overgraph.md](swe-overgraph.md)
4. **Done** — Jev provider + SHA-256 cache L1/L2 + single-flight, reuse `EvidenceScreenCache` contracts — [swe-provider.md](swe-provider.md)
5. **Done** — research integration on SWE (`research/swe_bridge.py`), differential tests and deletion of the replaced orchestration — [swe-cutover.md](swe-cutover.md)
6. Expert/model/subagent adapters. `wait`, UI, webcrawl later

Out of v1: distributed schedulers, brokers, arbitrary Python in YAML, auto-deletion, SWE-owned agent memory, a second Jev/research engine.

## Example

Fan-out/join + expression + Jev provider: `backend/tests/swe/research_parallel.yaml`. That file is engine semantics, not a replacement for `execute_attempt_research`.
