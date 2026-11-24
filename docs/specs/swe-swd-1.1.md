# SWE Workflow Definition (SWD) 1.1

**Status:** versioned extension of frozen SWD 1.0. SWD 1.0 stays frozen.

SWD 1.1 adds an opt-in static ready-map to the existing `map` node. It does
not add a node type, a scheduler, a journal event, or a budget ledger. The
normative schema is
[`python/swe/swd_v1_1.schema.json`](../../python/swe/swd_v1_1.schema.json).
A frozen document is
[`contracts/swd-v1_1-ready-map.json`](../../contracts/swd-v1_1-ready-map.json).
The decision is
[`docs/swe-standalone/18-ready-map-decision.md`](../swe-standalone/18-ready-map-decision.md).

## Version

- `version` is `"1.1"` (`SWD_VERSION_1_1`).
- SWD 1.0 documents continue to use `SWD_VERSION = "1.0"` and
  `python/swe/swd_v1.schema.json`.
- Unknown versions are a typed error. A 1.0 document that carries `schedule`,
  `item_id`, or `item_depends_on` is rejected.

## Ready map

`schedule` is `"ordered"` (default; today's map) or `"ready"`.

`item_id` and `item_depends_on` are required when `schedule` is `"ready"` and
forbidden otherwise. Both are JSON paths evaluated with the current element
bound as `item`.

At `start_map` the item array must be objects. Each item id matches
`[A-Za-z][A-Za-z0-9_.-]{0,127}`, is unique in the map, and the dependency
array names known ids. Self-dependencies and cycles fail closed before any
child invocation or child journal event.

Items with no unmet dependencies are admitted, up to `map.concurrency`, in
input-index order and then item id. Completing an item admits newly ready
dependents. Output order stays the input order. `join` and `next` are
unchanged.

`branch_path` for a ready child is `<index>/<item_id>` appended to the parent
path. The invocation id keeps
`<run>:<node>:<branch_path>:<loop_epoch>:<serial>`. Serials for the map's
items are reserved in input order when the map starts, so identity does not
follow later completion order. Resume replays those invocation records; the
host does not pre-seed dependency events.

`item_depends_on` admits work only. It does not copy a dependency output into
the handler input.

Cancellation and a child failure stop further admission. Peak in-flight
children stay at or below `map.concurrency`. Each child is an ordinary
invocation for the existing budget path.

Dynamic admission of new items is not part of SWD 1.1.
