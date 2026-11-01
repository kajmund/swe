# Fas C — Public contracts for a standalone SWE

**Status:** Investigation deliverable, based on the code at `origin/main`
(`666eccb0`). No production code is changed by this document.
**Scope:** Which APIs must be stable for SWE to be consumed as a versioned
Rust library (with a Python host binding) by Socialism or any other project.
**Rule applied:** reuse existing contracts where they are already generic; do
not invent an abstraction that duplicates one that already exists.

Legend for the "today" column:

| Status | Meaning |
| --- | --- |
| **KEEP** | Already generic; keep the shape, document it as public, version it. |
| **DECOUPLE** | The contract is generic but the code path imports Socialism. Split the generic shape from the injected implementation. |
| **PROMOTE** | The contract exists only inside the `swe-gym` experiment or the research domain; lift the generic part into SWE, keep the domain part in Socialism. |
| **BUILD** | No contract exists today. It must be designed before extraction. |
| **DOMAIN** | Not a SWE contract. Stays in Socialism and is only visible through an injected interface. |

## 0. Contract principles

1. **Deterministic contracts take precedence.** An operation gate and an
   evidence requirement are structural: a policy may choose *which allowed
   action* runs next, never whether the gate applies
   (`docs/SWE_Sweycoins_v2.md` §31).
2. **One identity for a definition.** Canonical JSON bytes + SHA-256
   (`crates/swe-native/src/canonicalize.rs`,
   `backend/app/swe/canonicalize.py`). Everything that persists or reproduces a
   run keys on that hash.
3. **Typed errors, no fallbacks.** `ErrorKind`
   (`crates/swe-native/src/error.rs:6`) is the public taxonomy; the Python host
   mirrors it (`app/swe/runtime_errors.py`).
4. **No parallel abstractions.** The contracts below reuse: the SWD 1.0 node
   model, the effect/completion protocol, the `DecisionCache` protocol, the
   decision-cache key, `RunRecord`/`InvocationRecord`, the Sweycoin ledger, and
   the `OperationSpec`/`NeedRef` shapes from the experiment. No new scheduler,
   DAG model, expression language, error taxonomy, cache key, ledger or
   provider framework is introduced.

## 1. Workflow

### 1.1 Workflow definition — KEEP (DECOUPLE prompt/feature validation)

The public document is SWD 1.0:

- JSON Schema: `backend/app/swe/swd_v1.schema.json`
- Python compile/validate: `backend/app/swe/definition.py`
- Rust preflight/compile: `crates/swe-native/src/preflight.rs`,
  `definition.rs`

Public semantics (already documented in `docs/specs/swe-swd-1.0.md` and
`docs/specs/swe-runtime.md`):

| Contract element | Where it is defined |
| --- | --- |
| Node types `task`, `decision`, `fork`, `join`, `map`, `end` | `swd_v1.schema.json`, `app/swe/schema.py:12` |
| `decision(evaluator=expression\|provider)` | `machine/schedule.rs`, `runtime_dispatch.py` |
| `limits: max_steps / max_depth / timeout_seconds` | schema + `machine/mod.rs` |
| Expression operators and strict JSON typing | `expression.rs`, `expression.py` |
| YAML authoring → same canonical JSON and SHA | `yaml_load.py`, `canonicalize.py` |
| Reserved and rejected: `wait`, `subworkflow`, `retry` | preflight (`runtime_preflight.py`) |

**Decoupling required before standalone extraction:**

| Coupling | Evidence | Target |
| --- | --- | --- |
| Prompt-key whitelist is the Socialism prompt catalog | `backend/app/swe/definition.py:13` (`from app.services.prompt_catalog import PROMPT_KEY_SET`), used at `definition.py:159` | `compile_definition(..., prompt_keys=...)` — host-supplied set, same pattern as the existing `providers` whitelist |
| Feature enum is a Socialism product enum | `backend/app/swe/schema.py:16` (`FEATURE_IDS = {"research","expert_routing","model_routing","group_chat","webcrawler"}`) | host-supplied feature registry; domain values stay in Socialism |
| Multi-tenant catalog path | `backend/app/swe/features.py:35` (`workflow_catalog_dir`) | move to the Socialism storage adapter; SWE only receives an opaque scope/catalog key or a store factory |

The **trusted host context** concept (a tenant/feature label that never comes
from the workflow document) is valid, but its *values and storage layout* are
domain: `FEATURE_IDS`, `CUSTOMER_ID_PATTERN`, `WorkflowHostContext` and
`workflow_catalog_dir` should move to the Socialism host
(`02-inventory-python.md` §5.1). SWE may keep a generic opaque
scope/catalog key if a consumer needs one; it must never validate a fixed
feature vocabulary.

### 1.2 Compiled definition — KEEP

`CompiledDefinition` is the frozen canonical document plus SHA-256; the engine
executes exactly the hash it was given. Public: `compile_definition`,
`compile_swd_text`, `canonical_json_bytes`, `sha256_hex`. Internal: graph
validation implementation detail.

### 1.3 Host driver protocol — KEEP

This is the seam a non-Python host (including the domain-neutral example)
implements. It already exists in both Rust and Python:

| Element | Rust | Python |
| --- | --- | --- |
| Start a run | `start_run(document, sha, run_id, input, now_ms)` (`machine/mod.rs:191`) | `runtime.run_workflow` / `WorkflowRunner` |
| Effects to the host | `Effect::{InvokeHandler, InvokeProvider, CancelInvocation}` (`effects.rs:31`) | `runtime_dispatch.spawn_invocations` |
| Completion | `Machine.complete / fail / cancel / tick` (`machine/mod.rs:90`) | asyncio tasks + `_apply_effects` |
| Invocation identity | `<run_id>:<node_id>:<branch_path>:<loop_epoch>:<serial>` | same, from the machine |
| Idempotent completion / typed conflict | `complete.rs` | `runtime_errors.py` |

Public invariant: the host may only affect the run through effects and
completions. The engine emits no callbacks and never calls Python. A completion
with the same payload is a no-op; a different payload for an already completed
invocation is a typed conflict; unknown ids are typed errors
(`docs/specs/swe-runtime.md`).

### 1.4 Run status and terminal semantics — KEEP

`RunStatus::{running, completed, incomplete, failed, cancelled}`
(`effects.rs:6`). `INSUFFICIENT_EVIDENCE` is represented today as a terminal
`incomplete` end node plus an explicit reason in the run output; that mapping
must be stated in the public contract (the demo proves it).

### 1.5 Timeouts, cancellation, deadlines — KEEP

`limits.timeout_seconds` is enforced by the kernel on every mutation and on
`tick`; the Python driver also bounds `asyncio.wait` by the remaining deadline.
Retries are **not** part of the contract (`retry` is preflight-rejected).
Cancellation is an effect to the host plus a terminal `cancelled` status.

### 1.6 Pause and resume — BUILD (Rust); Python shape already exists

Today resume is a **host** capability, not a kernel capability:

- `RunStore` protocol + `RunRecord` / `InvocationRecord`:
  `backend/app/swe/run_store.py:15-56` (protocol at `:42`; dataclasses at `:15` and `:31`)
- Persistent implementation: `backend/app/services/overgraph/workflows.py`
- Resume replays persisted invocation results instead of re-running handlers
  (`docs/specs/swe-overgraph.md` "Resume"; `runtime.py:294`)

For a standalone library the public contract should be a **Rust
`RunStore`/journal trait** plus the same replay semantics:

1. load run snapshot: `definition_sha256`, `input`, `status`, `last_seq`;
2. reject a resume whose definition hash differs;
3. replay completed invocation records in order against a fresh machine;
4. never re-charge a replayed provider/tool call (ledger idempotency key);
5. after replay, continue from the same effect boundary.

The offline reference experiment implements exactly this journal and proves the
semantics (`crates/swe-reference/`). It is the shape to promote, not a second
engine.

**Maturity note (verified in `02-inventory-python.md` §8, E10):** the Python
`RunStore` contract and the OverGraph implementation are unit- and
crash-tested, but **not wired into production** — the production research
bridge does not supply a run store (the argument defaults to `None`;
`backend/app/services/research/swe_bridge.py:118`). The same is true for
`JevDecisionCache` and `make_jev_provider`, which are only used by tests. So pause/resume is *design- and test-proven*, not
production-proven. The Rust journal contract and the reference demo must be
judged on that basis.

### 1.7 Kernel/host gaps found by the Fas D experiment

The domain-neutral reference experiment surfaced contract gaps that matter for a
standalone Rust library. They are observations, not blockers for the repo move,
but they belong to the public-contract work:

| # | Gap | Evidence | Consequence / target |
| --- | --- | --- | --- |
| G1 | Rust preflight is leaner than the Python compile step | `crates/swe-native/src/preflight.rs` validates node types and reserved features but does not run the JSON Schema nor the reachability/join-ownership/branch-termination checks in `backend/app/swe/{graph,joins}.py`; the reference demo calls `start_run` with a hand-written document | A Rust-only consumer can pass a document that the Python compiler would reject. Either port the graph validation into `swe-core` or document that `compile_definition` is the normative front door before PR 4 |
| G2 | `join.mode` / `join.on_failure` parsed but ignored in Rust | `crates/swe-native/src/definition.rs:72-75,379-383`; the schema constrains the values to `all`/`fail` (`swd_v1.schema.json:262-263`) | Safe while values are constants, but it must be stated: v1 supports only `all/fail`, and the Rust parser does not enforce the schema |
| G3 | Provider results are choice-shaped only | Demo README "Gaps found" §4; `runtime_dispatch.py:_invoke_provider` accepts extra keys | Facts/cost/usage must be extra keys or handler outputs today; typed provider metadata is BUILD (§6) |
| G4 | No reusable host driver | Demo README "Gaps found" §2; each host re-implements effects → dispatch → complete | Supports the `swe-bench` metered-runner promotion (`05-gym-split.md` G2) and the Rust journal contract (§1.6) |
| G5 | No `Serialize` for `Json` | Demo README "Gaps found" §8; the demo journals through the canonical JSON writer plus a hand-written reader | A documented `Json` parse/serialize pair would let external hosts persist without duplicating readers |

### 1.8 Workflow contract summary

| Element | Status | Public? |
| --- | --- | --- |
| SWD 1.0 document + canonical hash | KEEP/DECOUPLE | Yes |
| Node semantics, fork/join/map/decision/end | KEEP | Yes |
| Effect/completion protocol, invocation ids | KEEP | Yes |
| Terminal statuses and typed error kinds | KEEP | Yes |
| Limits, timeout, cancellation | KEEP | Yes |
| Pause/resume journal + replay | BUILD | Yes |
| Machine internal state, scheduler order, branch bookkeeping | — | No |
| `wait` / `subworkflow` / `retry` | rejected | Documented as unsupported in v1 |
| Prompt catalog, feature enum, customer/feature path | DOMAIN | No |

## 2. Needs

**Today:** there is no generic need type. Two non-generic shapes exist:

- `ResearchNeed` — Socialism domain (`backend/app/services/research/models.py:63`)
  with legal/research fields (`source_types`, `capabilities`, GLiNER2 span hints).
- `NeedRef { fact, document }` — experiment-only
  (`crates/swe-gym/src/ops.rs:12`), embedded with legal fixtures
  (`notice_period`, `Avtal_17`).

**Target contract (PROMOTE + BUILD):** a data model, not a new runtime.

- **PROMOTE** (exists in the experiment, in two shapes today): `NeedRef` —
  `{fact, document}` in `crates/swe-gym/src/ops.rs:12`, `{id, supplier, fact}`
  in the reference demo — and `NeedMore(NeedRef)` as an executor outcome. The
  two shapes should be unified when promoted; the experiment's current shape is
  the one to start from.
- **BUILD** (does not exist anywhere): `NeedPriority REQUIRED | SUPPORTING |
  EXPLORATORY`, `NeedCardinality SINGLE | MULTIPLE | UNKNOWN`, `NeedStatus
  UNRESOLVED | RESOLVED`, and the unified `NeedRef { kind, subject, scope? }`
  view. These semantics are documented in the experiment
  (`docs/SWE_Sweycoins_v2.md` §24–§26) but are not types in the tree.

```text
NeedRef        { fact, document } / { kind, subject }   // PROMOTE + unify
NeedMore(NeedRef)                                        // PROMOTE
NeedPriority / NeedCardinality / NeedStatus              // BUILD
```

Structural rules that must be public (they are the reason the type exists):

1. Needs are derived from **operation contracts**, never from what happened to
   get resolved (`crates/swe-gym/src/ops.rs:120` test
   `needs_derive_from_the_spec_not_from_resolution`).
2. A `NeedMore` for an identical `NeedRef` twice ends the investigation with
   `INSUFFICIENT_EVIDENCE`; it may never spin
   (`ops.rs:178` `investigate_operate_loop`; the guard is at `:198-200`).
3. A need that stays UNRESOLVED cannot silently drop out; the operation gate
   still blocks (`ops.rs:50`).
4. The need model may carry a source/capability hint, but it must be meaningful
   without a legal/document domain. Research-specific fields stay in
   `ResearchNeed` and are projected by the bridge.

**Public surface:** the types above plus a `needs()` query on a resolved
investigation state. The policy contract (§7) receives the remaining needs as
data.

**Not public / not moved:** `ResearchNeed`, research `source_types`,
`capabilities`, GLiNER2 span fields (`app/services/research/gliner/contract.py`).

## 3. Evidence

**Today:** no generic evidence type. Three partial shapes:

| Shape | Location | Domain coupling |
| --- | --- | --- |
| `ResearchEvidence` with `status = found \| not_found \| error` | `backend/app/services/research/models.py:126` (status field `:130`) | Socialism sources, `legal_result`, TextUnit locators |
| `VERIFIED / AMBIGUOUS / REJECTED` findings derived from observable signals | `crates/swe-gym/tools/eval_uncertainty_states.py:60-81`, `tools/run_blindtest_v2.py` | Python experiment tooling over legal spans |
| "verified findings" progress signal | `crates/swe-gym/src/real.rs:187`, `env.rs:58` | facit-driven experiment metric |

**Target contract (PROMOTE + BUILD):**

```text
Provenance  { source_id, locator, retrieved_at, content_hash, provider }
Evidence    { evidence_id, need_ref, value?, state, provenance, confidence? }
EvidenceState VERIFIED | AMBIGUOUS | REJECTED
EvidenceSet { evidence: Vec<Evidence> }
```

Public rules:

1. **Provenance is mandatory (BUILD).** Evidence without a source id, locator
   and content hash is not evidence. The engine must not know what a TextUnit, a
   graph node or a PDF is — it references stable ids and hashes supplied by the
   host. The reference demo records a single `source_ref` per fact and therefore
   demonstrates the status/abstention rules, not the full provenance contract;
   locator, content hash, `retrieved_at` and provider are PR 2 work.
2. **A need is RESOLVED only from VERIFIED, unambiguous evidence.** Conflicting
   VERIFIED values make the need AMBIGUOUS; a REJECTED candidate never counts.
   These are the observable-signal rules already implemented in
   `tools/eval_uncertainty_states.py`, stated domain-neutrally.
3. **Completeness is a requirement, not a score.** All REQUIRED needs must be
   RESOLVED before an ALL_REQUIRED operation may run (§4).
4. **No facit in the decision path.** Evidence-state derivation uses only
   observable provider/tool output (the experiment's own non-negotiable,
   `crates/swe-gym/AGENTS.md`).

The reference experiment implements the status and abstention rules for
supplier facts with a per-fact source reference; the mandatory provenance fields
(locator, content hash, `retrieved_at`, provider) are still to be built in
`swe-core` (B2 in `07-go-no-go.md`).

**Stays in Socialism:** `ResearchEvidence`, source types, TextUnit locators,
`LegalResearchResult`, GLiNER2 span verdicts.

## 4. Operations

**Today:** implemented and tested in the experiment crate only:
`crates/swe-gym/src/ops.rs` (317 lines). The shape is generic; the fixtures and
executors are legal.

**Target contract (PROMOTE):**

```text
OperationSpec {
  name,
  required_inputs: Vec<NeedRef>,
  execution_policy: ALL_REQUIRED,
  on_missing: INSUFFICIENT_EVIDENCE,
}
ExecutableOperation: gate(&resolved) -> Result<(), MissingOutcome>
OpOutcome { Answer(value) | NeedMore(NeedRef) | InsufficientEvidence }
```

Public rules (all already tested in `ops.rs`):

1. The gate runs **before** the executor, every round. A failed gate means no
   execution and no guess (`ops.rs:178`).
2. Partial inputs never produce an answer; the operation cannot degenerate to
   comparing one value (`ops.rs:96,133`).
3. An operation may discover a new need and return `NeedMore`; control returns
   to investigation, which resolves it and re-runs the operation
   (`ops.rs:157`).
4. The loop is bounded: an identical second `NeedMore` ends in
   `INSUFFICIENT_EVIDENCE` (`ops.rs:188`).

Public split:

- **SWE core:** the spec/gate/outcome types and the loop; deterministic and
  model-based executors share the same outcome type.
- **Socialism:** concrete executors (`MAX_NOTICE_PERIOD`, `APPLY_EXCEPTION`,
  contract-specific normalization) and their fixtures.

Model-based executors are allowed (an operation may call a provider) but they
are still gated by the same required inputs and may only return the same
`OpOutcome` types. The gate is never delegated to a model.

## 5. Budget (Sweycoins)

**Today:** a complete, generic ledger that lives in an experiment crate:
`crates/swe-gym/src/ledger.rs` (append-only, integer milli-Sweycoins,
reserve/settle/release, idempotency keys, `replay`, rewards outside
`available`). It is **not** used by production Python and not wired into the
engine.

**Target contract:**

| Contract | Status | Notes |
| --- | --- | --- |
| `Ledger` API (`allocate`, `reserve`, `settle`, `release`, `transfer`, `reward`, `balance`, `available`, `rewards`, `balances`, `events`, `replay`) and invariants | PROMOTE | Move from the gym crate to a SWE budget crate/module; tests move with it |
| Integer milli-unit amounts | KEEP | No floats; prevents accounting drift |
| Append-only log + replay reconstructs identical state | KEEP | Crash-safe resume without double charging |
| Idempotency key per reserve/settle | KEEP | Replay-and-resume contract (§1.6) depends on it |
| `CostRecord { run_id, invocation_id, action, tariff, amount_milli, idempotency_key }` and a per-run total | BUILD | Needed to "redovisa faktisk budgetförbrukning" |
| Budget exhaustion as a typed, loud outcome | PROMOTE | The gym driver already fails loudly on denial (`crates/swe-gym/src/driver.rs`); make it a typed error/status in the host contract |
| Tariffs and cost model injected by the host | BUILD | Sweycoins must not presuppose JEV, law or one cost model |
| Distinguish allocated vs marginal vs accumulated cost | KEEP as documentation | `docs/SWE_Sweycoins_v2.md` §28; reports must not conflate them |

**Not in the budget contract:** provider pricing, JEV tariffs, model names,
legal cost units. Those are host configuration.

## 6. Providers and tools

**Today:**

- Python provider registry/dispatch: `backend/app/swe/runtime_dispatch.py`
  (`Provider` callable, `ProviderDispatch`, prompt rendering).
- Decision-cache identity: `backend/app/swe/cache.py`
  (`decision_cache_key`, `DecisionCache` protocol).
- Rust emits a typed `InvokeProvider` effect (`effects.rs:53`).
- The JEV provider implementation sits **inside** the generic package:
  `backend/app/swe/providers.py` imports `app.config.settings`,
  `app.jev.system` and `app.services.research.fast_controller`
  (`providers.py:15-22`). It is not wired into production research; only
  `backend/tests/test_swe_provider.py` calls it (`02-inventory-python.md` E10).
- Prompt rendering is imported from Socialism:
  `backend/app/swe/runtime_dispatch.py:17`
  (`from app.services.prompt_render import render_prompt`).

**Target contract:**

| Element | Status | Notes |
| --- | --- | --- |
| Provider request `{provider, prompt_key, prompt, input, choices, cache_policy}` | KEEP | Generated by the host dispatch from the effect + injected prompt map. `params` is not a request field today: provider `cache_params` is an attribute used only to build the cache key (`runtime_dispatch.py:177-184`, `providers.py:77`). Typed request params is BUILD |
| Provider response `{choice, model?, usage?, latency_ms?, cost?}` | KEEP + BUILD `usage`/`cost` | Today any JSON object with a string `choice` is accepted (`runtime_dispatch.py:_invoke_provider`); typed usage/cost metadata is the missing piece |
| Choice must be one of the node's declared choices; otherwise the run fails | KEEP | No fallback provider |
| Typed errors; timeout/cancellation from engine; no retries in v1 | KEEP | |
| Exact decision cache identity + protocol | KEEP | `decision_cache_key`; storage adapter stays Socialism |
| Provider registry, host-injected; optional compile-time whitelist | KEEP | `definition.py` already accepts a `providers` whitelist |
| Prompt resolution (prompt text + placeholders) | DECOUPLE | SWE host package owns the generic 16-line default renderer; the host injects prompt text and may inject a renderer policy. Remove both the `PROMPT_KEY_SET` and `render_prompt` imports from the engine; no duplicate renderer stays in Socialism |
| Task handler contract `Callable[[input], Awaitable[json]]` + `output_schema` validation | KEEP | Reuse for "tools"; do not build a second tool framework |
| JEV provider implementation | DOMAIN | Move out of `app/swe/` to the Socialism integration layer |

The provider contract must not require a particular model vendor. Model choice,
auth, HTTP and retries are host concerns; the engine sees only a request and a
validated answer.

## 7. Policy interface

**Today:** deterministic decisions live in the workflow (`decision` nodes,
`expression`) and in domain code; learned/adaptive policy exists only as
experiment strategies (`crates/swe-gym/src/strategy.rs`, `bandit.rs`,
`td.rs`).

**Target contract (BUILD, small):**

```text
AllowedAction { action_id, kind, cost_hint, required_needs }   // supplied by core
Policy::choose(state, allowed_actions, budget) -> action_id     // must be in the set
```

Rules:

1. The policy chooses only among actions the core already considers allowed
   (resolve a REQUIRED need, run a gate-ready operation, validate a candidate,
   stop). It cannot add an action, skip a gate or declare evidence RESOLVED.
2. Policy output is logged in the run event history and is replayable with the
   same frozen state.
3. The deterministic default policy (priority, then expected yield per coin) is
   the baseline; a learned policy is an arm in the gym, not a core feature.

This is a thin interface over needs/operations/budget, not a new agent
framework. The demo uses a trivial deterministic policy to show that the
allowed-action set is sufficient.

## 8. Execution history and observability

**Today:**

| Element | Location | Status |
| --- | --- | --- |
| Typed events `RunStarted … RunCancelled`, run-local `seq`, lineage | `crates/swe-native/src/events.rs:6-56` | KEEP |
| In-memory `EventSink` boundary | `events.rs:64-67` | KEEP |
| Durable run/invocation/event records | `backend/app/swe/run_store.py`, `app/services/overgraph/workflows.py` | PROMOTE contract; DOMAIN storage |
| Resume semantics | `docs/specs/swe-overgraph.md` | KEEP as the contract |

**Target:**

- The event schema (kind, seq, node instance, lineage, payload) is public and
  versioned; payloads are canonical JSON.
- `RunStore` (Rust trait + Python protocol) is public; the OverGraph
  implementation remains a Socialism adapter.
- `RunRecord` / `InvocationRecord` fields are public; storage keys/layout are
  not.
- The Python host's `RunResult {run_id, status, output, events, error}` is the
  stable host-facing result shape.

## 9. Benchmark / experiment infrastructure

Public contract (detailed in `05-gym-split.md`): a runner over compiled
definitions with deterministic clock, frozen/replayed provider responses,
Sweycoin metering, golden-evaluation and strategy-arm interfaces, and a report
schema that states data, model, seed and cost. Domain benchmarks and fixtures
are not part of the contract.

## 10. Stability, versioning and non-contracts

- **Versioning:** semver `0.x` until the contract list is frozen; Socialism pins
  an exact version (tag) and bumps deliberately. No backward-compat shims while
  there are no customers.
- **Versioned per contract area:** workflow document version is already `1.0`
  (`SWD_VERSION`); event schema and journal format need their own version
  fields.
- **Explicitly not public / not stable:** machine internal state, scheduling
  order beyond deterministic replay, graph-validation internals, BranchPath
  encoding beyond the invocation id format, cache storage, driver loop, prompt
  catalog contents, OverGraph node layout, feature ids.

## 11. Changes this contract set forces (input to Fas F)

| # | Change | Today | Why it is a precondition |
| --- | --- | --- | --- |
| 1 | Inject prompt-key set into compile | `definition.py:13` imports `PROMPT_KEY_SET` | Removes SWE → prompt catalog dependency |
| 2 | Inject prompt renderer | `runtime_dispatch.py:17` imports `render_prompt` | Removes SWE → Socialism services dependency |
| 3 | Move the JEV provider out of `app/swe/providers.py` | imports config/JEV/research | Removes SWE → JEV/research dependency |
| 4 | Host-supply feature registry; move catalog path, host context and `FEATURE_IDS` to the Socialism host | `schema.py:16`, `features.py:28,35` | Removes the Socialism product enum from the core |
| 5 | Promote `NeedRef`/`OperationSpec`/gate semantics | `crates/swe-gym/src/ops.rs` | Generic capability needed by the demo and by any domain |
| 6 | Promote `Ledger`/cost report | `crates/swe-gym/src/ledger.rs` | Budget is generic per the spec |
| 7 | Add Rust run-store/journal contract | Python-only today, and not production-wired | Independent library must resume offline |
| 8 | Evidence/need contract | split across research and gym tools | Required by Fas B/§3 |
| 9 | `usage`/`cost` provider metadata | free-form payload | Required for per-action cost accounting |
| 10 | Policy allowed-action interface | experiment strategies only | Required generic capability |

**Python packaging constraint** (`02-inventory-python.md` §11.3): the generic
host package needs only `jsonschema` and `pyyaml` at runtime (plus the PyO3
extension); `overgraph`, SQLAlchemy, OpenAI/HTTP clients and pydantic settings
belong to the Socialism adapters. The current `backend` `conftest.py` imports
the whole application, so the standalone package needs its own minimal test
harness and neutral fixtures (the current `research_parallel.*` fixture is
research-flavoured).

The Go/No-Go verdict and the extraction order for these items are in
`07-go-no-go.md` and `06-extraction-plan.md`.
