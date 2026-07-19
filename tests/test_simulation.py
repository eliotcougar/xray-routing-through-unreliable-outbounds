from __future__ import annotations

import random
import unittest

from xray_strategy_sim.model import generate_world
from xray_strategy_sim.simulation import burst_observer_seed, simulate_strategy
from xray_strategy_sim.strategies import build_strategy, default_strategy_specs


class SimulationTests(unittest.TestCase):
    def test_matched_seed_is_reproducible(self) -> None:
        world = generate_world(
            42,
            duration_s=90,
            min_outbounds=5,
            max_outbounds=5,
            outbound_count=5,
        )
        spec = next(
            spec
            for spec in default_strategy_specs()
            if spec.key == "leastping_default_60s"
        )
        left = simulate_strategy(world, spec, keep_attempts=True)
        right = simulate_strategy(world, spec, keep_attempts=True)
        self.assertEqual(left, right)
        self.assertEqual(left.metrics["attempts"], 90)
        self.assertGreaterEqual(left.metrics["availability_pct"], 0)
        self.assertLessEqual(left.metrics["availability_pct"], 100)

    def test_equal_burst_settings_share_the_exact_probe_schedule(self) -> None:
        world = generate_world(
            43,
            duration_s=120,
            min_outbounds=5,
            max_outbounds=5,
            outbound_count=5,
        )
        specs = {spec.key: spec for spec in default_strategy_specs()}
        exact = specs["leastload_default_60s"]
        history = specs["leastload_history_guard_60s"]
        history_hysteresis = specs["leastload_history_guard_hysteresis_60s"]
        latest = specs["leastload_latest_hysteresis_60s"]
        left = build_strategy(
            exact,
            world,
            random.Random(1),
            world.tags[0],
            random.Random(burst_observer_seed(world, exact)),
        )
        right = build_strategy(
            history,
            world,
            random.Random(2),
            world.tags[0],
            random.Random(burst_observer_seed(world, history)),
        )
        simplified = build_strategy(
            latest,
            world,
            random.Random(3),
            world.tags[0],
            random.Random(burst_observer_seed(world, latest)),
        )
        sticky_history = build_strategy(
            history_hysteresis,
            world,
            random.Random(4),
            world.tags[0],
            random.Random(burst_observer_seed(world, history_hysteresis)),
        )
        self.assertEqual(left.observer._schedule, right.observer._schedule)  # type: ignore[attr-defined]
        self.assertEqual(left.observer._schedule, simplified.observer._schedule)  # type: ignore[attr-defined]
        self.assertEqual(left.observer._schedule, sticky_history.observer._schedule)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
