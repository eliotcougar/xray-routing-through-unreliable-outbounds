"use strict";

import { loadPyodide } from "./pyodide/pyodide.mjs";

const PYODIDE_VERSION = "314.0.2";
const PYODIDE_BASE = new URL("./pyodide/", self.location.href).href;
const MODULES = [
  "__init__.py",
  "model.py",
  "observatory.py",
  "strategies.py",
  "simulation.py",
  "webapp.py",
];

let runtimePromise = null;

function publishStatus(message) {
  self.postMessage({ type: "status", message });
}

async function loadRuntime() {
  publishStatus(`Loading self-hosted Pyodide ${PYODIDE_VERSION}. This happens once per page load.`);
  const pyodide = await loadPyodide({ indexURL: PYODIDE_BASE });

  publishStatus("Loading the Xray simulation model into the browser worker.");
  pyodide.FS.mkdirTree("/app/xray_strategy_sim");
  for (const filename of MODULES) {
    const url = new URL(`./python/xray_strategy_sim/${filename}`, self.location.href);
    const response = await fetch(url);
    if (!response.ok) throw new Error(`Could not load ${filename} (HTTP ${response.status}).`);
    pyodide.FS.writeFile(`/app/xray_strategy_sim/${filename}`, await response.text());
  }

  await pyodide.runPythonAsync(`
import sys
sys.path.insert(0, "/app")
from xray_strategy_sim.webapp import simulation_payload
`);
  return pyodide;
}

function runtime() {
  if (!runtimePromise) runtimePromise = loadRuntime();
  return runtimePromise;
}

self.addEventListener("message", async (event) => {
  const { id, request } = event.data ?? {};
  if (!id || !request) return;
  try {
    const pyodide = await runtime();
    publishStatus("Generating correlated outbound traces and replaying the selected balancer.");
    pyodide.globals.set("browser_scenario", String(request.scenario));
    pyodide.globals.set("browser_strategy", String(request.strategy));
    pyodide.globals.set("browser_seed", Number(request.seed));
    pyodide.globals.set("browser_outbounds", Number(request.outbounds));
    const serialized = await pyodide.runPythonAsync(`
import json
json.dumps(
    simulation_payload(
        browser_scenario,
        browser_strategy,
        browser_seed,
        browser_outbounds,
    ),
    ensure_ascii=False,
    allow_nan=False,
    separators=(",", ":"),
)
`);
    self.postMessage({ id, type: "result", payload: JSON.parse(serialized) });
  } catch (error) {
    self.postMessage({
      id,
      type: "error",
      error: error instanceof Error ? error.message : String(error),
    });
  }
});
