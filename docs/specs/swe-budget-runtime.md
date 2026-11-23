# Runtime budget

**Status:** implemented as an opt-in host feature. The frozen SWD 1.0 and
journal v1 contracts are unchanged.

## What this is

`WorkflowResources.budget` attaches a `swe.budget.RuntimeBudget` to a run.
When present, every handler and provider invocation is priced by a host
policy, reserved atomically before the call, and settled once by dispatch;
host reconciliation may release a pending hold. The ledger is the existing
Rust `swe-budget` crate, exposed to Python as `swe_budget.Ledger`; SWE does not
implement a second ledger.

When the resource is absent, runtime behaviour is exactly the previous
behaviour: no reservation, no settlement, no new event.

## Ownership

| Concern | Owner | Contract |
| --- | --- | --- |
| Ledger invariants: reserve/settle/release/replay, idempotency, no negative balance | Rust `swe-budget` | `crates/swe-budget/src/lib.rs` |
| PyO3 surface: `swe_budget.Ledger` | `crates/swe-budget-py` (`publish = false`) | `crates/swe-budget-py/src/lib.rs` |
| Lifecycle: when to reserve, settle, release, or keep pending | Python `swe.budget.RuntimeBudget` | this document |
| Prices, provider identity, token/cost estimates | Host `BudgetPolicy` | this document |

The policy is deliberately domain-neutral. SWE never interprets currency,
model names, token prices, tenant billing rules, or provider contracts. It
passes a `BudgetRequest` to the policy and receives integer milli-unit
amounts.

Budget callbacks are synchronous because the reference ledger is in-memory.
A host that wraps a blocking durable ledger must provide a non-blocking adapter
(or a local cache plus an async reconciler); it must not block the event loop
inside `reserve` or `settle`.

## Types

```python
BudgetRequest(
    run_id, invocation_id, node_id, kind, name, input
)

BudgetOutcome(
    ok, cancelled=False, timed_out=False, cache_state=None, value=None, error=None
)

BudgetPolicy:
    reserve(request) -> int          # conservative ceiling; 0 = known free
    settle(request, outcome) -> int  # actual cost; 0 <= actual <= reserved

BudgetLedger:
    reserve(run_id, account, amount_milli, key) -> reservation_id
    settle(reservation_id, spent_milli, key) -> None
    release(reservation_id, key) -> None
    lookup(key) -> {reservation_id, amount_milli, spent_milli, account, status} | None
    balance(account) -> {allocated, incoming, outgoing, spent, reserved, available}
    events() -> [...]

RuntimeBudget:
    pending() -> tuple[BudgetReservation, ...]
    unresolved() -> tuple[BudgetReservation, ...]
    reservation_for(invocation_id) -> BudgetReservation | None
    reconcile_invocation(invocation_id, spent_milli=..., release=...)
    mark_pending_invocation(invocation_id)
    restore(run_id=...)
```

`RuntimeBudget` composes one ledger, one policy, and one account. The host
constructs it; the runtime calls `reserve`, `settle`, `mark_pending`,
`mark_pending_invocation`, `restore`, and `reservation_for`. On a late return
after cancel or timeout, `BudgetOutcome` carries `cancelled`/`timed_out` from
the invocation handle; `ok` still reflects whether the call itself returned.

`pending()` is the recovery queue: cancel/timeout holds **and failed
settlements**. `unresolved()` additionally includes still-reserved in-flight
holds. `reservation_for()` returns by invocation id, and
`reconcile_invocation()` finishes a pending hold without the host retaining the
object returned by `reserve`.

## Invariants

1. **Reserve before dispatch.** The reservation happens immediately before the
   handler/provider call. For handlers it is taken after the concurrency permit
   is acquired; for providers it is taken before the exact-cache lookup, so a
   cache hit still reserves conservatively and the policy settles `0` using
   `cache_state="hit"`. A call that never reaches the handler/provider never
   reserves.
2. **Atomic reservation.** The ledger's reserve is the atomic check-and-hold.
   If funds are insufficient, the handler is not called and the invocation
   fails closed with a budget error.
3. **Exactly once.** A reservation reaches exactly one terminal transition:
   settle from dispatch, or release from host reconciliation. Dispatch itself
   never releases. `RuntimeBudget` rejects a second settle/release with
   `BudgetStateError`; the Rust ledger also deduplicates by key.
4. **Conservative ceiling.** `policy.reserve` must return a ceiling at or above
   any actual cost. `policy.settle` may not exceed it. An overrun is a policy
   bug: the runtime fails closed and keeps the reservation open for host
   reconciliation instead of releasing funds that may already be spent.
5. **Unknown cost fails closed.** A policy that cannot price a call raises
   `BudgetUnknownCost` (or returns a non-integer/negative value). No handler is
   called and no ledger event is written.
6. **Idempotency and retries.** The ledger keys are stable:
   `{run_id}:{invocation_id}:reserve`, `:settle`, and `:release`. A retry may
   reuse a `PENDING` hold, but receives a fresh `RESERVED` object bound to the
   actual dispatch request; a second overlapping retry is refused. A terminal
   `SETTLED`/`RELEASED` delivery is refused before the handler runs, including
   when the bounded in-process terminal window has evicted it: `reserve()`
   consults the ledger's authoritative `lookup(reserve_key)`. A hold whose
   ledger account differs from `RuntimeBudget.account` is refused before
   admission; it is never charged to the wrong account. If a settlement
   already exists under the stable settle key, the ledger is authoritative: a
   different amount fails closed instead of silently repricing or
   double-charging. SWE does not introduce a general retry loop; a host that
   needs a new cost for a settled delivery must issue a new run/invocation (a
   new logical delivery).
7. **Cancellation, timeout, abandonment, and failed settlement.** `mark_pending`
   keeps the reservation on the ledger. Funds are not released early, because
   SWE cannot know whether the provider billed the cancelled call. A later
   outcome may settle it; otherwise the host calls `reconcile_invocation(...)`
   or `reconcile(..., release=True)`. When the runtime abandons a
   cancellation-resistant handler, it marks that invocation's hold pending
   before registering the orphan. A settlement failure (policy exception,
   invalid amount, overrun, or ledger error) also marks the hold pending; it is
   never left as an invisible `RESERVED` hold.
8. **Replay.** The host persists and replays the ledger event log alongside the
   `RunStore`. `WorkflowRunner.resume` calls `budget.restore(run_id=...)` for
   both terminal and running resumes, before any early return, dispatch, or
   cancel path. Open reservations become `PENDING` and are reconcilable;
   terminal reservations enter the bounded terminal window, so a re-dispatch of
   a settled-but-unpersisted delivery fails closed before the handler. Replayed
   invocations do not reserve or settle again.

## Lifecycle

| Event | Action |
| --- | --- |
| Handler/provider about to run | `reserve`; on failure, invoke nothing and fail the invocation |
| Handler/provider returns | `settle` with the policy's actual cost |
| Handler/provider raises | `settle` with the policy's failure cost (often 0) |
| Provider exact-cache hit | `reserve` still happens; policy may settle 0 using `cache_state="hit"` |
| `cancel_event` or action timeout cancels the call | `mark_pending`; no release |
| Runtime abandons a cancellation-resistant handler | `mark_pending_invocation`; the hold is reconcilable, the orphan may still return |
| `policy.settle` raises, returns an invalid amount, or exceeds the ceiling | `mark_pending`; the hold stays reserved and visible |
| Ledger `settle` fails | `mark_pending`; the hold stays reserved and visible. If the ledger already has a settlement for the key, it is authoritative and the in-process state becomes terminal |
| Late handler returns after timeout | The dispatch boundary settles the actual cost; the machine still fails/cancels the invocation |
| Host decides a pending charge | `reconcile_invocation(invocation_id, ...)`; `reconcile(...)` also works with the object |
| Resume of a persisted invocation | No budget call; the recorded outcome is replayed |
| Resume with the same budget | `restore(run_id=...)` reattaches open holds and registers terminal holds before dispatch |
| Crash between settle and persist | The terminal hold is restored; a re-dispatch fails closed before the handler |

## Failure mapping

`BudgetError` subclasses are `SweRuntimeError` values:

- `BudgetUnknownCost` – policy could not price the call;
- `BudgetExceeded` – ledger refused the reservation;
- `BudgetOverrun` – actual cost exceeds the reservation;
- `BudgetStateError` – duplicated or invalid state transition.

The kernel's frozen `ErrorKind` enum has no `budget` variant. A budget failure
therefore surfaces as the kernel `kind="handler"` with a message beginning
`budget ...`. The persisted `InvocationRecord.error` keeps the host kind
`"budget"`; hosts that need the distinction read the record, not the kernel
error.

A failed settlement is **not** left as an invisible `RESERVED` hold. The
reservation is marked `PENDING`, `pending()` includes it, and the host can
finish it with `reconcile_invocation(invocation_id, spent_milli=...)` or
`reconcile_invocation(invocation_id, release=True)`. Repeating reconciliation
is a no-op; a second `settle` is rejected. An in-flight `RESERVED` hold is
visible through `unresolved()` but the public reconciliation path refuses it
until it is marked pending or settles normally. A host that manually calls
`mark_pending`/`release` on an in-flight hold accepts responsibility for racing
the handler; the ledger still prevents a double charge.

Terminal reservations are kept only in a bounded recent window
(`terminal_capacity`, default 1024) and their `request.input` is dropped. The
window exists for idempotent `reservation_for`/`reconcile_invocation` checks;
the ledger remains the source of truth for older charges.

## Concurrency and capacity

For handlers the reservation is taken inside the runtime's capacity permit, so
at most `concurrency` handler reservations are live for one runner. Provider
reservations are taken before the exact-cache lookup; a cache miss still
acquires capacity inside the provider call, while a coalesced waiter may hold
its reservation until its own dispatch settles or the host reconciles it. The
cache owner's completion does not settle a waiter for it. This makes a cap
smaller than the total workload possible: completed calls settle and free their
unspent
remainder before the next queued call reserves. A cap that is smaller than the
number of simultaneously live reservations fails closed rather than
overspending.

A pending reservation after cancel/timeout continues to hold its amount. If
the cap is too small to admit more work while it is pending, the run waits or
fails closed; the host must reconcile. This is intentional and is the
mechanism that prevents reusing reserved funds early.

## Contract change

This is an additive **host API** change:

- `WorkflowResources` gains optional `budget`;
- `RunResult`, SWD 1.0, journal v1, events, and the Rust machine are unchanged;
- no version bump is required for the frozen wire contracts.

See `docs/swe-standalone/17-budget-runtime-decision.md` for the decision record.

## Non-goals

- Exactly-once external billing. The runtime can reserve and settle once, but
  it cannot prove that a provider stopped billing.
- General retries. Retry remains unsupported in SWE v1.
- Rate limiting. The budget is a spend cap, not a request-rate limiter.
- A second ledger. `swe-budget` is the only currency implementation.
- Semantic or token-based cost estimation. The host policy owns that.

## Verification

- `python/tests/test_swe_budget_binding.py` – ledger binding, replay,
  idempotency, overrun and state transitions.
- `python/tests/test_swe_budget_runtime.py` – 10,000 microtasks at concurrency
  64 with a budget cap, fail-closed cap exhaustion, cancellation and
  cancellation-resistant abandonment, failure settlement, provider exact-cache
  hits, host-retry terminal refusal, crash-before-persist restoration, open-hold
  restore, bounded terminal retention, and crash/resume replay.
- `cargo test -p swe-budget --locked` – ledger acceptance tests.
- `python -m pytest` – the complete host suite.

Measured on 2026-11-23 (macOS 15.7.4 arm64, Python 3.13.6, 10 CPU), 10,000
synthetic microtasks at concurrency 64:

```text
baseline: 1606.6 ms
budget:   1739.1 ms
overhead: 8.2%
```

Repeated runs measured 8–10% overhead. The budget run reserved and settled
10,000 times, peaked at 64 concurrent handlers, and emitted no
remainder-release events (each settlement spent its full one-unit
reservation). The overhead is diagnostic, not a hard CI gate; the test's
assertion is intentionally generous (`<= 5x + 1s`).
