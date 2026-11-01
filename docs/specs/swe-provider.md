# Provider decisions and exact caching

**Status:** implemented in the Rust kernel and Python host adapter.

A provider decision delegates a bounded choice to a host-supplied callable:

```json
{
  "type": "decision",
  "evaluator": "provider",
  "provider": "mock.decision",
  "prompt_key": "demo.assessment",
  "input": {"catalog": "$.combine.catalog"},
  "choices": ["sufficient", "insufficient", "uncertain"],
  "routes": {
    "sufficient": "summarize",
    "insufficient": "refine",
    "uncertain": "inspect"
  },
  "cache_policy": "exact"
}
```

## Validation

- `provider` must be in the compile-time and runtime provider registries;
- `prompt_key` must be in the compile-time allowlist and runtime prompt map;
- `choices` contains 2–16 unique strings and `routes` covers them exactly;
- `cache_policy` is `off` (default) or `exact`;
- `exact` requires an injected `DecisionCache` implementation.

No provider, prompt, or model fallback exists. Missing registrations and choices
outside the declared set fail the run.

## Dispatch

The kernel emits `InvokeProvider` with run/invocation/node identity, branch path,
provider, prompt key, bound input, choices, and cache policy. The Python host
renders the selected prompt, calls the registered provider, validates its
response, and completes the invocation. Accepted payloads contain a `choice`
and may contain additional metadata.

Provider invocations use the same deadline, cancellation, event, and journal
semantics as tasks.

## Exact cache

`swe.cache.decision_cache_key` hashes canonical JSON containing the provider,
prompt key, rendered prompt, bound input, choices, and provider cache parameters.
Any input or prompt change therefore changes the key. `DecisionCache` is an
async host protocol; implementations may provide process or durable storage and
single-flight behavior. `cache_policy: off` reads and writes no cache.

This is an identity cache, not a semantic cache. The engine does not claim
provider quality, cross-process coordination, or retry/fallback policy.
