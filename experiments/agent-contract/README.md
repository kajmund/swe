# Agent contract join

Owner: t1bm (`experiment/agent-microtask-contract`).

t1bp owns `experiment/microtask-swarm` and does not edit this directory.
The join is offline. It selects the generation provider from configuration,
runs microtasks with `asyncio.gather` per dependency layer, and checks
relevance, extraction, entailment, atomicity, and operation in explicit
functions. Those functions do not retry a call.

`swe.budget.RuntimeBudget` from PR #11 is not imported successfully on this
branch because that PR is still open. A run without a caller-supplied ledger
stops before any provider call. The supplied object must implement `reserve`,
`settle`, and `release`. It is not a second production ledger.
