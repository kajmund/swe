# Runtime budget

**Status:** implemented as an opt-in host feature. The frozen SWD 1.0 and
journal v1 contracts are unchanged.

## What this is

`WorkflowResources.budget` attaches a `swe.budget.RuntimeBudget` to a run.
When present, every handler and provider invocation is priced by a host
policy, reserved atomically before the call, and settled or released exactly
once. The ledger is the existing Rust `swe-budget` crate, exposed to Python
as `swe_native.Ledger`; SWE does not implement a second ledger.

When the resource is absent, runtime behaviour is exactly the previous
behaviour: no reservation, no settlement, no new event.

## Ownership

| Concern | Owner | Contract |
| --- | --- | --- |
| Ledger invariants: reserve/settle/release/replay, idempotency, no negative balance | Rust `swe-budget` | `crates/swe-budget/src/lib.rs` |
| PyO3 surface: `swe_native.Ledger` | Rust `swe-native` (`python` feature) | `crates/swe-native/src/python.rs` |
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
    balance(account) -> {allocated, incoming, outgoing, spent, reserved, available}
    events() -> [...]
```

`RuntimeBudget` composes one ledger, one policy, and one account. The host
constructs it; the runtime only calls `reserve`, `settle`, `release`, and
`mark_pending`. On a late return after cancel or timeout, `BudgetOutcome`
carries `cancelled`/`timed_out` from the invocation handle; `ok` still reflects
whether the call itself returned.

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
   settle or release. `RuntimeBudget` rejects a second settle/release with
   `BudgetStateError`; the Rust ledger also deduplicates by key.
4. **Conservative ceiling.** `policy.reserve` must return a ceiling at or above
   any actual cost. `policy.settle` may not exceed it. An overrun is a policy
   bug: the runtime fails closed and keeps the reservation open for host
   reconciliation instead of releasing funds that may already be spent.
5. **Unknown cost fails closed.** A policy that cannot price a call raises
   `BudgetUnknownCost` (or returns a non-integer/negative value). No handler is
   called and no ledger event is written.
6. **Idempotency.** The ledger keys are stable:
   `{run_id}:{invocation_id}:reserve`, `:settle`, and `:release`. A retry of the
   same logical invocation reuses the same reservation and cannot double-charge.
   SWE does not introduce a general retry loop.
7. **Cancellation and timeout.** `mark_pending` keeps the reservation on the
   ledger. Funds are not released early, because SWE cannot know whether the
   provider billed the cancelled call. A later outcome may settle it; otherwise
   the host calls `reconcile(..., spent_milli=...)` or
   `reconcile(..., release=True)`.
8. **Replay.** The host persists and replays the ledger event log alongside the
   `RunStore`. Replayed invocations do not reserve or settle again. Open
   reservations from a crash are reused by the same stable key when the
   invocation is re-dispatched.

## Lifecycle

| Event | Action |
| --- | --- |
| Handler/provider about to run | `reserve`; on failure, invoke nothing and fail the invocation |
| Handler/provider returns | `settle` with the policy's actual cost |
| Handler/provider raises | `settle` with the policy's failure cost (often 0) |
| Provider exact-cache hit | `reserve` still happens; policy may settle 0 using `cache_state="hit"` |
| `cancel_event` or action timeout cancels the call | `mark_pending`; no release |
| Late handler returns after timeout | The dispatch boundary settles the actual cost; the machine still fails/cancels the invocation |
| Host decides a pending charge | `reconcile(spent_milli=...)` or `reconcile(release=True)` |
| Resume of a persisted invocation | No budget call; the recorded outcome is replayed |
| Crash between reserve and persist | The replayed ledger keeps the reservation; the same key is reused |

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

## Concurrency and capacity

For handlers the reservation is taken inside the runtime's capacity permit, so
at most `concurrency` handler reservations are live for one runner. Provider
reservations are taken before the exact-cache lookup; a cache miss still
acquires capacity inside the provider call, while a coalesced waiter may hold
its reservation until the cache owner completes. This makes a cap smaller than
the total workload possible: completed calls settle and free their unspent
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
  64 with a budget cap, fail-closed cap exhaustion, cancellation with a pending
  reservation, failure settlement, provider exact-cache hits, and crash/resume
  replay.
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
