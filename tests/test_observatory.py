from __future__ import annotations

import random
import unittest

from xray_strategy_sim.model import OutboundTrace, SimulatedWorld
from xray_strategy_sim.observatory import BurstObservatory, ClassicObservatory


def _trace(
    tag: str,
    probe_success: list[bool],
    probe_latency_ms: list[float | None],
) -> OutboundTrace:
    duration = len(probe_success)
    return OutboundTrace(
        tag=tag,
        archetype="test",
        provider=0,
        success_probability=tuple(1.0 for _ in range(duration)),
        expected_latency_ms=tuple(20.0 for _ in range(duration)),
        traffic_success=tuple(True for _ in range(duration)),
        traffic_latency_ms=tuple(20.0 for _ in range(duration)),
        probe_success=tuple(probe_success),
        probe_latency_ms=tuple(probe_latency_ms),
    )


def _world(*traces: OutboundTrace) -> SimulatedWorld:
    duration = len(traces[0].probe_success)
    return SimulatedWorld(
        seed=1,
        duration_s=duration,
        scenario_key="test",
        scenario="test",
        outbounds=tuple(traces),
        global_incident=tuple(False for _ in range(duration)),
    )


class ObservatoryCompletionTests(unittest.TestCase):
    def test_classic_results_arrive_at_success_or_timeout_completion(self) -> None:
        duration = 80
        fast_latency = [200.0] * duration
        fast_latency[65] = 100.0
        world = _world(
            _trace("fast", [True] * duration, fast_latency),
            _trace("dead", [False] * duration, [None] * duration),
        )
        observer = ClassicObservatory(world, probe_interval_s=60, timeout_s=5)

        observer.tick(0)
        self.assertEqual(observer.statuses(), ())
        observer.tick(1)
        self.assertEqual([(item.tag, item.alive) for item in observer.statuses()], [("fast", True)])
        for second in range(2, 6):
            observer.tick(second)
        self.assertEqual(
            [(item.tag, item.alive) for item in observer.statuses()],
            [("fast", True), ("dead", False)],
        )

        # The next concurrent round starts interval seconds after the slowest
        # completion (5 + 60), not at the fixed wall-clock second 60.
        for second in range(6, 67):
            observer.tick(second)
        self.assertEqual(observer.statuses()[0].delay_ms, 100.0)

    def test_burst_result_is_not_visible_until_probe_finishes(self) -> None:
        duration = 20
        world = _world(
            _trace("a", [True] * duration, [1500.0] * duration),
        )
        observer = BurstObservatory(
            world,
            interval_s=10,
            sampling_count=1,
            rng=random.Random(1),
            timeout_s=5,
        )

        observer.tick(0)
        observer.tick(1)
        self.assertEqual(observer.statuses(1), ())
        observer.tick(2)
        self.assertEqual(observer.statuses(2)[0].average_ms, 1500.0)

    def test_burst_failure_arrives_at_timeout(self) -> None:
        duration = 20
        world = _world(
            _trace("a", [False] * duration, [None] * duration),
        )
        observer = BurstObservatory(
            world,
            interval_s=10,
            sampling_count=1,
            rng=random.Random(1),
            timeout_s=5,
        )

        for second in range(5):
            observer.tick(second)
        self.assertEqual(observer.statuses(4), ())
        observer.tick(5)
        status = observer.statuses(5)[0]
        self.assertFalse(status.alive)
        self.assertEqual(status.failure_count, 1)

    def test_burst_scheduled_result_after_round_deadline_is_discarded(self) -> None:
        duration = 20
        world = _world(
            _trace("a", [True] * duration, [12_000.0] * duration),
        )
        observer = BurstObservatory(
            world,
            interval_s=10,
            sampling_count=1,
            rng=random.Random(1),
            timeout_s=20,
        )
        # Isolate one scheduled round probe that finishes after its cancellation
        # boundary; the separate startup Check has no such boundary.
        observer._schedule.clear()
        observer._schedule[0].append(("a", 10))

        for second in range(13):
            observer.tick(second)
        self.assertEqual(observer.statuses(12), ())


if __name__ == "__main__":
    unittest.main()
