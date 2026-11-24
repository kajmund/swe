# RuntimeBudget-backed policy comparison (offline)

Generated: 2026-11-24T00:49:02.536362+00:00
Fixture SHA-256: `be95745f8bed6981536c8c1592304be8afd02dfc4d78776eb4495f3138f0ad06`
Budget cap: 15000 milli-Sweycoin per policy
Training items: bio-01, bio-02, law-01, law-02, finance-01
Held-out items: finance-02, medicine-01, medicine-02, engineering-01, engineering-02
Backend: `swe.budget.RuntimeBudget` + `swe_budget.Ledger`; no local meter.
Gold is used for training statistics and post-hoc scoring only.

| Policy | Stop | Tasks | Accuracy | Coverage | False support | Need cov. | Latency | Calls | Spec. | Settled | Pending | Late | Released | Denied | Overspend | Replay | Reserve first | Calc USD |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: |
| equal_split | False | 20 | 0.72 | 0.0 | 0.0 | 0.8 | 5769.345 | 20 | 0 | 4000 | 0 | 0 | 0 | 5 | 0 | True | True | 0.000116715 |
| greedy | True | 23 | 0.84 | 1.0 | 0.0 | 0.92 | 6947.745 | 23 | 0 | 7600 | 0 | 0 | 0 | 0 | 0 | True | True | 0.00018651 |
| fixed_routing | True | 23 | 0.84 | 1.0 | 0.0 | 0.92 | 6563.291 | 23 | 0 | 8600 | 0 | 0 | 0 | 0 | 0 | True | True | 0.0002085154 |
| adaptive | True | 23 | 0.84 | 1.0 | 0.0 | 0.92 | 6563.291 | 28 | 5 | 9400 | 4000 | 800 | 3200 | 0 | 0 | True | True | N/A |
| adaptive_no_spec | True | 23 | 0.84 | 1.0 | 0.0 | 0.92 | 6947.745 | 23 | 0 | 7600 | 0 | 0 | 0 | 0 | 0 | True | True | 0.00018651 |

Totals: calls=117, settled=37200 milli, pending=4000 milli, late=800 milli, released=3200 milli, denied=5, overspent=0 milli, calculated USD=0.00069825 (complete subset 0.000906766).

Every run replays its ledger event log and compares the replayed balance.
Reservation-before-dispatch and reserve-before-terminal checks are in the JSON.
