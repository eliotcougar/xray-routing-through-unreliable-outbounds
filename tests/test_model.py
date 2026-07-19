from __future__ import annotations

import unittest

from xray_strategy_sim.model import (
    ARCHETYPES,
    DEFAULT_SCENARIO_KEYS,
    fleet_scenarios,
    generate_world,
    percentile,
)


class ModelTests(unittest.TestCase):
    def test_generation_is_deterministic_and_bounded(self) -> None:
        left = generate_world(1234, duration_s=120, min_outbounds=5, max_outbounds=8)
        right = generate_world(1234, duration_s=120, min_outbounds=5, max_outbounds=8)
        self.assertEqual(left, right)
        self.assertGreaterEqual(len(left.outbounds), 5)
        self.assertLessEqual(len(left.outbounds), 8)
        self.assertEqual(len(left.outbounds[0].traffic_success), 120)
        self.assertEqual(left.outbounds[0].archetype, "stable_fast")

    def test_percentile_uses_linear_interpolation(self) -> None:
        self.assertEqual(percentile([0, 10], 50), 5)
        self.assertEqual(percentile([1, 2, 3], 100), 3)

    def test_named_scenario_catalog_is_unique_and_generatable(self) -> None:
        scenarios = fleet_scenarios()
        self.assertEqual(tuple(item.key for item in scenarios), DEFAULT_SCENARIO_KEYS)
        self.assertEqual(len(DEFAULT_SCENARIO_KEYS), len(set(DEFAULT_SCENARIO_KEYS)))
        self.assertGreaterEqual(len(DEFAULT_SCENARIO_KEYS), 8)
        for index, scenario in enumerate(scenarios):
            self.assertTrue(set(scenario.required_archetypes) <= set(ARCHETYPES))
            self.assertTrue(
                {name for name, _ in scenario.archetype_weights} <= set(ARCHETYPES)
            )
            world = generate_world(
                1000 + index,
                duration_s=90,
                min_outbounds=8,
                max_outbounds=8,
                outbound_count=8,
                scenario_key=scenario.key,
            )
            self.assertEqual(world.scenario_key, scenario.key)
            self.assertEqual(world.scenario, scenario.label)
            self.assertTrue(
                set(scenario.required_archetypes) <= {item.archetype for item in world.outbounds}
            )

    def test_new_scheduled_archetypes_are_represented(self) -> None:
        fast_flaky = generate_world(
            77,
            duration_s=300,
            min_outbounds=8,
            max_outbounds=8,
            outbound_count=8,
            scenario_key="fast_but_flaky",
        )
        rolling = generate_world(
            78,
            duration_s=300,
            min_outbounds=8,
            max_outbounds=8,
            outbound_count=8,
            scenario_key="rolling_degradation",
        )
        self.assertTrue(
            {"periodic_blackouts", "mobile_handoffs"}
            <= {item.archetype for item in fast_flaky.outbounds}
        )
        self.assertTrue(
            {"brownouts", "fast_then_dead"}
            <= {item.archetype for item in rolling.outbounds}
        )


if __name__ == "__main__":
    unittest.main()
