# Migration plan: local SweycoinMeter -> SWE runtime budget (PR #11)

**Status:** `PREPARED_BLOCKED_PENDING_PR_11`. No integration has been made.
The local meter is still in place. This document plans its removal, not a
second ledger.

Inspected read-only on 2026-11-23:

- PR: `kajmund/swe#11`, `feat: make budget a first-class runtime resource`
- Branch: `experiment/runtime-budget`
- Head commit: `2d6e1252893ac6e6b5ea1771731dfe6d305e3c06`
- Base: `main`
- State: **OPEN**, `mergeStateStatus=UNSTABLE`, `reviewDecision=""`
- Reviews: one `COMMENTED` review from `chatgpt-codex-connector`; **no
  independent approval**
- CI: `Package dry runs` fails (`cargo package --locked -p swe-native`:
  `no matching package named swe-budget found` on crates.io); the aggregate
  `CI` job fails because of it. Other jobs pass.
- Contract documents:
  - `docs/specs/swe-budget-runtime.md`
  - `docs/swe-standalone/17-budget-runtime-decision.md`
  - `python/swe/budget.py`
  - `python/swe/runtime_dispatch.py`
  - `crates/swe-native/src/python.rs` (`PyLedger`)

No paid benchmark call was made for this inspection. Existing live/offline
reports are unrelated to this migration.

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

This local implementation duplicates ledger invariants. It must be deleted, not
wrapped or kept as a fallback, once the shared runtime budget is available.

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

`NativeBudgetLedger` is the adapter for `swe_native.Ledger`, which is the Rust
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

### Phase 0 - gate (not started)

Do not edit code until all conditions hold:

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

### Phase 2 - replace the meter

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

### Phase 3 - optional runtime-dispatch integration

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

## 6. Rollback

Before deleting the local meter, keep the migration in a separate commit. If
the native binding is unavailable or the ledger behaves differently, revert
that commit; do not reintroduce a second ledger. The experiment can continue
with the local meter only until Phase 0 is satisfied.

## 7. Exact blockers

1. **PR #11 is OPEN with no independent approval.** Only a `COMMENTED` review
   exists; `reviewDecision` is empty.
2. **CI is red.** `cargo package --locked -p swe-native` fails because
   `swe-budget` is not published on crates.io; the aggregate `CI` job fails.
   Publishing order or a packaging fix is required.
3. **Not merged.** The contract is not on `main`; integration would bind the
   experiment to an unlanded branch.
4. **No local Rust/PyO3 wheel with `Ledger`.** The current environment has no
   Rust toolchain and no `swe_native` built from PR #11, so the binding cannot
   be exercised locally yet.
5. **`swe_native` packaging depends on `swe-budget` publishing.** This is the
   same publish-order issue, but it also affects wheel distribution after
   merge.
6. **Cancellation semantics change.** The local meter charges cancelled calls;
   PR #11 keeps them pending. The report must distinguish spent vs pending and
   define host reconciliation before claiming exact Sweycoin totals.

## 8. Next commands after the gate

Read-only status check:

```sh
gh pr view 11 --repo kajmund/swe \
  --json state,reviewDecision,mergeStateStatus,statusCheckRollup
```

After approval and merge:

```sh
cd /Users/erikfremred/swe-worktrees/microtask-swarm
git fetch origin
git merge origin/main
cd experiments/microtask-swarm
PYTHONPATH=.:../../python python3 -m unittest discover -s tests -t . -v
```

Then build the native wheel and run the binding probe:

```sh
uvx maturin build --manifest-path ../../crates/swe-native/Cargo.toml \
  --interpreter python/.venv/bin/python --out python/dist
python -m pytest python/tests/test_swe_budget_binding.py \
  python/tests/test_swe_budget_runtime.py
```

No paid benchmark call is part of this migration.
