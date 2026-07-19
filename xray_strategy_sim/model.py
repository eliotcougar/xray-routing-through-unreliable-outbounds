"""Time-correlated synthetic outbound traces.

The model deliberately separates the latent quality of an outbound from the
traffic and health-probe observations.  Every strategy in a trial receives the
same pre-generated world, so comparisons use common random numbers rather than
giving one strategy an easier set of links by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import random
from typing import Sequence


ARCHETYPES = (
    "stable_ultrafast",
    "stable_fast",
    "stable_slow",
    "satellite",
    "flaky_fast",
    "jittery",
    "bursty_congestion",
    "long_outages",
    "periodic_blackouts",
    "mobile_handoffs",
    "peak_congestion",
    "brownouts",
    "fast_then_dead",
    "degrading",
    "recovering",
    "mostly_dead",
    "dead",
)


@dataclass(frozen=True)
class OutboundTrace:
    tag: str
    archetype: str
    provider: int
    success_probability: tuple[float, ...]
    expected_latency_ms: tuple[float, ...]
    traffic_success: tuple[bool, ...]
    traffic_latency_ms: tuple[float | None, ...]
    probe_success: tuple[bool, ...]
    probe_latency_ms: tuple[float | None, ...]


@dataclass(frozen=True)
class SimulatedWorld:
    seed: int
    duration_s: int
    scenario_key: str
    scenario: str
    outbounds: tuple[OutboundTrace, ...]
    global_incident: tuple[bool, ...]

    @property
    def tags(self) -> tuple[str, ...]:
        return tuple(outbound.tag for outbound in self.outbounds)

    def by_tag(self) -> dict[str, OutboundTrace]:
        return {outbound.tag: outbound for outbound in self.outbounds}

    def raw_mean_availability(self) -> float:
        values = [
            sum(outbound.traffic_success) / self.duration_s
            for outbound in self.outbounds
        ]
        return sum(values) / len(values)

    def oracle_availability(self) -> float:
        successful_seconds = sum(
            any(outbound.traffic_success[t] for outbound in self.outbounds)
            for t in range(self.duration_s)
        )
        return successful_seconds / self.duration_s


@dataclass(frozen=True)
class _Profile:
    base_latency: tuple[float, float]
    jitter_fraction: float
    up_success: float
    enter_bad: float
    leave_bad: float
    bad_success: float
    bad_latency_multiplier: float
    spike_probability: float = 0.0
    spike_multiplier: tuple[float, float] = (1.0, 1.0)


@dataclass(frozen=True)
class FleetScenario:
    """Named composition and shared-failure model for an outbound fleet."""

    key: str
    label: str
    description: str
    required_archetypes: tuple[str, ...]
    archetype_weights: tuple[tuple[str, int], ...]
    global_incident_chance: float = 0.35
    global_incident_duration_s: tuple[int, int] = (12, 55)
    global_success_multiplier: float = 0.20
    global_latency_multiplier: float = 2.5
    provider_incident_chance: float = 0.65
    provider_incident_duration_s: tuple[int, int] = (25, 180)
    provider_success_multiplier: float = 0.08
    provider_latency_multiplier: float = 2.0
    provider_divisor: float = 4.0


_PROFILES = {
    "stable_ultrafast": _Profile((10, 30), 0.05, 0.9995, 0.0002, 0.30, 0.90, 1.5),
    "stable_fast": _Profile((28, 75), 0.08, 0.998, 0.0004, 0.20, 0.80, 2.0),
    "stable_slow": _Profile((230, 620), 0.07, 0.997, 0.0005, 0.15, 0.75, 1.7),
    "satellite": _Profile((560, 950), 0.10, 0.993, 0.0010, 0.12, 0.70, 1.8, 0.006, (1.5, 3.0)),
    "flaky_fast": _Profile((35, 95), 0.12, 0.985, 0.010, 0.10, 0.04, 1.8),
    "jittery": _Profile((75, 190), 0.35, 0.990, 0.008, 0.09, 0.80, 3.2, 0.025, (2.0, 7.0)),
    "bursty_congestion": _Profile((45, 135), 0.16, 0.995, 0.004, 0.025, 0.72, 5.0, 0.010, (2.0, 5.0)),
    "long_outages": _Profile((55, 170), 0.13, 0.995, 0.0013, 0.008, 0.01, 2.0),
    "periodic_blackouts": _Profile((30, 90), 0.10, 0.997, 0.0002, 0.25, 0.65, 1.8),
    "mobile_handoffs": _Profile((40, 125), 0.20, 0.990, 0.002, 0.16, 0.45, 3.0, 0.008, (2.0, 5.0)),
    "peak_congestion": _Profile((38, 105), 0.13, 0.996, 0.0005, 0.18, 0.70, 2.0),
    "brownouts": _Profile((42, 125), 0.14, 0.995, 0.0010, 0.12, 0.55, 2.5),
    "fast_then_dead": _Profile((25, 70), 0.08, 0.998, 0.0001, 0.25, 0.70, 1.5),
    "mostly_dead": _Profile((260, 850), 0.25, 0.22, 0.018, 0.006, 0.01, 1.8, 0.02, (2.0, 5.0)),
    "dead": _Profile((500, 1200), 0.10, 0.0, 0.0, 0.0, 0.0, 1.0),
}


FLEET_SCENARIOS: tuple[FleetScenario, ...] = (
    FleetScenario(
        key="mixed_real_world",
        label="Mixed real-world fleet",
        description="A broad mix of healthy, slow, flaky, congested, and failed links.",
        required_archetypes=(
            "stable_fast", "flaky_fast", "stable_slow", "bursty_congestion", "long_outages"
        ),
        archetype_weights=(
            ("stable_ultrafast", 1), ("stable_fast", 3), ("stable_slow", 2),
            ("satellite", 1), ("flaky_fast", 3), ("jittery", 2),
            ("bursty_congestion", 2), ("long_outages", 2),
            ("periodic_blackouts", 1), ("mobile_handoffs", 1),
            ("peak_congestion", 1), ("brownouts", 1), ("fast_then_dead", 1),
            ("degrading", 1), ("recovering", 1), ("mostly_dead", 1), ("dead", 1),
        ),
    ),
    FleetScenario(
        key="mostly_healthy",
        label="Mostly healthy fleet",
        description="Good links dominate, with a few realistic slow or momentarily flaky alternatives.",
        required_archetypes=("stable_ultrafast", "stable_fast", "stable_slow", "jittery"),
        archetype_weights=(
            ("stable_ultrafast", 3), ("stable_fast", 7), ("stable_slow", 3),
            ("flaky_fast", 2), ("jittery", 2), ("peak_congestion", 1),
            ("long_outages", 1),
        ),
        global_incident_chance=0.15,
        provider_incident_chance=0.30,
    ),
    FleetScenario(
        key="latency_ladder",
        label="Latency ladder",
        description="Availability is similar while latency ranges from local-grade to satellite-grade.",
        required_archetypes=("stable_ultrafast", "stable_fast", "stable_slow", "satellite"),
        archetype_weights=(
            ("stable_ultrafast", 3), ("stable_fast", 4), ("stable_slow", 4),
            ("satellite", 3), ("jittery", 1), ("peak_congestion", 1),
        ),
        global_incident_chance=0.12,
        provider_incident_chance=0.25,
    ),
    FleetScenario(
        key="fast_but_flaky",
        label="Fast but flaky",
        description="Low baseline RTT masks intermittent loss, handoffs, and repeating blackouts.",
        required_archetypes=("stable_fast", "flaky_fast", "periodic_blackouts", "mobile_handoffs"),
        archetype_weights=(
            ("stable_fast", 2), ("flaky_fast", 6), ("periodic_blackouts", 4),
            ("mobile_handoffs", 4), ("long_outages", 2), ("fast_then_dead", 1),
        ),
        global_incident_chance=0.25,
        provider_incident_chance=0.55,
    ),
    FleetScenario(
        key="shared_provider_failure",
        label="Shared-provider failures",
        description="Many superficially independent links fail together behind a small provider set.",
        required_archetypes=("stable_fast", "stable_slow", "flaky_fast", "long_outages"),
        archetype_weights=(
            ("stable_fast", 5), ("stable_slow", 2), ("flaky_fast", 2),
            ("jittery", 1), ("long_outages", 2), ("recovering", 1),
        ),
        global_incident_chance=0.45,
        global_incident_duration_s=(30, 120),
        provider_incident_chance=0.95,
        provider_incident_duration_s=(60, 300),
        provider_success_multiplier=0.015,
        provider_latency_multiplier=3.0,
        provider_divisor=8.0,
    ),
    FleetScenario(
        key="congestion_waves",
        label="Congestion waves",
        description="Most paths stay up but suffer synchronized and individual latency waves.",
        required_archetypes=("stable_fast", "jittery", "bursty_congestion", "peak_congestion"),
        archetype_weights=(
            ("stable_fast", 2), ("stable_slow", 1), ("jittery", 4),
            ("bursty_congestion", 5), ("peak_congestion", 5), ("satellite", 1),
        ),
        global_incident_chance=0.55,
        global_incident_duration_s=(45, 180),
        global_success_multiplier=0.80,
        global_latency_multiplier=4.0,
        provider_incident_chance=0.70,
        provider_success_multiplier=0.55,
        provider_latency_multiplier=5.0,
    ),
    FleetScenario(
        key="rolling_degradation",
        label="Rolling degradation",
        description="Links age, recover, brown out, or fail permanently at different points in the run.",
        required_archetypes=("degrading", "recovering", "brownouts", "fast_then_dead"),
        archetype_weights=(
            ("stable_fast", 1), ("degrading", 5), ("recovering", 4),
            ("brownouts", 4), ("fast_then_dead", 3), ("mostly_dead", 1),
        ),
        global_incident_chance=0.20,
        provider_incident_chance=0.35,
    ),
    FleetScenario(
        key="hostile_internet",
        label="Hostile internet",
        description="Few dependable paths coexist with long outages, brownouts, and dead endpoints.",
        required_archetypes=("stable_fast", "long_outages", "mostly_dead", "dead"),
        archetype_weights=(
            ("stable_fast", 1), ("flaky_fast", 2), ("jittery", 2),
            ("long_outages", 4), ("periodic_blackouts", 2), ("brownouts", 3),
            ("fast_then_dead", 2), ("mostly_dead", 4), ("dead", 3),
        ),
        global_incident_chance=0.65,
        global_incident_duration_s=(45, 180),
        global_success_multiplier=0.08,
        provider_incident_chance=0.90,
        provider_incident_duration_s=(60, 300),
        provider_success_multiplier=0.025,
        provider_divisor=5.0,
    ),
)

DEFAULT_SCENARIO_KEYS: tuple[str, ...] = tuple(scenario.key for scenario in FLEET_SCENARIOS)
_SCENARIOS_BY_KEY = {scenario.key: scenario for scenario in FLEET_SCENARIOS}


def fleet_scenarios() -> tuple[FleetScenario, ...]:
    """Return the stable, public scenario catalog in display order."""
    return FLEET_SCENARIOS


def get_fleet_scenario(key: str) -> FleetScenario:
    """Resolve a scenario key with a useful error for CLI/API callers."""
    try:
        return _SCENARIOS_BY_KEY[key]
    except KeyError as error:
        choices = ", ".join(DEFAULT_SCENARIO_KEYS)
        raise ValueError(f"unknown scenario {key!r}; choose one of: {choices}") from error


def _profile_for(archetype: str) -> _Profile:
    if archetype in {"degrading", "recovering"}:
        return _Profile((45, 120), 0.15, 0.995, 0.003, 0.04, 0.25, 3.0, 0.008, (2.0, 5.0))
    return _PROFILES[archetype]


def _incident_mask(
    rng: random.Random,
    duration_s: int,
    event_chance: float,
    min_duration: int,
    max_duration: int,
) -> list[bool]:
    mask = [False] * duration_s
    if rng.random() >= event_chance:
        return mask
    count = 1 + int(rng.random() < 0.25)
    for _ in range(count):
        length = rng.randint(min_duration, max_duration)
        start = rng.randrange(max(1, duration_s - length))
        for t in range(start, min(duration_s, start + length)):
            mask[t] = True
    return mask


def _choose_archetypes(
    rng: random.Random,
    count: int,
    scenario: FleetScenario,
) -> list[str]:
    """Build a fleet while guaranteeing its defining archetypes when possible."""
    chosen = list(scenario.required_archetypes[:count])
    weighted = [
        archetype
        for archetype, weight in scenario.archetype_weights
        for _ in range(weight)
    ]
    if not weighted:
        raise ValueError(f"scenario {scenario.key!r} has no archetype weights")
    while len(chosen) < count:
        chosen.append(rng.choice(weighted))
    # Keep out-00 as the scenario's first required path (normally usable) so
    # the explicit fallback is plausible, while making other tag order random.
    tail = chosen[1:]
    rng.shuffle(tail)
    return [chosen[0], *tail]


def _trend(archetype: str, progress: float) -> tuple[float, float]:
    """Return success and latency multipliers for trend-based profiles."""
    if archetype == "degrading":
        eased = progress * progress * (3.0 - 2.0 * progress)
        return 1.0 - 0.58 * eased, 1.0 + 7.0 * eased
    if archetype == "recovering":
        eased = progress * progress * (3.0 - 2.0 * progress)
        return 0.35 + 0.65 * eased, 6.0 - 5.0 * eased
    return 1.0, 1.0


def _temporal_effect(
    archetype: str,
    second: int,
    duration_s: int,
    phase_s: int,
    cycle_s: int,
    event_start: int,
    event_end: int,
) -> tuple[float, float]:
    """Return deterministic scheduled loss/latency effects for richer links."""
    position = (second + phase_s) % cycle_s
    if archetype == "periodic_blackouts":
        outage_s = max(4, cycle_s // 10)
        if position < outage_s:
            return 0.005, 2.5
    elif archetype == "mobile_handoffs":
        handoff_s = max(3, cycle_s // 18)
        if position < handoff_s:
            return 0.12, 6.0
        if position < handoff_s * 2:
            return 0.75, 2.5
    elif archetype == "peak_congestion":
        progress = second / max(1, duration_s - 1)
        # A broad busy-hour peak, plus a smaller repeating queue cycle.
        peak = math.exp(-((progress - 0.58) / 0.19) ** 2)
        queue = max(0.0, math.sin(2.0 * math.pi * position / cycle_s))
        return 1.0 - 0.24 * peak, 1.0 + 8.0 * peak + 1.5 * queue
    elif archetype == "brownouts" and event_start <= second < event_end:
        # The path still answers often enough to remain tempting to observers.
        return 0.42, 7.0
    elif archetype == "fast_then_dead" and second >= event_start:
        return 0.003, 3.0
    return 1.0, 1.0


def generate_world(
    seed: int,
    duration_s: int = 30 * 60,
    min_outbounds: int = 5,
    max_outbounds: int = 20,
    outbound_count: int | None = None,
    scenario_key: str = "mixed_real_world",
) -> SimulatedWorld:
    """Generate one 1 Hz, 30-minute-style unreliable outbound world."""
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    if not 1 <= min_outbounds <= max_outbounds:
        raise ValueError("outbound range must satisfy 1 <= min <= max")

    scenario = get_fleet_scenario(scenario_key)
    rng = random.Random(seed)
    count = outbound_count or rng.randint(min_outbounds, max_outbounds)
    if not min_outbounds <= count <= max_outbounds:
        raise ValueError("outbound_count is outside the configured range")

    archetypes = _choose_archetypes(rng, count, scenario)
    provider_count = max(
        1,
        min(5, math.ceil(count / scenario.provider_divisor)),
    )
    providers = [i % provider_count for i in range(count)]
    rng.shuffle(providers)

    global_incident = _incident_mask(
        rng,
        duration_s,
        scenario.global_incident_chance,
        *scenario.global_incident_duration_s,
    )
    provider_incidents = [
        _incident_mask(
            rng,
            duration_s,
            scenario.provider_incident_chance,
            *scenario.provider_incident_duration_s,
        )
        for _ in range(provider_count)
    ]

    traces: list[OutboundTrace] = []
    for index, archetype in enumerate(archetypes):
        profile = _profile_for(archetype)
        provider = providers[index]
        base_latency = rng.uniform(*profile.base_latency)
        in_bad_state = archetype == "mostly_dead" and rng.random() < 0.7
        correlated_noise = 0.0
        cycle_s = rng.randint(max(20, duration_s // 12), max(21, duration_s // 4))
        phase_s = rng.randrange(cycle_s)
        if archetype == "fast_then_dead":
            event_start = rng.randint(duration_s // 4, max(duration_s // 4, 3 * duration_s // 4))
            event_end = duration_s
        else:
            event_length = rng.randint(max(6, duration_s // 10), max(7, duration_s // 3))
            event_start = rng.randrange(max(1, duration_s - event_length + 1))
            event_end = min(duration_s, event_start + event_length)

        probabilities: list[float] = []
        expected_latencies: list[float] = []
        traffic_success: list[bool] = []
        traffic_latency: list[float | None] = []
        probe_success: list[bool] = []
        probe_latency: list[float | None] = []

        for t in range(duration_s):
            if in_bad_state:
                if profile.leave_bad > 0 and rng.random() < profile.leave_bad:
                    in_bad_state = False
            elif profile.enter_bad > 0 and rng.random() < profile.enter_bad:
                in_bad_state = True

            progress = t / max(1, duration_s - 1)
            success_trend, latency_trend = _trend(archetype, progress)
            temporal_success, temporal_latency = _temporal_effect(
                archetype,
                t,
                duration_s,
                phase_s,
                cycle_s,
                event_start,
                event_end,
            )
            probability = profile.bad_success if in_bad_state else profile.up_success
            probability *= success_trend * temporal_success
            latency_multiplier = profile.bad_latency_multiplier if in_bad_state else 1.0
            latency_multiplier *= latency_trend * temporal_latency

            if provider_incidents[provider][t]:
                probability *= scenario.provider_success_multiplier
                latency_multiplier *= scenario.provider_latency_multiplier
            if global_incident[t]:
                probability *= scenario.global_success_multiplier
                latency_multiplier *= scenario.global_latency_multiplier

            probability = min(1.0, max(0.0, probability))
            correlated_noise = 0.92 * correlated_noise + rng.gauss(0.0, profile.jitter_fraction)
            expected = base_latency * latency_multiplier * max(0.25, 1.0 + correlated_noise)
            if rng.random() < profile.spike_probability:
                expected *= rng.uniform(*profile.spike_multiplier)
            expected = max(5.0, min(5000.0, expected))

            traffic_ok = rng.random() < probability
            probe_ok = rng.random() < probability
            traffic_observed = (
                max(1.0, expected * math.exp(rng.gauss(0.0, 0.07)))
                if traffic_ok
                else None
            )
            probe_observed = (
                max(1.0, expected * math.exp(rng.gauss(0.0, 0.09)))
                if probe_ok
                else None
            )

            probabilities.append(probability)
            expected_latencies.append(expected)
            traffic_success.append(traffic_ok)
            traffic_latency.append(traffic_observed)
            probe_success.append(probe_ok)
            probe_latency.append(probe_observed)

        traces.append(
            OutboundTrace(
                tag=f"out-{index:02d}",
                archetype=archetype,
                provider=provider,
                success_probability=tuple(probabilities),
                expected_latency_ms=tuple(expected_latencies),
                traffic_success=tuple(traffic_success),
                traffic_latency_ms=tuple(traffic_latency),
                probe_success=tuple(probe_success),
                probe_latency_ms=tuple(probe_latency),
            )
        )

    return SimulatedWorld(
        seed=seed,
        duration_s=duration_s,
        scenario_key=scenario.key,
        scenario=scenario.label,
        outbounds=tuple(traces),
        global_incident=tuple(global_incident),
    )


def percentile(values: Sequence[float], percent: float) -> float:
    """Small dependency-free linear percentile implementation."""
    if not values:
        return math.nan
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction)
