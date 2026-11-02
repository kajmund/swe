# ActionOffer 0.x

**Status:** implemented as a Python host layer over existing SWE dispatch.

An `ActionOffer` is a typed, immutable proposal for one handler invocation.
Creating or surfacing an offer performs no I/O. Approve, reject, and revoke are
explicit host operations. A accepted offer is revalidated and then executed by
`WorkflowRunner`; there is no second scheduler and no product-AINA runtime.

## Inventory before design

| Concern | Existing mechanism | P3 decision |
| --- | --- | --- |
| Execution | `WorkflowRunner` / `runtime_dispatch` | Approve compiles a one-task SWD 1.0 document and reuses the runner |
| Journal / resume | `RunStore`, `InvocationRecord` | Deterministic `run_id = offer-{offer_id}` so a second accept replays |
| Cancellation | `WorkflowResources.cancel_event` | Passed through unchanged |
| Budget | Host `swe-budget` ledger semantics | Optional `OfferBudget` adapter; same idempotency key as the delivery key |
| Delivery | Rust `delivery_key(run, invocation, action)` | Offer-level `offer_delivery_key(offer_id, action_ref)` plus existing journal keys |
| Cache | `DecisionCache` | Separate P3 L1; offers do not invent a semantic cache |
| Frozen wire | SWD / journal / events / provider / Rust | Unchanged; ActionOffer is `action_offer.v0` outside those contracts |

No existing approval type, `ActionOffer`, or formal AINA contract lives in this
repository or its extracted history. The host pattern is
context/pattern → offer → explicit approve/reject → SWE execution.

## Identity

`offer_id` is SHA-256 of canonical JSON over:

- `schema_version` (`action_offer.v0`)
- `action_ref` and `handler`
- bound `input`, `input_bound`, and `input_fingerprint`
- `policy_version`, `context_version`, `auth_scope`

Summary, risk, expiry, revalidation, and provenance are metadata. Key order and
process identity do not change the id. Changing input, policy, auth scope, or
action does.

## Host operations

`propose_action` is a pure function. Empty `input={}` is bound and executable;
fingerprint-only offers stay unbound until input is supplied. Nested input is
deep-thawed by `to_dict()` so the payload is JSON. `ActionOfferHost.approve`
then:

1. rejects stale, expired, or unauthorized offers before any reservation or
   handler call;
2. takes a persistent `APPROVING` claim in `OfferStore.transition` so approve,
   reject, and revoke share one atomic decision; same-process waiters still
   join an in-host single-flight;
3. reuses a stored acceptance, a completed `RunStore` record, or a persisted
   handler effect after reconciling reserve+settle;
4. reserves an optional budget key;
5. runs or resumes the existing SWE machine with handlers wrapped by
   `OfferStore.run_effect(delivery_key, ...)` so a second host or a crash
   after the side effect does not invoke the external effect again;
6. settles on `completed` (idempotent), otherwise releases.

Reject and revoke use the same compare-and-set. A rejected or revoked offer
cannot enter `APPROVING`, so its handler never starts. An in-flight approve
holds `APPROVING` and makes reject/revoke fail closed with `conflict`.
Unstarted claims revert to `proposed` so another host can retry; a completed
effect or run is not reverted. Claim leases (`claim_lease_ms`) let a second
host finish a crashed approve without starting a second effect.

Double approve is idempotent: a completed run or stored effect is replayed
and the handler is not called again. A cancelled or failed run is not treated
as success; a later approve of that offer fails closed without a second
dispatch. Hosts that need that guarantee inject a shared `OfferStore` and
`RunStore`. A deterministic `run_id` is not enough by itself.

## Non-goals

ActionOffer is not a workflow format, broker, durable database, legal engine,
or AINA product surface. Research and Socialism stay out of this package.
Semantic cache and a first-class v2 decision cache key are left to later
stages. Cross-process exactly-once requires a store adapter whose
`transition` / `run_effect` exclusivity holds across hosts; the reference
`MemoryOfferStore` provides that for any hosts that share the instance.
