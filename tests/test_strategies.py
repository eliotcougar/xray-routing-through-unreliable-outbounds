from __future__ import annotations

import random
import unittest

from xray_strategy_sim.observatory import (
    Observation,
    ProbeSample,
    filter_alive_like_xray,
)
from xray_strategy_sim.strategies import (
    LeastLoadStrategy,
    LeastPingStrategy,
    RoundRobinStrategy,
    StrategySpec,
    default_strategy_specs,
    validate_experiment_probe_floor,
)


class StubClassic:
    def __init__(self, statuses):
        self._statuses = tuple(statuses)

    def tick(self, second: int) -> None:
        pass

    def statuses(self):
        return self._statuses


class StubBurst(StubClassic):
    def statuses(self, second: int):
        return self._statuses


class StubHistoryBurst(StubBurst):
    def __init__(self, histories):
        super().__init__(())
        self._histories = histories

    def histories(self, second: int):
        return self._histories


class StrategyTests(unittest.TestCase):
    def test_default_matrix_has_required_modes_and_sixty_second_floor(self) -> None:
        specs = default_strategy_specs()
        self.assertTrue(any(spec.key == "leastping_default_60s" for spec in specs))
        self.assertTrue(any(spec.key == "leastload_default_60s" for spec in specs))
        self.assertTrue(any(spec.key == "random_blind" for spec in specs))
        self.assertTrue(any(spec.key == "roundrobin_blind" for spec in specs))
        self.assertTrue(any(spec.key == "random_health_120s" for spec in specs))
        self.assertTrue(any(spec.key == "roundrobin_health_300s" for spec in specs))
        self.assertTrue(any(spec.key == "leastload_mean_latency_60s" for spec in specs))
        self.assertTrue(any(spec.key == "leastload_success_rate_60s" for spec in specs))
        self.assertTrue(any(spec.key == "leastload_expected_cost_60s" for spec in specs))
        self.assertTrue(any(spec.key == "leastload_reliability_lcb_60s" for spec in specs))
        self.assertTrue(any(spec.key == "leastload_history_guard_60s" for spec in specs))
        self.assertTrue(any(spec.key == "leastload_history_guard_hysteresis_60s" for spec in specs))
        self.assertTrue(any(spec.key == "leastload_latest_hysteresis_60s" for spec in specs))
        tolerance_specs = [
            spec
            for spec in specs
            if spec.key.startswith("leastload_tolerance_")
        ]
        self.assertEqual(
            [spec.tolerance for spec in tolerance_specs],
            [0.10, 0.20, 0.30, 0.40, 0.50, 0.70, 1.0],
        )
        self.assertTrue(
            all(
                spec.observer_interval_s is None or spec.observer_interval_s >= 60
                for spec in specs
            )
        )

    def test_v2rayng_profiles_match_current_app_defaults(self) -> None:
        specs = {spec.key: spec for spec in default_strategy_specs()}
        least_ping = specs["leastping_v2rayng_default_180s"]
        self.assertEqual(least_ping.probe_interval_s, 180)
        self.assertEqual(least_ping.probe_timeout_s, 5)
        self.assertEqual(least_ping.probe_url, "https://www.gstatic.com/generate_204")
        self.assertEqual(least_ping.probe_http_method, "GET")

        least_load = specs["leastload_v2rayng_burst_300s"]
        self.assertEqual(least_load.burst_interval_s, 300)
        self.assertEqual(least_load.sampling_count, 2)
        self.assertEqual(least_load.probe_timeout_s, 30)
        self.assertEqual(least_load.probe_url, "https://www.gstatic.com/generate_204")
        self.assertEqual(least_load.probe_http_method, "HEAD")
        self.assertEqual(least_load.expected, 0)

    def test_probe_floor_rejects_faster_matrix(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be >= 60s"):
            validate_experiment_probe_floor(
                (StrategySpec("too_fast", "too fast", "leastping", probe_interval_s=30),)
            )
        with self.assertRaisesRegex(ValueError, "must be >= 60s"):
            validate_experiment_probe_floor(
                (StrategySpec("implicit_ten", "implicit", "leastping"),)
            )

    def test_random_roundrobin_filter_treats_unknown_as_alive(self) -> None:
        statuses = [Observation("a", False, 99_999_999)]
        self.assertEqual(filter_alive_like_xray(["a", "b"], statuses), ["b"])

    def test_leastping_selects_strict_lowest_alive(self) -> None:
        observer = StubClassic(
            [
                Observation("dead", False, 1),
                Observation("slow", True, 200),
                Observation("fast", True, 40),
            ]
        )
        strategy = LeastPingStrategy("test", "fallback", observer)  # type: ignore[arg-type]
        self.assertEqual(strategy.pick(0, ["dead", "slow", "fast"]), ("fast", False))

    def test_roundrobin_index_matches_current_alive_list(self) -> None:
        observer = StubClassic([Observation("a", True, 10), Observation("b", True, 20)])
        strategy = RoundRobinStrategy("test", "fallback", observer)  # type: ignore[arg-type]
        self.assertEqual(strategy.pick(0, ["a", "b"])[0], "a")
        self.assertEqual(strategy.pick(1, ["a", "b"])[0], "b")
        observer._statuses = (Observation("a", False, 99_999_999), Observation("b", True, 20))
        self.assertEqual(strategy.pick(2, ["a", "b"])[0], "b")

    def test_leastload_sorts_deviation_before_average(self) -> None:
        observer = StubBurst(
            [
                Observation("fast-noisy", True, 20, 5, 0, 20, 15),
                Observation("slow-stable", True, 200, 5, 0, 200, 2),
            ]
        )
        strategy = LeastLoadStrategy(
            "test", "fallback", observer, random.Random(1), expected=1  # type: ignore[arg-type]
        )
        self.assertEqual(strategy.pick(0, ["fast-noisy", "slow-stable"]), ("slow-stable", False))

    def test_leastload_tolerance_is_strictly_greater_than(self) -> None:
        observer = StubBurst(
            [
                Observation("at-limit", True, 50, 5, 2, 50, 5),
                Observation("over-limit", True, 40, 5, 3, 40, 1),
            ]
        )
        strategy = LeastLoadStrategy(
            "test", "fallback", observer, random.Random(1), expected=1, tolerance=0.4  # type: ignore[arg-type]
        )
        self.assertEqual(strategy.pick(0, ["at-limit", "over-limit"]), ("at-limit", False))

    def test_alternative_rankings_reorder_the_same_burst_statistics(self) -> None:
        observer = StubBurst(
            [
                Observation("fast-flaky", True, 40, 10, 5, 40, 2),
                Observation("slow-reliable", True, 100, 10, 0, 100, 10),
            ]
        )
        candidates = ["fast-flaky", "slow-reliable"]

        xray = LeastLoadStrategy(
            "xray", "fallback", observer, random.Random(1), rank_mode="xray_deviation"  # type: ignore[arg-type]
        )
        mean = LeastLoadStrategy(
            "mean", "fallback", observer, random.Random(1), rank_mode="mean_latency"  # type: ignore[arg-type]
        )
        success = LeastLoadStrategy(
            "success", "fallback", observer, random.Random(1), rank_mode="success_rate"  # type: ignore[arg-type]
        )
        expected = LeastLoadStrategy(
            "expected", "fallback", observer, random.Random(1), rank_mode="expected_cost"  # type: ignore[arg-type]
        )

        self.assertEqual(xray.pick(0, candidates)[0], "fast-flaky")
        self.assertEqual(mean.pick(0, candidates)[0], "fast-flaky")
        self.assertEqual(success.pick(0, candidates)[0], "slow-reliable")
        self.assertEqual(expected.pick(0, candidates)[0], "slow-reliable")

    def test_reliability_confidence_penalizes_tiny_perfect_samples(self) -> None:
        observer = StubBurst(
            [
                Observation("one-of-one", True, 20, 1, 0, 20, 10),
                Observation("nine-of-ten", True, 100, 10, 1, 100, 10),
            ]
        )
        strategy = LeastLoadStrategy(
            "confidence",
            "fallback",
            observer,  # type: ignore[arg-type]
            random.Random(1),
            rank_mode="reliability_lcb",
        )
        self.assertEqual(
            strategy.pick(0, ["one-of-one", "nine-of-ten"]),
            ("nine-of-ten", False),
        )

    def test_history_guard_opens_circuit_after_latest_failure(self) -> None:
        observer = StubHistoryBurst(
            {
                "formerly-good": tuple(
                    [ProbeSample(second, 30.0) for second in range(10, 100, 10)]
                    + [ProbeSample(100, None)]
                ),
                "working": (
                    ProbeSample(80, 120.0),
                    ProbeSample(99, 125.0),
                ),
            }
        )
        strategy = LeastLoadStrategy(
            "history",
            "fallback",
            observer,  # type: ignore[arg-type]
            random.Random(1),
            rank_mode="history_guard",
        )
        self.assertEqual(
            strategy.pick(100, ["formerly-good", "working"]),
            ("working", False),
        )

    def test_history_guard_prefers_fresh_and_reliably_successful_evidence(self) -> None:
        observer = StubHistoryBurst(
            {
                "stale-perfect": tuple(
                    ProbeSample(second, 20.0) for second in range(1, 11)
                ),
                "fresh-perfect": tuple(
                    ProbeSample(second, 100.0) for second in range(81, 101, 2)
                ),
                "fresh-flaky": tuple(
                    ProbeSample(81 + index * 2, 15.0 if index % 2 else None)
                    for index in range(10)
                ),
            }
        )
        strategy = LeastLoadStrategy(
            "history",
            "fallback",
            observer,  # type: ignore[arg-type]
            random.Random(1),
            rank_mode="history_guard",
        )
        self.assertEqual(
            strategy.pick(
                101,
                ["stale-perfect", "fresh-perfect", "fresh-flaky"],
            ),
            ("fresh-perfect", False),
        )

    def test_latest_health_hysteresis_escapes_newest_failure_immediately(self) -> None:
        observer = StubHistoryBurst(
            {
                "fast": (ProbeSample(10, 30.0),),
                "working": (ProbeSample(10, 100.0),),
            }
        )
        strategy = LeastLoadStrategy(
            "latest",
            "fallback",
            observer,  # type: ignore[arg-type]
            random.Random(1),
            rank_mode="latest_health_hysteresis",
            latest_min_dwell_s=60,
        )
        self.assertEqual(strategy.pick(10, ["fast", "working"]), ("fast", False))
        observer._histories = {
            "fast": (ProbeSample(10, 30.0), ProbeSample(11, None)),
            "working": (ProbeSample(10, 100.0),),
        }
        self.assertEqual(strategy.pick(11, ["fast", "working"]), ("working", False))

    def test_history_guard_hysteresis_escapes_newest_failure_immediately(self) -> None:
        observer = StubHistoryBurst(
            {
                "fast": (ProbeSample(10, 30.0),),
                "working": (ProbeSample(10, 100.0),),
            }
        )
        strategy = LeastLoadStrategy(
            "history sticky",
            "fallback",
            observer,  # type: ignore[arg-type]
            random.Random(1),
            rank_mode="history_guard_hysteresis",
            history_min_dwell_s=60,
        )
        self.assertEqual(strategy.pick(10, ["fast", "working"]), ("fast", False))
        observer._histories = {
            "fast": (ProbeSample(10, 30.0), ProbeSample(11, None)),
            "working": (ProbeSample(10, 100.0),),
        }
        self.assertEqual(strategy.pick(11, ["fast", "working"]), ("working", False))

    def test_history_guard_hysteresis_requires_cost_margin_and_dwell(self) -> None:
        observer = StubHistoryBurst(
            {
                "current": (ProbeSample(10, 100.0),),
                "other": (ProbeSample(10, 130.0),),
            }
        )
        strategy = LeastLoadStrategy(
            "history sticky",
            "fallback",
            observer,  # type: ignore[arg-type]
            random.Random(1),
            rank_mode="history_guard_hysteresis",
            history_hysteresis_ms=25.0,
            history_hysteresis_ratio=0.20,
            history_min_dwell_s=20,
        )
        self.assertEqual(strategy.pick(10, ["current", "other"])[0], "current")

        observer._histories = {
            "current": (ProbeSample(10, 100.0),),
            "other": (ProbeSample(10, 80.0),),
        }
        self.assertEqual(strategy.pick(30, ["current", "other"])[0], "current")

        observer._histories = {
            "current": (ProbeSample(10, 100.0),),
            "other": (ProbeSample(10, 40.0),),
        }
        self.assertEqual(strategy.pick(31, ["current", "other"])[0], "other")

    def test_latest_health_hysteresis_requires_margin_and_dwell(self) -> None:
        observer = StubHistoryBurst(
            {
                "current": (ProbeSample(10, 100.0),),
                "other": (ProbeSample(10, 130.0),),
            }
        )
        strategy = LeastLoadStrategy(
            "latest",
            "fallback",
            observer,  # type: ignore[arg-type]
            random.Random(1),
            rank_mode="latest_health_hysteresis",
            latest_hysteresis_ms=25.0,
            latest_hysteresis_ratio=0.20,
            latest_min_dwell_s=20,
        )
        self.assertEqual(strategy.pick(10, ["current", "other"])[0], "current")

        observer._histories = {
            "current": (ProbeSample(10, 100.0),),
            "other": (ProbeSample(11, 80.0),),
        }
        self.assertEqual(strategy.pick(30, ["current", "other"])[0], "current")

        observer._histories = {
            "current": (ProbeSample(10, 100.0),),
            "other": (ProbeSample(31, 70.0),),
        }
        self.assertEqual(strategy.pick(31, ["current", "other"])[0], "other")


if __name__ == "__main__":
    unittest.main()
