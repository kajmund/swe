# Migration plan: local SweycoinMeter -> SWE runtime budget (PR #11)

**Status:** `INTEGRATED_ON_MAIN` (2026-11-25). The shared runtime budget
landed, and `microtask_swarm/runtime_budget_policy.py` is the integration
path. It uses `swe.budget.RuntimeBudget` with `swe_budget.Ledger` and
`CarriedMilliPolicy`. The frozen baseline (`budget.py`, `executor.py`,
`async_benchmark.py`, `adaptive_budget.py`, `production_stop.py`)
intentionally still uses the local pilot meter so its recorded reports stay
reproducible; the integration path does not use it.

The rest of this document is the historical plan written on 2026-11-23. It is
kept for traceability; the sections below are annotated where the landed
design differs from the original plan.

## Landed state

- PR #11 (`experiment/runtime-budget`) merged to `main` at
  `252620be31a4bf0d2c1f451d3ce2137a2b158fc1`.
- PR #15 (`experiment/runtime-budget-policy`) added the integration path in
  `4ad210f`, `42b8114`, `58598a0`, `35b8edb`. The production tariff is
  `CarriedMilliPolicy` on `swe.budget.BudgetPolicy`.
- PR #16 (`experiment/ready-dag-contract`) merged the SWD 1.1 ready map at
  `f1ae2b8714fc6b88f66a00b47a1baa0469af718f`.
- `make python` builds both native wheels (`swe-native` and
  `swe-budget-native`). `tests/test_runtime_budget_policy.py` exercises the
  native ledger lifecycle, and `tests/test_integration_microtasks.py` runs a
  real `MicrotaskBatch` through `WorkflowRunner` with a `RuntimeBudget`. The
  agent-contract preflight exercises the same budget-through-runner boundary.
- The integration report records `backend: swe.budget.RuntimeBudget`,
  `ledger: swe_budget.Ledger`, and
  `local_meter_used_in_integration_path: False`.

The historical inspection below was read-only and made no paid benchmark call.

## 1. Current local implementation

`experiments/microtask-swarm/microtask_swarm/budget.py` implements a small
`SweycoinMeter`:

- fixed `allocated_milli` at construction;
- `reserve(key, amount) -> reservation_id`;
- `settle(reservation_id, amount)`;
- `release(reservation_id)`;
- `charge(key, amount)` for the deterministic verifier;
- `events` and `replay_events(...)`;
- integer milli only, idempotency keys, no negative balance.

Call sites:

- `microtask_swarm/executor.py` reserves before each provider attempt, settles
  every attempt (including cancelled attempts), releases on pre-dispatch
  failure, and calls `settle` for the verifier-style charge path.
- `microtask_swarm/runner.py` constructs one meter per run from
  `sweycoins.run_allocated_milli` and reports `meter.to_dict()`.
- `tests/test_budget.py` and `tests/test_executor.py` test the meter.

This local implementation duplicates ledger invariants. The landed design did
not delete it from the frozen baseline: doing so would invalidate the committed
reports and the tests that pin their hashes. Instead, the integration path
above was added and the local meter is excluded from it. A full baseline
migration remains a separate future change.

## 2. Target contract from PR #11

`RuntimeBudget` composes a host `BudgetLedger` with a host `BudgetPolicy`:

```python
BudgetRequest(run_id, invocation_id, node_id, kind, name, input)
BudgetOutcome(ok, cancelled=False, timed_out=False, cache_state=None, value=None, error=None)

BudgetPolicy.reserve(request) -> int          # conservative ceiling; 0 = free
BudgetPolicy.settle(request, outcome) -> int  # actual; 0 <= actual <= reserved

BudgetLedger.reserve(*, run_id, account, amount_milli, key) -> int
BudgetLedger.settle(*, reservation_id, spent_milli, key) -> None
BudgetLedger.release(*, reservation_id, key) -> None
BudgetLedger.balance(*, account) -> Mapping[str, int]
BudgetLedger.events() -> Sequence[Mapping[str, Any]]
```

`NativeBudgetLedger` is the adapter for `swe_budget.Ledger`, which is the Rust
`swe-budget` crate. `NativeBudgetLedger` also exposes `allocate(...)` and
`NativeBudgetLedger.replay(events)`.

`RuntimeBudget` lifecycle:

- `reserve` before dispatch; insufficient funds fail closed and the call is not
  made;
- `settle` exactly once with actual cost; an overrun keeps the reservation open;
- `release` only when the call definitely did not consume funds;
- `mark_pending` on cancel/timeout; funds are **not** released early;
- `reconcile(reservation, spent_milli=...)` or `release=True` is the host's
  later decision for a pending reservation;
- stable keys: `{run_id}:{invocation_id}:reserve|settle|release`;
- replay uses the persisted ledger event log, and replayed invocations do not
  reserve or settle again.

`WorkflowResources.budget` wires the same `RuntimeBudget` into
`runtime_dispatch`: handlers reserve after the concurrency permit, providers
reserve before the exact-cache lookup, and cancellation/timeout call
`mark_pending`.

## 3. Compatibility matrix

| Concern | Local `SweycoinMeter` | PR #11 `RuntimeBudget` | Migration decision |
| --- | --- | --- | --- |
| Currency | integer milli | integer milli | Compatible; keep milli everywhere |
| Allocation | constructor `allocated_milli` | `NativeBudgetLedger.allocate(run_id, account, amount, key)` | Allocate once per experiment run with a stable key, e.g. `{run_id}:allocate` |
| Reserve | `reserve(key, amount)` | `budget.reserve(BudgetRequest)` -> reservation | Build a `BudgetRequest` per provider attempt |
| Keys | arbitrary `str` | `{run_id}:{invocation_id}:...` | Use deterministic run/invocation ids; never reuse a key with different parameters |
| Settle | `settle(id, amount)` | `budget.settle(reservation, BudgetOutcome)` | Policy returns the actual tariff; settle once |
| Release | `release(id)` | `budget.release(reservation)` | Use only before dispatch or when the host knows no charge occurred |
| Verifier charge | `charge(key, amount)` | no direct `charge` | Reserve with `kind="verifier"`, then settle immediately; tariff comes from policy |
| Cancel/timeout | current code settles the tariff to be conservative | `mark_pending`; no release | Change to `mark_pending`; reconcile at run end. Offline simulations may release; live calls stay pending or settle conservatively |
| Retries | key includes `attempt-N`, each attempt charged | same key on a host retry is idempotent and is **not** charged twice | To preserve all-in cost including retries, give each provider attempt a distinct `invocation_id` (`task:model:attempt-N`). Do not describe these as retries of one logical invocation |
| Replay | `replay_events` reconstructs a local meter | `NativeBudgetLedger.replay(events)` | Persist ledger events and replay the native ledger; delete local replay |
| Balance/report | `to_dict()` counters | `budget.balance()` and `budget.events()` | Report `allocated/incoming/outgoing/spent/reserved/available`; never recompute locally |
| Pending reservations | not represented | `budget.pending()` | Report pending count/amount; host reconciles |
| Concurrency | external semaphore only | runtime capacity permit for handlers; provider reservations before cache lookup | If routed through `WorkflowRunner`, runtime owns the permit; if direct, keep the experiment semaphore and document that it is the capacity boundary |
| Cache | none | provider exact-cache hit still reserves, policy may settle 0 | No cache in this experiment now; policy must handle `cache_state="hit"` if added |
| Failure mapping | local `BudgetError` | `BudgetUnknownCost`, `BudgetExceeded`, `BudgetOverrun`, `BudgetStateError` | Surface the PR's typed errors; kernel error kind is `handler` by design |
| Threading | Python object | PyO3 `Mutex`-backed ledger, synchronous callbacks | Calls are short; do not block inside policy/ledger |

### Key identity decision

The experiment must charge every real provider attempt. PR #11's retry
idempotency is correct for a host retry of the **same logical invocation**, but
the swarm's retry is a new billable attempt. Therefore:

```text
run_id        = experiment run id (mode/model/concurrency)
invocation_id = f"{task_id}:{model_id}:attempt-{n}"
node_id       = task_id
kind          = "provider" | "verifier"
name          = model id | "deterministic-verifier"
```

This preserves the existing all-in accounting and does not double-charge the
same attempt. If the host later wants true retry idempotency, it must reuse the
same `invocation_id` and accept that the ledger will not charge twice.

## 4. Migration phases

### Phase 0 - gate (satisfied 2026-11-24)

The conditions below were the gate before integration. All are now satisfied.

1. PR #11 has an independent **APPROVED** review (`reviewDecision=APPROVED`).
2. All required CI jobs are green, including `Package dry runs`. The known
   publish-order fix is to publish `swe-budget` before verifying
   `swe-native`, or to make the packaging check build the workspace path
   dependency without contacting crates.io.
3. PR #11 is merged to `main`.
4. The merge commit/tag is pinned in this experiment.
5. A Rust toolchain and a `swe_native` wheel containing `Ledger` are available
   in the test environment.

### Phase 1 - contract probe only (safe now)

Add a skipped-by-default test that checks the public API names exist when
`swe.budget` is importable. It must not instantiate a native ledger unless the
wheel is present, and it must not call any model.

### Phase 2 - replace the meter (not applied to the frozen baseline)

The landed design keeps the frozen baseline and adds the integration path
instead. The steps below are the historical plan for a full baseline
migration; they were not executed.

1. Delete `microtask_swarm/budget.py` (`SweycoinMeter`, `Event`,
   `replay_events`). Do not leave a fallback ledger.
2. Add a thin `SwarmBudgetPolicy` implementing `BudgetPolicy`:
   - `reserve` returns the contract tariff from `config["contracts"]`, or the
     verifier tariff for `kind="verifier"`;
   - `settle` returns the actual tariff; contract/transport failures that
     reached the provider settle the tariff, pre-dispatch failures release;
   - no token/currency arithmetic in the policy; USD stays in `cost.py`.
3. Construct one `NativeBudgetLedger`, allocate the run budget once, and build
   `RuntimeBudget(ledger, policy, account=...)`.
4. In `SwarmExecutor`, replace `meter.reserve/settle/release` with
   `RuntimeBudget` calls and `BudgetRequest` objects. Keep the experiment's
   global semaphore as the capacity boundary unless Phase 3 routes through
   `WorkflowRunner`.
5. Replace `meter.charge` for the verifier with reserve + immediate settle.
6. On `CancelledError`/timeout, call `mark_pending`, not `settle`/`release`.
   At run end:
   - offline: `reconcile(release=True)` for cancelled simulated calls;
   - live: keep pending or `reconcile(spent_milli=tariff)` conservatively,
     and report the pending amount.
7. Replace `meter.to_dict()` with `budget.balance()` plus event/pending counts.
   Remove local counters.
8. Persist `ledger.events()` alongside the run report; on resume, reconstruct
   via `NativeBudgetLedger.replay(events)` and assert the same balance.

### Phase 3 - optional runtime-dispatch integration (covered outside the swarm simulator)

The agent-contract preflight already attaches `RuntimeBudget` through
`WorkflowResources.budget` on `WorkflowRunner`. The swarm experiment keeps its
host-level policy simulator rather than inventing a second scheduler. The
historical steps below remain optional.

If the experiment should use `WorkflowResources.budget` rather than direct
`RuntimeBudget` calls:

1. Run each microtask through `swe.microtasks.compile_microtasks` /
   `MicrotaskBatch.run` with a handler that performs the provider call.
2. Attach `WorkflowResources(budget=RuntimeBudget(...))`.
3. Model competing candidates as separate SWD invocations/map children and use
   the existing invocation handles for early cancellation. Do not invent a
   second scheduler.
4. Remove the experiment's manual reserve/settle calls once runtime dispatch
   owns them.

Phase 2 is the minimal migration that removes the duplicated ledger. Phase 3
is a larger scheduling change and should be a separate reviewed change.

## 5. Tests to add before deleting the local meter

All offline; no model calls.

1. **API probe**: `swe.budget` exposes the PR #11 names and methods.
2. **Parity fixture**: replay a recorded sequence of reserve/settle/release
   operations through the native ledger and compare balance, spent, reserved,
   and available with the expected values. Do not keep the old meter as a
   runtime dependency; compute expected values as constants in the test.
3. **Retry identity**: two attempts with distinct invocation ids produce two
   charges; the same invocation id reused produces one charge.
4. **Cancel pending**: cancellation leaves the reservation reserved; a later
   reconcile settles or releases exactly once.
5. **Overrun**: settle above the reservation raises `BudgetOverrun` and leaves
   the reservation open.
6. **Unknown cost**: policy failure prevents dispatch and writes no event.
7. **Replay**: `NativeBudgetLedger.replay(events)` reconstructs the balance.
8. **Verifier**: reserve+settle charges the verifier tariff once.
9. **Report mapping**: balance/events/pending map to the report without a
   second set of counters.
10. **No duplicate ledger**: a repository assertion that
    `microtask_swarm/budget.py` is gone and no local reserve/settle arithmetic
    remains outside tests.

## 6. Rollback (historical)

Before deleting the local meter, keep the migration in a separate commit. If
the native binding is unavailable or the ledger behaves differently, revert
that commit; do not reintroduce a second ledger. Phase 0 has since been
satisfied, and the landed design is additive: the frozen baseline is untouched
and the integration path can be reverted without changing it.

## 7. Resolved blockers (2026-11-24)

The six blockers from the historical plan are resolved:

1. **PR #11 review and merge.** The budget runtime merged at `252620b`.
2. **CI and packaging.** The native wheels build via `make python`; the
   integration tests run against them.
3. **Not merged.** The contract is on `main`; the experiment is no longer bound
   to an unlanded branch.
4. **Rust/PyO3 wheel with the ledger.** `swe_budget.Ledger` is built and
   installed by `make python`.
5. **Packaging order.** `make package` and CI build both native wheels from the
   workspace.
6. **Cancellation semantics.** The integration path calls `mark_pending` and
   reconciles at the end; the report distinguishes settled, pending,
   late-billed, and released milli. The frozen baseline keeps its historical
   accounting so its reports remain reproducible.

## 8. Current verification commands

Build the native wheels once (Rust 1.83), then run the integration tests with
the interpreter that has them installed:

```sh
export PATH="$HOME/.cargo/bin:$PATH"
make python

cd experiments/microtask-swarm
make PYTHON=../../python/.venv/bin/python runtime-budget-policy
PYTHONPATH=.:../../python ../../python/.venv/bin/python -m unittest tests.test_runtime_budget_policy -v
PYTHONPATH=.:../../python ../../python/.venv/bin/python -m unittest discover -s tests -t . -v

cd ../..
python/.venv/bin/python -m pytest experiments/agent-contract/tests -q
```

No paid benchmark call is part of this migration.
