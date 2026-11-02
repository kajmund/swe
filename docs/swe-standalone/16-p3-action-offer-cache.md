# 16 — P3 ActionOffer 0.x and exact cache

**Status:** GO for a separate PR. Branched from pre-P2 `main`; later merged
`main` after P2 (#3) landed. ActionOffer and the L1 cache still reuse
`WorkflowRunner`, `DecisionCache`, `RunStore`, and cancellation. They do not
use the P2 microtask compiler. Both layers share the existing runner, store,
budget, and dispatch; neither duplicates them.

## Inventory

- No `ActionOffer`, approval contract, or formal AINA definition exists in
  `kajmund/swe` or the extracted history. The discussed pattern is used:
  context/pattern → typed offer → explicit approve/reject → SWE execution.
- `DecisionCache` and `decision_cache_key` are already public. There was no
  in-tree L1; the historical Jev FIFO/OverGraph store stayed in Socialism.
- `cache_policy: exact` is executable only on provider decisions. `off` never
  touches the cache. Single-flight is part of the protocol contract.
- Frozen SWD, journal, event, provider request, and Rust wire fixtures are
  unchanged. ActionOffer lives in Python as `action_offer.v0`.

## Contract / version assessment

| Surface | Decision |
| --- | --- |
| SWD 1.0 / journal v1 / events / Rust wire | No change |
| `decision_cache_key` v1 | **KEEP.** Not a silent v2. Isolation fields stay in `cache_params` and are now fail-closed. |
| `DecisionCache.resolve` | Unchanged signature |
| ActionOffer | New host contract `action_offer.v0`, outside frozen wire |

## Threat model

| Threat | Mitigation |
| --- | --- |
| Stale / expired / policy-changed offer | Revalidate on approve; fail closed before reserve or dispatch |
| Unauthorized caller | `auth_scope` must match exactly |
| Cross-scope cache reuse | v1 key includes `params`; `scoped_cache_params` and exact dispatch require `auth_scope`, `policy_version`, and `model`. Missing fields fail closed. Instance `namespace` is extra isolation, not a substitute. |
| Concurrent approve/reject/revoke | Persistent `OfferStore.transition` CAS. Reject/revoke cannot start a claimed offer; a rejected/revoked offer cannot become `APPROVING`. |
| Double accept / double external effect | Persistent `APPROVING` claim + `run_effect(delivery_key)` + `RunStore` replay + budget key. In-process single-flight is only a same-host fast path. |
| Lost settle after completed run | Recovery path re-reserves and settles before remembering approval |
| Cancelled waiter corrupts L1 | Shared flight is shielded; last waiter cancels compute; failures are not stored |
| Wrong const reused via definition cache | Cache identity is handler plus canonical thawed bindings, including const values |
| Nested frozen const cannot compile | `_task_bindings` deep-thaws input before embedding SWD constants |

## Review response (P1/P2)

Approve, reject, and revoke now share `OfferStore.transition`. The winner of
`None|proposed → approving|rejected|revoked` is the only caller that may start
a handler. Two hosts that share the store see the same order. Handler
invocation goes through `run_effect(delivery_key)`, so a crash after the side
effect and a retry on another host replay the stored result. A deterministic
`run_id` is no longer the only idempotency story.

Exact cache isolation is enforced in `scoped_cache_params` and
`cache_policy: exact` dispatch. Missing `auth_scope`, `policy_version`, or
`model` fail closed. `decision_cache_key` v1 bytes are unchanged.

Compiled offer definitions are cached by handler plus the SHA-256 of the
thawed task bindings. SWD-valid identifier keys stay path bindings (`$.name`)
and can still share a compiled document. Other keys such as `document-id` are
embedded as thawed `const` values under a deterministic SWD-safe alias and
restored to the original name before the handler runs, so two offers with the
same keys and different values compile separately.

Regression tests cover concurrent approve/reject/revoke, two-host approve,
reject-then-approve, crash/retry across hosts, cross-tenant/policy/model
cache keys, exact dispatch without isolation, distinct const values on a
shared host, and nested object/list consts. Do not use this for production
side effects until a durable store adapter implements the same exclusivity.

## Left to P4

- First-class v2 `decision_cache_key` fields (auth/policy/model/version)
- Durable L2 adapters
- Semantic cache
- Multi-action offers, dynamic graphs, and product-AINA surfaces
- Binding P2 microtask batches to an offer
