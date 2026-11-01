# SWE provider decisions and exact cache (phase 4)

**Status:** Provider decisions and the exact decision cache are implemented.  
**Not in this phase:** Jev cache for task handlers, `wait`/`subworkflow`/`retry`.

## Scope

## Research cutover note

The research wave loop now runs on SWE (`swe-runtime.md#research-wave-loop`).
That cutover does **not** route assessment/planning through `decision(provider)`:
the assessor, follow-up planner and completeness reviewer keep calling Jev
inside the research domain handlers. The provider decision node and its cache
remain available for later adapters; no LLM call moved into this phase.


`decision(evaluator=provider)` now executes. The Rust kernel parses the node,
emits an `invoke_provider` effect, and routes on the validated choice. The Python
adapter renders the decision prompt, calls a registered provider, and optionally
resolves the answer through an exact L1/L2 cache.

Definitions, registry checks, and prompt-catalog validation stay in
`app.swe.definition`. Runtime preflight stays in `app.swe.runtime_preflight`.
Dispatch lives in `app.swe.runtime_dispatch`. The cache contract and key live in
`app.swe.cache`; `app.services.overgraph.jev_cache` implements it, and the engine
never imports the OverGraph storage layer.

## Provider decision node

```json
{
  "type": "decision",
  "evaluator": "provider",
  "provider": "jev",
  "prompt_key": "research.assessment.user",
  "input": {"plan_json": "$.input.plan_json", "evidence_json": "$.input.evidence_json"},
  "choices": ["sufficient", "insufficient", "uncertain"],
  "routes": {"sufficient": "synthesize", "insufficient": "follow_up", "uncertain": "inspect"},
  "cache_policy": "exact"
}
```

* `provider` is validated against the provider registry at compile time when a
  whitelist is supplied, and against the runtime registry before a run.
* `prompt_key` must exist in `PROMPT_KEY_SET` (compile) and in the active prompt
  map passed to the run (runtime).
* `choices` are 2–16 unique strings. `routes` must cover every choice exactly
  once; a route that is not a declared choice is a preflight error in both
  Python and Rust.
* `cache_policy` is `off` (default) or `exact`. `exact` is only executable on
  provider decisions; task handlers still reject it.

## Execution (Rust)

Scheduling a provider decision emits `NodeScheduled` then `ProviderInvoked` and
the typed effect:

```text
invoke_provider {
  run_id, invocation_id, node_id, branch_path,
  provider, prompt_key, input, choices, cache_policy
}
```

The Python completion payload must be a JSON object with a string `choice` that
is one of the node's choices. Anything else fails the run with a typed
`type` error (`unknown provider choice ...`). An accepted completion:

1. stores the payload as the node result (`$.<node>`),
2. emits `ProviderCompleted`, `DecisionTaken`, and `NodeCompleted`,
3. routes to `routes[choice]`.

Cancellation and deadlines behave like task invocations: `CancelInvocation`
cancels the Python provider task, and a provider that outlives
`limits.timeout_seconds` fails the run with a `timeout` error.

## Python adapter

```python
resources = WorkflowResources(
    providers={"assessment": assessment_provider},
    prompts=active_prompts,      # require_active_prompts(...) map
    cache=JevDecisionCache(),    # required for any cache_policy: exact
)
result = await run_workflow(compiled, handlers, input_data, resources=resources)
```

Preflight fails loudly when a provider decision names an unregistered provider,
references a prompt key missing from `prompts`, or declares `cache_policy: exact`
without a cache.

The provider callable receives one mapping:

```text
{provider, prompt_key, prompt, input, choices, cache_policy}
```

`prompt` is `render_prompt(prompts, prompt_key, **input)`. A missing prompt key or
a missing placeholder fails the run; no default prompt text is substituted. The
callable returns a choice string or an object containing `choice` plus optional
metadata (for example `confidence`). The adapter validates the choice against the
declared choices before completing the invocation.

## Jev provider — removed (B1)

The previous `app.swe.providers.make_jev_provider` was a `decision(provider)`
adapter around JEV System One. It had **no production caller**: production
research decides through `app.services.research.fast_controller`, which calls
`classify_research_decision` from the compact research state directly. The
adapter was therefore only exercised by tests.

B1 moved it out of the engine and the project owner decided to delete it rather
than keep a generic SWE interface with no consumer. Rationale and evidence:
`docs/swe-standalone/02-inventory-python.md` E10 and
`docs/swe-standalone/08-verification-log.md` §7.5.

The **provider contract** it exercised is unchanged and still public: a node
declares `provider` and `prompt_key`, the host supplies a callable through
`WorkflowResources.providers`, the adapter renders the declared prompt, and the
returned `choice` is validated against the node's declared choices. A choice
outside them fails the run; there is no provider or model fallback. Re-adding a
JEV decision provider is a Host concern and belongs in
`app/services/research/`, not in the engine.

## Exact cache

`JevDecisionCache` in `app/services/overgraph/jev_cache.py`:

* **L1** — process FIFO, 256 entries, same contract as `EvidenceScreenCache`.
* **L2** — one OverGraph catalog directory at `OVERGRAPH_DIR/jev`
  (`wal_sync_mode="immediate"`, node label `JevDecisionCacheEntry`). The answer is
  stored as canonical JSON under the SHA-256 key.
* **Key** — SHA-256 over canonical JSON of provider, prompt key, rendered prompt
  text, bound input, declared choices, and provider cache params (the Jev adapter
  contributes `provider` and `model`). Changing the active prompt text therefore
  changes the key.
* **Single-flight** — concurrent identical keys share one provider call; the
  first caller computes and the rest await the same future.
* **Visibility** — the completion payload carries `cache: "hit" | "miss"` for
  `cache_policy: exact`; `off` reads and writes no cache.

The cache is process-owned like the other OverGraph catalogs. Pass the same
`JevDecisionCache` instance to runs that should share L1 and use one process per
catalog directory (single writer; no cross-process coordination is claimed).

## Guarantees and non-guarantees

Guaranteed:

* unknown provider choices fail the run with a typed error and never route,
* a prompt key or prompt placeholder missing at runtime fails loudly,
* `cache_policy: exact` always goes through one cache implementation,
* provider invocations produce the same event/lineage shape as task invocations.

Not claimed:

* provider answer quality (that is the provider's contract),
* a semantic cache (only exact canonical-input keys),
* cross-process cache writers or multi-process recovery,
* retry/failure fallback for providers (both remain rejected).

## Tests

* Rust: `crates/swe-native/src/machine/mod.rs` (parse, effect, routing, unknown
  choice, route/choice validation) and `crates/swe-native/tests/machine.rs`.
* Python: `backend/tests/test_swe_provider.py` (end-to-end fake provider, prompt
  rendering, unknown provider/prompt, out-of-contract choice, provider exception,
  deadline, exact cache hit/miss, cache preflight, key stability, single-flight,
  L2 across instances, Jev adapter mapping) and
  `backend/tests/test_swe_runtime_preflight.py`.
