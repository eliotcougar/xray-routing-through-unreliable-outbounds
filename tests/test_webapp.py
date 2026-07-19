from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

from xray_strategy_sim.webapp import catalog_payload, simulation_payload, summary_payload


class WebAppTests(unittest.TestCase):
    def test_hidden_panels_cannot_override_the_hidden_attribute(self) -> None:
        styles = (Path(__file__).parents[1] / "site" / "styles.css").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            styles,
            re.compile(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", re.S),
        )

    def test_catalog_exposes_every_scenario_and_strategy(self) -> None:
        catalog = catalog_payload()
        self.assertGreaterEqual(len(catalog["scenarios"]), 8)
        self.assertEqual(len(catalog["strategies"]), 31)
        self.assertEqual(catalog["outbound_range"]["min"], 5)
        self.assertEqual(catalog["outbound_range"]["max"], 20)
        intervals = [
            strategy["observatory"].get("interval_s")
            for strategy in catalog["strategies"]
            if strategy["observatory"]["type"] != "none"
        ]
        self.assertTrue(all(interval >= 60 for interval in intervals))
        by_key = {strategy["key"]: strategy for strategy in catalog["strategies"]}
        self.assertEqual(
            by_key["leastload_v2rayng_burst_300s"]["observatory"],
            {
                "type": "burst",
                "interval_s": 300,
                "sampling": 2,
                "timeout_s": 30,
                "url": "https://www.gstatic.com/generate_204",
                "http_method": "HEAD",
            },
        )
        self.assertEqual(
            by_key["leastload_latest_hysteresis_60s"]["settings"],
            {
                "rank_mode": "latest_health_hysteresis",
                "expected": 1,
                "baselines_ms": [],
                "max_rtt_ms": 0.0,
                "tolerance": 0.0,
                "raw_history": "newest timestamped probe per outbound",
                "hysteresis_ms": 25.0,
                "hysteresis_ratio": 0.20,
                "minimum_dwell_s": 20,
            },
        )
        self.assertEqual(
            by_key["leastload_history_guard_hysteresis_60s"]["settings"],
            {
                "rank_mode": "history_guard_hysteresis",
                "expected": 1,
                "baselines_ms": [],
                "max_rtt_ms": 0.0,
                "tolerance": 0.0,
                "failure_cost_ms": 5000.0,
                "state_half_life_s": 180.0,
                "prior_strength": 2.0,
                "probability_margin": 0.0,
                "switch_probability_margin": 0.05,
                "hysteresis_ms": 25.0,
                "hysteresis_ratio": 0.20,
                "minimum_dwell_s": 20,
                "raw_history": "last 10 timestamped probes",
            },
        )
        self.assertEqual(
            by_key["leastload_tolerance_30_60s"]["settings"]["tolerance"],
            0.30,
        )

    def test_simulation_payload_is_aligned_and_strict_json(self) -> None:
        payload = simulation_payload(
            "fast_but_flaky",
            "leastping_default_60s",
            12345,
            5,
            60,
        )
        self.assertEqual(payload["duration_s"], 60)
        self.assertEqual(len(payload["outbounds"]), 5)
        self.assertEqual(len(payload["route"]["selected_tag"]), 60)
        outbound_tags = {outbound["tag"] for outbound in payload["outbounds"]}
        self.assertLessEqual(set(payload["route"]["selected_tag"]), outbound_tags)
        self.assertTrue(
            all(len(outbound["traffic_success"]) == 60 for outbound in payload["outbounds"])
        )
        json.dumps(payload, allow_nan=False)

    def test_outbound_matrix_wires_the_selected_route_overlay(self) -> None:
        app = (Path(__file__).parents[1] / "site" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("function drawSelectedRoute(", app)
        self.assertIn(
            "drawSelectedRoute(context, result.routing.selectedTag, rows, plot, rowHeight)",
            app,
        )

    def test_monte_carlo_context_can_switch_between_scenario_and_all(self) -> None:
        root = Path(__file__).parents[1]
        app = (root / "site" / "app.js").read_text(encoding="utf-8")
        html = (root / "site" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="summary-scope"', html)
        self.assertIn('appendOption(elements.summaryScope, "all", "All scenarios")', app)
        self.assertIn("elements.summaryScope.value = elements.scenario.value", app)
        self.assertIn("row.scenarioKey === selectedScenarioKey", app)
        self.assertIn('elements.summaryScope.addEventListener("change", renderSummaryTable)', app)
        self.assertNotIn("if (!rows.length) rows = state.summary.rows", app)

    def test_monte_carlo_table_exposes_worst_outage_and_sortable_headers(self) -> None:
        root = Path(__file__).parents[1]
        app = (root / "site" / "app.js").read_text(encoding="utf-8")
        html = (root / "site" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('data-summary-sort="'), 6)
        self.assertIn("Worst observed outage", html)
        self.assertIn("row.max_outage_max_s", app)
        self.assertIn("state.summarySort.direction", app)
        self.assertIn('header?.setAttribute("aria-sort"', app)

    def test_summary_payload_converts_absolute_max_outage(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "summary_with_worst_outage"
        payload = summary_payload(fixture)
        self.assertEqual(payload["trials"], 400)
        self.assertEqual(payload["rows"][0]["trials"], 50)
        self.assertEqual(payload["rows"][0]["max_outage_max_s"], 774.0)

    def test_published_site_uses_static_data_and_browser_worker(self) -> None:
        root = Path(__file__).parents[1]
        app = (root / "site" / "app.js").read_text(encoding="utf-8")
        worker = (root / "site" / "simulator-worker.js").read_text(encoding="utf-8")
        self.assertIn('./data/catalog.json', app)
        self.assertIn('./data/summary.json', app)
        self.assertIn('simulateInBrowser(request, controller.signal)', app)
        self.assertNotIn('/api/simulate', app)
        self.assertIn('const PYODIDE_VERSION = "v314.0.2"', worker)
        self.assertIn('cdn.jsdelivr.net/pyodide/', worker)
        self.assertIn('simulation_payload', worker)

    def test_simulation_rejects_outbound_count_outside_ui_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 5 and 20"):
            simulation_payload(
                "mixed_real_world",
                "leastping_default_60s",
                1,
                4,
                60,
            )


if __name__ == "__main__":
    unittest.main()
