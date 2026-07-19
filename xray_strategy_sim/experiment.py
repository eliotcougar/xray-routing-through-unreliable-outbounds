"""Monte Carlo experiment orchestration, CSV output, and graphs."""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import multiprocessing
import os
from pathlib import Path
import statistics
from typing import Callable, Iterable

from .model import (
    DEFAULT_SCENARIO_KEYS,
    SimulatedWorld,
    fleet_scenarios,
    generate_world,
    get_fleet_scenario,
    percentile,
)
from .simulation import SimulationResult, simulate_strategy
from .strategies import (
    V2RAYNG_COMMIT,
    XRAY_CORE_COMMIT,
    StrategySpec,
    default_strategy_specs,
    validate_experiment_probe_floor,
)


@dataclass(frozen=True)
class ExperimentConfig:
    trials: int = 300
    duration_s: int = 30 * 60
    min_outbounds: int = 5
    max_outbounds: int = 20
    seed: int = 20_260_719
    attempt_interval_s: int = 1
    failure_penalty_ms: float = 5000.0
    scenario_profiles: tuple[str, ...] = DEFAULT_SCENARIO_KEYS
    # Zero selects all logical CPUs (bounded by the number of worlds).
    workers: int = 0
    output_dir: Path = Path("outputs")


@dataclass(frozen=True)
class ExperimentResult:
    config: ExperimentConfig
    summary_rows: tuple[dict[str, object], ...]
    trial_rows: tuple[dict[str, object], ...]
    representative_trial: int
    output_dir: Path


@dataclass(frozen=True)
class _TrialTask:
    trial: int
    trial_seed: int
    scenario_key: str
    duration_s: int
    min_outbounds: int
    max_outbounds: int
    attempt_interval_s: int
    failure_penalty_ms: float
    specs: tuple[StrategySpec, ...]


@dataclass(frozen=True)
class _TrialResult:
    trial: int
    raw_availability: float
    rows: tuple[dict[str, object], ...]


def _mean(values: list[float]) -> float:
    finite = [value for value in values if math.isfinite(value)]
    return statistics.fmean(finite) if finite else math.nan


def _t_critical_95(degrees_of_freedom: int) -> float:
    """Two-sided 95% Student-t critical value without a SciPy dependency."""
    anchors = (
        (1, 12.706), (2, 4.303), (3, 3.182), (4, 2.776), (5, 2.571),
        (6, 2.447), (7, 2.365), (8, 2.306), (9, 2.262), (10, 2.228),
        (11, 2.201), (12, 2.179), (13, 2.160), (14, 2.145), (15, 2.131),
        (16, 2.120), (17, 2.110), (18, 2.101), (19, 2.093), (20, 2.086),
        (21, 2.080), (22, 2.074), (23, 2.069), (24, 2.064), (25, 2.060),
        (26, 2.056), (27, 2.052), (28, 2.048), (29, 2.045), (30, 2.042),
        (40, 2.021), (60, 2.000), (120, 1.980), (1_000_000, 1.960),
    )
    df = max(1, degrees_of_freedom)
    for (left_df, left_value), (right_df, right_value) in zip(anchors, anchors[1:]):
        if df <= right_df:
            fraction = (df - left_df) / (right_df - left_df)
            return left_value + fraction * (right_value - left_value)
    return 1.960


def _ci95(values: list[float]) -> float:
    finite = [value for value in values if math.isfinite(value)]
    if len(finite) < 2:
        return 0.0
    return (
        _t_critical_95(len(finite) - 1)
        * statistics.stdev(finite)
        / math.sqrt(len(finite))
    )


def _trial_seed(base_seed: int, trial_index: int) -> int:
    return base_seed + trial_index * 1_000_003


def _scenario_for_trial(config: ExperimentConfig, trial: int) -> str:
    return config.scenario_profiles[trial % len(config.scenario_profiles)]


def _trial_tasks(
    config: ExperimentConfig,
    specs: tuple[StrategySpec, ...],
) -> tuple[_TrialTask, ...]:
    return tuple(
        _TrialTask(
            trial=trial,
            trial_seed=_trial_seed(config.seed, trial),
            scenario_key=_scenario_for_trial(config, trial),
            duration_s=config.duration_s,
            min_outbounds=config.min_outbounds,
            max_outbounds=config.max_outbounds,
            attempt_interval_s=config.attempt_interval_s,
            failure_penalty_ms=config.failure_penalty_ms,
            specs=specs,
        )
        for trial in range(config.trials)
    )


def _run_trial_task(task: _TrialTask) -> _TrialResult:
    """Generate and evaluate one world; deliberately module-level for spawn."""
    world = generate_world(
        seed=task.trial_seed,
        duration_s=task.duration_s,
        min_outbounds=task.min_outbounds,
        max_outbounds=task.max_outbounds,
        scenario_key=task.scenario_key,
    )
    raw_availability = world.raw_mean_availability() * 100.0
    oracle_availability = world.oracle_availability() * 100.0
    archetype_counts = Counter(outbound.archetype for outbound in world.outbounds)
    archetype_mix = json.dumps(dict(sorted(archetype_counts.items())), separators=(",", ":"))
    rows: list[dict[str, object]] = []
    for spec in task.specs:
        simulation = simulate_strategy(
            world,
            spec,
            failure_penalty_ms=task.failure_penalty_ms,
            attempt_interval_s=task.attempt_interval_s,
        )
        rows.append(
            {
                "trial": task.trial,
                "trial_seed": task.trial_seed,
                "scenario_key": world.scenario_key,
                "scenario": world.scenario,
                "archetype_mix": archetype_mix,
                "outbound_count": len(world.outbounds),
                "raw_outbound_availability_pct": raw_availability,
                "oracle_availability_pct": oracle_availability,
                "strategy_key": spec.key,
                "strategy": spec.label,
                "kind": spec.kind,
                **simulation.metrics,
            }
        )
    return _TrialResult(task.trial, raw_availability, tuple(rows))


def _resolved_worker_count(config: ExperimentConfig) -> int:
    requested = config.workers
    if requested < 0:
        raise ValueError("workers must be zero (auto) or a positive integer")
    available = os.cpu_count() or 1
    # concurrent.futures enforces this platform ceiling on Windows.
    if os.name == "nt":
        available = min(available, 61)
    return max(1, min(config.trials, requested or available, available))


def _collect_trial_results(
    config: ExperimentConfig,
    specs: tuple[StrategySpec, ...],
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict[str, object]], list[tuple[int, float]]]:
    """Collect worlds in trial order, independent of scheduling/worker count."""
    tasks = _trial_tasks(config, specs)
    worker_count = _resolved_worker_count(config)
    if worker_count == 1:
        results: Iterable[_TrialResult] = map(_run_trial_task, tasks)
        executor = None
    else:
        # Spawn is Windows' native start method and also exercises the same
        # pickling boundary on other platforms.  No closures or live RNG state
        # cross into workers; every task is wholly determined by its seed.
        executor = ProcessPoolExecutor(
            max_workers=worker_count,
            mp_context=multiprocessing.get_context("spawn"),
        )
        chunksize = max(1, len(tasks) // (worker_count * 4))
        results = executor.map(_run_trial_task, tasks, chunksize=chunksize)

    rows: list[dict[str, object]] = []
    scores: list[tuple[int, float]] = []
    try:
        for completed, result in enumerate(results, 1):
            rows.extend(result.rows)
            scores.append((result.trial, result.raw_availability))
            if progress is not None:
                progress(completed, config.trials)
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
    return rows, scores


def _summarize_group(
    rows: list[dict[str, object]],
    specs: tuple[StrategySpec, ...],
    scenario_key: str,
    scenario: str,
) -> list[dict[str, object]]:
    effective_wins: dict[str, int] = {spec.key: 0 for spec in specs}
    availability_wins: dict[str, int] = {spec.key: 0 for spec in specs}
    trials = sorted({int(row["trial"]) for row in rows})
    for trial in trials:
        trial_rows = [row for row in rows if int(row["trial"]) == trial]
        best_effective = min(float(row["effective_mean_ms"]) for row in trial_rows)
        best_availability = max(float(row["availability_pct"]) for row in trial_rows)
        for row in trial_rows:
            if math.isclose(float(row["effective_mean_ms"]), best_effective):
                effective_wins[str(row["strategy_key"])] += 1
            if math.isclose(float(row["availability_pct"]), best_availability):
                availability_wins[str(row["strategy_key"])] += 1

    summaries: list[dict[str, object]] = []
    for spec in specs:
        strategy_rows = [row for row in rows if row["strategy_key"] == spec.key]
        availability = [float(row["availability_pct"]) for row in strategy_rows]
        usable = [float(row["usable_pct"]) for row in strategy_rows]
        effective = [float(row["effective_mean_ms"]) for row in strategy_rows]
        p95 = [float(row["success_p95_ms"]) for row in strategy_rows]
        max_outage = [float(row["max_outage_s"]) for row in strategy_rows]
        oracle_miss = [float(row["oracle_miss_pct"]) for row in strategy_rows]
        switching = [float(row["route_switches_per_min"]) for row in strategy_rows]
        summaries.append(
            {
                "scenario_key": scenario_key,
                "scenario": scenario,
                "strategy_key": spec.key,
                "strategy": spec.label,
                "kind": spec.kind,
                "trials": len(strategy_rows),
                "availability_mean_pct": _mean(availability),
                "availability_ci95_pct": _ci95(availability),
                "availability_p10_pct": percentile(availability, 10),
                "availability_p90_pct": percentile(availability, 90),
                "usable_mean_pct": _mean(usable),
                "effective_mean_ms": _mean(effective),
                "effective_ci95_ms": _ci95(effective),
                "success_p95_mean_ms": _mean(p95),
                "max_outage_mean_s": _mean(max_outage),
                "max_outage_ci95_s": _ci95(max_outage),
                "max_outage_max_s": max(max_outage),
                "oracle_miss_mean_pct": _mean(oracle_miss),
                "switches_per_min_mean": _mean(switching),
                "effective_win_pct": 100.0 * effective_wins[spec.key] / len(trials),
                "availability_win_pct": 100.0 * availability_wins[spec.key] / len(trials),
            }
        )
    summaries.sort(key=lambda row: float(row["effective_mean_ms"]))
    for rank, row in enumerate(summaries, 1):
        row["effective_rank"] = rank
    return summaries


def _summarize(
    rows: list[dict[str, object]], specs: tuple[StrategySpec, ...]
) -> list[dict[str, object]]:
    """Return one aggregate table followed by a table for each scenario."""
    summaries = _summarize_group(rows, specs, "all", "All scenarios")
    represented = {str(row["scenario_key"]): str(row["scenario"]) for row in rows}
    catalog_order = [scenario.key for scenario in fleet_scenarios()]
    ordered_keys = [key for key in catalog_order if key in represented]
    ordered_keys.extend(sorted(set(represented) - set(ordered_keys)))
    for scenario_key in ordered_keys:
        scenario_rows = [row for row in rows if row["scenario_key"] == scenario_key]
        summaries.extend(
            _summarize_group(
                scenario_rows,
                specs,
                scenario_key,
                represented[scenario_key],
            )
        )
    return summaries


def _write_csv(path: Path, rows: Iterable[dict[str, object]]) -> None:
    rows = list(rows)
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _representative_trial(scores: list[tuple[int, float]]) -> int:
    median = percentile([score for _, score in scores], 50)
    return min(scores, key=lambda item: abs(item[1] - median))[0]


def _rolling_mean(values: list[float], window: int) -> list[float]:
    result: list[float] = []
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= window:
            running -= values[index - window]
        result.append(running / min(index + 1, window))
    return result


def _write_representative_data(
    output_dir: Path,
    world: SimulatedWorld,
    simulations: list[SimulationResult],
) -> None:
    outbound_rows: list[dict[str, object]] = []
    for outbound in world.outbounds:
        for second in range(world.duration_s):
            outbound_rows.append(
                {
                    "second": second,
                    "minute": second / 60.0,
                    "scenario_key": world.scenario_key,
                    "scenario": world.scenario,
                    "tag": outbound.tag,
                    "archetype": outbound.archetype,
                    "provider": outbound.provider,
                    "success_probability": outbound.success_probability[second],
                    "expected_latency_ms": outbound.expected_latency_ms[second],
                    "traffic_success": outbound.traffic_success[second],
                    "traffic_latency_ms": outbound.traffic_latency_ms[second],
                }
            )
    _write_csv(output_dir / "representative_outbounds.csv", outbound_rows)

    strategy_rows: list[dict[str, object]] = []
    for simulation in simulations:
        for attempt in simulation.attempts:
            strategy_rows.append(
                {
                    "second": attempt.second,
                    "minute": attempt.second / 60.0,
                    "scenario_key": world.scenario_key,
                    "scenario": world.scenario,
                    "strategy_key": simulation.strategy_key,
                    "strategy": simulation.strategy_label,
                    "selected_tag": attempt.selected_tag,
                    "success": attempt.success,
                    "latency_ms": attempt.latency_ms,
                    "effective_latency_ms": attempt.effective_latency_ms,
                    "used_fallback": attempt.used_fallback,
                }
            )
    _write_csv(output_dir / "representative_strategies.csv", strategy_rows)


def _make_grouped_scenario_plots(
    figures: Path,
    scenario_summary_rows: list[dict[str, object]],
    plt: object,
) -> None:
    """Compare the proposed selector with practical baselines in every scenario."""
    strategy_keys = (
        "leastload_history_guard_hysteresis_60s",
        "leastload_history_guard_60s",
        "leastload_latest_hysteresis_60s",
        "leastload_default_60s",
        "leastping_default_60s",
        "leastload_v2rayng_burst_300s",
    )
    strategy_labels = {
        "leastload_history_guard_hysteresis_60s": "History Guard + hysteresis (proposed)",
        "leastload_history_guard_60s": "History Guard",
        "leastload_latest_hysteresis_60s": "Latest health + hysteresis (proposed)",
        "leastload_default_60s": "Xray leastLoad",
        "leastping_default_60s": "Xray leastPing",
        "leastload_v2rayng_burst_300s": "v2rayNG leastLoad",
    }
    strategy_colors = {
        "leastload_history_guard_hysteresis_60s": "#9b59b6",
        "leastload_history_guard_60s": "#00aeca",
        "leastload_latest_hysteresis_60s": "#0f9d78",
        "leastload_default_60s": "#707780",
        "leastping_default_60s": "#3977c3",
        "leastload_v2rayng_burst_300s": "#e28b25",
    }
    metrics = (
        ("availability_mean_pct", "availability_ci95_pct", "Availability (%)"),
        ("effective_mean_ms", "effective_ci95_ms", "Effective response (ms)"),
        ("max_outage_mean_s", "max_outage_ci95_s", "Mean longest outage (s)"),
    )
    indexed = {
        (str(row["scenario_key"]), str(row["strategy_key"])): row
        for row in scenario_summary_rows
        if row.get("scenario_key") != "all"
    }
    scenario_keys = [
        key
        for key in DEFAULT_SCENARIO_KEYS
        if all((key, strategy_key) in indexed for strategy_key in strategy_keys)
    ]
    if not scenario_keys:
        return

    scenario_labels = [
        str(indexed[(key, strategy_keys[0])]["scenario"]).replace(" ", "\n")
        for key in scenario_keys
    ]
    trials_per_scenario = int(indexed[(scenario_keys[0], strategy_keys[0])]["trials"])
    positions = list(range(len(scenario_keys)))
    width = 0.125
    offsets = [
        (index - (len(strategy_keys) - 1) / 2) * width
        for index in range(len(strategy_keys))
    ]

    fig, axes = plt.subplots(3, 1, figsize=(18, 13), sharex=True)
    for axis, (field, ci_field, label) in zip(axes, metrics):
        for strategy_index, strategy_key in enumerate(strategy_keys):
            values = [
                float(indexed[(scenario_key, strategy_key)][field])
                for scenario_key in scenario_keys
            ]
            errors = [
                float(indexed[(scenario_key, strategy_key)][ci_field])
                for scenario_key in scenario_keys
            ]
            bars = axis.bar(
                [position + offsets[strategy_index] for position in positions],
                values,
                width=width,
                yerr=errors,
                capsize=3,
                error_kw={"elinewidth": 1.0, "capthick": 1.0, "ecolor": "#374151"},
                color=strategy_colors[strategy_key],
                edgecolor="#083d49" if strategy_index == 0 else "none",
                linewidth=1.0 if strategy_index == 0 else 0.0,
                label=strategy_labels[strategy_key],
                zorder=3,
            )
            if strategy_index == 0:
                value_format = "{:.1f}" if field != "effective_mean_ms" else "{:.0f}"
                for bar, value, error in zip(bars, values, errors):
                    axis.annotate(
                        value_format.format(value),
                        (bar.get_x() + bar.get_width() / 2, value + error),
                        xytext=(0, 4),
                        textcoords="offset points",
                        ha="center",
                        va="bottom",
                        fontsize=8,
                        fontweight="bold",
                    )
        axis.set_ylabel(label)
        axis.grid(axis="y", alpha=0.22, zorder=0)
        axis.spines[["top", "right"]].set_visible(False)
        axis.set_axisbelow(True)
        if field == "availability_mean_pct":
            axis.set_ylim(0, 110)

    axes[-1].set_xticks(positions, scenario_labels)
    axes[-1].tick_params(axis="x", labelsize=9)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.925),
        ncols=6,
        frameon=False,
    )
    fig.suptitle(
        "Hysteresis-enhanced selectors across every simulated fleet scenario",
        fontsize=15,
        y=0.995,
    )
    fig.text(
        0.5,
        0.962,
        f"Means and 95% confidence intervals; {trials_per_scenario} matched worlds per scenario; 30 minutes per world",
        ha="center",
        va="top",
        fontsize=10,
        color="#4b5563",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.88), h_pad=2.0)
    fig.savefig(figures / "scenario_strategy_grouped_bars.png", dpi=180)
    plt.close(fig)


def _tolerance_sweep_rows(
    summary_rows: list[dict[str, object]],
    scenario_key: str,
) -> list[dict[str, object]]:
    order = {
        "leastload_default_60s": 0,
        "leastload_tolerance_10_60s": 1,
        "leastload_tolerance_20_60s": 2,
        "leastload_tolerance_30_60s": 3,
        "leastload_tolerance_40_60s": 4,
        "leastload_tolerance_50_60s": 5,
        "leastload_tolerance_70_60s": 6,
        "leastload_tolerance_100_60s": 7,
    }
    rows = [
        row
        for row in summary_rows
        if row["scenario_key"] == scenario_key
        and row["strategy_key"] in order
    ]
    rows.sort(key=lambda row: order[str(row["strategy_key"])])
    return rows


def _make_tolerance_plots(
    figures: Path,
    summary_rows: list[dict[str, object]],
    plt: object,
) -> None:
    labels = ("Disabled\n(default)", "10%", "20%", "30%", "40%", "50%", "70%", "100%")
    aggregate = _tolerance_sweep_rows(summary_rows, "all")
    if len(aggregate) != len(labels):
        return

    metrics = (
        ("availability_mean_pct", "availability_ci95_pct", "Availability (%)", "#2a9d8f", "{:.1f}"),
        ("effective_mean_ms", "effective_ci95_ms", "Effective response (ms)", "#7c4dba", "{:.0f}"),
        ("max_outage_mean_s", "max_outage_ci95_s", "Mean longest outage (s)", "#e76f51", "{:.1f}"),
    )
    positions = list(range(len(labels)))
    trials = int(aggregate[0]["trials"])
    fig, axes = plt.subplots(3, 1, figsize=(14, 12), sharex=True)
    for axis, (field, ci_field, ylabel, color, value_format) in zip(axes, metrics):
        values = [float(row[field]) for row in aggregate]
        errors = [float(row[ci_field]) for row in aggregate]
        axis.errorbar(
            positions,
            values,
            yerr=errors,
            marker="o",
            markersize=6,
            linewidth=2,
            capsize=4,
            color=color,
        )
        for position, value, error in zip(positions, values, errors):
            axis.annotate(
                value_format.format(value),
                (position, value + error),
                xytext=(0, 5),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=8,
            )
        axis.set_ylabel(ylabel)
        axis.grid(alpha=0.22)
        axis.spines[["top", "right"]].set_visible(False)
    axes[-1].set_xticks(positions, labels)
    axes[-1].set_xlabel("Configured tolerance (failure fraction allowed before exclusion)")
    fig.suptitle("Exact Xray leastLoad sensitivity to failure tolerance", fontsize=15)
    fig.text(
        0.5,
        0.955,
        f"Means and 95% confidence intervals across {trials} matched worlds; identical 60 s / 10-sample burst schedules",
        ha="center",
        va="top",
        fontsize=10,
        color="#4b5563",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.93), h_pad=2.0)
    fig.savefig(figures / "leastload_tolerance_sensitivity.png", dpi=180)
    plt.close(fig)

    scenario_rows = [
        (scenario_key, _tolerance_sweep_rows(summary_rows, scenario_key))
        for scenario_key in DEFAULT_SCENARIO_KEYS
    ]
    if any(len(rows) != len(labels) for _, rows in scenario_rows):
        return
    trials_per_scenario = int(scenario_rows[0][1][0]["trials"])
    fig, axes = plt.subplots(2, 4, figsize=(18, 9), sharex=True)
    for axis, (scenario_key, rows) in zip(axes.flat, scenario_rows):
        values = [float(row["effective_mean_ms"]) for row in rows]
        errors = [float(row["effective_ci95_ms"]) for row in rows]
        axis.errorbar(
            positions,
            values,
            yerr=errors,
            marker="o",
            markersize=4,
            linewidth=1.6,
            capsize=2,
            color="#7c4dba",
        )
        axis.set_title(str(rows[0]["scenario"]), fontsize=10)
        axis.grid(alpha=0.2)
        axis.spines[["top", "right"]].set_visible(False)
        axis.set_xticks(positions, labels, rotation=35, ha="right")
    for axis in axes[:, 0]:
        axis.set_ylabel("Effective response (ms)")
    fig.suptitle("leastLoad tolerance response by fleet scenario", fontsize=15)
    fig.text(
        0.5,
        0.95,
        f"Means and 95% confidence intervals; {trials_per_scenario} matched worlds per scenario",
        ha="center",
        va="top",
        fontsize=10,
        color="#4b5563",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.92), h_pad=2.0, w_pad=1.5)
    fig.savefig(figures / "leastload_tolerance_by_scenario.png", dpi=180)
    plt.close(fig)


def _make_plots(
    output_dir: Path,
    summary_rows: list[dict[str, object]],
    scenario_summary_rows: list[dict[str, object]],
    trial_rows: list[dict[str, object]],
    world: SimulatedWorld,
    simulations: list[SimulationResult],
    failure_penalty_ms: float,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm

    figures = output_dir / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    colors = plt.get_cmap("tab20")

    # Availability/responsiveness trade-off with confidence intervals.
    fig, ax = plt.subplots(figsize=(14, 8))
    for index, row in enumerate(summary_rows):
        x = float(row["effective_mean_ms"])
        y = float(row["availability_mean_pct"])
        ax.errorbar(
            x,
            y,
            xerr=float(row["effective_ci95_ms"]),
            yerr=float(row["availability_ci95_pct"]),
            fmt="o",
            color=colors(index),
            capsize=3,
            label=str(row["strategy"]),
        )
    ax.set_xlabel(f"Effective mean response (ms; failures = {failure_penalty_ms:g} ms)")
    ax.set_ylabel("Successful connection attempts (%)")
    ax.set_title("Availability vs. combined responsiveness across Monte Carlo trials")
    ax.grid(alpha=0.25)
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=8)
    fig.tight_layout()
    fig.savefig(figures / "strategy_tradeoff.png", dpi=170)
    plt.close(fig)

    # Trial-to-trial distributions.
    ordered_keys = [str(row["strategy_key"]) for row in summary_rows]
    labels = [str(row["strategy"]) for row in summary_rows]
    availability_groups = [
        [float(row["availability_pct"]) for row in trial_rows if row["strategy_key"] == key]
        for key in ordered_keys
    ]
    effective_groups = [
        [float(row["effective_mean_ms"]) for row in trial_rows if row["strategy_key"] == key]
        for key in ordered_keys
    ]
    fig, axes = plt.subplots(1, 2, figsize=(17, 8))
    axes[0].boxplot(availability_groups, tick_labels=labels, showfliers=False)
    axes[0].set_ylabel("Availability (%)")
    axes[0].set_title("Trial distribution: availability")
    axes[1].boxplot(effective_groups, tick_labels=labels, showfliers=False)
    axes[1].set_ylabel("Effective mean response (ms)")
    axes[1].set_title("Trial distribution: response + failures")
    for ax in axes:
        ax.tick_params(axis="x", labelrotation=70)
        ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(figures / "trial_distributions.png", dpi=170)
    plt.close(fig)

    _make_grouped_scenario_plots(figures, scenario_summary_rows, plt)
    _make_tolerance_plots(figures, scenario_summary_rows, plt)

    # Focus on leastLoad-like behavior in the scenarios where stale or partial
    # health is most expensive.
    hard_scenarios = (
        "fast_but_flaky",
        "shared_provider_failure",
        "rolling_degradation",
        "hostile_internet",
    )
    comparison_keys = (
        "leastload_history_guard_hysteresis_60s",
        "leastload_latest_hysteresis_60s",
        "leastload_default_60s",
        "leastload_mean_latency_60s",
        "leastload_success_rate_60s",
        "leastload_expected_cost_60s",
        "leastload_reliability_lcb_60s",
        "leastload_history_guard_60s",
        "leastload_tuned_e2_60s",
        "leastload_v2rayng_burst_300s",
    )
    short_labels = {
        "leastload_history_guard_hysteresis_60s": "History + hysteresis",
        "leastload_latest_hysteresis_60s": "Latest + hysteresis",
        "leastload_default_60s": "Xray deviation",
        "leastload_mean_latency_60s": "Mean RTT",
        "leastload_success_rate_60s": "Success rate",
        "leastload_expected_cost_60s": "Expected outcome",
        "leastload_reliability_lcb_60s": "Reliability LCB",
        "leastload_history_guard_60s": "History Guard",
        "leastload_tuned_e2_60s": "Tuned Xray",
        "leastload_v2rayng_burst_300s": "v2rayNG burst",
    }
    hard_rows = {
        (str(row["scenario_key"]), str(row["strategy_key"])): row
        for row in scenario_summary_rows
        if row["scenario_key"] in hard_scenarios
        and row["strategy_key"] in comparison_keys
    }
    available_keys = [
        key
        for key in comparison_keys
        if any(row_key[1] == key for row_key in hard_rows)
    ]
    if hard_rows and available_keys:
        fig, axes = plt.subplots(2, 1, figsize=(14, 10), sharex=True)
        positions = list(range(len(available_keys)))
        plotted_scenarios = 0
        for scenario_key in hard_scenarios:
            rows = [hard_rows.get((scenario_key, key)) for key in available_keys]
            if any(row is None for row in rows):
                continue
            plotted_scenarios += 1
            scenario_label = str(rows[0]["scenario"])
            axes[0].plot(
                positions,
                [float(row["availability_mean_pct"]) for row in rows],
                marker="o",
                linewidth=1.5,
                label=scenario_label,
            )
            axes[1].plot(
                positions,
                [float(row["effective_mean_ms"]) for row in rows],
                marker="o",
                linewidth=1.5,
                label=scenario_label,
            )
        axes[0].set_ylabel("Availability (%)")
        axes[0].set_title("leastLoad-like strategies in hard fleet scenarios")
        if plotted_scenarios:
            axes[0].legend(ncols=2, fontsize=8)
        axes[1].set_ylabel("Effective response (ms)")
        axes[1].set_xticks(
            positions,
            [short_labels.get(key, key) for key in available_keys],
            rotation=25,
            ha="right",
        )
        for axis in axes:
            axis.grid(alpha=0.22)
        fig.tight_layout()
        fig.savefig(figures / "hard_scenario_leastload_comparison.png", dpi=170)
        plt.close(fig)

    # Representative world's ground truth and selected user-visible quality.
    matrix = []
    for outbound in world.outbounds:
        matrix.append([
            outbound.expected_latency_ms[t] if outbound.traffic_success[t] else math.nan
            for t in range(world.duration_s)
        ])
    fig, axes = plt.subplots(3, 1, figsize=(17, 12), height_ratios=(1.4, 1, 1), sharex=True)
    latency_cmap = plt.get_cmap("viridis").copy()
    latency_cmap.set_bad("#111111")
    image = axes[0].imshow(
        matrix,
        aspect="auto",
        interpolation="nearest",
        extent=(0, world.duration_s / 60.0, len(world.outbounds) - 0.5, -0.5),
        cmap=latency_cmap,
        norm=LogNorm(vmin=20, vmax=3000),
    )
    axes[0].set_yticks(range(len(world.outbounds)))
    axes[0].set_yticklabels([f"{o.tag} {o.archetype}" for o in world.outbounds], fontsize=7)
    axes[0].set_title("Representative trial: outbound ground truth (black = failed attempt)")
    fig.colorbar(image, ax=axes[0], label="Expected latency (ms)", pad=0.01)

    highlight = {
        "leastload_history_guard_hysteresis_60s",
        "leastload_latest_hysteresis_60s",
        "leastping_default_60s",
        "leastload_default_60s",
        "leastload_mean_latency_60s",
        "leastload_success_rate_60s",
        "leastload_expected_cost_60s",
        "leastload_reliability_lcb_60s",
        "leastload_history_guard_60s",
    }
    for index, simulation in enumerate(simulations):
        if simulation.strategy_key not in highlight:
            continue
        seconds = [attempt.second / 60.0 for attempt in simulation.attempts]
        success_values = [100.0 if attempt.success else 0.0 for attempt in simulation.attempts]
        effective_values = [attempt.effective_latency_ms for attempt in simulation.attempts]
        axes[1].plot(seconds, _rolling_mean(success_values, 60), label=simulation.strategy_label, linewidth=1.2)
        axes[2].plot(seconds, _rolling_mean(effective_values, 60), label=simulation.strategy_label, linewidth=1.2)
    axes[1].set_ylabel("60 s availability (%)")
    axes[1].set_ylim(-2, 102)
    axes[1].grid(alpha=0.2)
    axes[2].set_ylabel("60 s effective response (ms)")
    axes[2].set_xlabel("Minute")
    axes[2].grid(alpha=0.2)
    axes[1].legend(ncols=3, fontsize=8, loc="lower center")
    fig.tight_layout()
    fig.savefig(figures / "representative_timeline.png", dpi=170)
    plt.close(fig)

    # Isolate the leastPing probe interval experiment.
    leastping = [row for row in summary_rows if row["kind"] == "leastping"]
    leastping.sort(key=lambda row: int(str(row["strategy_key"]).rsplit("_", 1)[1][:-1]))
    fig, ax1 = plt.subplots(figsize=(9, 6))
    positions = list(range(len(leastping)))
    widths = 0.38
    ax1.bar(
        [position - widths / 2 for position in positions],
        [float(row["availability_mean_pct"]) for row in leastping],
        width=widths,
        color="#2a9d8f",
        label="Availability (%)",
    )
    ax2 = ax1.twinx()
    ax2.bar(
        [position + widths / 2 for position in positions],
        [float(row["effective_mean_ms"]) for row in leastping],
        width=widths,
        color="#e76f51",
        label="Effective response (ms)",
    )
    ax1.set_xticks(positions, [str(row["strategy"]) for row in leastping])
    ax1.set_ylabel("Availability (%)", color="#2a9d8f")
    ax2.set_ylabel("Effective mean response (ms)", color="#e76f51")
    ax1.set_title("leastPing sensitivity to classic-observatory interval")
    ax1.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    fig.savefig(figures / "leastping_probe_sensitivity.png", dpi=170)
    plt.close(fig)


def _write_report(
    output_dir: Path,
    config: ExperimentConfig,
    aggregate_rows: list[dict[str, object]],
    scenario_rows: list[dict[str, object]],
    trial_rows: list[dict[str, object]],
    representative_trial: int,
) -> None:
    lines = [
        "# Xray balancer simulation results",
        "",
        f"- Total simulated worlds: {config.trials}",
        f"- Fleet scenarios: {len(config.scenario_profiles)} ({', '.join(config.scenario_profiles)})",
        f"- Simulated time per trial: {config.duration_s / 60:g} minutes",
        f"- Outbounds per trial: {config.min_outbounds}-{config.max_outbounds}",
        f"- Connection attempts: one every {config.attempt_interval_s} second(s)",
        f"- Failed-attempt response penalty: {config.failure_penalty_ms:g} ms",
        f"- Representative trial: {representative_trial}",
        f"- Xray source commit: `{XRAY_CORE_COMMIT}`",
        "",
        "## Aggregate ranking",
        "",
        "| Rank | Strategy/settings | Availability | Effective response | Successful p95 | Mean max outage | Win rate |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    for row in aggregate_rows:
        lines.append(
            "| {effective_rank} | {strategy} | {availability_mean_pct:.2f}% | "
            "{effective_mean_ms:.0f} ms | {success_p95_mean_ms:.0f} ms | "
            "{max_outage_mean_s:.1f} s | {effective_win_pct:.1f}% |".format(**row)
        )
    tolerance_rows = _tolerance_sweep_rows(scenario_rows, "all")
    if tolerance_rows:
        tolerance_labels = ("Disabled (default)", "10%", "20%", "30%", "40%", "50%", "70%", "100%")
        lines.extend(
            [
                "",
                "## Exact leastLoad tolerance sweep",
                "",
                "Xray applies the filter only when tolerance is greater than zero. An otherwise-alive outbound is excluded when `failure_count / sample_count` is strictly greater than the configured tolerance; therefore the default zero value means disabled, not zero failures allowed.",
                "",
                "| Tolerance | Availability (95% CI) | Effective response (95% CI) | Successful p95 | Mean longest outage | Switches/min |",
                "|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for label, row in zip(tolerance_labels, tolerance_rows):
            lines.append(
                "| {label} | {availability_mean_pct:.2f}% +/- {availability_ci95_pct:.2f} | "
                "{effective_mean_ms:.0f} +/- {effective_ci95_ms:.0f} ms | "
                "{success_p95_mean_ms:.0f} ms | {max_outage_mean_s:.1f} s | "
                "{switches_per_min_mean:.2f} |".format(label=label, **row)
            )
        lines.extend(
            [
                "",
                "### Paired effect relative to default leastLoad",
                "",
                "Each value compares the tolerance preset with default disabled tolerance on the same generated world. Positive availability and negative effective-response/outage deltas are improvements.",
                "",
                "| Tolerance | Availability delta | Effective-response delta | Longest-outage delta |",
                "|---:|---:|---:|---:|",
            ]
        )
        tolerance_keys = (
            "leastload_tolerance_10_60s",
            "leastload_tolerance_20_60s",
            "leastload_tolerance_30_60s",
            "leastload_tolerance_40_60s",
            "leastload_tolerance_50_60s",
            "leastload_tolerance_70_60s",
            "leastload_tolerance_100_60s",
        )
        trial_strategies: dict[int, dict[str, dict[str, object]]] = defaultdict(dict)
        for row in trial_rows:
            trial_strategies[int(row["trial"])][str(row["strategy_key"])] = row
        for label, strategy_key in zip(tolerance_labels[1:], tolerance_keys):
            differences = {
                "availability_pct": [],
                "effective_mean_ms": [],
                "max_outage_s": [],
            }
            for strategies in trial_strategies.values():
                candidate = strategies.get(strategy_key)
                default = strategies.get("leastload_default_60s")
                if candidate is None or default is None:
                    continue
                for field in differences:
                    differences[field].append(
                        float(candidate[field]) - float(default[field])
                    )
            if not differences["availability_pct"]:
                continue
            lines.append(
                "| {label} | {availability:+.2f} +/- {availability_ci:.2f} pp | "
                "{effective:+.0f} +/- {effective_ci:.0f} ms | "
                "{outage:+.1f} +/- {outage_ci:.1f} s |".format(
                    label=label,
                    availability=_mean(differences["availability_pct"]),
                    availability_ci=_ci95(differences["availability_pct"]),
                    effective=_mean(differences["effective_mean_ms"]),
                    effective_ci=_ci95(differences["effective_mean_ms"]),
                    outage=_mean(differences["max_outage_s"]),
                    outage_ci=_ci95(differences["max_outage_s"]),
                )
            )
    latest_rows = [
        row
        for row in scenario_rows
        if row["strategy_key"] == "leastload_latest_hysteresis_60s"
        and row["scenario_key"] != "all"
    ]
    if latest_rows:
        lines.extend(
            [
                "",
                "## Latest-result health plus hysteresis by scenario",
                "",
                "| Scenario | Availability (95% CI) | Effective response (95% CI) | Mean longest outage | Switches/min |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for row in latest_rows:
            lines.append(
                "| {scenario} | {availability_mean_pct:.2f}% +/- {availability_ci95_pct:.2f} | "
                "{effective_mean_ms:.0f} +/- {effective_ci95_ms:.0f} ms | "
                "{max_outage_mean_s:.1f} s | {switches_per_min_mean:.2f} |".format(**row)
            )
    history_hysteresis_rows = [
        row
        for row in scenario_rows
        if row["strategy_key"] == "leastload_history_guard_hysteresis_60s"
        and row["scenario_key"] != "all"
    ]
    if history_hysteresis_rows:
        lines.extend(
            [
                "",
                "## History Guard plus hysteresis by scenario",
                "",
                "| Scenario | Availability (95% CI) | Effective response (95% CI) | Mean longest outage | Switches/min |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for row in history_hysteresis_rows:
            lines.append(
                "| {scenario} | {availability_mean_pct:.2f}% +/- {availability_ci95_pct:.2f} | "
                "{effective_mean_ms:.0f} +/- {effective_ci95_ms:.0f} ms | "
                "{max_outage_mean_s:.1f} s | {switches_per_min_mean:.2f} |".format(**row)
            )
        lines.extend(
            [
                "",
                "## Paired effect of adding hysteresis to History Guard",
                "",
                "Each value is the hysteretic result minus original History Guard on the same generated world. Negative effective response and switching deltas are improvements; a positive outage delta is a cost.",
                "",
                "| Scenario | Availability delta | Effective-response delta | Switches/min delta | Longest-outage delta |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        paired_scenarios = (("all", "All scenarios"),) + tuple(
            (key, get_fleet_scenario(key).label)
            for key in config.scenario_profiles
        )
        for scenario_key, scenario_label in paired_scenarios:
            scoped_rows = (
                trial_rows
                if scenario_key == "all"
                else [
                    row
                    for row in trial_rows
                    if row["scenario_key"] == scenario_key
                ]
            )
            by_trial: dict[int, dict[str, dict[str, object]]] = defaultdict(dict)
            for row in scoped_rows:
                by_trial[int(row["trial"])][str(row["strategy_key"])] = row
            differences = {
                "availability_pct": [],
                "effective_mean_ms": [],
                "route_switches_per_min": [],
                "max_outage_s": [],
            }
            for strategies in by_trial.values():
                sticky = strategies.get("leastload_history_guard_hysteresis_60s")
                plain = strategies.get("leastload_history_guard_60s")
                if sticky is None or plain is None:
                    continue
                for field in differences:
                    differences[field].append(
                        float(sticky[field]) - float(plain[field])
                    )
            if not differences["availability_pct"]:
                continue
            lines.append(
                "| {scenario} | {availability:+.2f} +/- {availability_ci:.2f} pp | "
                "{effective:+.0f} +/- {effective_ci:.0f} ms | "
                "{switches:+.2f} +/- {switches_ci:.2f} | "
                "{outage:+.1f} +/- {outage_ci:.1f} s |".format(
                    scenario=scenario_label,
                    availability=_mean(differences["availability_pct"]),
                    availability_ci=_ci95(differences["availability_pct"]),
                    effective=_mean(differences["effective_mean_ms"]),
                    effective_ci=_ci95(differences["effective_mean_ms"]),
                    switches=_mean(differences["route_switches_per_min"]),
                    switches_ci=_ci95(differences["route_switches_per_min"]),
                    outage=_mean(differences["max_outage_s"]),
                    outage_ci=_ci95(differences["max_outage_s"]),
                )
            )
    lines.extend(
        [
            "",
            "The rank uses mean effective response, where each failed connection attempt counts as the configured timeout penalty. Availability and successful-only latency remain separate columns so the penalty does not hide the trade-off.",
            "",
            "## Alternative ranks in hard scenarios",
            "",
            "| Scenario | Best alternative | Alternative availability | Alternative effective | Exact Xray availability | Exact Xray effective | Effective improvement |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    alternative_keys = {
        "leastload_history_guard_hysteresis_60s",
        "leastload_latest_hysteresis_60s",
        "leastload_mean_latency_60s",
        "leastload_success_rate_60s",
        "leastload_expected_cost_60s",
        "leastload_reliability_lcb_60s",
        "leastload_history_guard_60s",
    }
    for scenario_key in (
        "fast_but_flaky",
        "shared_provider_failure",
        "rolling_degradation",
        "hostile_internet",
    ):
        alternatives = [
            row
            for row in scenario_rows
            if row["scenario_key"] == scenario_key
            and row["strategy_key"] in alternative_keys
        ]
        exact = next(
            (
                row
                for row in scenario_rows
                if row["scenario_key"] == scenario_key
                and row["strategy_key"] == "leastload_default_60s"
            ),
            None,
        )
        if not alternatives or exact is None:
            continue
        best = min(alternatives, key=lambda row: float(row["effective_mean_ms"]))
        improvement = float(exact["effective_mean_ms"]) - float(best["effective_mean_ms"])
        lines.append(
            "| {scenario} | {strategy} | {availability_mean_pct:.2f}% | "
            "{effective_mean_ms:.0f} ms | {exact_availability:.2f}% | "
            "{exact_effective:.0f} ms | {improvement:+.0f} ms |".format(
                **best,
                exact_availability=float(exact["availability_mean_pct"]),
                exact_effective=float(exact["effective_mean_ms"]),
                improvement=improvement,
            )
        )
    lines.extend(
        [
            "",
            "Positive improvement means the best experimental rank reduced the failure-penalized effective response relative to exact Xray leastLoad at the same 60-second burst settings.",
            "",
            "See `metadata.json` for the complete strategy settings and modeling assumptions, `summary.csv` for confidence intervals, and `figures/` for plots.",
        ]
    )
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_experiment(
    config: ExperimentConfig = ExperimentConfig(),
    specs: tuple[StrategySpec, ...] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> ExperimentResult:
    if config.trials <= 0:
        raise ValueError("trials must be positive")
    if config.duration_s <= 0:
        raise ValueError("duration_s must be positive")
    if not 5 <= config.min_outbounds <= config.max_outbounds <= 20:
        raise ValueError("experiment outbounds must satisfy 5 <= min <= max <= 20")
    if config.attempt_interval_s <= 0:
        raise ValueError("attempt_interval_s must be positive")
    if config.failure_penalty_ms <= 0:
        raise ValueError("failure_penalty_ms must be positive")
    if not config.scenario_profiles:
        raise ValueError("scenario_profiles must contain at least one scenario key")
    if len(set(config.scenario_profiles)) != len(config.scenario_profiles):
        raise ValueError("scenario_profiles must not contain duplicate keys")
    for scenario_key in config.scenario_profiles:
        get_fleet_scenario(scenario_key)
    _resolved_worker_count(config)

    specs = default_strategy_specs() if specs is None else specs
    if not specs:
        raise ValueError("at least one strategy spec is required")
    if len({spec.key for spec in specs}) != len(specs):
        raise ValueError("strategy spec keys must be unique")
    validate_experiment_probe_floor(specs)
    output_dir = Path(config.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    rows, world_scores = _collect_trial_results(config, specs, progress)

    summary_rows = _summarize(rows, specs)
    aggregate_summary_rows = [
        row for row in summary_rows if row["scenario_key"] == "all"
    ]
    representative_trial = _representative_trial(world_scores)
    representative_world = generate_world(
        seed=_trial_seed(config.seed, representative_trial),
        duration_s=config.duration_s,
        min_outbounds=config.min_outbounds,
        max_outbounds=config.max_outbounds,
        scenario_key=_scenario_for_trial(config, representative_trial),
    )
    representative_simulations = [
        simulate_strategy(
            representative_world,
            spec,
            failure_penalty_ms=config.failure_penalty_ms,
            attempt_interval_s=config.attempt_interval_s,
            keep_attempts=True,
        )
        for spec in specs
    ]

    _write_csv(output_dir / "trial_metrics.csv", rows)
    _write_csv(output_dir / "summary.csv", summary_rows)
    _write_representative_data(output_dir, representative_world, representative_simulations)
    _make_plots(
        output_dir,
        aggregate_summary_rows,
        summary_rows,
        rows,
        representative_world,
        representative_simulations,
        config.failure_penalty_ms,
    )
    _write_report(
        output_dir,
        config,
        aggregate_summary_rows,
        summary_rows,
        rows,
        representative_trial,
    )

    scenario_counts = Counter(
        _scenario_for_trial(config, trial) for trial, _ in world_scores
    )
    scenario_metadata = []
    for scenario_key in config.scenario_profiles:
        scenario = get_fleet_scenario(scenario_key)
        scenario_metadata.append(
            {
                "key": scenario.key,
                "label": scenario.label,
                "description": scenario.description,
                "worlds": scenario_counts[scenario.key],
            }
        )

    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {**asdict(config), "output_dir": str(output_dir)},
        "total_worlds": config.trials,
        "trial_semantics": (
            "trials is the total number of generated worlds; named scenarios are "
            "assigned round-robin and every world is evaluated with every strategy"
        ),
        "workers": {
            "requested": config.workers,
            "resolved": _resolved_worker_count(config),
            "start_method": "spawn" if _resolved_worker_count(config) > 1 else "sequential",
        },
        "scenario_profiles": scenario_metadata,
        "xray_core": {
            "commit": XRAY_CORE_COMMIT,
            "source": f"https://github.com/XTLS/Xray-core/tree/{XRAY_CORE_COMMIT}/app/router",
            "files": [
                "app/router/balancing.go",
                "app/router/strategy_random.go",
                "app/router/strategy_leastping.go",
                "app/router/strategy_leastload.go",
                "app/observatory/observer.go",
                "app/observatory/burst/healthping.go",
                "app/observatory/burst/healthping_result.go",
                "app/observatory/burst/burstobserver.go",
            ],
        },
        "v2rayng": {
            "commit": V2RAYNG_COMMIT,
            "source": f"https://github.com/2dust/v2rayNG/tree/{V2RAYNG_COMMIT}/V2rayNG/app/src/main",
            "defaults": {
                "delay_test_url": "https://www.gstatic.com/generate_204",
                "least_ping_interval": "3m",
                "least_ping_concurrency": True,
                "least_load_interval": "5m",
                "least_load_http_method": "HEAD",
                "least_load_sampling": 2,
                "least_load_timeout": "30s",
            },
        },
        "strategy_specs": [asdict(spec) for spec in specs],
        "assumptions": [
            "One new connection attempt is routed each simulated second; existing connections are outside scope.",
            "Xray balancer selection does not automatically retry a failed selected outbound.",
            "All strategies in a trial share identical outbound traffic and the same per-second potential probe outcomes.",
            "Strategies with identical burst settings share the exact randomized probe-start schedule and retained raw histories; selection randomness is seeded separately.",
            "Fleet scenarios are assigned to global trial indices in deterministic round-robin order.",
            "Worker scheduling cannot change seeds, scenario assignment, row order, or results.",
            "Reported 95% confidence intervals are two-sided Student-t intervals for the mean across independent generated worlds.",
            "Classic observatory is modeled in concurrent mode: results publish at request completion, then the next interval begins after the slowest completion.",
            "Burst observatory uses one immediate sample plus randomized samples over interval*sampling windows; results publish at completion and scheduled results finishing after the next-window cancellation are discarded.",
            "Successful probe completion is quantized upward to the next one-second tick; failed probes consume their configured timeout because the numerical world does not distinguish fast refusal from black-hole timeout.",
            "History Guard is experimental and assumes the router can read ordered raw completion timestamps and results for all retained burst samples, which current Xray does not expose.",
            "History Guard plus hysteresis keeps the same raw-history assumption, immediately escapes a newest failure, and otherwise requires a 5-point reliability gain or a 25 ms/20% expected-cost improvement after a 20-second dwell.",
            "Latest-result health plus hysteresis is experimental and assumes the router can read each outbound's newest retained raw burst-probe result; current Xray exposes only aggregates.",
            "Health-aware random/roundRobin and leastPing/leastLoad use explicit fallbackTag out-00; blind random/roundRobin have no fallback or observer.",
            f"A failed attempt counts as {config.failure_penalty_ms:g} ms only in combined effective-response metrics; latency-only columns exclude failures.",
        ],
        "representative_trial": representative_trial,
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )

    return ExperimentResult(
        config=config,
        summary_rows=tuple(summary_rows),
        trial_rows=tuple(rows),
        representative_trial=representative_trial,
        output_dir=output_dir,
    )
