# Xray algorithm mapping

The Python strategies are translations of Xray-core `main` at commit
[`6e3322d219140a025285ded1114fe17a5edb74d8`](https://github.com/XTLS/Xray-core/tree/6e3322d219140a025285ded1114fe17a5edb74d8).

| Simulator behavior | Xray-core source |
|---|---|
| `random`, health filtering only when `fallbackTag` is set, and unknown observer tags considered alive | [`strategy_random.go`](https://github.com/XTLS/Xray-core/blob/6e3322d219140a025285ded1114fe17a5edb74d8/app/router/strategy_random.go) |
| `roundRobin`, fallbackTag-conditional health filtering, unknown tags considered alive, and modulo index update | [`balancing.go`](https://github.com/XTLS/Xray-core/blob/6e3322d219140a025285ded1114fe17a5edb74d8/app/router/balancing.go) |
| `leastPing`, latest alive observation with strictly lowest delay | [`strategy_leastping.go`](https://github.com/XTLS/Xray-core/blob/6e3322d219140a025285ded1114fe17a5edb74d8/app/router/strategy_leastping.go) |
| `leastLoad` qualification, cost transform, sort keys, baseline/expected selection, random final pick | [`strategy_leastload.go`](https://github.com/XTLS/Xray-core/blob/6e3322d219140a025285ded1114fe17a5edb74d8/app/router/strategy_leastload.go) |
| Fixed `fallbackTag` after an empty strategy result | [`balancing.go`](https://github.com/XTLS/Xray-core/blob/6e3322d219140a025285ded1114fe17a5edb74d8/app/router/balancing.go) |
| Latest-result classic observation, 5-second HTTP timeout, completion-time publication, and next round after all concurrent results plus the configured interval | [`observer.go`](https://github.com/XTLS/Xray-core/blob/6e3322d219140a025285ded1114fe17a5edb74d8/app/observatory/observer.go) |
| Burst scheduling, defaults, blocking `MeasureDelay`, randomized samples over `interval * sampling`, and next-window cancellation | [`healthping.go`](https://github.com/XTLS/Xray-core/blob/6e3322d219140a025285ded1114fe17a5edb74d8/app/observatory/burst/healthping.go) |
| HTTP timeout and elapsed-time measurement for successful burst probes | [`ping.go`](https://github.com/XTLS/Xray-core/blob/6e3322d219140a025285ded1114fe17a5edb74d8/app/observatory/burst/ping.go) |
| Rolling successful RTT average, population deviation, and one-sample `average / 2` rule | [`healthping_result.go`](https://github.com/XTLS/Xray-core/blob/6e3322d219140a025285ded1114fe17a5edb74d8/app/observatory/burst/healthping_result.go) |
| Burst `Alive = All != Fail` and health fields exposed to the router | [`burstobserver.go`](https://github.com/XTLS/Xray-core/blob/6e3322d219140a025285ded1114fe17a5edb74d8/app/observatory/burst/burstobserver.go) |

## Deliberate abstraction boundary

This project does not run sockets, protocols, Mux, DNS, routing rules, or Xray's dispatcher. It simulates a stream of new connection attempts after the routing rule has already selected a balancer. A failed selected outbound is a failed attempt: the Xray balancer selection code does not itself retry the connection on a second candidate.

Probe completion is quantized upward to the simulator's one-second clock, so even a sub-second result first affects routing on the following tick. Classic observation uses Xray's concurrent mode and starts its next round only after the slowest completion plus `probeInterval`. Burst observation publishes each request at completion, retains Xray's scheduling shape, rolling capacity, expiry, and statistics, and discards scheduled results that complete after the next window cancels their round. Exact sub-second cancellation races are outside the model.

The numerical world does not classify failed probes as immediate refusal versus silent black-hole timeout. It therefore models each failure as consuming the configured HTTP timeout. A generated nominal success whose RTT exceeds that timeout is also recorded as a timed-out failure.

## Defaults and the 60-second experiment floor

Xray's classic observatory defaults to a 10-second interval and sequential probing when `probeInterval` and `enableConcurrency` are omitted. This experiment intentionally excludes that literal observer default because every benchmark interval must be at least 60 seconds. “Default leastPing” therefore means the default leastPing selection algorithm paired with an explicit concurrent 60/120/300-second observer.

The burst observatory defaults to a 60-second configured interval, 10 retained samples, and a 5-second timeout. Its samples are randomly distributed across `interval × sampling`, so the configured interval is an average cadence rather than a guaranteed minimum gap. Default leastLoad uses protobuf zero values (`expected=0`, no baselines, maximum RTT, tolerance, or costs); the selection code internally converts non-positive `expected` to one candidate.

`tolerance` is a qualification filter, not part of the leastLoad sort key. Xray applies it only when the configured value is greater than zero, excluding an otherwise-alive outbound when `failure_count / sample_count > tolerance`. Consequently, default `tolerance=0` disables the filter rather than requiring a perfect probe history. With ten retained samples, the sweep presets at 10%, 20%, 30%, 40%, 50%, 70%, and 100% exercise the natural discrete failure-count boundaries; 100% is a no-filter control.

For random and round-robin, health filtering is not a separate Xray setting: it occurs when a non-empty `fallbackTag` causes an observatory to be injected. The simulator's health-aware presets model that configuration. The no-health presets have neither an observer nor a fallback, so repeating them at different observatory intervals would be meaningless.

## Alternative leastLoad-like ranks

The `xray_deviation` mode remains the exact selector above. Four experimental modes reuse its candidate/health qualification and the same retained burst fields, but replace only the rank key:

| Mode | Primary rank |
|---|---|
| `mean_latency` | Successful RTT average, then empirical failure rate |
| `success_rate` | Empirical failure rate, then successful RTT average |
| `expected_cost` | `success_rate × average_RTT + failure_rate × failure_cost` |
| `reliability_lcb` | 95% Wilson lower confidence bound for success, then expected cost |
| `history_guard` | Latest-failure circuit breaker, then a timestamp-aware Bayesian estimate of next-attempt success; latency only resolves an equal-reliability choice |

These are simulator proposals, not Xray-core algorithms. The default experiment uses a 5000 ms failure cost for the composite ranks. `history_guard` additionally assumes a richer observer API than Xray currently exposes: it reads the ordered completion timestamp and result of every retained raw sample instead of only `All`, `Fail`, `Average`, and `Deviation`.

For History Guard, each outbound's long-run success estimate is shrunk toward the fleet-wide result rate, then updated in timestamp order using a noisy two-state up/down observation model. State evidence decays toward that baseline with a 180-second half-life. A most-recent failure removes the outbound whenever any candidate has a most-recent success. Among the remaining candidates, predicted success probability is strictly primary; the recency-weighted successful RTT is used only after an equal probability. Equal burst configurations share one deterministic randomized probe schedule per world.

## v2rayNG presets

The app defaults are pinned separately to v2rayNG commit [`ca79a574fa7cc93aac6d9cda2f2a43e05bda2b5d`](https://github.com/2dust/v2rayNG/tree/ca79a574fa7cc93aac6d9cda2f2a43e05bda2b5d). [`AppConfig.kt`](https://github.com/2dust/v2rayNG/blob/ca79a574fa7cc93aac6d9cda2f2a43e05bda2b5d/V2rayNG/app/src/main/java/com/v2ray/ang/AppConfig.kt) defines leastPing `3m`, leastLoad `5m`, sampling `2`, method `HEAD`, and timeout `30s`; [`CoreConfigManager.kt`](https://github.com/2dust/v2rayNG/blob/ca79a574fa7cc93aac6d9cda2f2a43e05bda2b5d/V2rayNG/app/src/main/java/com/v2ray/ang/core/CoreConfigManager.kt) emits the classic observatory with concurrency enabled and the matching burst observatory.
