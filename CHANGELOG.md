# Changelog

All notable changes to this project are documented here.

## Unreleased

- Add an opt-in first-class runtime budget. `WorkflowResources.budget` accepts
  `swe.budget.RuntimeBudget`, which composes a host policy with the existing
  Rust `swe-budget` ledger exposed as `swe_native.Ledger`. Dispatch reserves
  atomically before the handler/provider call, settles or releases exactly
  once, and keeps cancel/timeout reservations pending so already-billed funds
  are not reused early. Unknown cost fails closed; no general retry is added.
  SWD 1.0, journal v1, `RunResult`, and the Rust machine are unchanged.
- Give cancelled siblings a cancel grace before they count as abandoned, and
  signal `InvocationHandle` from `reap_owned` when `run` / `resume` itself is
  cancelled so hosts still see `handle.cancel`.
- Add `InvocationHandle` and `current_invocation()` around every handler and
  provider call. Hosts keep `async def handler(payload)` and read the
  ContextVar. SWE sets `handle.cancel` and `reason` (`timeout` vs `cancel`)
  before `task.cancel()`. Unfinished work is detached into a bounded
  abandoned-work registry so `reap_owned` cannot hang; exceeding the cap
  fails closed on new dispatch. The registry is keyed by task identity so
  resume of the same invocation id cannot overwrite an earlier attempt.
  Orphaned exact-cache provider computes count toward the same cap of the
  runner whose capacity owns the compute, including when caches are shared.
  The cache notifies that owner immediately when the last waiter leaves, so
  late exceptions are consumed without charging a coalesced runner; the last
  waiter still drops the L1 flight. Compute cancellation precedes that
  notification, whose failure is contained so it cannot replace the waiter's
  cancellation. SWE does not claim the provider received cancel, stopped
  billing, or that the OS socket closed.
- Add host-owned per-action timeouts on `WorkflowResources.action_timeouts`,
  keyed by SWD node id. SWD 1.0 is unchanged, retry stays unsupported, and
  action expiry does not call `Machine.tick`. Once the per-action deadline
  has passed, the invocation is terminal `kind=timeout` even if the handler
  swallows `CancelledError` or later returns a value; the late result is
  discarded. Expiry uses handler completion time, not delayed settlement.
  After cancel, SWE waits a short grace period and then detaches a task
  that has not finished into a runner-owned abandoned-work registry (cap =
  `concurrency`), including when `cancel_event` fires during grace. New
  dispatch fails closed at that cap. Blocking I/O (`time.sleep`, blocking
  sockets, threads that ignore cancel) is not force-stopped and may continue
  after SWE has settled the timeout.
- Accept structured invocation errors (`kind`, `message`, optional `path` and
  opaque `payload`) on `InvocationRecord.error` and `Machine.fail`, while still
  replaying legacy string errors as `handler`. No journal schema change.
  Live and replayed failures share one normalization; empty messages and
  non-JSON payloads fail cleanly. `RunStore` must persist the error as JSON.
- Extract the generic SWE kernel, Python host, budget ledger, reference app,
  frozen contracts, and verification gates into an independent repository.
- Add a static microtask DAG compiler that lowers batches to existing SWD 1.0
  map stages, with bounded concurrency, cancellation, replay coverage, and a
  10,000-operation throughput quality gate. No frozen wire contract changes.
- Add ActionOffer 0.x and a bounded exact `DecisionCache` L1, with approve
  going through existing SWE dispatch, budget, cancellation, and journal
  recovery. Frozen wire contracts and `decision_cache_key` are unchanged.
- Serialize ActionOffer approve/reject/revoke through a persistent store
  claim, persist handler effects for cross-host retry, and fail closed when
  exact-cache isolation (`auth_scope`, `policy_version`, `model`) is missing.
- Make ActionOffer claim takeover a lease-token CAS, refuse const-alias
  collisions, skip freshness only when completed or replayable work exists,
  and keep both the definition cache and L1 decision cache on true LRU eviction.
- Reconcile expired ActionOffers when a handler effect is already persisted,
  so crash recovery can settle without a second side effect.
