from __future__ import annotations

import json
import unittest

from xray_strategy_sim.experiment import (
    ExperimentConfig,
    _ci95,
    _collect_trial_results,
    _summarize,
)
from xray_strategy_sim.strategies import StrategySpec


class ExperimentParallelismTests(unittest.TestCase):
    def test_ci95_uses_student_t_for_small_samples(self) -> None:
        self.assertAlmostEqual(_ci95([1.0, 2.0]), 6.353, places=3)

    def test_results_are_identical_across_worker_counts(self) -> None:
        spec = StrategySpec("random_blind_test", "random test", "random")
        common = dict(
            trials=4,
            duration_s=30,
            min_outbounds=5,
            max_outbounds=5,
            seed=20260719,
            scenario_profiles=("mostly_healthy", "hostile_internet"),
        )
        serial_rows, serial_scores = _collect_trial_results(
            ExperimentConfig(**common, workers=1), (spec,)
        )
        try:
            parallel_rows, parallel_scores = _collect_trial_results(
                ExperimentConfig(**common, workers=2), (spec,)
            )
        except PermissionError as error:
            if getattr(error, "winerror", None) == 5:
                self.skipTest("Windows sandbox denied worker-process pipes")
            raise

        # JSON renders NaN consistently; direct container equality does not,
        # because IEEE NaN is intentionally unequal to itself.
        self.assertEqual(
            json.dumps(serial_rows, sort_keys=True),
            json.dumps(parallel_rows, sort_keys=True),
        )
        self.assertEqual(serial_scores, parallel_scores)
        self.assertEqual(
            [row["scenario_key"] for row in parallel_rows],
            ["mostly_healthy", "hostile_internet"] * 2,
        )
        for row in parallel_rows:
            mix = json.loads(str(row["archetype_mix"]))
            self.assertEqual(sum(mix.values()), row["outbound_count"])

        summaries = _summarize(serial_rows, (spec,))
        self.assertEqual(
            [row["scenario_key"] for row in summaries],
            ["all", "mostly_healthy", "hostile_internet"],
        )
        aggregate = summaries[0]
        self.assertEqual(
            aggregate["max_outage_max_s"],
            max(float(row["max_outage_s"]) for row in serial_rows),
        )


if __name__ == "__main__":
    unittest.main()
