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

`ValidatorInput.generation.output` is the payload the validator reads.
The Pydantic model checks the outer value: `output` must be a dict, so a list
is rejected. It does not apply the semantic rules in `join.validate`. A dict
such as `{"relevant": "yes"}` is accepted here, and `validate("relevance", ...)`
rejects it separately. `bind_validator` does not call `validate`. The identity
fields are frozen after construction.

The planned kinds are `relevance`, `extraction`, `entailment`, `atomicity`,
and `operation`. A generation is `{kind}.generate`. Its validator is
`{kind}.validate`, and `source_task_id` is that same generation id. A crossed
kind, an unplanned kind, or a direct `ValidatorInput` whose task, source, and
generation name different kinds fails closed. This check stays inside the
proposal. It does not add a runtime value channel.
`docs/swe-standalone/18-ready-map-decision.md` section 7 keeps the map value
channel absent.

No provider is called. No `RuntimeBudget` tariff is implied by this proposal.
The production tariff stays with the microtask-swarm policy.
