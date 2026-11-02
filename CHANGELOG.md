# Changelog

All notable changes to this project are documented here.

## Unreleased

- Extract the generic SWE kernel, Python host, budget ledger, reference app,
  frozen contracts, and verification gates into an independent repository.
- Add a static microtask DAG compiler that lowers batches to existing SWD 1.0
  map stages, with bounded concurrency, cancellation, replay coverage, and a
  10,000-operation throughput quality gate. No frozen wire contract changes.
