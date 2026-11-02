# Exact decision-cache L1

**Status:** implemented as the reference `InMemoryDecisionCache`.

This is an identity cache for validated provider successes. It is not a
semantic cache. Durable L2 stays a host adapter; SWE does not import Redis,
OverGraph, or SQL.

## Frozen v1 key

`swe.cache.decision_cache_key` remains the public identity:

`provider`, `prompt_key`, rendered `prompt`, bound `input`, `choices`, and
`params`.

It does **not** grow first-class `auth_scope`, `policy_version`, `model`, or
`provider_version` fields. Those dimensions affect results, so a host that
needs isolation must place them in `cache_params` via `scoped_cache_params`
or use a dedicated cache instance (`namespace`). Changing the frozen function
would be a v2 version decision; P3 does not do that silently.

`cache_policy: off` never calls `resolve`, so a present cache records zero
reads and writes.

## Reference L1

`InMemoryDecisionCache` provides:

- bounded entries and bytes, FIFO-by-`(inserted_at, key)` eviction;
- optional TTL with deterministic expiry (`expires_at`, then key);
- single-flight per exact key;
- cache of validated successful objects only (`choice` is a non-empty string);
- no store on exception, timeout, cancellation, or invalid answer;
- waiter cancellation that does not corrupt an entry or leave a stuck flight
  (the last waiter cancels the compute task);
- metrics: hit, miss, coalesced/wait, compute, error, eviction, expiry,
  latency, peak in-flight, bytes.

The `DecisionCache` protocol is unchanged. Hosts may still wrap this L1 with a
durable store.

## Quality gate

The synthetic gate performs 10,000 exact resolves cold and 10,000 warm hits at
concurrency 64, with no model or network calls. It asserts unique compute on
cold unique keys, warm hits, peak in-flight ≤ 64, and a memory bound.
The committed baseline is
[`benchmarks/offers-cache/independent-10000.json`](../../benchmarks/offers-cache/independent-10000.json):
10,000 cold unique resolves plus 10,000 warm hits at concurrency 64, peak
in-flight 64, peak entries 256, about 22,900 cache ops/s and 3,800 offer
ops/s on the recorded Linux host. Throughput is diagnostic; correctness,
idempotent reuse, bounded concurrency, and the memory cap are the hard gates.
