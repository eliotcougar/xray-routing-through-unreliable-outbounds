"""Python translations of xray-core balancer selection algorithms.

Source pinned for this project:
https://github.com/XTLS/Xray-core/tree/6e3322d219140a025285ded1114fe17a5edb74d8/app/router
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import random
from typing import Protocol, Sequence

from .model import SimulatedWorld
from .observatory import (
    BurstObservatory,
    ClassicObservatory,
    ProbeSample,
    filter_alive_like_xray,
)


XRAY_CORE_COMMIT = "6e3322d219140a025285ded1114fe17a5edb74d8"
V2RAYNG_COMMIT = "ca79a574fa7cc93aac6d9cda2f2a43e05bda2b5d"
MIN_EXPERIMENT_PROBE_INTERVAL_S = 60
XRAY_RANK_MODE = "xray_deviation"
ALTERNATIVE_RANK_MODES = (
    "mean_latency",
    "success_rate",
    "expected_cost",
    "reliability_lcb",
    "history_guard",
    "history_guard_hysteresis",
    "latest_health_hysteresis",
)


class Strategy(Protocol):
    label: str

    def tick(self, second: int) -> None: ...

    def pick(self, second: int, candidates: Sequence[str]) -> tuple[str, bool]: ...


@dataclass(frozen=True)
class StrategySpec:
    key: str
    label: str
    kind: str
    probe_interval_s: int | None = None
    health_filter: bool = False
    # Xray burst-observatory defaults from NewHealthPing.
    burst_interval_s: int = 60
    sampling_count: int = 10
    probe_timeout_s: int = 5
    probe_url: str = ""
    probe_http_method: str = ""
    # Zero is the protobuf/config default. leastLoad internally treats it as 1.
    expected: int = 0
    baselines_ms: tuple[float, ...] = ()
    max_rtt_ms: float = 0.0
    tolerance: float = 0.0
    costs: tuple[tuple[str, float], ...] = ()
    # The exact Xray selector ranks deviation first. Alternative modes consume
    # the same HealthPingMeasurementResult fields in different orders/scores.
    rank_mode: str = XRAY_RANK_MODE
    failure_cost_ms: float = 5000.0
    history_state_half_life_s: float = 180.0
    history_prior_strength: float = 2.0
    history_probability_margin: float = 0.0
    history_switch_probability_margin: float = 0.05
    history_hysteresis_ms: float = 25.0
    history_hysteresis_ratio: float = 0.20
    history_min_dwell_s: int = 20
    # Simplified raw-history selector: newest result decides health, while
    # latency hysteresis and a minimum dwell suppress needless route churn.
    latest_hysteresis_ms: float = 25.0
    latest_hysteresis_ratio: float = 0.20
    latest_min_dwell_s: int = 20

    @property
    def observer_interval_s(self) -> int | None:
        if self.kind == "leastload":
            return self.burst_interval_s
        if self.kind == "leastping" or self.health_filter:
            # A missing classic-observatory interval means Xray's native 10s.
            # Resolve it here so the experiment's 60s floor cannot be bypassed.
            return self.probe_interval_s if self.probe_interval_s is not None else 10
        return None


def validate_experiment_probe_floor(
    specs: Sequence[StrategySpec],
    minimum_s: int = MIN_EXPERIMENT_PROBE_INTERVAL_S,
) -> None:
    """Reject experiment matrices that probe faster than the requested floor."""
    invalid = [
        f"{spec.key}={spec.observer_interval_s}s"
        for spec in specs
        if spec.observer_interval_s is not None
        and spec.observer_interval_s < minimum_s
    ]
    if invalid:
        raise ValueError(
            f"experiment probe intervals must be >= {minimum_s}s: "
            + ", ".join(invalid)
        )


@dataclass
class RandomStrategy:
    label: str
    rng: random.Random
    fallback_tag: str
    observer: ClassicObservatory | None = None

    def tick(self, second: int) -> None:
        if self.observer is not None:
            self.observer.tick(second)

    def pick(self, second: int, candidates: Sequence[str]) -> tuple[str, bool]:
        eligible = list(candidates)
        if self.observer is not None:
            eligible = filter_alive_like_xray(eligible, self.observer.statuses())
        if not eligible:
            return self.fallback_tag, True
        return self.rng.choice(eligible), False


@dataclass
class RoundRobinStrategy:
    label: str
    fallback_tag: str
    observer: ClassicObservatory | None = None
    index: int = 0

    def tick(self, second: int) -> None:
        if self.observer is not None:
            self.observer.tick(second)

    def pick(self, second: int, candidates: Sequence[str]) -> tuple[str, bool]:
        eligible = list(candidates)
        if self.observer is not None:
            eligible = filter_alive_like_xray(eligible, self.observer.statuses())
        if not eligible:
            return self.fallback_tag, True
        selected = eligible[self.index % len(eligible)]
        self.index = (self.index + 1) % len(eligible)
        return selected, False


@dataclass
class LeastPingStrategy:
    label: str
    fallback_tag: str
    observer: ClassicObservatory

    def tick(self, second: int) -> None:
        self.observer.tick(second)

    def pick(self, second: int, candidates: Sequence[str]) -> tuple[str, bool]:
        candidate_set = set(candidates)
        least_ping = 99_999_999.0
        selected = ""
        for status in self.observer.statuses():
            if status.tag in candidate_set and status.alive and status.delay_ms < least_ping:
                selected = status.tag
                least_ping = status.delay_ms
        if not selected:
            return self.fallback_tag, True
        return selected, False


@dataclass(frozen=True)
class _LeastLoadNode:
    tag: str
    sample_count: int
    failure_count: int
    average_ms: float
    deviation_ms: float
    deviation_cost_ms: float

    @property
    def failure_rate(self) -> float:
        return self.failure_count / self.sample_count if self.sample_count else 1.0

    @property
    def success_rate(self) -> float:
        return 1.0 - self.failure_rate


@dataclass(frozen=True)
class _HistoryNode:
    tag: str
    next_success_probability: float
    latency_ms: float
    latest_success: bool
    latest_age_s: int
    sample_count: int
    failure_count: int


@dataclass(frozen=True)
class _LatestHealthNode:
    tag: str
    latency_ms: float
    completed_second: int


@dataclass
class LeastLoadStrategy:
    label: str
    fallback_tag: str
    observer: BurstObservatory
    rng: random.Random
    expected: int = 1
    baselines_ms: tuple[float, ...] = ()
    max_rtt_ms: float = 0.0
    tolerance: float = 0.0
    costs: tuple[tuple[str, float], ...] = ()
    rank_mode: str = XRAY_RANK_MODE
    failure_cost_ms: float = 5000.0
    history_state_half_life_s: float = 180.0
    history_prior_strength: float = 2.0
    history_probability_margin: float = 0.0
    history_switch_probability_margin: float = 0.05
    history_hysteresis_ms: float = 25.0
    history_hysteresis_ratio: float = 0.20
    history_min_dwell_s: int = 20
    latest_hysteresis_ms: float = 25.0
    latest_hysteresis_ratio: float = 0.20
    latest_min_dwell_s: int = 20
    selected_tag: str | None = None
    selected_since_s: int | None = None

    def tick(self, second: int) -> None:
        self.observer.tick(second)

    def _cost(self, tag: str) -> float:
        for match, cost in self.costs:
            if match in tag and cost > 0:
                return cost
        return 1.0

    def _expected_cost(self, node: _LeastLoadNode) -> float:
        return (
            node.success_rate * node.average_ms
            + node.failure_rate * self.failure_cost_ms
        )

    @staticmethod
    def _wilson_success_lower_bound(node: _LeastLoadNode) -> float:
        """95% Wilson lower bound using the retained probe outcomes only."""
        count = node.sample_count
        if count <= 0:
            return 0.0
        probability = node.success_rate
        z = 1.96
        z_squared = z * z
        denominator = 1.0 + z_squared / count
        center = probability + z_squared / (2.0 * count)
        margin = z * math.sqrt(
            probability * (1.0 - probability) / count
            + z_squared / (4.0 * count * count)
        )
        return (center - margin) / denominator

    def _rank_key(self, node: _LeastLoadNode) -> tuple[object, ...]:
        if self.rank_mode == XRAY_RANK_MODE:
            # Keep this tuple byte-for-byte conceptually aligned with
            # strategy_leastload.go: deviation*sqrt(cost), average, failures,
            # sample count descending, tag.
            return (
                node.deviation_cost_ms,
                node.average_ms,
                node.failure_count,
                -node.sample_count,
                node.tag,
            )
        if self.rank_mode == "mean_latency":
            return (
                node.average_ms,
                node.failure_rate,
                node.deviation_cost_ms,
                node.failure_count,
                -node.sample_count,
                node.tag,
            )
        if self.rank_mode == "success_rate":
            return (
                node.failure_rate,
                node.average_ms,
                node.deviation_cost_ms,
                -node.sample_count,
                node.tag,
            )
        if self.rank_mode == "expected_cost":
            return (
                self._expected_cost(node),
                node.failure_rate,
                node.deviation_cost_ms,
                node.average_ms,
                -node.sample_count,
                node.tag,
            )
        if self.rank_mode == "reliability_lcb":
            return (
                -self._wilson_success_lower_bound(node),
                self._expected_cost(node),
                node.average_ms,
                node.deviation_cost_ms,
                node.tag,
            )
        raise ValueError(f"unsupported leastLoad-like rank mode: {self.rank_mode}")

    @staticmethod
    def _bayes_observation_update(probability: float, success: bool) -> float:
        """Update an up/down belief from one noisy HTTP probe result."""
        if success:
            likelihood_up = 0.985
            likelihood_down = 0.12
        else:
            likelihood_up = 0.02
            likelihood_down = 0.88
        numerator = probability * likelihood_up
        denominator = numerator + (1.0 - probability) * likelihood_down
        return numerator / denominator if denominator else probability

    def _history_probability(
        self,
        samples: Sequence[ProbeSample],
        second: int,
        pool_success_rate: float,
    ) -> float:
        successes = sum(sample.success for sample in samples)
        strength = max(0.0, self.history_prior_strength)
        base = (
            strength * pool_success_rate + successes
        ) / (strength + len(samples))
        base = min(0.995, max(0.005, base))
        half_life = max(1.0, self.history_state_half_life_s)

        probability = base
        previous_second = samples[0].completed_second
        for sample in samples:
            elapsed = max(0, sample.completed_second - previous_second)
            persistence = math.exp(-math.log(2.0) * elapsed / half_life)
            probability = base + (probability - base) * persistence
            probability = self._bayes_observation_update(
                probability,
                sample.success,
            )
            previous_second = sample.completed_second

        age = max(0, second - previous_second)
        persistence = math.exp(-math.log(2.0) * age / half_life)
        return base + (probability - base) * persistence

    def _history_latency(
        self,
        samples: Sequence[ProbeSample],
        second: int,
    ) -> float:
        half_life = max(1.0, self.history_state_half_life_s)
        weighted_sum = 0.0
        total_weight = 0.0
        for sample in samples:
            if sample.latency_ms is None:
                continue
            age = max(0, second - sample.completed_second)
            weight = math.exp(-math.log(2.0) * age / half_life)
            weighted_sum += sample.latency_ms * weight
            total_weight += weight
        return weighted_sum / total_weight if total_weight else self.failure_cost_ms

    def _history_guard_nodes(
        self,
        second: int,
        candidates: Sequence[str],
        apply_probability_filter: bool = True,
    ) -> list[_HistoryNode]:
        histories = self.observer.histories(second)
        candidate_set = set(candidates)
        all_samples = [
            sample
            for tag, samples in histories.items()
            if tag in candidate_set
            for sample in samples
        ]
        if not all_samples:
            return []
        pool_success_rate = (
            1.0 + sum(sample.success for sample in all_samples)
        ) / (2.0 + len(all_samples))

        nodes: list[_HistoryNode] = []
        for tag in candidates:
            samples = histories.get(tag, ())
            if not samples or not any(sample.success for sample in samples):
                continue
            failures = sum(not sample.success for sample in samples)
            if (
                self.tolerance > 0
                and failures / len(samples) > self.tolerance
            ):
                continue
            latency = self._history_latency(samples, second)
            if self.max_rtt_ms != 0 and latency >= self.max_rtt_ms:
                continue
            nodes.append(
                _HistoryNode(
                    tag=tag,
                    next_success_probability=self._history_probability(
                        samples,
                        second,
                        pool_success_rate,
                    ),
                    latency_ms=latency,
                    latest_success=samples[-1].success,
                    latest_age_s=max(0, second - samples[-1].completed_second),
                    sample_count=len(samples),
                    failure_count=failures,
                )
            )
        if not nodes:
            return []

        # A latest failure opens a circuit. Do not route through it while any
        # candidate's most recent completed probe succeeded.
        latest_successes = [node for node in nodes if node.latest_success]
        if latest_successes:
            nodes = latest_successes

        if apply_probability_filter:
            best_probability = max(node.next_success_probability for node in nodes)
            margin = max(0.0, self.history_probability_margin)
            nodes = [
                node
                for node in nodes
                if node.next_success_probability >= best_probability - margin
            ]
        nodes.sort(
            key=lambda node: (
                self._history_expected_cost(node),
                -node.next_success_probability,
                node.latest_age_s,
                node.failure_count,
                -node.sample_count,
                node.tag,
            )
        )
        return nodes

    def _history_expected_cost(self, node: _HistoryNode) -> float:
        return (
            node.next_success_probability * node.latency_ms
            + (1.0 - node.next_success_probability) * self.failure_cost_ms
        )

    def _pick_history_guard_hysteresis(
        self,
        second: int,
        candidates: Sequence[str],
    ) -> tuple[str, bool]:
        preferred = self._history_guard_nodes(second, candidates)
        eligible = self._history_guard_nodes(
            second,
            candidates,
            apply_probability_filter=False,
        )
        if not preferred or not eligible:
            self.selected_tag = None
            self.selected_since_s = None
            return self.fallback_tag, True

        by_tag = {node.tag: node for node in eligible}
        best = preferred[0]
        current = by_tag.get(self.selected_tag or "")

        # A newest failure removes the current route from ``eligible`` and
        # therefore bypasses both dwell and improvement hysteresis.
        if current is None:
            self.selected_tag = best.tag
            self.selected_since_s = second
            return best.tag, False
        if best.tag == current.tag:
            return current.tag, False

        dwell_start = (
            self.selected_since_s
            if self.selected_since_s is not None
            else second
        )
        if second - dwell_start < max(0, self.history_min_dwell_s):
            return current.tag, False

        probability_gain = (
            best.next_success_probability - current.next_success_probability
        )
        decisive_reliability_gain = (
            probability_gain
            >= max(0.0, self.history_switch_probability_margin)
        )
        current_cost = self._history_expected_cost(current)
        cost_improvement = current_cost - self._history_expected_cost(best)
        required_cost_improvement = max(
            max(0.0, self.history_hysteresis_ms),
            current_cost * max(0.0, self.history_hysteresis_ratio),
        )
        if (
            decisive_reliability_gain
            or cost_improvement >= required_cost_improvement
        ):
            self.selected_tag = best.tag
            self.selected_since_s = second

        return self.selected_tag or current.tag, False

    def _latest_health_nodes(
        self,
        second: int,
        candidates: Sequence[str],
    ) -> list[_LatestHealthNode]:
        """Return candidates whose newest completed raw probe succeeded."""
        histories = self.observer.histories(second)
        nodes: list[_LatestHealthNode] = []
        for tag in candidates:
            samples = histories.get(tag, ())
            if not samples:
                continue
            latest = samples[-1]
            if not latest.success or latest.latency_ms is None:
                continue
            if self.max_rtt_ms != 0 and latest.latency_ms >= self.max_rtt_ms:
                continue
            nodes.append(
                _LatestHealthNode(
                    tag=tag,
                    latency_ms=latest.latency_ms,
                    completed_second=latest.completed_second,
                )
            )
        nodes.sort(
            key=lambda node: (
                node.latency_ms,
                -node.completed_second,
                node.tag,
            )
        )
        return nodes

    def _pick_latest_health_hysteresis(
        self,
        second: int,
        candidates: Sequence[str],
    ) -> tuple[str, bool]:
        healthy = self._latest_health_nodes(second, candidates)
        if not healthy:
            self.selected_tag = None
            self.selected_since_s = None
            return self.fallback_tag, True

        by_tag = {node.tag: node for node in healthy}
        current = by_tag.get(self.selected_tag or "")
        best = healthy[0]

        # The newest failure (or loss of eligibility) overrides hysteresis.
        if current is None:
            self.selected_tag = best.tag
            self.selected_since_s = second
            return best.tag, False

        if best.tag != current.tag:
            dwell_start = (
                self.selected_since_s
                if self.selected_since_s is not None
                else second
            )
            dwell_elapsed = second - dwell_start
            improvement = current.latency_ms - best.latency_ms
            required_improvement = max(
                max(0.0, self.latest_hysteresis_ms),
                current.latency_ms * max(0.0, self.latest_hysteresis_ratio),
            )
            if (
                dwell_elapsed >= max(0, self.latest_min_dwell_s)
                and improvement >= required_improvement
            ):
                self.selected_tag = best.tag
                self.selected_since_s = second

        return self.selected_tag or best.tag, False

    def _qualified_nodes(self, second: int, candidates: Sequence[str]) -> list[_LeastLoadNode]:
        candidate_set = set(candidates)
        nodes: list[_LeastLoadNode] = []
        for status in self.observer.statuses(second):
            if not status.alive or status.tag not in candidate_set:
                continue
            if self.max_rtt_ms != 0 and status.delay_ms >= self.max_rtt_ms:
                continue
            if (
                status.sample_count > 0
                and self.tolerance > 0
                and status.failure_count / status.sample_count > self.tolerance
            ):
                continue
            nodes.append(
                _LeastLoadNode(
                    tag=status.tag,
                    sample_count=status.sample_count,
                    failure_count=status.failure_count,
                    average_ms=status.average_ms,
                    deviation_ms=status.deviation_ms,
                    # xray-core multiplies by sqrt(cost).
                    deviation_cost_ms=status.deviation_ms * math.sqrt(self._cost(status.tag)),
                )
            )
        nodes.sort(key=self._rank_key)
        return nodes

    def _select_least_load(self, nodes: list[_LeastLoadNode]) -> list[_LeastLoadNode]:
        if not nodes:
            return []
        expected = self.expected
        available_count = len(nodes)
        if expected > available_count:
            return nodes
        if expected <= 0:
            expected = 1
        if not self.baselines_ms:
            return nodes[:expected]

        count = 0
        for baseline in self.baselines_ms:
            for index in range(count, available_count):
                if nodes[index].deviation_cost_ms >= baseline:
                    break
                count = index + 1
            if count >= expected:
                break
        if self.expected > 0 and count < expected:
            count = expected
        return nodes[:count]

    def pick(self, second: int, candidates: Sequence[str]) -> tuple[str, bool]:
        if self.rank_mode == "history_guard":
            selected = self._history_guard_nodes(second, candidates)
            if not selected:
                return self.fallback_tag, True
            return selected[0].tag, False
        if self.rank_mode == "history_guard_hysteresis":
            return self._pick_history_guard_hysteresis(second, candidates)
        if self.rank_mode == "latest_health_hysteresis":
            return self._pick_latest_health_hysteresis(second, candidates)
        selected = self._select_least_load(self._qualified_nodes(second, candidates))
        if not selected:
            return self.fallback_tag, True
        return self.rng.choice(selected).tag, False


def build_strategy(
    spec: StrategySpec,
    world: SimulatedWorld,
    rng: random.Random,
    fallback_tag: str,
    observer_rng: random.Random | None = None,
) -> Strategy:
    if spec.kind == "random":
        observer = (
            ClassicObservatory(
                world,
                spec.probe_interval_s or 10,
                spec.probe_timeout_s,
            )
            if spec.health_filter
            else None
        )
        # In xray-core the RandomStrategy only receives an observatory when its
        # FallbackTag is non-empty.  Blind mode therefore also has no fallback.
        return RandomStrategy(
            spec.label,
            rng,
            fallback_tag if spec.health_filter else "",
            observer,
        )
    if spec.kind == "roundrobin":
        observer = (
            ClassicObservatory(
                world,
                spec.probe_interval_s or 10,
                spec.probe_timeout_s,
            )
            if spec.health_filter
            else None
        )
        return RoundRobinStrategy(
            spec.label,
            fallback_tag if spec.health_filter else "",
            observer,
        )
    if spec.kind == "leastping":
        observer = ClassicObservatory(
            world,
            spec.probe_interval_s or 10,
            spec.probe_timeout_s,
        )
        return LeastPingStrategy(spec.label, fallback_tag, observer)
    if spec.kind == "leastload":
        schedule_rng = observer_rng if observer_rng is not None else rng
        observer = BurstObservatory(
            world,
            spec.burst_interval_s,
            spec.sampling_count,
            schedule_rng,
            spec.probe_timeout_s,
        )
        return LeastLoadStrategy(
            label=spec.label,
            fallback_tag=fallback_tag,
            observer=observer,
            rng=rng,
            expected=spec.expected,
            baselines_ms=spec.baselines_ms,
            max_rtt_ms=spec.max_rtt_ms,
            tolerance=spec.tolerance,
            costs=spec.costs,
            rank_mode=spec.rank_mode,
            failure_cost_ms=spec.failure_cost_ms,
            history_state_half_life_s=spec.history_state_half_life_s,
            history_prior_strength=spec.history_prior_strength,
            history_probability_margin=spec.history_probability_margin,
            history_switch_probability_margin=spec.history_switch_probability_margin,
            history_hysteresis_ms=spec.history_hysteresis_ms,
            history_hysteresis_ratio=spec.history_hysteresis_ratio,
            history_min_dwell_s=spec.history_min_dwell_s,
            latest_hysteresis_ms=spec.latest_hysteresis_ms,
            latest_hysteresis_ratio=spec.latest_hysteresis_ratio,
            latest_min_dwell_s=spec.latest_min_dwell_s,
        )
    raise ValueError(f"unsupported strategy kind: {spec.kind}")


def default_strategy_specs() -> tuple[StrategySpec, ...]:
    """Source-faithful defaults plus 60/120/300-second observer sweeps.

    The strategy defaults are preserved, while the classic observatory's
    upstream 10-second implicit default is intentionally not benchmarked: this
    experiment has a user-requested 60-second minimum interval.
    """
    specs = (
        StrategySpec("random_blind", "random / no health", "random"),
        StrategySpec(
            "random_health_60s", "random / health 60s", "random",
            probe_interval_s=60, health_filter=True,
        ),
        StrategySpec(
            "random_health_120s", "random / health 120s", "random",
            probe_interval_s=120, health_filter=True,
        ),
        StrategySpec(
            "random_health_300s", "random / health 300s", "random",
            probe_interval_s=300, health_filter=True,
        ),
        StrategySpec("roundrobin_blind", "roundRobin / no health", "roundrobin"),
        StrategySpec(
            "roundrobin_health_60s", "roundRobin / health 60s", "roundrobin",
            probe_interval_s=60, health_filter=True,
        ),
        StrategySpec(
            "roundrobin_health_120s", "roundRobin / health 120s", "roundrobin",
            probe_interval_s=120, health_filter=True,
        ),
        StrategySpec(
            "roundrobin_health_300s", "roundRobin / health 300s", "roundrobin",
            probe_interval_s=300, health_filter=True,
        ),
        StrategySpec(
            "leastping_default_60s", "leastPing / default strategy / 60s", "leastping",
            probe_interval_s=60,
        ),
        StrategySpec(
            "leastping_default_120s", "leastPing / default strategy / 120s", "leastping",
            probe_interval_s=120,
        ),
        StrategySpec(
            "leastping_default_300s", "leastPing / default strategy / 300s", "leastping",
            probe_interval_s=300,
        ),
        StrategySpec(
            "leastping_v2rayng_default_180s", "leastPing / v2rayNG default / 180s", "leastping",
            probe_interval_s=180, probe_timeout_s=5,
            probe_url="https://www.gstatic.com/generate_204",
            probe_http_method="GET",
        ),
        StrategySpec(
            "leastload_default_60s", "leastLoad / default strategy settings / 60s", "leastload",
            burst_interval_s=60, sampling_count=10, expected=0,
        ),
        StrategySpec(
            "leastload_tolerance_10_60s", "leastLoad / tolerance 10% / 60s", "leastload",
            burst_interval_s=60, sampling_count=10, expected=0, tolerance=0.10,
        ),
        StrategySpec(
            "leastload_tolerance_20_60s", "leastLoad / tolerance 20% / 60s", "leastload",
            burst_interval_s=60, sampling_count=10, expected=0, tolerance=0.20,
        ),
        StrategySpec(
            "leastload_tolerance_30_60s", "leastLoad / tolerance 30% / 60s", "leastload",
            burst_interval_s=60, sampling_count=10, expected=0, tolerance=0.30,
        ),
        StrategySpec(
            "leastload_tolerance_40_60s", "leastLoad / tolerance 40% / 60s", "leastload",
            burst_interval_s=60, sampling_count=10, expected=0, tolerance=0.40,
        ),
        StrategySpec(
            "leastload_tolerance_50_60s", "leastLoad / tolerance 50% / 60s", "leastload",
            burst_interval_s=60, sampling_count=10, expected=0, tolerance=0.50,
        ),
        StrategySpec(
            "leastload_tolerance_70_60s", "leastLoad / tolerance 70% / 60s", "leastload",
            burst_interval_s=60, sampling_count=10, expected=0, tolerance=0.70,
        ),
        StrategySpec(
            "leastload_tolerance_100_60s", "leastLoad / tolerance 100% / 60s", "leastload",
            burst_interval_s=60, sampling_count=10, expected=0, tolerance=1.0,
        ),
        StrategySpec(
            "leastload_default_120s", "leastLoad / default strategy settings / 120s", "leastload",
            burst_interval_s=120, sampling_count=10, expected=0,
        ),
        StrategySpec(
            "leastload_default_300s", "leastLoad / default strategy settings / 300s", "leastload",
            burst_interval_s=300, sampling_count=10, expected=0,
        ),
        StrategySpec(
            "leastload_v2rayng_burst_300s", "leastLoad / v2rayNG burst default / 300s", "leastload",
            burst_interval_s=300, sampling_count=2, probe_timeout_s=30,
            probe_url="https://www.gstatic.com/generate_204",
            probe_http_method="HEAD", expected=0,
        ),
        StrategySpec(
            "leastload_mean_latency_60s", "leastLoad-like / mean latency first / 60s", "leastload",
            burst_interval_s=60, sampling_count=10, expected=1,
            rank_mode="mean_latency",
        ),
        StrategySpec(
            "leastload_success_rate_60s", "leastLoad-like / success rate first / 60s", "leastload",
            burst_interval_s=60, sampling_count=10, expected=1,
            rank_mode="success_rate",
        ),
        StrategySpec(
            "leastload_expected_cost_60s", "leastLoad-like / expected outcome / 60s", "leastload",
            burst_interval_s=60, sampling_count=10, expected=1,
            rank_mode="expected_cost", failure_cost_ms=5000.0,
        ),
        StrategySpec(
            "leastload_reliability_lcb_60s", "leastLoad-like / reliability confidence / 60s", "leastload",
            burst_interval_s=60, sampling_count=10, expected=1,
            rank_mode="reliability_lcb", failure_cost_ms=5000.0,
        ),
        StrategySpec(
            "leastload_history_guard_60s", "leastLoad-like / history guard / 60s", "leastload",
            burst_interval_s=60, sampling_count=10, expected=1,
            rank_mode="history_guard", failure_cost_ms=5000.0,
            history_state_half_life_s=180.0,
            history_prior_strength=2.0,
            history_probability_margin=0.0,
        ),
        StrategySpec(
            "leastload_history_guard_hysteresis_60s",
            "leastLoad-like / history guard + hysteresis / 60s",
            "leastload",
            burst_interval_s=60,
            sampling_count=10,
            expected=1,
            rank_mode="history_guard_hysteresis",
            failure_cost_ms=5000.0,
            history_state_half_life_s=180.0,
            history_prior_strength=2.0,
            history_probability_margin=0.0,
            history_switch_probability_margin=0.05,
            history_hysteresis_ms=25.0,
            history_hysteresis_ratio=0.20,
            history_min_dwell_s=20,
        ),
        StrategySpec(
            "leastload_latest_hysteresis_60s",
            "leastLoad-like / latest health + hysteresis / 60s",
            "leastload",
            burst_interval_s=60,
            sampling_count=10,
            expected=1,
            rank_mode="latest_health_hysteresis",
            latest_hysteresis_ms=25.0,
            latest_hysteresis_ratio=0.20,
            latest_min_dwell_s=20,
        ),
        StrategySpec(
            "leastload_tuned_e2_60s", "leastLoad / e2 s5 tol40 / 60s", "leastload",
            burst_interval_s=60, sampling_count=5, expected=2,
            tolerance=0.40, max_rtt_ms=800,
        ),
    )
    validate_experiment_probe_floor(specs)
    return specs
