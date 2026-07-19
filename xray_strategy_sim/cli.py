"""Command-line entry point."""

from __future__ import annotations

import argparse
import multiprocessing
from pathlib import Path

from .experiment import ExperimentConfig, run_experiment
from .model import DEFAULT_SCENARIO_KEYS, fleet_scenarios
from .strategies import default_strategy_specs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simulate unreliable Xray outbounds and balancer strategies."
    )
    parser.add_argument("--trials", type=int, default=300)
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--min-outbounds", type=int, default=5)
    parser.add_argument("--max-outbounds", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20_260_719)
    parser.add_argument("--attempt-interval", type=int, default=1, metavar="SECONDS")
    parser.add_argument("--failure-penalty-ms", type=float, default=5000.0)
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        metavar="N",
        help="worker processes (default: 0 = all logical CPUs)",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=DEFAULT_SCENARIO_KEYS,
        dest="scenarios",
        help="fleet scenario to include; repeat to select several (default: all)",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="list named fleet scenarios and exit",
    )
    strategy_keys = tuple(spec.key for spec in default_strategy_specs())
    parser.add_argument(
        "--strategy",
        action="append",
        choices=strategy_keys,
        dest="strategies",
        help="strategy preset to include; repeat to select several (default: all)",
    )
    parser.add_argument(
        "--list-strategies",
        action="store_true",
        help="list strategy presets and exit",
    )
    parser.add_argument("--output", type=Path, default=Path("outputs"))
    return parser


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    args = build_parser().parse_args(argv)
    if args.list_scenarios:
        for scenario in fleet_scenarios():
            print(f"{scenario.key:<28} {scenario.label}: {scenario.description}")
        return 0
    all_specs = default_strategy_specs()
    if args.list_strategies:
        for spec in all_specs:
            print(f"{spec.key:<36} {spec.label}")
        return 0
    requested = set(args.strategies or ())
    specs = tuple(spec for spec in all_specs if not requested or spec.key in requested)
    duration_s = round(args.minutes * 60)
    config = ExperimentConfig(
        trials=args.trials,
        duration_s=duration_s,
        min_outbounds=args.min_outbounds,
        max_outbounds=args.max_outbounds,
        seed=args.seed,
        attempt_interval_s=args.attempt_interval,
        failure_penalty_ms=args.failure_penalty_ms,
        scenario_profiles=tuple(args.scenarios or DEFAULT_SCENARIO_KEYS),
        workers=args.workers,
        output_dir=args.output,
    )

    last_reported = 0

    def show_progress(completed: int, total: int) -> None:
        nonlocal last_reported
        stride = max(1, total // 10)
        if completed == total or completed - last_reported >= stride:
            print(f"Completed {completed}/{total} trials", flush=True)
            last_reported = completed

    result = run_experiment(config, specs=specs, progress=show_progress)
    print(f"Results: {result.output_dir}")
    print("Top strategies by combined effective response:")
    aggregate_rows = [
        row for row in result.summary_rows if row["scenario_key"] == "all"
    ]
    for row in aggregate_rows[:5]:
        print(
            f"  {row['effective_rank']:>2}. {row['strategy']:<29} "
            f"availability={row['availability_mean_pct']:.2f}%  "
            f"effective={row['effective_mean_ms']:.0f} ms"
        )
    return 0
