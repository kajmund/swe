# Gold-free production early-stop comparison (offline)

Generated: 2026-11-23T23:38:18.458198+00:00
Fixture SHA-256: `be95745f8bed6981536c8c1592304be8afd02dfc4d78776eb4495f3138f0ad06`
Gold is used only for post-hoc scoring; the stop decision never reads it.
No model or network call was made.

| Scheduler | Condition | Stop reason | Wall ms | Calls | Completed items | Need cov. | Evidence cov. | Abstention | Avg conf. | Post-hoc acc. | Post-hoc cov. | Post-hoc false support | Pending milli | Late milli | Calc USD |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| D | full | None | 5766.682 | 50 | 10 | 1.0 | 1.0 | 0.0 | 0.956778 | 0.9 | 0.8 | 0.2 | 0 | 0 | 0.000399765 |
| D | count_25 | early_stop | 2595.533 | 32 | 0 | 0.5 | 0.9 | 0.0 | 0.921056 | None | None | None | 1250 | 1250 | N/A |
| D | coverage | production:coverage | 4350.414 | 49 | 5 | 0.88 | 1.0 | 0.0 | 0.950884 | 0.88 | 0.8 | 0.2 | 750 | 750 | N/A |
| D | confidence | production:confidence | 4345.739 | 49 | 5 | 0.88 | 1.0 | 0.0 | 0.950884 | 0.88 | 0.8 | 0.2 | 750 | 750 | N/A |
| D | hybrid | production:hybrid | 4352.402 | 49 | 6 | 0.9 | 1.0 | 0.0 | 0.951975 | 0.9 | 0.833333 | 0.166667 | 750 | 750 | N/A |
| P | full | None | 5173.842 | 50 | 10 | 1.0 | 1.0 | 0.0 | 0.956778 | 0.9 | 0.8 | 0.2 | 0 | 0 | 0.000399765 |
| P | count_25 | early_stop | 3835.69 | 27 | 1 | 0.5 | 1.0 | 0.0 | 0.913556 | 0.8 | 0.0 | 1.0 | 500 | 250 | N/A |
| P | coverage | production:coverage | 5051.988 | 47 | 5 | 0.9 | 1.0 | 0.0 | 0.951975 | 0.92 | 0.8 | 0.2 | 500 | 250 | N/A |
| P | confidence | production:confidence | 5054.674 | 47 | 5 | 0.9 | 1.0 | 0.0 | 0.951975 | 0.92 | 0.8 | 0.2 | 500 | 250 | N/A |
| P | hybrid | production:hybrid | 5071.462 | 48 | 6 | 0.92 | 1.0 | 0.0 | 0.953019 | 0.933333 | 0.833333 | 0.166667 | 250 | 0 | N/A |

Totals: calls=448, settled=183500 milli, pending=5250 milli, late=4250 milli, released=1000 milli, calculated USD=0.00346287. actual_usd is N/A.

Verifier confidence is a deterministic host heuristic from verification and answer support, not a calibrated probability.
Frozen async and adaptive baseline hashes are included in the JSON.
