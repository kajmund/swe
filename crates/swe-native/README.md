# swe-native

Deterministic in-memory execution kernel for SWD 1.0 workflows.

- `cargo test` — canonicalize, expressions, fork/join/map machine
- `maturin develop --manifest-path crates/swe-native/Cargo.toml`
  installs the `swe_native` Python module in the active environment
- the `python` feature also exposes the workspace `swe-budget` ledger as
  `swe_native.Ledger`; the kernel itself does not call it

Python owns async handlers. This crate owns routing, barriers, and events.
It has no storage, provider, network, or product dependency. Those capabilities
are supplied by the host.
