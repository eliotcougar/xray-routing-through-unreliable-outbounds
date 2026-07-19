"""Connection-attempt simulation and quality metrics."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import random
from typing import Sequence

from .model import SimulatedWorld, percentile
from .strategies import StrategySpec, build_strategy


@dataclass(frozen=True)
class Attempt:
    second: int
    selected_tag: str
    success: bool
    latency_ms: float | None
    effective_latency_ms: float
    used_fallback: bool


@dataclass(frozen=True)
class SimulationResult:
    strategy_key: str
    strategy_label: str
    metrics: dict[str, float | int]
    attempts: tuple[Attempt, ...] = ()


def stable_seed(*parts: object) -> int:
    digest = hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def burst_observer_seed(world: SimulatedWorld, spec: StrategySpec) -> int:
    """Common schedule seed for strategies sharing burst settings."""
    return stable_seed(
        world.seed,
        "burst-observer",
        spec.burst_interval_s,
        spec.sampling_count,
        spec.probe_timeout_s,
        spec.probe_url,
        spec.probe_http_method,
    )


def _max_false_run(successes: Sequence[bool], step_s: int) -> int:
    longest = 0
    current = 0
    for success in successes:
        if success:
            current = 0
        else:
            current += step_s
            longest = max(longest, current)
    return longest


def simulate_strategy(
    world: SimulatedWorld,
    spec: StrategySpec,
    failure_penalty_ms: float = 5000.0,
    attempt_interval_s: int = 1,
    keep_attempts: bool = False,
) -> SimulationResult:
    if attempt_interval_s <= 0:
        raise ValueError("attempt_interval_s must be positive")
    if failure_penalty_ms <= 0:
        raise ValueError("failure_penalty_ms must be positive")

    rng = random.Random(stable_seed(world.seed, spec.key, "selection"))
    # Strategies with identical observer settings must see the same randomized
    # probe starts and therefore the same retained raw histories. Selection
    # randomness remains strategy-specific.
    observer_rng = random.Random(burst_observer_seed(world, spec))
    fallback_tag = world.tags[0]
    strategy = build_strategy(
        spec,
        world,
        rng,
        fallback_tag,
        observer_rng,
    )
    traces = world.by_tag()
    candidates = world.tags
    attempts: list[Attempt] = []
    successes: list[bool] = []
    success_latencies: list[float] = []
    effective_latencies: list[float] = []
    selected_tags: list[str] = []
    fallback_count = 0
    oracle_misses = 0

    for second in range(world.duration_s):
        strategy.tick(second)
        if second % attempt_interval_s != 0:
            continue
        selected_tag, used_fallback = strategy.pick(second, candidates)
        trace = traces[selected_tag]
        success = trace.traffic_success[second]
        latency = trace.traffic_latency_ms[second] if success else None
        effective = float(latency) if latency is not None else failure_penalty_ms
        oracle_has_path = any(outbound.traffic_success[second] for outbound in world.outbounds)
        if not success and oracle_has_path:
            oracle_misses += 1

        selected_tags.append(selected_tag)
        successes.append(success)
        effective_latencies.append(effective)
        if latency is not None:
            success_latencies.append(float(latency))
        fallback_count += int(used_fallback)
        if keep_attempts:
            attempts.append(
                Attempt(
                    second=second,
                    selected_tag=selected_tag,
                    success=success,
                    latency_ms=float(latency) if latency is not None else None,
                    effective_latency_ms=effective,
                    used_fallback=used_fallback,
                )
            )

    total = len(successes)
    success_count = sum(successes)
    route_switches = sum(
        left != right for left, right in zip(selected_tags, selected_tags[1:])
    )
    usable_count = sum(
        success and latency <= 500.0
        for success, latency in zip(successes, effective_latencies)
    )
    metrics: dict[str, float | int] = {
        "attempts": total,
        "successes": success_count,
        "availability_pct": 100.0 * success_count / total,
        "usable_pct": 100.0 * usable_count / total,
        "effective_mean_ms": sum(effective_latencies) / total,
        "effective_p95_ms": percentile(effective_latencies, 95),
        "success_mean_ms": (
            sum(success_latencies) / len(success_latencies)
            if success_latencies else math.nan
        ),
        "success_p50_ms": percentile(success_latencies, 50),
        "success_p95_ms": percentile(success_latencies, 95),
        "success_p99_ms": percentile(success_latencies, 99),
        "max_outage_s": _max_false_run(successes, attempt_interval_s),
        "route_switches_per_min": route_switches / (world.duration_s / 60.0),
        "unique_outbounds": len(set(selected_tags)),
        "fallback_pct": 100.0 * fallback_count / total,
        "oracle_miss_pct": 100.0 * oracle_misses / total,
    }
    return SimulationResult(
        strategy_key=spec.key,
        strategy_label=spec.label,
        metrics=metrics,
        attempts=tuple(attempts),
    )
