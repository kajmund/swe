# Gold-free production early-stop comparison (offline)

Generated: 2026-11-23T23:48:21.051970+00:00
Fixture SHA-256: `be95745f8bed6981536c8c1592304be8afd02dfc4d78776eb4495f3138f0ad06`
Gold is used only for post-hoc scoring; the stop decision never reads it.
No model or network call was made.

| Scheduler | Condition | Stop reason | Wall ms | Calls | Completed items | Need cov. | Evidence cov. | Abstention | Avg conf. | Post-hoc acc. | Post-hoc cov. | Post-hoc false support | Pending milli | Late milli | Calc USD |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| D | full | None | 5763.467 | 50 | 10 | 1.0 | 1.0 | 0.0 | 0.956778 | 0.9 | 0.8 | 0.2 | 0 | 0 | 0.000399765 |
| D | count_25 | early_stop | 2599.297 | 32 | 0 | 0.5 | 0.9 | 0.0 | 0.921056 | None | None | None | 1250 | 1250 | N/A |
| D | coverage | production:coverage | 4350.18 | 49 | 5 | 0.88 | 1.0 | 0.0 | 0.950884 | 0.92 | 0.8 | 0.2 | 750 | 750 | N/A |
| D | confidence | production:confidence | 4350.388 | 49 | 5 | 0.88 | 1.0 | 0.0 | 0.950884 | 0.88 | 0.8 | 0.2 | 750 | 750 | N/A |
| D | hybrid | production:hybrid | 4354.237 | 49 | 6 | 0.9 | 1.0 | 0.0 | 0.951975 | 0.9 | 0.833333 | 0.166667 | 750 | 750 | N/A |
| D | strict | production:strict | 4416.012 | 49 | 6 | 0.9 | 1.0 | 0.0 | 0.951975 | 0.9 | 0.833333 | 0.166667 | 750 | 750 | N/A |
| P | full | None | 5200.193 | 50 | 10 | 1.0 | 1.0 | 0.0 | 0.956778 | 0.9 | 0.8 | 0.2 | 0 | 0 | 0.000399765 |
| P | count_25 | early_stop | 3839.853 | 27 | 1 | 0.5 | 1.0 | 0.0 | 0.913556 | 0.8 | 0.0 | 1.0 | 500 | 250 | N/A |
| P | coverage | production:coverage | 5056.459 | 47 | 5 | 0.9 | 1.0 | 0.0 | 0.951975 | 0.92 | 0.8 | 0.2 | 500 | 250 | N/A |
| P | confidence | production:confidence | 5057.6 | 47 | 5 | 0.9 | 1.0 | 0.0 | 0.951975 | 0.92 | 0.8 | 0.2 | 500 | 250 | N/A |
| P | hybrid | production:hybrid | 5138.555 | 48 | 6 | 0.92 | 1.0 | 0.0 | 0.953019 | 0.9 | 0.833333 | 0.166667 | 500 | 0 | N/A |
| P | strict | production:strict | 5080.555 | 48 | 6 | 0.92 | 1.0 | 0.0 | 0.953019 | 0.9 | 0.833333 | 0.166667 | 500 | 0 | N/A |
| P_stress | stress_full | None | 5174.341 | 50 | 10 | 0.96 | 0.8 | 0.04 | 0.916972 | 0.88 | 0.7 | 0.125 | 0 | 0 | 0.000399765 |
| P_stress | stress_count_25 | early_stop | 3839.36 | 27 | 1 | 0.46 | 0.8 | 0.08 | 0.849944 | 0.8 | 0.0 | 0.0 | 500 | 250 | N/A |
| P_stress | stress_coverage | production:coverage | 5066.776 | 47 | 5 | 0.86 | 0.8 | 0.044444 | 0.907747 | 0.88 | 0.6 | 0.0 | 500 | 250 | N/A |
| P_stress | stress_confidence | production:confidence | 5058.269 | 47 | 5 | 0.86 | 0.8 | 0.044444 | 0.907747 | 0.88 | 0.6 | 0.0 | 500 | 250 | N/A |
| P_stress | stress_hybrid | production:hybrid | 5075.525 | 48 | 6 | 0.88 | 0.8 | 0.043478 | 0.909752 | 0.866667 | 0.666667 | 0.0 | 500 | 0 | N/A |
| P_stress | stress_strict | production:strict | 5083.68 | 48 | 6 | 0.88 | 0.8 | 0.043478 | 0.909752 | 0.866667 | 0.666667 | 0.0 | 500 | 0 | N/A |

Totals: calls=812, settled=332000 milli, pending=9250 milli, late=5750 milli, released=3500 milli, calculated USD=0.00629952. actual_usd is N/A.

## False support (full runs)

| Scheduler | Condition | Items | Emitted | Matched | Unsupported | Unverified | False-support rate | Examples |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| D | full | 10 | 10 | 8 | 2 | 0 | 0.2 | bio-01:extraction, finance-01:extraction |
| P | full | 10 | 10 | 8 | 2 | 0 | 0.2 | bio-01:extraction, finance-01:extraction |
| P_stress | stress_full | 10 | 8 | 7 | 1 | 0 | 0.125 | bio-01:extraction |

## Verifier confidence calibration (full runs)

| Confidence bin | Count | Accuracy | False-support count | False-support rate |
| --- | ---: | ---: | ---: | ---: |
| [0.0, 0.5) | 2 | 0.0 | 0 | 0.0 |
| [0.5, 0.8) | 18 | 0.777778 | 3 | 0.166667 |
| [0.8, 0.95) | 11 | 0.818182 | 2 | 0.181818 |
| [0.95, 1.000001) | 119 | 0.932773 | 0 | 0.0 |

## Proposed verifier-based gate

`verifier_gate_v1` requires at least 6 complete items, need coverage ≥ 0.8, evidence coverage ≥ 0.8, abstention ≤ 0.05, confidence ≥ 0.8, and at most 0 invalid outputs. Hard rules: every emitted extraction quote must be an exact passage substring; at least one verified extract per complete item; no unverified extraction may remain in the completed set.

Runtime cannot see gold. A verified quote that is not the gold span is false support and is only measurable post-hoc.

Verifier confidence is a deterministic host heuristic, not a calibrated probability. Bins are descriptive.

Frozen async and adaptive baseline hashes are included in the JSON.
