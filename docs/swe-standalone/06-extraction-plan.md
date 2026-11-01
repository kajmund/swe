# Fas F — Extraction plan: SWE as a standalone repository

**Status:** Plan for review. **No physical move is performed in this phase.**
Base: `origin/main` `666eccb0`. Companion documents: `01-inventory-rust.md`,
`02-inventory-python.md`, `04-public-contracts.md`, `05-gym-split.md`,
`07-go-no-go.md`.
**Constraint:** Socialism has no customers; prefer a clean separation over
temporary dual implementations. No fallback layers and no parallel engine.

## 0. Target end state

One new repository (working name **`swe`**) that contains the generic engine,
its host binding, the generic benchmark infrastructure and a domain-neutral
reference example. Socialism consumes it as a pinned dependency and keeps only
its application layer.

```text
swe/                                   # new repo, independent versioning
├── Cargo.toml                         # [workspace]
├── Cargo.lock                         # committed (tests/examples are binaries)
├── rust-toolchain.toml                # 1.83, same as today
├── crates/
│   ├── swe-core/                      # renamed swe-native + generic needs,
│   │                                  # evidence, operations and journal traits
│   │   ├── src/ (machine, effects, events, expression, preflight,
│   │   │         canonicalize, needs, evidence, operations, journal)
│   │   └── tests/
│   ├── swe-budget/                    # Sweycoin ledger + cost report
│   │                                  # (promoted from swe-gym/src/ledger.rs)
│   ├── swe-bindings/                  # PyO3 module `swe_native` (bindings only)
│   ├── swe-bench/                     # generic experiment runner, strategy arms,
│   │                                  # frozen replay, golden evaluation
│   └── swe-reference/                 # this PR's domain-neutral example
├── python/
│   └── swe/                           # Python host package (the generic part
│                                      # of backend/app/swe today)
├── docs/                              # SWD 1.0, runtime, provider, persistence,
│                                      # event schema, versioning
└── examples/                          # (optional) non-Rust samples
```

**Deliberate non-goal:** the domain experiment currently in `crates/swe-gym`
(legal/JEV/GLiNER2 fixtures) does not move here. Its generic core is handled
separately in `05-gym-split.md`.

### Why a Rust library *and* a Python host package in the same repo

A Rust library alone cannot drive `task` handlers in Python, and Socialism's
production integration is Python. The Python host adapter (`backend/app/swe/`)
is generic code today — it must not stay in Socialism, or Socialism would keep
owning SWE's public host contract. One repo, one version, two artifacts:

- Rust crates (`swe-core`, `swe-budget`, `swe-bench`) — library consumers.
- Python package `swe` (including the built PyO3 extension) — Python consumers
  such as Socialism.

Both are released from the same tag so the Rust/Python contract cannot skew
silently.

## 1. What moves, what stays

### 1.1 Moves to the SWE repository

| Source (Socialism today) | Destination | Notes |
| --- | --- | --- |
| `crates/swe-native/**` (src, tests, Cargo.toml, pyproject, README) | `crates/swe-core/` + `crates/swe-bindings/` | Kernel becomes `swe-core`; the PyO3 `#[pymodule] fn swe_native` and `pyproject.toml`/maturin config move to the bindings crate, keeping the module name `swe_native` (no import churn) |
| `backend/app/swe/*.py` + `swd_v1.schema.json` | `python/swe/` | 16 `.py` + the JSON schema move unchanged; 4 need a small dependency inversion (`definition`, `runtime_dispatch`, `schema`, `__init__`); `features.py` (host/feature/catalog model) and `providers.py` (JEV) move to the Socialism app (`02-inventory-python.md` §3, §14) |
| `backend/tests/test_swe_*.py` — 13 files, 111 test functions — + `backend/tests/swe/` helpers + fixtures | `python/tests/` + `crates/swe-core/tests/` | Test-move matrix in §2 item 9: `test_swe_canonicalize` and `test_swe_expression` can move immediately; 8 files move after PR 1's dependency inversions; `test_swe_features` becomes a Socialism host test; `test_swe_persistence` and `test_swe_provider` are split (generic replay/dispatch with a fake adapter vs OverGraph/JEV adapter tests that stay). The research-flavoured `research_parallel.*` fixture is replaced by a neutral provider fixture |
| `backend/scripts/benchmark_swe_runtime.py` | `python/scripts/` or `swe-bench` | Generic runtime benchmark (the two `benchmark_research_swe*.py` stay) |
| `docs/specs/swe-swd-1.0.md`, `swe-runtime.md`, `swe-provider.md` | `docs/` (sanitised) | Move the generic contract sections and split out the product sections: the research wave loop / cutover notes and JEV provider description stay in Socialism, as does the product feature enum in `swe-swd-1.0.md`. Do not move product-specific text verbatim (`06` §10.6) |
| Persistence contract parts of `docs/specs/swe-overgraph.md` + `app/swe/run_store.py` | `docs/persistence.md` + `swe-core::journal` | The OverGraph implementation and its layout stay in Socialism |
| `crates/swe-gym/src/ledger.rs` + `tests/ledger_tests.rs` | `crates/swe-budget/` | Pure generic ledger; tests move unchanged |
| Generic types in `crates/swe-gym/src/ops.rs` | `crates/swe-core/src/operations.rs` | `NeedRef`, `OperationSpec`, gate, `OpOutcome`; legal executors stay |
| `crates/swe-reference/` (this PR) | `crates/swe-reference/` | Proves a domain-neutral application layer can run on the kernel end-to-end. It defines its own local need/evidence/operation types; the promoted `swe-core` contracts are still PR 2 work and the demo must be re-pointed at them then |
| New: needs/evidence/operations contracts (§2 items 5, 8) | `crates/swe-core/` | Promoted from the experiment, not invented twice |
| New: Rust run-store/journal trait and replay semantics | `crates/swe-core/src/journal.rs` | Modelled on the existing Python `RunStore` protocol |

### 1.2 Stays in Socialism

| Area | Today | Why it stays |
| --- | --- | --- |
| Research orchestration and handlers | `backend/app/services/research/**` (incl. `swe_bridge.py`, `execution.py`, `loop_control.py`, assessment, completeness, follow-up, claims, worker) | Domain application layer |
| JEV provider and JEV system | `backend/app/jev/**`, `backend/app/swe/providers.py` (moved out of `app/swe/`) | Model/vendor integration owned by the application |
| Prompt catalog, prompt rendering and DB prompts | `backend/app/services/prompt_catalog.py`, `prompt_render.py`, configuration tables | Product configuration; injected into SWE |
| OverGraph run store and JEV cache adapters | `backend/app/services/overgraph/workflows.py`, `jev_cache.py`, `catalogs.py` | Storage backend and owner protocol |
| OverGraph knowledge model, document ingest, PDF/Word, TextUnits | `backend/app/services/overgraph/**`, knowledge/document modules | Socialism knowledge model |
| GLiNER2 and the frozen span contract | `backend/app/services/research/gliner/**` | Document-extraction domain |
| External legal sources (lagen.nu, Riksdagen) | research sources | Domain |
| Legal golden questions, contract fixtures, blindtest artifacts | `crates/swe-gym/tools/**`, `benchmarks/**`, `backend/tests/research_reuse/**` | Domain experiments and facit |
| Product API, frontend, user configuration, auth | `backend/app/api/**`, `frontend/**` | Product |
| `crates/swe-gym` experiment (domain part) | `crates/swe-gym/**` minus generic pieces | Experiment; see `05-gym-split.md` |

### 1.3 Docs that stay

`docs/specs/swe-cutover.md`, `swe-inventory.md` (research integration record),
`docs/SWE_Sweycoins_*.md` (experiment record), and all research/architecture docs
stay with the application. `docs/swe-standalone/*` in this PR is the decision
record for the split.

## 2. Dependencies to break first

These are preconditions, not optional cleanups. Each item is verified in
`01-inventory-rust.md` / `02-inventory-python.md`.

| # | Wrong-direction dependency | Evidence | Fix (in Socialism, before moving) | Test that proves the fix |
| --- | --- | --- | --- | --- |
| 1 | SWE compile → Socialism prompt catalog | `backend/app/swe/definition.py:13,159` (`PROMPT_KEY_SET`) | `compile_definition(..., prompt_keys=...)`; host passes the catalog's key set. Same pattern as the existing `providers` whitelist | `test_swe_definition`: compiling with a host-supplied key set; unknown key rejected; no import of `prompt_catalog` inside `app/swe` |
| 2 | SWE dispatch → Socialism prompt renderer | `backend/app/swe/runtime_dispatch.py:17` (`render_prompt`) | Move the generic 16-line default renderer into the SWE Python host package; dispatch accepts an injected renderer for hosts that need another policy. No prompt store import | `test_swe_provider`: rendering from the inherited prompt map; missing prompt fails preflight |
| 3 | SWE package contains a JEV provider | `backend/app/swe/providers.py:15-22` imports `settings`, `app.jev.system`, `research.fast_controller` | Move `make_jev_provider` to the application layer (e.g. `app/services/research/jev_provider.py`); SWE keeps only the provider registry/interface. The provider is not wired into production research today (only `test_swe_provider.py` uses it) | `test_swe_provider` moved/renamed for the JEV adapter; an import-direction test asserts `backend/app/swe` has no `app.jev`/`app.services` imports |
| 4 | Socialism product enum inside SWE | `backend/app/swe/schema.py:16` (`FEATURE_IDS`) + `features.py:28,35` (`workflow_catalog_dir` is at `:35`) | Host supplies a feature registry; `WorkflowHostContext`/`workflow_catalog_dir`/`FEATURE_IDS` move to the Socialism host; the engine accepts an opaque scope/catalog key or a store factory | `test_swe_features` becomes a host test; the core has no hardcoded product ids |
| 5 | Needs/evidence/operations only exist in the experiment | `crates/swe-gym/src/ops.rs`, `tools/eval_uncertainty_states.py` | Promote generic types + gate + outcome; keep domain executors. See `04-public-contracts.md` §2–§4 | New unit tests in core (ported from `ops.rs`) + the reference demo |
| 6 | Budget ledger lives in the experiment crate | `crates/swe-gym/src/ledger.rs` (re-exported only by `swe-gym`) | Move ledger + tests to `swe-budget`; `swe-gym` depends on it | `ledger_tests.rs` move unchanged; the reference demo uses it for real cost reporting |
| 7 | Resume contract is Python-only | `app/swe/run_store.py`, `runtime.py:294`, OverGraph spec | Add a Rust `RunStore`/journal trait with the same replay semantics (definition-hash guard, invocation replay, idempotent charge) | Reference demo pause/resume test; future `swe-core` trait test |
| 8 | Provider response has no typed usage/cost | `runtime_dispatch.py:_invoke_provider` accepts any object with `choice` | Add optional typed `usage`/`cost` metadata; host meter prices it; engine still only routes on `choice` | Provider-contract test with and without metadata |
| 9 | Generic executable tests are mixed with research tests | `backend/tests/test_swe_*.py` vs `test_research_swe/`, `research_reuse/` | 13 files / 111 `test_swe_*` functions (129 counted the 4+14 research-marked consumer tests, which stay). Move matrix: immediately = `test_swe_canonicalize` (9) and `test_swe_expression` (13); after PR 1 (prompt_keys, renderer, JEV out, neutral fixture) = `test_swe_definition`, `test_swe_joins` and the six `test_swe_runtime_*` files (8 files, ~60 tests); split = `test_swe_persistence` (generic replay with a fake RunStore moves, OverGraph-specific tests stay) and `test_swe_provider` (generic dispatch/cache vs JEV adapter); `test_swe_features` becomes a Socialism host test | CI job boundaries (§5) |

**Import-direction gate (new, cheap):** a test/CI step that fails if
`backend/app/swe/**` contains `from app.jev`, `from app.services.research`,
`from app.services.prompt_catalog`, `from app.services.prompt_render`,
`from app.config`, or imports `swe_gym`. This is the executable version of the
spec's "SWE Core ska aldrig vara beroende av ..." rule. The reference demo adds
the same gate for its own sources (no Python, research, OverGraph, GLiNER2,
legal types, product config).

## 3. Git history

**Recommendation: preserve history for the genuinely generic paths; do not
carry the experiment history into SWE.**

Facts: `crates/swe-native` has 4 commits, `backend/app/swe` 7, `crates/swe-gym`
55 (2026-10-26 PoC onward), most authored by one person. The monorepo has no
customers. Whether it is public could not be verified offline (a tool docstring
states it is); treat that as an assumption to confirm before publishing crates
or wheels.

Procedure (dry-run first, verify file lists and commit counts before pushing):

1. `git filter-repo --path crates/swe-native --path backend/app/swe --path backend/tests/swe --path-glob 'backend/tests/test_swe_*.py' --path backend/scripts/benchmark_swe_runtime.py --path docs/specs/swe-swd-1.0.md --path docs/specs/swe-runtime.md --path docs/specs/swe-provider.md --path crates/swe-reference` on a clone,
   with `--path-rename` to the target layout (`crates/swe-native:crates/swe-core`,
   `backend/app/swe:python/swe`).
2. Add `swe-budget`/`swe-bench`/generic-contract code as new commits whose
   messages cite the source commit SHAs from Socialism (`git show <sha>`) and
   the original files — provenance without importing domain fixture history.
3. Tag the new repo `v0.1.0` only after the move PR and the consuming PR both
   exist (never publish a tag that Socialism cannot consume).

Alternative considered and rejected: moving the whole `crates/swe-gym` history.
It would carry the legal experiment, redacted blindtest artifacts and domain
fixtures into a generic repository, violating the boundary the split is meant to
create.

## 4. Cargo workspace and manifests

- One workspace, `resolver = "2"`, members as in §0.
- Shared `[workspace.dependencies]` with **exact** versions (the repo already
  pins exact versions in `crates/swe-native/Cargo.toml`); `Cargo.lock` committed.
- `rust-toolchain.toml` pins `1.83.0` (current CI toolchain) until a deliberate
  upgrade.
- No `path` dependency across repository boundaries; Socialism uses a git tag.
- `swe-bindings` is the only crate with a `cdylib`; it has the `pyo3` optional
  dependency and the `extension-module` feature today.
- `publish = false` today; if crates.io publication is wanted later, that is a
  separate decision requiring a license (today `UNLICENSED`) and a
  `cargo publish` dry run. Until then, consume by git tag.
- The `swe-native` name: keep the package name `swe-core` for clarity and keep
  the PyO3 module name `swe_native` (avoids touching every Python import). A
  rename of the module can be a later, separate change.

## 5. Versioning and consumption

**Versioning**

- Semver `0.x`; the contract list in `04-public-contracts.md` is the stability
  surface. Breaking 0.x changes are allowed while there are no customers, but
  every release needs a changelog entry and a one-line migration note.
- Contract versions carried explicitly: SWD `1.0` (already), run-event schema
  `v1`, run-journal format `v1`, provider request/response `v1`.
- One release tag versions both artifacts: `v0.1.0` → crates at `0.1.0` and the
  Python package at `0.1.0`.
- Rust/Python skew is prevented by one tag and by Socialism's CI installing the
  extension from the pinned tag (source build or prebuilt wheel, whichever PR 4
  chooses).

**How Socialism consumes SWE**

```toml
# Cargo dependency (only if Socialism keeps Rust consumers; the Python path is
# the production integration today)
swe-core = { git = "https://github.com/kajmund/swe", tag = "v0.1.0" }
```

```toml
# backend/pyproject.toml (uv)
swe = { git = "https://github.com/kajmund/swe", tag = "v0.1.0", subdirectory = "python" }
```

- Exact tag/version pins only — no ranges, no branch tracking in `main`.
- **The distribution form must be chosen and tested in PR 4.** Two options:
  - *Source build:* uv builds `python/` from the tag; `python/pyproject.toml`
    uses maturin with `manifest-path = "../crates/swe-bindings/Cargo.toml"`.
    This requires a Rust 1.83 toolchain and the whole checkout at install time
    (Socialism's CI has Rust today). The uv `subdirectory` + out-of-tree
    maturin manifest path is **not verified offline** and must be proven in the
    move PR.
  - *Prebuilt wheel (recommended long-term):* SWE CI builds a wheel per release
    and publishes it as a release artefact or to a private index; Socialism
    installs the pinned wheel and needs no Rust toolchain.
  Do **not** claim "no Rust toolchain" until a wheel is actually published.
- No submodule and no vendored copy. The `backend/app/swe` and
  `crates/swe-native` directories are **deleted** in the consuming PR, so a
  local copy cannot silently shadow the dependency.

**How future SWE changes reach Socialism**

1. Change lands in `swe` with tests and a version bump.
2. Socialism opens a dependency-bump PR against the new tag.
3. If the change is breaking, the same PR adapts the application adapters
   (OverGraph run store, JEV provider, prompt injection) and the research
   bridge; the research regression suites stay the acceptance gate.
4. A scheduled (nightly, non-blocking) job in Socialism compiles and runs a
   smoke workflow against `swe` `main` to catch drift early. It never replaces
   the pinned-version gate.
5. Never copy a fix back into Socialism; no "SWE vendored" fallback path.

## 6. CI and regression tests

**New repository CI**

| Job | Content |
| --- | --- |
| `rust` | `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test --workspace` (includes `swe-reference`) |
| `python` | build `swe_native` with maturin, `pytest python/tests` (generic contract tests, offline) |
| `reference` | `cargo run -p swe-reference --offline` smoke; asserts the domain-neutral demo output |
| `gates` | import-direction gate (§2), no-domain-fixtures gate (forbidden path/content check), license/notice check |
| `release` | tag → wheel + crate artifacts, changelog check |

**What leaves Socialism's CI**

- `swe-runtime` job's `cargo test --manifest-path crates/swe-native` and the
  generic `tests/test_swe_*.py` run move to the new repo.
- `swe-gym` job stays until the gym split lands (`05-gym-split.md`).

**What stays / is added in Socialism's CI**

- Backend tests that exercise the research bridge and the adapters, against the
  pinned SWE version.
- **Fix an existing coverage gap while moving:** today
  `backend/tests/test_research_swe/test_wiring.py` and
  `backend/tests/test_sufficiency_experiment_reuse.py` are marked `swe_runtime`
  but are matched by neither `swe-runtime` (`tests/test_swe_*.py`) nor
  `backend` (`-m "not swe_runtime"`) — see `02-inventory-python.md` §10.4 and
  E11. After the split they must run in a dedicated Socialism job against the
  pinned SWE package, or coverage silently drops.
- The redundant `maturin develop` step in the `research-reuse` job
  (`02-inventory-python.md` §10.4) is removed when it no longer builds local SWE.
- A small dependency contract test: compile and run one workflow through the
  pinned package (catches a broken tag immediately).
- A gate that fails if generic engine code is reintroduced under `backend/app/`
  (for example `backend/app/swe` reappearing, or `swe_native` being built from a
  local path).

## 7. Providers, prompts and configuration

| Concern | After extraction |
| --- | --- |
| Prompt text | Still in the Socialism DB / prompt catalog, injected via the host `prompts` map; compile receives a host-supplied key whitelist. SWE contains no prompt keys. |
| Prompt rendering | The SWE host package ships the generic 16-line default renderer; dispatch also accepts an injected renderer policy. Prompt text and the prompt catalog stay in Socialism. |
| Provider registry | Built by the host. SWE ships the interface, dispatch, validation and exact cache identity; no JEV implementation. |
| Auth, model names, HTTP, timeouts | Host configuration (`app/config.py`) — not part of SWE. |
| Feature ids / customer ids | Host-supplied registry. The generic trusted-host-context type carries opaque ids; the OverGraph directory layout stays in the Socialism adapter. |
| Storage | OverGraph run store and JEV cache remain Socialism adapters implementing SWE traits/protocols; SWE defines the contracts and the replay semantics. |
| Observability | SWE emits typed events; Socialism's existing logging/observability attaches at the host. The event schema is public, the sink is injected. |
| Cost/tariff configuration | Host-supplied tariffs; `swe-budget` records amounts and idempotency keys, never a vendor price list. |

## 8. Ordered PR plan (after this investigation PR)

| PR | Repo | Content | Exit criterion |
| --- | --- | --- | --- |
| **0** | Socialism | This investigation: docs + `crates/swe-reference` + CI step for the demo | Reviewed and accepted; demo green; Go/No-Go decision recorded |
| **1** | Socialism | Dependency breaks §2 items 1–4, 8, 9 + import-direction gate | `grep` gate green; all `test_swe_*`/research tests green; no behaviour change in research |
| **2** | Socialism (or new repo, see note) | Promote needs/evidence/operations/ledger contracts into `swe-core`/`swe-budget` inside the monorepo; reference demo re-pointed at them | Core unit tests + demo green; no duplicated gate/ledger code |
| **3** | Socialism | Add Rust `RunStore`/journal trait; port the Python resume semantics; demo pause/resume uses the trait | Resume test green; event/journal schema documented |
| **4** | new `swe` | Extract via `filter-repo`, add workspace/bindings/python package/docs/CI, tag `v0.1.0` | New repo CI green; tag consumable |
| **5** | Socialism | Replace local SWE with the pinned dependency; delete `crates/swe-native` + `backend/app/swe`; keep adapters; delete the old CI jobs | Full backend + research regression green; no local SWE code remains; dependency contract test green |
| **6** | Socialism + `swe` | Gym split per `05-gym-split.md` (G1–G5: ledger → `swe-budget` first, then runner/replay/arm/ops; RL env stays) | Generic gym tests in SWE; legal fixtures absent from SWE; experiment still runs in Socialism; reference demo re-pointed from `swe_gym::ledger` to `swe-budget` |

**Project-owner decision (2026-10-31): the contract decision comes first.**

| Step | Before the repo move | Document |
| --- | --- | --- |
| P0.5 | Contract decision: Rust is normative semantics, versioned serialization contracts, journal = attempt / confirmed result / charge, framework boundary | [`09-contract-decision.md`](09-contract-decision.md) |
| P1 (B1) | Break the five Python domain imports | [`10-pr1-b1-python-boundary.md`](10-pr1-b1-python-boundary.md) |
| P2 (B2) | Promote needs/evidence/operations to `swe-core` (Rust normative) + `swe-budget`; Python projects | [`11-pr2-b2-core-contracts.md`](11-pr2-b2-core-contracts.md) |
| P3 (B3) | Journal/resume contract in `swe-core` incl. recovery strategies | [`12-pr3-b3-journal-resume.md`](12-pr3-b3-journal-resume.md) |
| P4 (B4) | Own CI harness and consumer tests | this document §6 |

Each of P1–P3 must be mergeable without changing Research or existing SWE
workflow behaviour. The gym split (`05-gym-split.md`) and the licence decision
run in parallel and must not delay P1–P3.

Note on PR 2/3: they can be done directly in the new repo only if PR 4 happens
first. The recommended order keeps the monorepo green at every step and avoids a
long-lived duplicate: break dependencies in Socialism, then move. If the project
prefers to create the new repo earlier, PRs 2–3 become the first commits in
`swe` and PR 5 consumes that tag; the monorepo never contains two working
engines either way.

**Explicitly not in the plan:** a temporary compatibility shim, a dual runner, a
vendored copy of `swe-native`, or a second gym implementation.

## 9. Risks and mitigations

| Risk | Severity | Mitigation |
| --- | --- | --- |
| Hidden domain coupling in "generic" code | High | §2 import gate; inventory references; move in small PRs; reference demo compiles without any domain import |
| `swe-gym` generic/domain entanglement makes the bench split expensive | Medium | Do not force a full generic core now; move only ledger + metering + the minimal runner interface; keep the rest in Socialism (`05-gym-split.md`) |
| Rust/Python contract drift | Medium | One tag, one CI build, exact pins, nightly non-blocking drift job |
| PyO3 packaging breaks the Socialism deploy path | Medium | Build the wheel in the SWE CI from the tag; keep a documented source-build fallback in the move PR only as a build instruction, not a code path |
| Git history filter mistakes / lost provenance | Medium | Dry-run on a clone, compare moved-file hashes against `origin/main`, record source SHAs in commit messages; never rewrite the Socialism repo |
| CI cost and duplicate test runs during transition | Low | Remove the monorepo SWE jobs in the consuming PR, not before |
| Regression coverage silently drops in the move | Medium | Two `swe_runtime`-marked consumer tests already bypass CI today (`02-inventory-python.md` §10.4); add them to a dedicated Socialism job in the consuming PR and assert the collected count is non-zero |
| License (`UNLICENSED`, `publish = false`) blocks crate/wheel distribution | Medium | Decide the license for the new repo in PR 4; git-tag consumption works without crates.io publication |
| Research behaviour regresses during the cutover | High | The existing research regression suites stay in Socialism and are the gate for PR 5; no behaviour changes are allowed in the move |
| "No parallel implementations" violated by the example crate | Medium | The reference demo only consumes public contracts; it must not reimplement engine primitives (`crates/swe-reference/README.md` states this) |

## 10. Acceptance criteria for the extraction itself

1. New `swe` repo builds and tests offline-clean (`cargo test --workspace`,
   `pytest`, reference example) with no Socialism checkout.
2. `backend/app/swe`, `crates/swe-native` and their generic tests are deleted
   from Socialism; only adapters and application code remain.
3. The import-direction gate passes in both repos.
4. The reference example compiles against the published contracts and is part of
   the new repo's CI.
5. Socialism's research suites and full backend CI pass against the pinned tag.
6. No legal fixtures, blindtest artifacts, GLiNER2 code, OverGraph schema,
   prompts or product configuration exist in the SWE repo.
7. Version `v0.1.0` is tagged and consumable by a clean Socialism checkout
   without editing paths.
8. Rollback is a revert of the consuming PR plus re-pinning the previous tag —
   no data migration is involved.
