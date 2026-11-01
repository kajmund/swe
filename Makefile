.PHONY: test rust python reference contract-gate mutation-gate license-gate package

test: rust python reference contract-gate license-gate

rust:
	cargo fmt --all -- --check
	cargo clippy --workspace --all-targets -- -D warnings
	cargo test --workspace

python:
	cd python && uv sync --locked --group dev
	uvx maturin build --manifest-path crates/swe-native/Cargo.toml --interpreter python/.venv/bin/python --out python/dist
	cd python && uv pip install --reinstall $$(find dist -name '*.whl' -type f | head -1)
	cd python && uv run --no-sync pytest

reference:
	cargo run --locked -p swe-reference

contract-gate:
	python3 scripts/test_check_contract_freeze.py

mutation-gate:
	cargo fetch --locked
	python3 scripts/test_check_mutations.py --offline

license-gate:
	python3 scripts/check_licenses.py

package:
	cargo package --locked -p swe-budget --allow-dirty
	cargo package --locked -p swe-native --allow-dirty
	cd python && uv build
	cd crates/swe-native && uv build
