# Async microtask scheduling benchmark (offline)

Generated: 2026-11-23T21:56:07.835166+00:00
Fixture SHA-256: `be95745f8bed6981536c8c1592304be8afd02dfc4d78776eb4495f3138f0ad06`
Models: A=`deepseek-v4.1-flash`, B/C/D=`nemotron-3-super`
Provider rate limit: 3
Trace scale: 1.0

Deterministic latency traces. No model or network call was made.
Quality values are offline heuristic personas, not live model results.

| Strategy | Scenario | Conc. | Wall ms | First verified ms | p50 ms | p95 ms | Critical path ms | Worker util | Provider util | Accuracy | Coverage | False support | Calls | Settled milli | Pending before | Late milli | Released milli | Calc USD |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A_single_large | main | 1 | 13951.755 | 1277.341 | 7428.366 | 13950.824 | 2142.385 | 1.0 | 0.333239 | 0.68 | 0.1 | 0.0 | 10 | 12500 | 0 | 0 | 0 | 0.001370544 |
| A_single_large | main | 2 | 7144.218 | 1278.53 | 4211.809 | 7143.659 | 2142.385 | 1.0 | 0.650523 | 0.68 | 0.1 | 0.0 | 10 | 12500 | 0 | 0 | 0 | 0.001370544 |
| A_single_large | main | 4 | 5264.862 | 1277.628 | 2857.974 | 5263.846 | 2142.385 | 1.0 | 0.882182 | 0.68 | 0.1 | 0.0 | 10 | 12500 | 0 | 0 | 0 | 0.001370544 |
| A_single_large | main | 8 | 5272.076 | 1283.775 | 2863.806 | 5264.144 | 2142.385 | 0.725146 | 0.881615 | 0.68 | 0.1 | 0.0 | 10 | 12500 | 0 | 0 | 0 | 0.001370544 |
| A_single_large | main | 16 | 5262.129 | 1278.871 | 2855.605 | 5261.698 | 2142.385 | 0.363073 | 0.882542 | 0.68 | 0.1 | 0.0 | 10 | 12500 | 0 | 0 | 0 | 0.001370544 |
| A_single_large | main | 32 | 5262.604 | 1277.429 | 2855.823 | 5262.129 | 2142.385 | 0.181492 | 0.882449 | 0.68 | 0.1 | 0.0 | 10 | 12500 | 0 | 0 | 0 | 0.001370544 |
| A_single_large | main | 64 | 5263.305 | 1276.517 | 2864.637 | 5262.837 | 2142.385 | 0.090785 | 0.882421 | 0.68 | 0.1 | 0.0 | 10 | 12500 | 0 | 0 | 0 | 0.001370544 |
| B_sequential | main | 1 | 15507.991 | 109.309 | 174.266 | 911.153 | 3528.595 | 0.999758 | 0.333243 | 0.9 | 0.8 | 0.2 | 50 | 20000 | 0 | 0 | 0 | 0.000399765 |
| C_layered_barrier | main | 1 | 15592.792 | 109.468 | 2352.677 | 5802.072 | 3528.595 | 1.0 | 0.332853 | 0.9 | 0.8 | 0.2 | 50 | 20000 | 0 | 0 | 0 | 0.000399765 |
| D_event_ready | main | 1 | 15542.864 | 109.449 | 174.323 | 906.735 | 3528.595 | 0.998722 | 0.332892 | 0.9 | 0.8 | 0.2 | 50 | 20000 | 0 | 0 | 0 | 0.000399765 |
| C_layered_barrier | main | 2 | 7804.831 | 101.357 | 1208.773 | 2920.556 | 3528.595 | 1.0 | 0.662388 | 0.9 | 0.8 | 0.2 | 50 | 20000 | 0 | 0 | 0 | 0.000399765 |
| D_event_ready | main | 2 | 7824.051 | 101.274 | 174.053 | 906.387 | 3528.595 | 0.991818 | 0.661185 | 0.9 | 0.8 | 0.2 | 50 | 20000 | 0 | 0 | 0 | 0.000399765 |
| C_layered_barrier | main | 4 | 5495.306 | 105.274 | 784.065 | 1976.338 | 3528.595 | 1.0 | 0.941728 | 0.9 | 0.8 | 0.2 | 50 | 20000 | 0 | 0 | 0 | 0.000399765 |
| D_event_ready | main | 4 | 5765.247 | 100.96 | 244.312 | 1009.583 | 3528.595 | 0.865296 | 0.896305 | 0.9 | 0.8 | 0.2 | 50 | 20000 | 0 | 0 | 0 | 0.000399765 |
| C_layered_barrier | main | 8 | 5495.75 | 101.137 | 786.535 | 1971.869 | 3528.595 | 1.0 | 0.941006 | 0.9 | 0.8 | 0.2 | 50 | 20000 | 0 | 0 | 0 | 0.000399765 |
| D_event_ready | main | 8 | 5777.813 | 100.372 | 714.401 | 1466.564 | 3528.595 | 0.803486 | 0.894724 | 0.9 | 0.8 | 0.2 | 50 | 20000 | 0 | 0 | 0 | 0.000399765 |
| C_layered_barrier | main | 16 | 5487.142 | 104.492 | 786.625 | 1969.81 | 3528.595 | 0.507473 | 0.942042 | 0.9 | 0.8 | 0.2 | 50 | 20000 | 0 | 0 | 0 | 0.000399765 |
| D_event_ready | main | 16 | 5763.969 | 101.514 | 1071.781 | 1651.302 | 3528.595 | 0.601248 | 0.896628 | 0.9 | 0.8 | 0.2 | 50 | 20000 | 0 | 0 | 0 | 0.000399765 |
| C_layered_barrier | main | 32 | 5499.16 | 101.293 | 781.524 | 1973.327 | 3528.595 | 0.253502 | 0.940341 | 0.9 | 0.8 | 0.2 | 50 | 20000 | 0 | 0 | 0 | 0.000399765 |
| D_event_ready | main | 32 | 5770.299 | 100.948 | 1122.357 | 1735.8 | 3528.595 | 0.304568 | 0.895687 | 0.9 | 0.8 | 0.2 | 50 | 20000 | 0 | 0 | 0 | 0.000399765 |
| C_layered_barrier | main | 64 | 5483.685 | 100.819 | 788.091 | 1974.414 | 3528.595 | 0.127091 | 0.943248 | 0.9 | 0.8 | 0.2 | 50 | 20000 | 0 | 0 | 0 | 0.000399765 |
| D_event_ready | main | 64 | 5764.674 | 100.67 | 1110.898 | 1737.778 | 3528.595 | 0.152278 | 0.895529 | 0.9 | 0.8 | 0.2 | 50 | 20000 | 0 | 0 | 0 | 0.000399765 |
| C_layered_barrier | early_stop_C | 8 | 2898.422 | 100.252 | 491.481 | 1325.102 | 3528.595 | 0.997339 | 0.881456 | 0.42 | 0.8 | 0.2 | 40 | 14250 | 500 | 500 | 0 | N/A |
| D_event_ready | early_stop_D | 8 | 2597.794 | 100.269 | 685.587 | 1444.286 | 3528.595 | 0.999672 | 0.876903 | 0.44 | 0.8 | 0.111111 | 32 | 14250 | 1250 | 1250 | 0 | N/A |
| C_layered_barrier | cancel_C | 8 | 1501.136 | 100.254 | 561.492 | 858.629 | 3528.595 | 0.842872 | 0.753707 | 0.22 | 0.2 | 0.333333 | 20 | 7500 | 3000 | 2000 | 1000 | N/A |
| D_event_ready | cancel_D | 8 | 1501.743 | 99.782 | 562.354 | 911.388 | 3528.595 | 0.999099 | 0.952122 | 0.28 | 0.5 | 0.166667 | 24 | 10500 | 3000 | 2000 | 1000 | N/A |

Totals across all runs: calls=936, provider calls=909, cancelled=37, settled=434000 milli, pending before reconciliation=7750 milli, late-billed=5750 milli, released=2000 milli, calculated USD=0.016329858 (complete-call subset 0.016329858).

`actual_usd` is N/A; calculated USD is a price-list equivalent. Pending/late milli are Sweycoin reservations, not provider dollars.

Worker utilization = task-held worker time / (wall × requested workers); it includes waiting for a provider permit. Provider utilization = provider-call time / (wall × provider rate limit).

A = one combined call per item. B = sequential microtasks. C = bounded parallel with a global barrier per layer. D = event-driven ready queue without unrelated barriers.
