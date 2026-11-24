# swe-budget-py

PyO3 bindings for the workspace `swe-budget` ledger, exposed as the Python
module `swe_budget`.

This crate is a Python-extension shell only. The ledger, its invariants and its
event log stay in `crates/swe-budget`; there is no second implementation. It is
`publish = false` because the crate is shipped as the `swe-budget-native` wheel,
while `swe-budget` itself remains the published Rust library.

```sh
maturin build --manifest-path crates/swe-budget-py/Cargo.toml \
  --interpreter python/.venv/bin/python --out python/dist
```
