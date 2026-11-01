# swe-reference — domain-neutral SWE reference experiment

A small, fully offline Rust experiment that answers one supplier-selection
question by executing a real SWD 1.0 workflow on the `swe-native` kernel and
metering every action with the reused `swe-budget` Sweycoin ledger.

The point is to show that the SWE core works on its own: the crate has no
product configuration, no network access, no model calls, no document or legal
domain types and no dependency other than the two local path crates. Every
fact in the final answer was retrieved through the engine, verified, and paid
for; the workflow is never given the answer.

**Question:** *"Vilken leverantör erbjuder lägst pris och uppfyller samtliga tre
krav?"*

**Supplier register (provider-side only, never part of the run input):**

| Supplier | Price (SEK) | A | B | C |
| -------- | ----------- | - | - | - |
| Alpha    | 800         | ja | ja | nej |
| Beta     | 950         | ja | ja | ja |
| Gamma    | 900         | ja | nej | ja |

**Expected conclusion:** **Beta** — the only supplier that meets all three
requirements, and therefore the cheapest compliant one.

The mock provider serves five deterministic scenarios, all offline:

| Scenario | What changes | Outcome |
| --- | --- | --- |
| `baseline` | nothing — all facts found and unambiguous | **Beta**, 950 SEK |
| `missing_evidence` | `beta.req_c` is absent, `gamma.price` has conflicting sources | terminal `INSUFFICIENT_EVIDENCE` (incomplete run), price operation never runs |
| `no_compliant_supplier` | every fact found, but no supplier meets requirement B | terminal `NO_COMPLIANT_SUPPLIER` (completed run, no winner) |
| `two_exceptions` | two suppliers fail requirement C, so one screening round discovers two missing facts | the engine surfaces one need per round; both are resolved over subsequent rounds, then **Beta** |
| `ambiguous_exception` | the discovered `c_exception` fact exists but its source is ambiguous | the engine refuses the discovered input with status `AMBIGUOUS`; terminal `INSUFFICIENT_EVIDENCE` |

## What is reused vs. what is application layer

| Concern | Where it lives | Role in this crate |
| ------- | -------------- | ------------------ |
| SWD 1.0 document loading, SHA-256 definition hash, canonical JSON | `swe-native` | Embedded definition is hashed and executed as-is |
| Host-paced machine, effects, routing, fork/join, map, expression + provider decisions, `end` statuses | `swe-native` | The workflow graph; the host only answers effects |
| Append-only Sweycoin ledger (`allocate` / `reserve` / `settle` / `balance` / `events` / `replay`) | `swe-budget` | Budget, per-action metering and deduplicated (at-most-once) settlement |
| Information needs and status (`NeedRef`, `NeedStatus`, `derive_needs`), evidence classification (`EvidenceAssessment` → `EvidenceStatus`), operation contracts and the ALL_REQUIRED gate (`OperationSpec::gate`, typed `OpOutcome`, `GateError`) | `swe-native` | The engine decides; this crate projects |
| Supplier register, its facts and screening rule, mock provider, run journal, handler protocol | **This crate (application layer)** | Demo-level objects the engine must not know about |

No engine logic is copied: the only "driver" code here is effect dispatch,
metering, and the pause/resume journal, mirroring `swe-gym/src/driver.rs`. The
operations gate is **called**, not reimplemented — `gate_inputs` builds the
engine's `NeedStatus` list and asks `OperationSpec::gate`.

## Layout

```
src/lib.rs            crate docs, error type, small JSON helpers
src/domain.rs         supplier register + the two provider scenarios
src/mock_provider.rs  deterministic offline provider (found / missing)
src/evidence.rs       the demo's VERIFIED / AMBIGUOUS / REJECTED vocabulary and its mapping to NeedStatus
src/operations.rs     supplier needs/contracts, gate projection, screening, comparison, reports
src/workflow.rs       embedded SWD 1.0 definition + run input
src/journal.rs        pause/resume journal (canonical JSON file)
src/host.rs           effect dispatch, metering, pause/resume driver
src/main.rs           walkthrough printing all ten demonstrations
tests/reference.rs    integration tests through the public API
```

## Run it

```sh
export PATH="$HOME/.cargo/bin:$PATH"
cd crates/swe-reference
cargo run --offline            # walkthrough, exits 0
cargo test --offline           # integration tests
cargo fmt --check
cargo clippy --offline --all-targets -- -D warnings
```

Everything is offline. The only dependencies are the path crates
`../swe-native` and `../swe-budget`; `Cargo.lock` is committed. It does not
depend on `swe-gym`.

## Walkthrough: the ten required demonstrations

| # | Requirement | How it is demonstrated | Code |
| - | ----------- | ---------------------- | ---- |
| 1 | Needs derived from operation contracts, not resolved data | The `plan` handler reads only the candidate list and the static operation contracts (`screen_compliance`: 9 inputs, `select_cheapest`: 3) and plans 12 needs before any fetch. A test also shows the need list is identical for the damaged-register scenario. | `operations::plan_needs_handler`, `OperationRegistry::for_candidates` |
| 2 | Retrieval through a deterministic mock provider | Fetching is a provider `decision` node; the host answers every `InvokeProvider` effect from a pure `(scenario, supplier, fact)` function. No network, no time, no randomness. | `mock_provider.rs`, `domain::fetch_observation` |
| 3 | Detect and handle missing data | The provider has a `missing` choice; the `missing` route records a rejected observation instead of guessing. | `evidence::record_missing_handler` |
| 4 | Evidence verification with generic statuses + provenance | Every observation becomes evidence with `VERIFIED` / `AMBIGUOUS` / `REJECTED`, a confidence and a per-fact `source_ref`. Rules: no value or no provenance → `REJECTED`; conflicting sources or low confidence → `AMBIGUOUS`; otherwise `VERIFIED`. | `evidence::verify` |
| 5 | ALL_REQUIRED gate on operation `required_inputs` | Both operations gate on every declared input before running; the gate returns the exact missing list with the reason. `AMBIGUOUS` and `REJECTED` both fail the gate. | `operations::gate_inputs` (calls `swe_native::OperationSpec::gate`), `screen_compliance_handler`, `select_cheapest_handler` |
| 6 | Deterministic comparison | Lowest price among compliant suppliers, tie-broken by supplier id, so map completion order can never change the answer. | `operations::cheapest_compliant` |
| 7 | Abstain with `INSUFFICIENT_EVIDENCE` | The `missing_evidence` provider scenario loses `beta.req_c` (rejected) and makes `gamma.price` conflicting (ambiguous). Screening's gate fails, the workflow routes to an `end` node with status `incomplete`, and the terminal output is `status/conclusion = INSUFFICIENT_EVIDENCE` with an explicit reason. | `build_abstain_handler`, `abstain_screen`/`insufficient_end` nodes |
| 8 | Actual Sweycoin consumption per action + total | Every handler and provider call is `reserve`d and `settled` in the reused ledger, then reported per action and summed. The binary asserts `sum(per action) == ledger.spent`. | `host::Session::dispatch`, `RunReport::action_totals` |
| 9 | Pause/resume without recharging or re-executing | With a spend checkpoint, segment 1 pauses and returns a journal (definition SHA-256, document, input, completed invocation results, charges, ledger events). Resume replays completions into a fresh machine in effect order and rebuilds the ledger with `Ledger::replay`, then finishes with the remaining budget. Tests assert identical total spend, identical unique execution count, identical charges and an identical ledger event log compared to an uninterrupted run. | `host::run`/`resume`, `journal.rs` |
| 10 | `NeedMore(NeedRef)` resolved and the operation re-run | Screening discovers that Alpha fails requirement C and that its static contract does not cover the documented exception. The operation returns the engine's singular `OpOutcome::NeedMore(NeedRef)` (serialised to `status: "need_more"` with `alpha.c_exception`); the workflow resolves that need through the same map/provider path, merges the evidence and routes back to `screen_compliance`, which now completes. | `operations::screen_compliance` (`OpOutcome`), `extra_evidence`/`evidence` nodes |

## Expected output (excerpt)

```
SWE reference experiment — supplier selection, domain-neutral
Question: Vilken leverantör erbjuder lägst pris och uppfyller samtliga tre krav?
Candidates: Alpha, Beta, Gamma
SWD definition sha256: 5b33a36739aee47cae537794373c9fc1d3e119124347ea267badedb42eb2ba8f
Run input: question + candidate identities only. No prices, no answers.

[1] Information needs derived from operation contracts, before any data was read
    contract screen_compliance: 9 declared required inputs
    contract select_cheapest: 3 declared required inputs
    plan produced 12 needs (9 compliance + 3 price):
      - alpha.req_a
      ...
[2] Retrieval through a deterministic, offline mock provider
    provider effects dispatched: 13; source = in-memory supplier register, no network, pure function
[3] Missing data is detected and recorded, never guessed (scenario: missing_evidence)
    beta.req_c -> REJECTED (no data: supplier register has no record for beta/req_c)
    gamma.price -> AMBIGUOUS (sources disagree: conflicting source references for the same fact)
[4] Evidence verification: generic VERIFIED / AMBIGUOUS / REJECTED + provenance
    baseline: VERIFIED=13 AMBIGUOUS=0 REJECTED=0
[7] Abstention is a terminal incomplete run with an explicit reason
    terminal status: Incomplete
    conclusion: INSUFFICIENT_EVIDENCE
    reason: INSUFFICIENT_EVIDENCE: operation screen_compliance ALL_REQUIRED gate is missing 1 required input(s): beta.req_c
[8] Sweycoin consumption from the reused swe_budget ledger (per action + total)
    mock.supplier_facts                 13       2000          26000
    reference.accept_evidence           13       1000          13000
    ...
    total: 46000 milli Sweycoin spent of 50000 allocated (4000 remaining); 67 ledger events; sum(per action) == ledger spent
[9] Pause and resume an ongoing investigation with a journal
    segment 1 paused at 20000 milli spent (checkpoint 20000 milli); 13 actions executed; 13 completed invocation results journaled
    segment 2 replayed 13 completed invocations without re-running handlers or re-charging, then executed 20 new actions
    resumed conclusion: Beta; total spent 46000 milli (uninterrupted run spent 46000 milli)
    unique invocations executed: 33 (uninterrupted: 33) — nothing ran twice
[10] NeedMore: an operation discovers a new need, resolves it, then re-runs
    screen_compliance pass 1: NeedMore
      NeedRef alpha.c_exception — requirement C is not met for Alpha; ...
    screen_compliance pass 2: ok (compliant: beta)
    investigation resolved it: alpha.c_exception = none from mock://supplier-register/v1/alpha/c_exception

Conclusion: Beta (950 SEK) — Beta has the lowest price (950 SEK) among 1 compliant supplier(s)
```

## What the pause/resume guarantee is — and is not

The tests assert three different things, and they must not be conflated:

* **Deduplicated (at-most-once) settlement — guaranteed here.** The ledger is
  rebuilt from its own event log with `Ledger::replay`, so a resumed run cannot
  settle the same logical call twice: a resumed run spends exactly what the
  uninterrupted run spends (46 000 milli in both cases), with identical ledger
  event logs. *Exact* settlement per confirmed call holds under the liveness
  assumption that the journal reaches durable storage.
* **At-least-once delivery of the demo's own actions, idempotent here —
  guaranteed here.** All actions in this experiment are deterministic pure
  functions (handlers and a mock provider), so replaying a confirmed result is
  provably identical to re-running it: 33 unique invocations in both runs,
  nothing ran twice.
* **Exactly-once execution of *external* tools — NOT claimed.** If a real
  provider performs a side effect and the process crashes before the result is
  journaled, the outcome is *unknown*: it is not the same as "did not happen".
  That needs the tool to honour an idempotency key (at-least-once delivery,
  at most one external effect), or an explicit fail-stop recovery policy. It
  also means an unconfirmed external charge may be unknown rather than zero:
  the journal reserves `pending_reconciliation` for that case.

**The demo now uses the engine journal.** The journal file carries, and requires,
these fields (a missing one is a typed error, never a default):

| Field | Meaning |
| --- | --- |
| `journal_version` | the demo journal format |
| `contract_version` | version of the engine need/evidence/operation contracts |
| `definition_sha256` | hash of the workflow document |
| `tariff_sha256` | hash of the tariffs the run was billed at |
| `dependency_versions` | the provider/operation versions this run actually used |
| `journal_events` | the engine's append-only event log |
| `document`, `input`, `completed`, `charges`, `ledger_events` | the run state |

**The demo now uses the engine journal.** Every metered action is recorded in
`swe_native::journal` as four events — `AttemptStarted`, `ChargeReserved`,
`ResultConfirmed`, `ChargeSettled` — and those events are persisted with the run
journal and replayed into a fresh engine journal on resume. The walkthrough
prints the summary: over a paused-and-resumed run, 132 events with 33 attempts,
33 confirmations, 33 reservations and 33 settlements.

Because every action here is a deterministic pure function, the delivery is
declared `Pure`: an interrupted attempt is recomputed, never replayed from an
unknown. The external delivery strategies (`ExternalIdempotent`,
`ExternalNonIdempotent` → `fail_stop`) and `plan_recovery` exist in the engine
and are unit-tested there; this experiment does not exercise them, because it
has no external side effects to exercise them with.

## Pause/resume design

A "run" is a sequence of segments. Each segment allocates its own ledger and
drives the kernel until either terminal status or a spend checkpoint.

* **Pause**: when the next metered action would exceed
  `RunConfig::pause_at_spend_milli`, the host does not fail the run. It stops,
  builds a `Journal`, and returns `InvestigationOutcome::Paused`. The journal
  holds the definition SHA-256 and document, the run input, the run id, the
  scenario, the tariffs, **every completed invocation result in completion
  order**, the accumulated charges, the full ledger event log and the call
  counts. It is written as canonical JSON (`Journal::to_json` uses the kernel's
  canonical JSON writer).
* **Resume**: `resume` validates the definition hash, rebuilds the ledger with
  `Ledger::replay`, starts a fresh machine with the same run id, input and
  logical clock, and replays persisted invocation results in the order the
  fresh machine emits effects. Replayed invocations are **not** executed and
  **not** charged; only invocation ids that were still pending in the paused
  segment reach a handler or the provider. Because the kernel's invocation ids
  are deterministic (`run:node:branch_path:epoch:serial`), the replay lines up
  exactly.
* The logical clock is fixed, and no wall clock or randomness is involved, so
  pause+resume produces the same ledger event log as an uninterrupted run.

Journal file shape (written to `$TMPDIR/swe-reference-journal.json` by the
walkthrough):

```json
{"journal_version":1,"run_id":"reference-investigation",
 "definition_sha256":"<hex>","document":{...},"input":{...},
 "now_ms":1000,"scenario":"baseline","budget_milli":100000,
 "handler_cost_milli":1000,"provider_cost_milli":2000,
 "completed":[{"invocation_id":"...","node_id":"plan","output":{...}}],
 "charges":[{"invocation_id":"...","action":"reference.plan_needs","kind":"handler","cost_milli":1000}],
 "ledger_events":[{"kind":"allocate","run_id":"...","account":"investigation",...}],
 "handler_calls":1,"provider_calls":0}
```

## Tests

`cargo test --offline` runs these integration tests (plus five journal codec unit tests in `src/journal.rs`):

- `a_discovered_fact_the_engine_refuses_stops_the_investigation`
- `a_journal_from_another_contract_version_is_refused`
- `a_journal_missing_a_recorded_dependency_is_refused`
- `a_journal_recording_another_dependency_version_is_refused`
- `a_journal_replayed_under_other_tariffs_is_refused`
- `a_journal_whose_definition_changed_is_refused`
- `a_journal_with_an_unreadable_event_log_is_refused`
- `a_journal_without_a_contract_version_is_refused`
- `a_run_priced_with_custom_tariffs_round_trips_and_resumes`
- `a_tampered_journal_is_rejected`
- `a_tampered_tariff_hash_is_refused`
- `conclusion_is_beta`
- `confirmed_outcomes_survive_the_round_trip`
- `deterministic_comparison_prefers_cheapest_compliant`
- `every_action_is_journaled_as_attempt_result_and_charge`
- `every_critical_transition_survives_a_pause_and_resume`
- `metering_is_per_action_and_sums_to_the_ledger`
- `missing_and_ambiguous_evidence_abstains`
- `need_more_is_resolved_and_operation_reruns`
- `need_more_is_the_engines_singular_protocol`
- `needs_are_derived_from_operation_contracts_not_data`
- `no_action_settles_twice_across_a_resume`
- `no_compliant_supplier_is_a_terminal_report_not_a_crash`
- `pause_and_resume_charge_and_execute_exactly_once`
- `repeated_runs_are_deterministic`
- `several_discovered_needs_are_resolved_over_several_rounds`
- `the_demo_cannot_accept_an_input_the_engine_refuses`
- `the_gate_is_the_engines_and_lists_every_missing_input`
- `the_journal_round_trip_is_lossless`
- `the_refusal_payload_reports_every_missing_input_and_its_status`

## Gaps found in SWE core

These are observations from building this experiment; none were worked around by
copying engine logic.

1. **No generic evidence / need / operation types.** `swe-native` can route a
   provider decision but has no notion of an information need, evidence status,
   provenance or an operation contract. Everything in `evidence.rs` and
   `operations.rs` is application code.
2. **No reusable host driver.** Each host (the Python runtime, `swe-gym`, this
   crate) re-implements the same loop: take effects → dispatch → complete/fail →
   tick. A small reusable dispatcher with a metering hook would remove that
   duplication without moving domain logic into the kernel.
3. **No engine-level resume/journal.** The kernel exposes a deterministic
   machine but no persistence or replay protocol. The Python host builds replay
   on top of its durable store; a Rust host must write its own journal and
   replay loop (as done here).
4. **Provider output is choice-shaped.** A provider decision requires a `choice`
   string; any facts must either be carried as extra keys in the same output
   object (as this demo does informally) or routed through handlers. There is no
   typed provider result.
5. **Budget is not in the kernel — resolved in B2.** The generic Sweycoin ledger
   used to live in the `swe-gym` experiment crate, so a non-gym host had to take
   a path dependency on an experiment. B2 moved it to its own `swe-budget` crate
   and this demo now depends on that. The ledger is still not wired into the
   engine's effect loop; metering remains a host responsibility.
6. **`join` mode is declared but not parsed.** `docs/specs/swe-swd-1.0.md`
   documents `mode: all` / `on_failure: fail`, but the Rust definition parser
   only reads join `outputs` and `next`; the field is silently ignored.
7. **Leaner preflight than the Python reference.** The Rust kernel validates
   node types, handler/provider names and task features, but does not perform
   the reachability / join-ownership / branch-termination graph checks described
   for the Python parser.
8. **No `Serialize` for `Json`.** A journal must use the kernel's canonical JSON
   writer plus a hand-written reader; acceptable, but a documented
   parse/serialize pair for `Json` would help hosts.

## Constraints this experiment respects

* Only path dependencies `../swe-native` and `../swe-budget`; own `Cargo.toml` and
  committed `Cargo.lock`; no workspace membership changes.
* No network, no model calls, no Python, no product configuration, no document
  or legal domain types.
* No changes outside `crates/swe-reference/`; existing crates untouched.
