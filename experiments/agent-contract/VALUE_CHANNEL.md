# Value-channel contract

Status: proposal. SWD 1.0 is unchanged. `python/swe/swd_v1.schema.json` does
not gain a field, and `compile_microtasks` still hands a handler only
`task_id`, `payload`, and `depends_on`.

`schedule`, `item_id`, and `item_depends_on` are the merged ready-map admission
fields on `origin/main` `f1ae2b8`. Admission is not a value channel. A
validator cannot read generation output through `item_depends_on`.

## Typed bind

The host already has the generation `InvocationRecord`. It builds one
`GenerationOutput` from that record and calls `bind_validator`:

```text
GenerationOutput(task_id="relevance.generate", kind="relevance", output={...})
ValidatorInput(task_id="relevance.validate", source_task_id="relevance.generate", generation=...)
```

`ValidatorInput.generation.output` is the typed payload the validator reads.
The bind is explicit. The map child does not perform it, and a missing or
mistyped output fails in the Pydantic model before a validator runs.

No provider is called. No `RuntimeBudget` tariff is implied by this proposal.
The production tariff stays with the microtask-swarm policy.
