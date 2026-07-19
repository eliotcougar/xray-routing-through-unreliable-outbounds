"""Numerical equivalents of Xray classic and burst observatories."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import math
import random
from typing import Iterable

from .model import OutboundTrace, SimulatedWorld


@dataclass(frozen=True)
class Observation:
    tag: str
    alive: bool
    delay_ms: float
    sample_count: int = 1
    failure_count: int = 0
    average_ms: float = 0.0
    deviation_ms: float = 0.0


@dataclass(frozen=True)
class ProbeSample:
    """One retained burst probe result at its completion timestamp."""

    completed_second: int
    latency_ms: float | None

    @property
    def success(self) -> bool:
        return self.latency_ms is not None


@dataclass(frozen=True)
class _ProbeCompletion:
    """One probe result that becomes observable after the request finishes."""

    tag: str
    latency_ms: float | None
    # Scheduled burst rounds are canceled when their next window starts.
    # The one-time startup check has no deadline.
    batch_deadline_s: int | None = None


def _completion_for_probe(
    trace: OutboundTrace,
    start_second: int,
    timeout_s: int,
    batch_deadline_s: int | None = None,
) -> tuple[int, _ProbeCompletion]:
    """Quantize Xray's blocking HTTP probe to the one-second simulation clock.

    Successful requests complete after their measured RTT.  An unsuccessful
    request, or a nominal success whose RTT exceeds the HTTP timeout, completes
    at the timeout.  A sub-second success cannot affect a route choice made at
    the same instant that the probe starts, so it appears on the following tick.
    """
    tag = trace.tag
    latency = trace.probe_latency_ms[start_second]
    timeout_ms = timeout_s * 1000.0
    success = (
        trace.probe_success[start_second]
        and latency is not None
        and float(latency) <= timeout_ms
    )
    if success:
        duration_s = max(1, math.ceil(float(latency) / 1000.0))
        observed_latency = float(latency)
    else:
        duration_s = timeout_s
        observed_latency = None
    return (
        start_second + duration_s,
        _ProbeCompletion(tag, observed_latency, batch_deadline_s),
    )


class ClassicObservatory:
    """Latest-result observer used by leastPing and health-filtered basics.

    The simulation uses Xray's concurrent mode: all outbounds are probed at
    startup and together every ``probe_interval_s`` thereafter.
    """

    def __init__(
        self,
        world: SimulatedWorld,
        probe_interval_s: int,
        timeout_s: int = 5,
    ) -> None:
        if probe_interval_s <= 0:
            raise ValueError("probe_interval_s must be positive")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.world = world
        self.probe_interval_s = probe_interval_s
        self.timeout_s = timeout_s
        self._by_tag = world.by_tag()
        self._statuses: dict[str, Observation] = {}
        self._pending: dict[int, list[_ProbeCompletion]] = defaultdict(list)
        self._next_batch_s: int | None = 0
        self._outstanding = 0

    def tick(self, second: int) -> None:
        completions = self._pending.pop(second, ())
        for completion in completions:
            delay = completion.latency_ms
            self._statuses[completion.tag] = Observation(
                tag=completion.tag,
                alive=delay is not None,
                delay_ms=delay if delay is not None else 99_999_999.0,
                sample_count=1,
                failure_count=0 if delay is not None else 1,
                average_ms=delay if delay is not None else 0.0,
                deviation_ms=(delay / 2.0) if delay is not None else 0.0,
            )
        if completions:
            self._outstanding -= len(completions)
            if self._outstanding == 0:
                # observer.go waits for every concurrent result and only then
                # sleeps for ProbeInterval before beginning the next round.
                self._next_batch_s = second + self.probe_interval_s

        if self._next_batch_s != second:
            return
        self._next_batch_s = None
        self._outstanding = len(self.world.tags)
        for tag in self.world.tags:
            completion_second, completion = _completion_for_probe(
                self._by_tag[tag],
                second,
                self.timeout_s,
            )
            self._pending[completion_second].append(completion)

    def statuses(self) -> tuple[Observation, ...]:
        # Xray strategies consume the observer's result order.  Preserve the
        # outbound manager order for deterministic equal-delay tie behavior.
        return tuple(
            self._statuses[tag]
            for tag in self.world.tags
            if tag in self._statuses
        )


class BurstObservatory:
    """Rolling health samples with Xray's burst scheduling semantics.

    Xray performs one immediate check, then schedules ``sampling_count`` probes
    per outbound at random offsets inside a window of
    ``interval * sampling_count``.  Results are a bounded rolling sample and
    are valid for twice that window.
    """

    def __init__(
        self,
        world: SimulatedWorld,
        interval_s: int,
        sampling_count: int,
        rng: random.Random,
        timeout_s: int = 5,
    ) -> None:
        if interval_s < 10:
            # NewHealthPing clamps sub-10-second settings to 10 seconds.
            interval_s = 10
        if sampling_count <= 0:
            sampling_count = 10
        if timeout_s <= 0:
            timeout_s = 5
        self.world = world
        self.interval_s = interval_s
        self.sampling_count = sampling_count
        self.timeout_s = timeout_s
        self.window_s = interval_s * sampling_count
        self.validity_s = 2 * self.window_s
        self._by_tag = world.by_tag()
        self._samples: dict[str, deque[ProbeSample]] = {
            tag: deque(maxlen=sampling_count) for tag in world.tags
        }
        self._schedule: dict[int, list[tuple[str, int | None]]] = defaultdict(list)
        self._pending: dict[int, list[_ProbeCompletion]] = defaultdict(list)
        self._build_schedule(rng)

    def _build_schedule(self, rng: random.Random) -> None:
        for tag in self.world.tags:
            # StartScheduler launches this one-time Check separately from the
            # first full randomized sampling round.
            self._schedule[0].append((tag, None))
        for batch_start in range(0, self.world.duration_s, self.window_s):
            batch_deadline = batch_start + self.window_s
            for tag in self.world.tags:
                for _ in range(self.sampling_count):
                    scheduled = batch_start + rng.randrange(self.window_s)
                    if scheduled < self.world.duration_s:
                        self._schedule[scheduled].append((tag, batch_deadline))

    def tick(self, second: int) -> None:
        # Make results visible only after MeasureDelay returns.  At an exact
        # one-second boundary, completions are processed before the next round
        # starts; sub-second cancellation races are outside this model.
        for completion in self._pending.pop(second, ()):
            if (
                completion.batch_deadline_s is not None
                and second > completion.batch_deadline_s
            ):
                continue
            self._samples[completion.tag].append(
                ProbeSample(second, completion.latency_ms)
            )

        for tag, batch_deadline in self._schedule.get(second, ()):
            completion_second, completion = _completion_for_probe(
                self._by_tag[tag],
                second,
                self.timeout_s,
                batch_deadline,
            )
            self._pending[completion_second].append(completion)

    def _valid_samples(self, tag: str, second: int) -> list[ProbeSample]:
        cutoff = second - self.validity_s
        return [
            sample
            for sample in self._samples[tag]
            if sample.completed_second >= cutoff
        ]

    def histories(self, second: int) -> dict[str, tuple[ProbeSample, ...]]:
        """Return immutable timestamped histories visible at ``second``.

        Xray currently exposes aggregates to the router.  This experimental
        surface models the richer premise where the last retained raw probe
        results are available to a leastLoad-like strategy.
        """
        return {
            tag: tuple(samples)
            for tag in self.world.tags
            if (samples := self._valid_samples(tag, second))
        }

    def statuses(self, second: int) -> tuple[Observation, ...]:
        result: list[Observation] = []
        for tag in self.world.tags:
            samples = self._valid_samples(tag, second)
            if not samples:
                continue
            successes = [
                sample.latency_ms
                for sample in samples
                if sample.latency_ms is not None
            ]
            failures = len(samples) - len(successes)
            alive = bool(successes)  # All != Fail in burstobserver.go
            if successes:
                average = sum(successes) / len(successes)
                if len(successes) < 2:
                    deviation = average / 2.0
                else:
                    variance = sum((value - average) ** 2 for value in successes) / len(successes)
                    deviation = math.sqrt(variance)
            else:
                average = 0.0
                deviation = 0.0
            result.append(
                Observation(
                    tag=tag,
                    alive=alive,
                    delay_ms=average,
                    sample_count=len(samples),
                    failure_count=failures,
                    average_ms=average,
                    deviation_ms=deviation,
                )
            )
        return tuple(result)


def filter_alive_like_xray(
    candidates: Iterable[str], statuses: Iterable[Observation]
) -> list[str]:
    """Random/RoundRobin filtering: unknown tags count as alive."""
    status_map = {status.tag: status for status in statuses}
    return [
        tag
        for tag in candidates
        if tag not in status_map or status_map[tag].alive
    ]
