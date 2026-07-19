"""Browser payload adapter and optional local server for Xray Route Lab."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from functools import lru_cache
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .model import fleet_scenarios, generate_world, get_fleet_scenario
from .simulation import simulate_strategy
from .strategies import StrategySpec, default_strategy_specs


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "build" / "pages"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "monte-carlo"
DEFAULT_DURATION_S = 30 * 60
DEFAULT_SEED = 20_260_719
DEFAULT_OUTBOUNDS = 12
MIN_OUTBOUNDS = 5
MAX_OUTBOUNDS = 20
DEFAULT_STRATEGY_KEY = "leastping_default_60s"


def _strategy_description(spec: StrategySpec) -> str:
    if spec.kind in {"random", "roundrobin"} and not spec.health_filter:
        return "Selects from the complete candidate pool without observatory health filtering."
    if spec.kind == "random":
        return "Randomly selects among candidates currently marked alive by the classic observatory."
    if spec.kind == "roundrobin":
        return "Cycles through candidates currently marked alive by the classic observatory."
    if spec.kind == "leastping":
        if "v2rayng" in spec.key:
            return "Uses Xray leastPing with v2rayNG's default concurrent 3-minute classic observatory."
        return "Uses default leastPing behavior: select the alive candidate with the lowest latest delay."
    alternative_descriptions = {
        "mean_latency": "Ranks the same burst statistics by successful mean RTT first, then observed failure rate.",
        "success_rate": "Ranks the same burst statistics by observed success rate first, then successful mean RTT.",
        "expected_cost": "Ranks by empirical expected response: successful RTT plus a configured cost for failed probes.",
        "reliability_lcb": "Ranks by the 95% Wilson lower confidence bound for probe success, then expected response cost.",
        "history_guard": "Uses all timestamped retained probes: opens a circuit after the latest failure, estimates next-attempt success with a recency-aware Bayesian state filter, and considers latency only after reliability.",
        "history_guard_hysteresis": "Uses History Guard's recency-aware reliability model, but immediately switches only after a newest failure; healthy-route changes require a decisive reliability gain or a material expected-cost improvement after a minimum dwell.",
        "latest_health_hysteresis": "Uses only each outbound's newest completed probe: a newest failure forces an immediate escape, while healthy-route changes require a material latency improvement and a minimum dwell.",
    }
    if spec.rank_mode in alternative_descriptions:
        return alternative_descriptions[spec.rank_mode]
    if "v2rayng" in spec.key:
        return "Uses Xray's exact leastLoad selector with v2rayNG's default 5-minute, 2-sample, 30-second-timeout burst observatory."
    if spec.key.startswith("leastload_tolerance_"):
        return "Uses Xray's exact leastLoad selector, excluding an otherwise-alive outbound when its retained burst-probe failure fraction is strictly greater than the configured tolerance."
    if spec.expected == 0:
        return "Uses Xray's default leastLoad strategy settings and burst-observatory statistics."
    return "Tuned leastLoad control: randomly choose from two qualified low-deviation candidates."


def _strategy_payload(spec: StrategySpec) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    observatory: dict[str, Any] | None = None
    if spec.kind == "leastload":
        settings = {
            "rank_mode": spec.rank_mode,
            "expected": spec.expected,
            "baselines_ms": list(spec.baselines_ms),
            "max_rtt_ms": spec.max_rtt_ms,
            "tolerance": spec.tolerance,
        }
        if spec.rank_mode in {"expected_cost", "reliability_lcb"}:
            settings["failure_cost_ms"] = spec.failure_cost_ms
        if spec.rank_mode == "history_guard":
            settings.update(
                {
                    "failure_cost_ms": spec.failure_cost_ms,
                    "state_half_life_s": spec.history_state_half_life_s,
                    "prior_strength": spec.history_prior_strength,
                    "probability_margin": spec.history_probability_margin,
                    "raw_history": "last 10 timestamped probes",
                }
            )
        if spec.rank_mode == "history_guard_hysteresis":
            settings.update(
                {
                    "failure_cost_ms": spec.failure_cost_ms,
                    "state_half_life_s": spec.history_state_half_life_s,
                    "prior_strength": spec.history_prior_strength,
                    "probability_margin": spec.history_probability_margin,
                    "switch_probability_margin": spec.history_switch_probability_margin,
                    "hysteresis_ms": spec.history_hysteresis_ms,
                    "hysteresis_ratio": spec.history_hysteresis_ratio,
                    "minimum_dwell_s": spec.history_min_dwell_s,
                    "raw_history": "last 10 timestamped probes",
                }
            )
        if spec.rank_mode == "latest_health_hysteresis":
            settings.update(
                {
                    "raw_history": "newest timestamped probe per outbound",
                    "hysteresis_ms": spec.latest_hysteresis_ms,
                    "hysteresis_ratio": spec.latest_hysteresis_ratio,
                    "minimum_dwell_s": spec.latest_min_dwell_s,
                }
            )
        observatory = {
            "type": "burst",
            "interval_s": spec.burst_interval_s,
            "sampling": spec.sampling_count,
            "timeout_s": spec.probe_timeout_s,
            "url": spec.probe_url or "https://connectivitycheck.gstatic.com/generate_204",
            "http_method": spec.probe_http_method or "HEAD",
        }
    elif spec.kind == "leastping" or spec.health_filter:
        observatory = {
            "type": "classic",
            "interval_s": spec.observer_interval_s,
            "concurrent": True,
            "timeout_s": spec.probe_timeout_s,
            "url": spec.probe_url or "https://www.google.com/generate_204",
            "http_method": spec.probe_http_method or "GET",
        }
    else:
        observatory = {"type": "none"}
    return {
        "key": spec.key,
        "label": spec.label,
        "kind": spec.kind,
        "description": _strategy_description(spec),
        "settings": settings,
        "observatory": observatory,
    }


def catalog_payload() -> dict[str, Any]:
    scenarios = fleet_scenarios()
    specs = default_strategy_specs()
    return {
        "duration_s": DEFAULT_DURATION_S,
        "outbound_range": {
            "min": MIN_OUTBOUNDS,
            "max": MAX_OUTBOUNDS,
            "default": DEFAULT_OUTBOUNDS,
        },
        "defaults": {
            "scenario": scenarios[0].key,
            "strategy": DEFAULT_STRATEGY_KEY,
            "seed": DEFAULT_SEED,
            "outbounds": DEFAULT_OUTBOUNDS,
        },
        "scenarios": [
            {
                "key": scenario.key,
                "label": scenario.label,
                "description": scenario.description,
                "required_archetypes": list(scenario.required_archetypes),
            }
            for scenario in scenarios
        ],
        "strategies": [_strategy_payload(spec) for spec in specs],
    }


def _finite_or_none(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@lru_cache(maxsize=48)
def simulation_payload(
    scenario_key: str,
    strategy_key: str,
    seed: int,
    outbound_count: int,
    duration_s: int = DEFAULT_DURATION_S,
) -> dict[str, Any]:
    if not MIN_OUTBOUNDS <= outbound_count <= MAX_OUTBOUNDS:
        raise ValueError(
            f"outbounds must be between {MIN_OUTBOUNDS} and {MAX_OUTBOUNDS}"
        )
    if not 60 <= duration_s <= DEFAULT_DURATION_S:
        raise ValueError("duration_s must be between 60 and 1800")
    scenario = get_fleet_scenario(scenario_key)
    specs = {spec.key: spec for spec in default_strategy_specs()}
    try:
        spec = specs[strategy_key]
    except KeyError as error:
        raise ValueError(f"unknown strategy {strategy_key!r}") from error

    world = generate_world(
        seed=seed,
        duration_s=duration_s,
        min_outbounds=MIN_OUTBOUNDS,
        max_outbounds=MAX_OUTBOUNDS,
        outbound_count=outbound_count,
        scenario_key=scenario_key,
    )
    result = simulate_strategy(
        world,
        spec,
        failure_penalty_ms=5000.0,
        attempt_interval_s=1,
        keep_attempts=True,
    )
    attempts = result.attempts
    route_switches = sum(
        left.selected_tag != right.selected_tag
        for left, right in zip(attempts, attempts[1:])
    )
    metrics = {
        key: _finite_or_none(value) for key, value in result.metrics.items()
    }
    metrics.update(
        {
            "route_switches": route_switches,
            "oracle_availability_pct": world.oracle_availability() * 100.0,
            "raw_outbound_availability_pct": world.raw_mean_availability() * 100.0,
        }
    )
    return {
        "duration_s": world.duration_s,
        "step_s": 1,
        "failure_penalty_ms": 5000.0,
        "scenario": {
            "key": scenario.key,
            "label": scenario.label,
            "description": scenario.description,
        },
        "strategy": _strategy_payload(spec),
        "metrics": metrics,
        "outbounds": [
            {
                "tag": outbound.tag,
                "archetype": outbound.archetype,
                "provider": outbound.provider,
                "traffic_latency_ms": [
                    round(value, 2) if value is not None else None
                    for value in outbound.traffic_latency_ms
                ],
                "traffic_success": list(outbound.traffic_success),
                "expected_latency_ms": [
                    round(value, 2) for value in outbound.expected_latency_ms
                ],
                "success_probability": [
                    round(value, 5) for value in outbound.success_probability
                ],
            }
            for outbound in world.outbounds
        ],
        "route": {
            "selected_tag": [attempt.selected_tag for attempt in attempts],
            "success": [attempt.success for attempt in attempts],
            "latency_ms": [
                round(attempt.latency_ms, 2)
                if attempt.latency_ms is not None
                else None
                for attempt in attempts
            ],
            "effective_latency_ms": [
                round(attempt.effective_latency_ms, 2) for attempt in attempts
            ],
            "used_fallback": [attempt.used_fallback for attempt in attempts],
        },
    }


def summary_payload(output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    summary_path = output_dir / "summary.csv"
    metadata_path = output_dir / "metadata.json"
    if not summary_path.exists():
        return {"generated_at": None, "trials": 0, "rows": []}
    with summary_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    metadata: dict[str, Any] = {}
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    numeric_fields = {
        "trials": int,
        "availability_mean_pct": float,
        "effective_mean_ms": float,
        "success_p95_mean_ms": float,
        "max_outage_mean_s": float,
        "max_outage_max_s": float,
        "effective_rank": int,
    }
    converted: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = dict(row)
        item["strategy_label"] = item.get("strategy", "")
        for field, converter in numeric_fields.items():
            if item.get(field) not in {None, ""}:
                converted_value = converter(item[field])
                item[field] = _finite_or_none(converted_value)
        converted.append(item)
    return {
        "generated_at": metadata.get("generated_at"),
        "trials": metadata.get("config", {}).get("trials", 0),
        "rows": converted,
    }


class RouteLabHandler(SimpleHTTPRequestHandler):
    """Same-origin JSON API plus the static dashboard files."""

    output_dir = DEFAULT_OUTPUT_DIR

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/catalog":
                self._send_json(catalog_payload())
                return
            if parsed.path == "/api/summary":
                self._send_json(summary_payload(self.output_dir))
                return
            if parsed.path == "/api/simulate":
                query = parse_qs(parsed.query)
                defaults = catalog_payload()["defaults"]
                scenario_key = query.get("scenario", [defaults["scenario"]])[0]
                strategy_key = query.get("strategy", [defaults["strategy"]])[0]
                seed = int(query.get("seed", [defaults["seed"]])[0])
                outbound_count = int(
                    query.get("outbounds", [defaults["outbounds"]])[0]
                )
                self._send_json(
                    simulation_payload(
                        scenario_key,
                        strategy_key,
                        seed,
                        outbound_count,
                    )
                )
                return
        except (TypeError, ValueError) as error:
            self._send_json(
                {"error": str(error)}, status=HTTPStatus.BAD_REQUEST
            )
            return
        except Exception as error:  # pragma: no cover - defensive HTTP boundary
            self.log_error("simulation API error: %s", error)
            self._send_json(
                {"error": "The simulation could not be completed."},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        super().do_GET()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the Xray Route Lab locally.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not WEB_ROOT.is_dir():
        raise SystemExit(f"web UI not found: {WEB_ROOT}")
    RouteLabHandler.output_dir = args.output.resolve()
    server = ThreadingHTTPServer((args.host, args.port), RouteLabHandler)
    print(f"Xray Route Lab: http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
