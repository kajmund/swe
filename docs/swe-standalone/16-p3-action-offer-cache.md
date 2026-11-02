# 16 — P3 ActionOffer 0.x and exact cache

**Status:** GO for a separate PR off `main`.
**Does not stack on P2.** ActionOffer and the L1 cache reuse `WorkflowRunner`,
`DecisionCache`, `RunStore`, and cancellation. They do not use the P2
microtask compiler.

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
| `decision_cache_key` v1 | **KEEP.** Not a silent v2. Hosts put auth/policy/model/provider version in `cache_params`. |
| `DecisionCache.resolve` | Unchanged signature |
| ActionOffer | New host contract `action_offer.v0`, outside frozen wire |

## Threat model

| Threat | Mitigation |
| --- | --- |
| Stale / expired / policy-changed offer | Revalidate on approve; fail closed before reserve or dispatch |
| Unauthorized caller | `auth_scope` must match exactly |
| Cross-scope cache reuse | v1 key includes `params`; `scoped_cache_params` is the supported escape hatch. Instance `namespace` isolates process-level tenants. A first-class v2 key is P4. |
| Double accept / double external effect | Offer ledger + deterministic `run_id` + `RunStore` replay + budget idempotency key |
| Cancelled waiter corrupts L1 | Shared flight is shielded; last waiter cancels compute; failures are not stored |

## Left to P4

- First-class v2 `decision_cache_key` fields (auth/policy/model/version)
- Durable L2 adapters
- Semantic cache
- Multi-action offers, dynamic graphs, and product-AINA surfaces
- Binding P2 microtask batches to an offer
