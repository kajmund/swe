# Changelog

All notable changes to this project are documented here.

## Unreleased

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
