# swe-native

In-memory SWD 1.0 execution kernel for Socialism Workflow Engine.

- `cargo test` — canonicalize, expressions, fork/join/map machine
- `maturin develop --manifest-path crates/swe-native/Cargo.toml` from `backend/`
  installs the `swe_native` Python module

Python owns async handlers. This crate owns routing, barriers, and events.
It does not talk to OverGraph, Jev, or research.
