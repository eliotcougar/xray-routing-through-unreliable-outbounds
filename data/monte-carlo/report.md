# Xray balancer simulation results

- Total simulated worlds: 400
- Fleet scenarios: 8 (mixed_real_world, mostly_healthy, latency_ladder, fast_but_flaky, shared_provider_failure, congestion_waves, rolling_degradation, hostile_internet)
- Simulated time per trial: 30 minutes
- Outbounds per trial: 5-20
- Connection attempts: one every 1 second(s)
- Failed-attempt response penalty: 5000 ms
- Representative trial: 8
- Xray source commit: `6e3322d219140a025285ded1114fe17a5edb74d8`

## Aggregate ranking

| Rank | Strategy/settings | Availability | Effective response | Successful p95 | Mean max outage | Win rate |
|---:|---|---:|---:|---:|---:|---:|
| 1 | leastLoad-like / history guard + hysteresis / 60s | 94.53% | 322 ms | 119 ms | 27.4 s | 15.5% |
| 2 | leastPing / default strategy / 60s | 94.35% | 328 ms | 108 ms | 27.3 s | 18.0% |
| 3 | leastLoad-like / latest health + hysteresis / 60s | 93.61% | 362 ms | 94 ms | 33.1 s | 19.5% |
| 4 | leastLoad-like / expected outcome / 60s | 93.28% | 380 ms | 109 ms | 36.2 s | 19.2% |
| 5 | leastLoad-like / success rate first / 60s | 93.27% | 382 ms | 113 ms | 36.2 s | 19.2% |
| 6 | leastPing / default strategy / 120s | 93.22% | 386 ms | 120 ms | 34.0 s | 13.5% |
| 7 | leastLoad-like / reliability confidence / 60s | 93.25% | 405 ms | 239 ms | 36.3 s | 2.5% |
| 8 | leastLoad / tolerance 10% / 60s | 92.46% | 422 ms | 110 ms | 45.8 s | 5.2% |
| 9 | leastLoad-like / history guard / 60s | 94.58% | 425 ms | 478 ms | 21.9 s | 5.8% |
| 10 | leastPing / v2rayNG default / 180s | 92.29% | 432 ms | 120 ms | 41.7 s | 14.2% |
| 11 | leastLoad / tolerance 20% / 60s | 91.70% | 458 ms | 105 ms | 52.0 s | 4.8% |
| 12 | leastPing / default strategy / 300s | 91.19% | 488 ms | 121 ms | 49.6 s | 14.8% |
| 13 | leastLoad / tolerance 30% / 60s | 90.85% | 499 ms | 99 ms | 59.1 s | 5.2% |
| 14 | leastLoad / e2 s5 tol40 / 60s | 90.66% | 518 ms | 130 ms | 26.6 s | 0.5% |
| 15 | leastLoad / tolerance 40% / 60s | 90.10% | 536 ms | 96 ms | 64.4 s | 5.5% |
| 16 | leastLoad / tolerance 50% / 60s | 89.40% | 570 ms | 94 ms | 71.2 s | 4.5% |
| 17 | roundRobin / health 60s | 91.22% | 596 ms | 526 ms | 14.8 s | 0.0% |
| 18 | random / health 60s | 91.20% | 596 ms | 526 ms | 15.8 s | 0.0% |
| 19 | leastLoad / v2rayNG burst default / 300s | 89.37% | 598 ms | 216 ms | 58.9 s | 3.5% |
| 20 | leastLoad / tolerance 70% / 60s | 88.22% | 629 ms | 91 ms | 81.8 s | 4.5% |
| 21 | roundRobin / health 120s | 90.21% | 645 ms | 530 ms | 15.7 s | 0.0% |
| 22 | leastLoad-like / mean latency first / 60s | 87.74% | 646 ms | 70 ms | 85.0 s | 20.0% |
| 23 | random / health 120s | 90.14% | 649 ms | 529 ms | 16.1 s | 0.0% |
| 24 | leastLoad / default strategy settings / 60s | 87.21% | 679 ms | 96 ms | 87.3 s | 4.2% |
| 25 | leastLoad / tolerance 100% / 60s | 87.21% | 679 ms | 96 ms | 87.3 s | 4.2% |
| 26 | random / health 300s | 88.73% | 719 ms | 534 ms | 18.6 s | 0.0% |
| 27 | roundRobin / health 300s | 88.68% | 720 ms | 533 ms | 17.1 s | 0.0% |
| 28 | leastLoad / default strategy settings / 300s | 85.64% | 769 ms | 158 ms | 100.6 s | 5.0% |
| 29 | leastLoad / default strategy settings / 120s | 85.02% | 791 ms | 119 ms | 109.4 s | 6.2% |
| 30 | random / no health | 81.16% | 1093 ms | 560 ms | 17.6 s | 0.0% |
| 31 | roundRobin / no health | 81.11% | 1095 ms | 560 ms | 16.3 s | 0.0% |

## Exact leastLoad tolerance sweep

Xray applies the filter only when tolerance is greater than zero. An otherwise-alive outbound is excluded when `failure_count / sample_count` is strictly greater than the configured tolerance; therefore the default zero value means disabled, not zero failures allowed.

| Tolerance | Availability (95% CI) | Effective response (95% CI) | Successful p95 | Mean longest outage | Switches/min |
|---:|---:|---:|---:|---:|---:|
| Disabled (default) | 87.21% +/- 1.37 | 679 +/- 68 ms | 96 ms | 87.3 s | 0.33 |
| 10% | 92.46% +/- 0.70 | 422 +/- 36 ms | 110 ms | 45.8 s | 0.33 |
| 20% | 91.70% +/- 0.79 | 458 +/- 40 ms | 105 ms | 52.0 s | 0.32 |
| 30% | 90.85% +/- 0.88 | 499 +/- 44 ms | 99 ms | 59.1 s | 0.32 |
| 40% | 90.10% +/- 0.98 | 536 +/- 49 ms | 96 ms | 64.4 s | 0.32 |
| 50% | 89.40% +/- 1.06 | 570 +/- 53 ms | 94 ms | 71.2 s | 0.32 |
| 70% | 88.22% +/- 1.24 | 629 +/- 62 ms | 91 ms | 81.8 s | 0.32 |
| 100% | 87.21% +/- 1.37 | 679 +/- 68 ms | 96 ms | 87.3 s | 0.33 |

### Paired effect relative to default leastLoad

Each value compares the tolerance preset with default disabled tolerance on the same generated world. Positive availability and negative effective-response/outage deltas are improvements.

| Tolerance | Availability delta | Effective-response delta | Longest-outage delta |
|---:|---:|---:|---:|
| 10% | +5.25 +/- 0.94 pp | -256 +/- 46 ms | -41.5 +/- 10.2 s |
| 20% | +4.49 +/- 0.82 pp | -220 +/- 40 ms | -35.3 +/- 9.2 s |
| 30% | +3.64 +/- 0.72 pp | -179 +/- 36 ms | -28.2 +/- 7.8 s |
| 40% | +2.88 +/- 0.62 pp | -143 +/- 31 ms | -22.9 +/- 6.7 s |
| 50% | +2.19 +/- 0.50 pp | -109 +/- 25 ms | -16.1 +/- 5.4 s |
| 70% | +1.01 +/- 0.30 pp | -50 +/- 15 ms | -5.5 +/- 3.4 s |
| 100% | +0.00 +/- 0.00 pp | +0 +/- 0 ms | +0.0 +/- 0.0 s |

## Latest-result health plus hysteresis by scenario

| Scenario | Availability (95% CI) | Effective response (95% CI) | Mean longest outage | Switches/min |
|---|---:|---:|---:|---:|
| Mixed real-world fleet | 94.94% +/- 1.14 | 292 +/- 58 ms | 25.4 s | 0.21 |
| Mostly healthy fleet | 98.08% +/- 0.82 | 113 +/- 41 ms | 10.6 s | 0.04 |
| Latency ladder | 98.95% +/- 0.61 | 69 +/- 31 ms | 6.2 s | 0.03 |
| Fast but flaky | 91.63% +/- 1.18 | 460 +/- 59 ms | 29.2 s | 0.34 |
| Shared-provider failures | 89.45% +/- 1.75 | 568 +/- 87 ms | 76.1 s | 0.28 |
| Congestion waves | 96.66% +/- 0.54 | 250 +/- 33 ms | 4.2 s | 0.41 |
| Rolling degradation | 93.06% +/- 1.41 | 399 +/- 70 ms | 51.1 s | 0.29 |
| Hostile internet | 86.07% +/- 1.84 | 743 +/- 92 ms | 61.6 s | 0.34 |

## History Guard plus hysteresis by scenario

| Scenario | Availability (95% CI) | Effective response (95% CI) | Mean longest outage | Switches/min |
|---|---:|---:|---:|---:|
| Mixed real-world fleet | 95.82% +/- 0.83 | 258 +/- 43 ms | 22.0 s | 0.46 |
| Mostly healthy fleet | 98.51% +/- 0.71 | 96 +/- 37 ms | 8.5 s | 0.12 |
| Latency ladder | 99.11% +/- 0.50 | 66 +/- 27 ms | 5.8 s | 0.09 |
| Fast but flaky | 93.43% +/- 1.10 | 373 +/- 55 ms | 25.3 s | 0.46 |
| Shared-provider failures | 90.49% +/- 1.73 | 530 +/- 88 ms | 67.1 s | 0.70 |
| Congestion waves | 96.56% +/- 0.58 | 248 +/- 33 ms | 4.1 s | 0.23 |
| Rolling degradation | 94.24% +/- 1.11 | 351 +/- 57 ms | 30.3 s | 0.74 |
| Hostile internet | 88.10% +/- 1.59 | 656 +/- 80 ms | 56.3 s | 1.09 |

## Paired effect of adding hysteresis to History Guard

Each value is the hysteretic result minus original History Guard on the same generated world. Negative effective response and switching deltas are improvements; a positive outage delta is a cost.

| Scenario | Availability delta | Effective-response delta | Switches/min delta | Longest-outage delta |
|---|---:|---:|---:|---:|
| All scenarios | -0.05 +/- 0.26 pp | -102 +/- 16 ms | -4.34 +/- 0.21 | +5.6 +/- 1.7 s |
| Mixed real-world fleet | -1.19 +/- 0.83 pp | -70 +/- 39 ms | -4.80 +/- 0.47 | +10.9 +/- 7.2 s |
| Mostly healthy fleet | +0.10 +/- 0.60 pp | -134 +/- 30 ms | -5.81 +/- 0.51 | +1.6 +/- 3.2 s |
| Latency ladder | +0.03 +/- 0.44 pp | -281 +/- 36 ms | -6.55 +/- 0.67 | +1.8 +/- 2.7 s |
| Fast but flaky | -1.48 +/- 1.06 pp | +54 +/- 52 ms | -3.96 +/- 0.42 | +7.7 +/- 4.4 s |
| Shared-provider failures | -0.47 +/- 0.65 pp | -74 +/- 34 ms | -4.03 +/- 0.42 | +8.1 +/- 6.4 s |
| Congestion waves | +1.11 +/- 0.51 pp | -189 +/- 35 ms | -4.86 +/- 0.46 | -0.4 +/- 0.6 s |
| Rolling degradation | +1.56 +/- 0.82 pp | -108 +/- 42 ms | -2.95 +/- 0.32 | +11.3 +/- 5.6 s |
| Hostile internet | -0.04 +/- 0.46 pp | -15 +/- 23 ms | -1.79 +/- 0.27 | +3.6 +/- 5.2 s |

The rank uses mean effective response, where each failed connection attempt counts as the configured timeout penalty. Availability and successful-only latency remain separate columns so the penalty does not hide the trade-off.

## Alternative ranks in hard scenarios

| Scenario | Best alternative | Alternative availability | Alternative effective | Exact Xray availability | Exact Xray effective | Effective improvement |
|---|---|---:|---:|---:|---:|---:|
| Fast but flaky | leastLoad-like / history guard / 60s | 94.91% | 318 ms | 88.99% | 592 ms | +274 ms |
| Shared-provider failures | leastLoad-like / history guard + hysteresis / 60s | 90.49% | 530 ms | 84.13% | 836 ms | +306 ms |
| Rolling degradation | leastLoad-like / history guard + hysteresis / 60s | 94.24% | 351 ms | 64.78% | 1797 ms | +1446 ms |
| Hostile internet | leastLoad-like / history guard + hysteresis / 60s | 88.10% | 656 ms | 75.47% | 1266 ms | +609 ms |

Positive improvement means the best experimental rank reduced the failure-penalized effective response relative to exact Xray leastLoad at the same 60-second burst settings.

See `metadata.json` for the complete strategy settings and modeling assumptions, `summary.csv` for confidence intervals, and `figures/` for plots.
