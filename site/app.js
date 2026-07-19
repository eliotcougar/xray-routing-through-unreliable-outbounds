"use strict";

const STATIC_ASSETS = Object.freeze({
  catalog: "./data/catalog.json",
  summary: "./data/summary.json",
});

const simulatorWorker = new Worker("./simulator-worker.js?v=20260720-2", { type: "module" });
const workerRequests = new Map();
let workerRequestSequence = 0;

const $ = (selector) => document.querySelector(selector);
const elements = {
  form: $("#simulation-form"),
  scenario: $("#scenario-select"),
  strategy: $("#strategy-select"),
  count: $("#outbound-count"),
  countDown: $("#count-down"),
  countUp: $("#count-up"),
  seed: $("#seed-input"),
  randomSeed: $("#randomize-seed"),
  simulate: $("#simulate-button"),
  scenarioDescription: $("#scenario-description"),
  strategySettings: $("#strategy-settings"),
  catalogError: $("#catalog-error"),
  catalogErrorMessage: $("#catalog-error-message"),
  retryCatalog: $("#retry-catalog"),
  simulationError: $("#simulation-error"),
  simulationErrorMessage: $("#simulation-error-message"),
  retrySimulation: $("#retry-simulation"),
  results: $("#results"),
  resultContent: $("#result-content"),
  loadingPanel: $("#loading-panel"),
  loadingTitle: $("#loading-title"),
  loadingCopy: $("#loading-copy"),
  runStatus: $("#run-status"),
  liveDot: $(".live-dot"),
  durationLabel: $("#duration-label"),
  resultScenario: $("#result-scenario"),
  resultStrategy: $("#result-strategy"),
  resultSeed: $("#result-seed"),
  metricAvailability: $("#metric-availability"),
  metricAvailabilityNote: $("#metric-availability-note"),
  metricEffective: $("#metric-effective"),
  metricP95: $("#metric-p95"),
  metricOutage: $("#metric-outage"),
  metricSwitches: $("#metric-switches"),
  detailTitle: $("#outbound-detail-title"),
  detailCopy: $("#outbound-detail-copy"),
  profileBadge: $("#outbound-profile-badge"),
  availabilityWindow: $("#availability-window"),
  summaryStatus: $("#summary-status"),
  summaryScope: $("#summary-scope"),
  summaryCopy: $("#comparison-copy"),
  summaryBody: $("#summary-table-body"),
  summarySortButtons: [...document.querySelectorAll("[data-summary-sort]")],
  matrix: $("#outbound-matrix"),
  detail: $("#outbound-detail"),
  response: $("#response-chart"),
  availability: $("#availability-chart"),
};

const state = {
  catalog: null,
  summary: null,
  result: null,
  selectedOutbound: 0,
  simulationController: null,
  renderRequest: 0,
  hitTests: new Map(),
  summarySort: { key: "effectiveMs", direction: "asc" },
};

simulatorWorker.addEventListener("message", (event) => {
  const message = event.data ?? {};
  if (message.type === "status") {
    elements.loadingCopy.textContent = message.message;
    return;
  }
  const pending = workerRequests.get(message.id);
  if (!pending) return;
  workerRequests.delete(message.id);
  pending.cleanup();
  if (message.type === "result") pending.resolve(message.payload);
  else pending.reject(new Error(message.error || "The browser simulator failed."));
});

simulatorWorker.addEventListener("error", (event) => {
  const error = new Error(event.message || "The browser simulator could not start.");
  for (const pending of workerRequests.values()) {
    pending.cleanup();
    pending.reject(error);
  }
  workerRequests.clear();
});

function simulateInBrowser(request, signal) {
  return new Promise((resolve, reject) => {
    const id = ++workerRequestSequence;
    const abort = () => {
      workerRequests.delete(id);
      reject(new DOMException("Simulation cancelled.", "AbortError"));
    };
    const cleanup = () => signal?.removeEventListener("abort", abort);
    if (signal?.aborted) {
      abort();
      return;
    }
    signal?.addEventListener("abort", abort, { once: true });
    workerRequests.set(id, { resolve, reject, cleanup });
    simulatorWorker.postMessage({ id, request });
  });
}

const chartCanvases = [
  elements.matrix,
  elements.detail,
  elements.response,
  elements.availability,
];

function finite(value, fallback = null) {
  if (value === null || value === undefined || value === "") return fallback;
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function titleFromKey(value) {
  return String(value ?? "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatDuration(seconds) {
  const amount = Math.max(0, finite(seconds, 0));
  if (amount >= 60) {
    const minutes = amount / 60;
    return `${Number.isInteger(minutes) ? minutes : minutes.toFixed(1)} minute trace`;
  }
  return `${amount.toFixed(0)} second trace`;
}

function formatClock(seconds) {
  const safe = Math.max(0, finite(seconds, 0));
  const minutes = Math.floor(safe / 60);
  const remainder = Math.floor(safe % 60);
  return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

function formatMs(value, digits = 0) {
  const number = finite(value);
  if (number === null) return "—";
  if (number >= 1000) return `${(number / 1000).toFixed(number >= 10000 ? 1 : 2)} s`;
  return `${number.toFixed(digits)} ms`;
}

function formatPercent(value, digits = 2) {
  const number = finite(value);
  return number === null ? "—" : `${number.toFixed(digits)}%`;
}

function formatSeconds(value) {
  const number = finite(value);
  if (number === null) return "—";
  return number >= 60 ? `${(number / 60).toFixed(1)} min` : `${number.toFixed(0)} s`;
}

function percentile(values, fraction) {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const position = (sorted.length - 1) * fraction;
  const lower = Math.floor(position);
  const upper = Math.ceil(position);
  if (lower === upper) return sorted[lower];
  return sorted[lower] + (sorted[upper] - sorted[lower]) * (position - lower);
}

function average(values) {
  let total = 0;
  let count = 0;
  for (const value of values) {
    if (Number.isFinite(value)) {
      total += value;
      count += 1;
    }
  }
  return count ? total / count : null;
}

function rollingAverage(values, windowSize, multiplier = 1) {
  const result = new Array(values.length);
  const window = Math.max(1, Math.round(windowSize));
  let sum = 0;
  let valid = 0;
  for (let index = 0; index < values.length; index += 1) {
    const added = finite(values[index]);
    if (added !== null) {
      sum += added;
      valid += 1;
    }
    if (index >= window) {
      const removed = finite(values[index - window]);
      if (removed !== null) {
        sum -= removed;
        valid -= 1;
      }
    }
    result[index] = valid ? (sum / valid) * multiplier : null;
  }
  return result;
}

function rollingAvailability(values, windowSize) {
  return rollingAverage(values.map((value) => (value ? 1 : 0)), windowSize, 100);
}

function maximumOutage(success) {
  let current = 0;
  let maximum = 0;
  for (const item of success) {
    if (item) current = 0;
    else {
      current += 1;
      maximum = Math.max(maximum, current);
    }
  }
  return maximum;
}

function routeSwitches(tags) {
  let switches = 0;
  for (let index = 1; index < tags.length; index += 1) {
    if (tags[index] !== tags[index - 1]) switches += 1;
  }
  return switches;
}

function coerceBooleanArray(values, length, latency) {
  const source = asArray(values);
  if (source.length) {
    return Array.from({ length }, (_, index) => Boolean(source[index]));
  }
  return Array.from({ length }, (_, index) => Number.isFinite(latency[index]));
}

function coerceNumberArray(values, length) {
  const source = asArray(values);
  return Array.from({ length }, (_, index) => finite(source[index]));
}

function listFromObject(value) {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== "object") return [];
  return Object.entries(value).map(([key, item]) =>
    typeof item === "object" && item !== null ? { key, ...item } : { key, label: String(item) },
  );
}

function normalizeCatalog(payload) {
  if (!payload || typeof payload !== "object") throw new Error("The catalog response is not a JSON object.");
  const scenarios = listFromObject(payload.scenarios ?? payload.profiles).map((item, index) => {
    const key = String(item.key ?? item.id ?? item.value ?? `scenario-${index + 1}`);
    return {
      ...item,
      key,
      label: String(item.label ?? item.name ?? titleFromKey(key)),
      description: String(item.description ?? item.summary ?? "A generated outbound reliability profile."),
    };
  });
  const strategies = listFromObject(payload.strategies ?? payload.strategy_presets).map((item, index) => {
    const key = String(item.key ?? item.id ?? item.value ?? `strategy-${index + 1}`);
    return {
      ...item,
      key,
      label: String(item.label ?? item.name ?? titleFromKey(key)),
      kind: String(item.kind ?? item.type ?? "strategy"),
      description: String(item.description ?? ""),
      settings: {
        ...(item.settings && typeof item.settings === "object" ? item.settings : {}),
        ...(item.observatory && typeof item.observatory === "object"
          ? { observatory: item.observatory }
          : {}),
      },
    };
  });
  if (!scenarios.length) throw new Error("The catalog contains no scenarios.");
  if (!strategies.length) throw new Error("The catalog contains no strategy presets.");

  const range = payload.outbound_range ?? payload.outbounds ?? {};
  const minimum = finite(range.min, 5);
  const maximum = finite(range.max, 20);
  const defaultCount = clamp(finite(range.default, finite(payload.defaults?.outbounds, 12)), minimum, maximum);
  return {
    ...payload,
    scenarios,
    strategies,
    outboundRange: { minimum, maximum, defaultCount },
    durationS: finite(payload.duration_s, 1800),
    defaults: {
      scenario: String(payload.defaults?.scenario ?? scenarios[0].key),
      strategy: String(payload.defaults?.strategy ?? strategies[0].key),
      seed: finite(payload.defaults?.seed, 20260719),
      outbounds: defaultCount,
    },
  };
}

function pointsToArrays(points, fields) {
  const output = Object.fromEntries(fields.map((field) => [field, []]));
  for (const point of asArray(points)) {
    for (const field of fields) output[field].push(point?.[field] ?? null);
  }
  return output;
}

function normalizeOutbound(item, index) {
  const pointArrays = pointsToArrays(item.points, [
    "second",
    "latency_ms",
    "available",
    "success",
    "success_probability",
    "expected_latency_ms",
  ]);
  const latencySource = item.latency_ms ?? item.traffic_latency_ms ?? pointArrays.latency_ms;
  const expectedSource = item.expected_latency_ms ?? pointArrays.expected_latency_ms;
  const availabilitySource = item.available ?? item.success ?? item.traffic_success ??
    (pointArrays.available.some((value) => value !== null) ? pointArrays.available : pointArrays.success);
  const probabilitySource = item.success_probability ?? pointArrays.success_probability;
  const length = Math.max(
    asArray(latencySource).length,
    asArray(expectedSource).length,
    asArray(availabilitySource).length,
    asArray(probabilitySource).length,
  );
  const latency = coerceNumberArray(latencySource, length);
  const expectedLatency = coerceNumberArray(expectedSource, length);
  return {
    tag: String(item.tag ?? item.name ?? `out-${String(index).padStart(2, "0")}`),
    profile: String(item.profile ?? item.archetype ?? item.type ?? "unknown"),
    provider: item.provider ?? null,
    latency,
    expectedLatency,
    available: coerceBooleanArray(availabilitySource, length, latency),
    successProbability: coerceNumberArray(probabilitySource, length),
  };
}

function metricPercent(metrics, percentKey, genericKey, fallback) {
  const exact = finite(metrics?.[percentKey]);
  if (exact !== null) return exact;
  const generic = finite(metrics?.[genericKey]);
  if (generic !== null) return generic >= 0 && generic <= 1 ? generic * 100 : generic;
  return fallback;
}

function normalizeResult(payload, request) {
  if (!payload || typeof payload !== "object") throw new Error("The simulation response is not a JSON object.");
  const rawOutbounds = payload.outbounds ?? payload.outbound_traces ?? payload.world?.outbounds;
  const outbounds = asArray(rawOutbounds).map(normalizeOutbound).filter((item) => item.latency.length);
  if (!outbounds.length) throw new Error("The simulation returned no outbound time-series data.");

  const rawRouting = payload.routing ?? payload.route ?? payload.strategy_trace ?? {};
  const routePoints = pointsToArrays(rawRouting.points ?? payload.attempts, [
    "second",
    "selected_tag",
    "success",
    "latency_ms",
    "effective_latency_ms",
    "used_fallback",
    "oracle_available",
  ]);
  const selectedTag = asArray(rawRouting.selected_tag ?? rawRouting.selected_tags ?? routePoints.selected_tag)
    .map((value) => String(value ?? "—"));
  const latencySource = rawRouting.latency_ms ?? routePoints.latency_ms;
  const successSource = rawRouting.success ?? routePoints.success;
  const effectiveSource = rawRouting.effective_latency_ms ?? routePoints.effective_latency_ms;
  const length = Math.max(selectedTag.length, asArray(latencySource).length, asArray(successSource).length);
  if (!length) throw new Error("The simulation returned no routed strategy trace.");
  const latency = coerceNumberArray(latencySource, length);
  const success = coerceBooleanArray(successSource, length, latency);
  const failurePenalty = finite(payload.failure_penalty_ms ?? payload.settings?.failure_penalty_ms, 5000);
  const effectiveLatency = Array.from({ length }, (_, index) =>
    finite(asArray(effectiveSource)[index], success[index] ? latency[index] : failurePenalty),
  );
  while (selectedTag.length < length) selectedTag.push("—");

  const stepS = finite(payload.step_s ?? payload.attempt_interval_s, 1);
  const durationS = finite(payload.duration_s, Math.max(length, outbounds[0].latency.length) * stepS);
  const windowPoints = Math.max(1, Math.round(60 / stepS));
  const successfulLatency = latency.filter(Number.isFinite);
  const calculatedAvailability = success.filter(Boolean).length / success.length * 100;
  const metrics = payload.metrics ?? {};
  const normalizedMetrics = {
    availabilityPct: metricPercent(metrics, "availability_pct", "availability", calculatedAvailability),
    effectiveMeanMs: finite(metrics.effective_mean_ms ?? metrics.effective_response_ms, average(effectiveLatency)),
    successP95Ms: finite(metrics.success_p95_ms ?? metrics.p95_latency_ms, percentile(successfulLatency, 0.95)),
    maxOutageS: finite(metrics.max_outage_s, maximumOutage(success) * stepS),
    routeSwitches: finite(metrics.route_switches, routeSwitches(selectedTag)),
    fallbackPct: metricPercent(metrics, "fallback_pct", "fallback_rate", null),
    oracleAvailabilityPct: metricPercent(metrics, "oracle_availability_pct", "oracle_availability", null),
  };

  return {
    raw: payload,
    request,
    durationS,
    stepS,
    windowPoints,
    scenario: {
      key: String(payload.scenario?.key ?? payload.scenario_key ?? request.scenario),
      label: String(payload.scenario?.label ?? payload.scenario_label ?? selectedScenario()?.label ?? titleFromKey(request.scenario)),
      description: String(payload.scenario?.description ?? selectedScenario()?.description ?? ""),
    },
    strategy: {
      key: String(payload.strategy?.key ?? payload.strategy_key ?? request.strategy),
      label: String(payload.strategy?.label ?? payload.strategy_label ?? selectedStrategy()?.label ?? titleFromKey(request.strategy)),
      kind: String(payload.strategy?.kind ?? selectedStrategy()?.kind ?? "strategy"),
      settings: payload.strategy?.settings ?? selectedStrategy()?.settings ?? {},
    },
    outbounds,
    routing: {
      selectedTag,
      latency,
      success,
      effectiveLatency,
      usedFallback: asArray(rawRouting.used_fallback ?? routePoints.used_fallback).map(Boolean),
      oracleAvailable: asArray(rawRouting.oracle_available ?? routePoints.oracle_available).map(Boolean),
      rollingAvailability: asArray(rawRouting.rolling_availability_pct).length
        ? coerceNumberArray(rawRouting.rolling_availability_pct, length)
        : rollingAvailability(success, windowPoints),
      rollingEffective: rollingAverage(effectiveLatency, windowPoints),
    },
    metrics: normalizedMetrics,
  };
}

function normalizeSummary(payload) {
  const rows = asArray(payload?.rows ?? payload?.summary ?? payload?.strategies).map((row) => ({
    ...row,
    scenarioKey: String(row.scenario_key ?? row.scenarioKey ?? "all") || "all",
    outboundCount: finite(row.outbound_count),
    strategyKey: String(row.strategy_key ?? row.key ?? ""),
    strategyLabel: String(row.strategy_label ?? row.strategy ?? row.label ?? titleFromKey(row.strategy_key)),
    availabilityPct: finite(row.availability_mean_pct ?? row.availability_pct),
    effectiveMs: finite(row.effective_mean_ms),
    p95Ms: finite(row.success_p95_mean_ms ?? row.success_p95_ms),
    maxOutageS: finite(row.max_outage_mean_s ?? row.max_outage_s),
    worstOutageS: finite(row.max_outage_max_s),
    trials: finite(row.trials),
  }));
  return {
    generatedAt: payload?.generated_at ?? null,
    trials: finite(payload?.trials, rows.find((row) => row.trials !== null)?.trials ?? null),
    rows,
  };
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { Accept: "application/json" },
    cache: "no-store",
    ...options,
  });
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`${response.status || "Invalid"} response was not JSON.`);
  }
  if (!response.ok) {
    const detail = payload?.detail ?? payload?.error ?? payload?.message;
    throw new Error(detail ? `${response.status}: ${detail}` : `Request failed with HTTP ${response.status}.`);
  }
  return payload;
}

function selectedScenario() {
  return state.catalog?.scenarios.find((item) => item.key === elements.scenario.value) ?? null;
}

function selectedStrategy() {
  return state.catalog?.strategies.find((item) => item.key === elements.strategy.value) ?? null;
}

function appendOption(select, value, label) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label;
  select.append(option);
  return option;
}

function populateControls() {
  const { catalog } = state;
  const query = new URLSearchParams(window.location.search);
  elements.scenario.replaceChildren();
  for (const scenario of catalog.scenarios) appendOption(elements.scenario, scenario.key, scenario.label);

  elements.strategy.replaceChildren();
  const groups = new Map();
  for (const strategy of catalog.strategies) {
    const groupKey = strategy.kind;
    if (!groups.has(groupKey)) {
      const group = document.createElement("optgroup");
      group.label = titleFromKey(groupKey);
      groups.set(groupKey, group);
      elements.strategy.append(group);
    }
    appendOption(groups.get(groupKey), strategy.key, strategy.label);
  }

  const requestedScenario = query.get("scenario") ?? catalog.defaults.scenario;
  const requestedStrategy = query.get("strategy") ?? catalog.defaults.strategy;
  elements.scenario.value = catalog.scenarios.some((item) => item.key === requestedScenario)
    ? requestedScenario
    : catalog.scenarios[0].key;
  elements.strategy.value = catalog.strategies.some((item) => item.key === requestedStrategy)
    ? requestedStrategy
    : catalog.strategies[0].key;

  elements.summaryScope.replaceChildren();
  appendOption(elements.summaryScope, "all", "All scenarios");
  for (const scenario of catalog.scenarios) {
    appendOption(elements.summaryScope, scenario.key, scenario.label);
  }
  elements.summaryScope.value = elements.scenario.value;

  const { minimum, maximum, defaultCount } = catalog.outboundRange;
  elements.count.min = String(minimum);
  elements.count.max = String(maximum);
  elements.count.value = String(clamp(finite(query.get("outbounds"), defaultCount), minimum, maximum));
  elements.seed.value = String(Math.trunc(finite(query.get("seed"), catalog.defaults.seed)));

  for (const control of [
    elements.scenario,
    elements.strategy,
    elements.summaryScope,
    elements.count,
    elements.countDown,
    elements.countUp,
    elements.seed,
    elements.randomSeed,
    elements.simulate,
  ]) control.disabled = false;

  elements.durationLabel.textContent = formatDuration(catalog.durationS);
  updateSelectionNotes();
}

function flattenSettings(object, prefix = "") {
  if (!object || typeof object !== "object") return [];
  const result = [];
  for (const [key, value] of Object.entries(object)) {
    const label = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      result.push(...flattenSettings(value, label));
    } else if (Array.isArray(value)) {
      result.push([label, value.join(", ") || "none"]);
    } else if (value !== undefined && value !== null) {
      result.push([label, typeof value === "boolean" ? (value ? "on" : "off") : String(value)]);
    }
  }
  return result;
}

function prettySettingName(value) {
  return String(value)
    .replace(/^observatory\./, "")
    .replace(/_s$/, "")
    .replaceAll("_", " ");
}

function updateSelectionNotes() {
  const scenario = selectedScenario();
  const strategy = selectedStrategy();
  elements.scenarioDescription.textContent = scenario?.description ?? "—";
  elements.strategySettings.replaceChildren();
  const settings = [["algorithm", strategy?.kind ?? "—"], ...flattenSettings(strategy?.settings)];
  if (!settings.length) settings.push(["observatory", "none"]);
  for (const [key, value] of settings) {
    const chip = document.createElement("span");
    chip.className = "setting-chip";
    const name = document.createElement("b");
    name.textContent = prettySettingName(key);
    chip.append(name, ` ${value}`);
    elements.strategySettings.append(chip);
  }
  renderSummaryTable();
}

function requestFromControls() {
  const range = state.catalog.outboundRange;
  const count = clamp(Math.trunc(finite(elements.count.value, range.defaultCount)), range.minimum, range.maximum);
  const seed = Math.trunc(finite(elements.seed.value, state.catalog.defaults.seed));
  elements.count.value = String(count);
  elements.seed.value = String(seed);
  return {
    scenario: elements.scenario.value,
    strategy: elements.strategy.value,
    seed,
    outbounds: count,
  };
}

function setConnectionStatus(message, status = "loading") {
  elements.runStatus.textContent = message;
  elements.liveDot.classList.toggle("is-ready", status === "ready");
  elements.liveDot.classList.toggle("is-error", status === "error");
}

function setSimulationLoading(loading, hasPrevious = Boolean(state.result)) {
  elements.results.setAttribute("aria-busy", String(loading));
  elements.simulate.disabled = loading || !state.catalog;
  elements.simulate.classList.toggle("is-loading", loading);
  elements.simulate.querySelector(".run-button-label").textContent = loading ? "Simulating…" : "Run simulation";
  if (loading && !hasPrevious) {
    elements.loadingPanel.hidden = false;
    elements.resultContent.hidden = true;
  } else if (!loading && state.result) {
    elements.loadingPanel.hidden = true;
    elements.resultContent.hidden = false;
  }
}

async function loadSummary() {
  elements.summaryStatus.textContent = "Loading reference…";
  elements.summaryStatus.classList.remove("is-error");
  try {
    state.summary = normalizeSummary(await fetchJson(STATIC_ASSETS.summary));
    elements.summaryStatus.textContent = state.summary.trials
      ? `${state.summary.trials} matched trials`
      : "Reference loaded";
    renderSummaryTable();
  } catch (error) {
    state.summary = null;
    elements.summaryStatus.textContent = "Reference unavailable";
    elements.summaryStatus.classList.add("is-error");
    elements.summaryBody.replaceChildren();
    const row = elements.summaryBody.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 5;
    cell.textContent = `Aggregate context could not be loaded: ${error.message}`;
  }
}

async function loadCatalog() {
  elements.catalogError.hidden = true;
  setConnectionStatus("Loading simulation catalog");
  try {
    state.catalog = normalizeCatalog(await fetchJson(STATIC_ASSETS.catalog));
    populateControls();
    setConnectionStatus("Simulator ready", "ready");
    loadSummary();
    await runSimulation();
  } catch (error) {
    state.catalog = null;
    setConnectionStatus("Simulator unavailable", "error");
    elements.catalogError.hidden = false;
    elements.catalogErrorMessage.textContent = `${error.message} Reload the published site and retry.`;
    elements.loadingTitle.textContent = "Waiting for the simulation catalog";
    elements.loadingCopy.textContent = "The dashboard will load its published scenario and strategy catalog when you retry.";
    setSimulationLoading(false);
  }
}

async function runSimulation(event) {
  event?.preventDefault();
  if (!state.catalog) return;
  const request = requestFromControls();
  state.simulationController?.abort();
  const controller = new AbortController();
  state.simulationController = controller;
  elements.simulationError.hidden = true;
  elements.loadingTitle.textContent = "Generating this unreliable world";
  elements.loadingCopy.textContent = "Starting the in-browser Python simulator. The first run may take a little longer.";
  setSimulationLoading(true);
  setConnectionStatus("Simulation running");

  const parameters = new URLSearchParams({
    scenario: request.scenario,
    strategy: request.strategy,
    seed: String(request.seed),
    outbounds: String(request.outbounds),
  });
  try {
    const payload = await simulateInBrowser(request, controller.signal);
    if (controller.signal.aborted) return;
    state.result = normalizeResult(payload, request);
    state.selectedOutbound = 0;
    updateResultView();
    setSimulationLoading(false);
    setConnectionStatus("Simulation complete", "ready");
    history.replaceState(null, "", `${window.location.pathname}?${parameters}`);
  } catch (error) {
    if (error.name === "AbortError") return;
    setSimulationLoading(false);
    setConnectionStatus(state.result ? "Showing previous run" : "Simulation failed", "error");
    if (state.result) {
      elements.simulationError.hidden = false;
      elements.simulationErrorMessage.textContent = `${error.message} The previous result remains available below.`;
    } else {
      elements.loadingPanel.hidden = false;
      elements.loadingTitle.textContent = "Simulation failed";
      elements.loadingCopy.textContent = error.message;
      elements.simulationError.hidden = false;
      elements.simulationErrorMessage.textContent = error.message;
    }
  } finally {
    if (state.simulationController === controller) state.simulationController = null;
  }
}

function updateResultView() {
  const result = state.result;
  const { metrics } = result;
  elements.resultScenario.textContent = result.scenario.label;
  elements.resultStrategy.textContent = result.strategy.label;
  elements.resultSeed.textContent = `seed ${result.request.seed}`;
  elements.durationLabel.textContent = formatDuration(result.durationS);
  elements.metricAvailability.textContent = formatPercent(metrics.availabilityPct);
  elements.metricAvailabilityNote.textContent = metrics.oracleAvailabilityPct === null
    ? "successful attempts"
    : `${formatPercent(metrics.oracleAvailabilityPct)} oracle ceiling`;
  elements.metricEffective.textContent = formatMs(metrics.effectiveMeanMs);
  elements.metricP95.textContent = formatMs(metrics.successP95Ms);
  elements.metricOutage.textContent = formatSeconds(metrics.maxOutageS);
  elements.metricSwitches.textContent = Math.round(metrics.routeSwitches).toLocaleString();
  elements.availabilityWindow.textContent = `${Math.round(result.windowPoints * result.stepS)} s window`;
  updateOutboundDetailHeading();
  renderSummaryTable();
  queueCharts();
}

function updateOutboundDetailHeading() {
  const outbound = state.result?.outbounds[state.selectedOutbound];
  if (!outbound) return;
  const rawAvailability = outbound.available.filter(Boolean).length / outbound.available.length * 100;
  elements.detailTitle.textContent = outbound.tag;
  elements.detailCopy.textContent = `${formatPercent(rawAvailability)} raw availability · provider ${outbound.provider ?? "—"}`;
  elements.profileBadge.textContent = outbound.profile.replaceAll("_", " ");
}

function renderSummaryTable() {
  if (!state.summary) return;
  const selectedScenarioKey = elements.summaryScope.value || elements.scenario.value;
  const selectedStrategyKey = elements.strategy.value;
  const count = finite(elements.count.value);
  let rows = state.summary.rows.filter((row) => row.scenarioKey === selectedScenarioKey);
  const countMatches = rows.filter((row) => row.outboundCount === null || row.outboundCount === count);
  if (countMatches.length) rows = countMatches;
  const { key: sortKey, direction } = state.summarySort;
  rows = [...rows].sort((a, b) => {
    const left = a[sortKey];
    const right = b[sortKey];
    if (left === null || left === undefined) return right === null || right === undefined ? 0 : 1;
    if (right === null || right === undefined) return -1;
    const comparison = typeof left === "string"
      ? left.localeCompare(String(right), undefined, { sensitivity: "base" })
      : left - right;
    return direction === "asc" ? comparison : -comparison;
  });

  for (const button of elements.summarySortButtons) {
    const active = button.dataset.summarySort === sortKey;
    const header = button.closest("th");
    const marker = button.querySelector(".summary-sort-marker");
    header?.setAttribute("aria-sort", active ? (direction === "asc" ? "ascending" : "descending") : "none");
    if (marker) marker.textContent = active ? (direction === "asc" ? "↑" : "↓") : "↕";
  }

  elements.summaryBody.replaceChildren();
  if (!rows.length) {
    const row = elements.summaryBody.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 6;
    cell.textContent = "No Monte Carlo rows are available for these conditions.";
    elements.summaryStatus.textContent = "No reference data";
    const scenario = selectedScenarioKey === "all"
      ? null
      : state.catalog?.scenarios.find((item) => item.key === selectedScenarioKey) ?? null;
    elements.summaryCopy.textContent = `No reference results${scenario ? ` for ${scenario.label}` : " across all scenarios"}.`;
    return;
  }
  for (const item of rows) {
    const row = elements.summaryBody.insertRow();
    row.classList.toggle("is-selected", item.strategyKey === selectedStrategyKey);
    const values = [
      item.strategyLabel,
      formatPercent(item.availabilityPct),
      formatMs(item.effectiveMs),
      formatMs(item.p95Ms),
      formatSeconds(item.maxOutageS),
      formatSeconds(item.worstOutageS),
    ];
    for (const value of values) row.insertCell().textContent = value;
  }
  const scenario = selectedScenarioKey === "all"
    ? null
    : state.catalog?.scenarios.find((item) => item.key === selectedScenarioKey) ?? null;
  const displayedTrials = rows.find((item) => item.trials !== null)?.trials ?? state.summary.trials;
  elements.summaryStatus.textContent = displayedTrials
    ? `${displayedTrials} matched worlds`
    : "Reference loaded";
  elements.summaryCopy.textContent = displayedTrials
    ? `${displayedTrials} matched generated worlds${scenario ? ` for ${scenario.label}` : " across all scenarios"}.`
    : `Aggregate results${scenario ? ` for ${scenario.label}` : " across all scenarios"}.`;
}

function prepareCanvas(canvas) {
  const bounds = canvas.getBoundingClientRect();
  if (bounds.width < 2 || bounds.height < 2) return null;
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  const width = Math.round(bounds.width * ratio);
  const height = Math.round(bounds.height * ratio);
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, bounds.width, bounds.height);
  context.imageSmoothingEnabled = true;
  return { context, width: bounds.width, height: bounds.height };
}

function chartPlot(width, height, options = {}) {
  const left = options.left ?? (width < 520 ? 54 : 62);
  const right = options.right ?? (width < 520 ? 42 : 52);
  const top = options.top ?? 18;
  const bottom = options.bottom ?? 34;
  return { x: left, y: top, width: Math.max(20, width - left - right), height: Math.max(20, height - top - bottom) };
}

function drawPlotBackground(context, plot) {
  context.fillStyle = "#081512";
  context.fillRect(plot.x, plot.y, plot.width, plot.height);
}

function drawTimeAxis(context, plot, durationS, divisions = 6) {
  context.save();
  context.font = '10px "Cascadia Code", Consolas, monospace';
  context.textAlign = "center";
  context.textBaseline = "top";
  for (let tick = 0; tick <= divisions; tick += 1) {
    const ratio = tick / divisions;
    const x = plot.x + ratio * plot.width;
    context.strokeStyle = "rgba(196, 255, 224, 0.075)";
    context.lineWidth = 1;
    context.beginPath();
    context.moveTo(Math.round(x) + 0.5, plot.y);
    context.lineTo(Math.round(x) + 0.5, plot.y + plot.height);
    context.stroke();
    context.fillStyle = "#789289";
    context.fillText(formatClock(durationS * ratio), x, plot.y + plot.height + 10);
  }
  context.restore();
}

function drawYAxis(context, plot, maximum, formatter, divisions = 4, side = "left") {
  context.save();
  context.font = '10px "Cascadia Code", Consolas, monospace';
  context.textBaseline = "middle";
  context.textAlign = side === "left" ? "right" : "left";
  for (let tick = 0; tick <= divisions; tick += 1) {
    const ratio = tick / divisions;
    const y = plot.y + plot.height - ratio * plot.height;
    context.strokeStyle = "rgba(196, 255, 224, 0.075)";
    context.beginPath();
    context.moveTo(plot.x, Math.round(y) + 0.5);
    context.lineTo(plot.x + plot.width, Math.round(y) + 0.5);
    context.stroke();
    context.fillStyle = "#789289";
    const x = side === "left" ? plot.x - 8 : plot.x + plot.width + 8;
    context.fillText(formatter(maximum * ratio), x, y);
  }
  context.restore();
}

function latencyColor(value, maximum) {
  if (!Number.isFinite(value)) return "#481827";
  const normalized = clamp(Math.log1p(value / 18) / Math.log1p(maximum / 18), 0, 1);
  const hue = 166 - 154 * normalized;
  const light = 48 + 8 * Math.sin(normalized * Math.PI);
  return `hsl(${hue} 76% ${light}%)`;
}

function drawPath(context, values, plot, yMaximum, color, width = 1.5, dash = []) {
  if (!values.length || !Number.isFinite(yMaximum) || yMaximum <= 0) return;
  context.save();
  context.beginPath();
  context.strokeStyle = color;
  context.lineWidth = width;
  context.lineJoin = "round";
  context.lineCap = "round";
  context.setLineDash(dash);
  let drawing = false;
  const denominator = Math.max(1, values.length - 1);
  for (let index = 0; index < values.length; index += 1) {
    const value = finite(values[index]);
    if (value === null) {
      drawing = false;
      continue;
    }
    const x = plot.x + index / denominator * plot.width;
    const y = plot.y + plot.height - clamp(value / yMaximum, 0, 1) * plot.height;
    if (drawing) context.lineTo(x, y);
    else {
      context.moveTo(x, y);
      drawing = true;
    }
  }
  context.stroke();
  context.restore();
}

function drawFailureTicks(context, success, plot, height = 8) {
  const denominator = Math.max(1, success.length);
  context.save();
  context.fillStyle = "rgba(255, 101, 125, 0.72)";
  for (let index = 0; index < success.length; index += 1) {
    if (success[index]) continue;
    const x = plot.x + index / denominator * plot.width;
    context.fillRect(x, plot.y + plot.height - height, Math.max(1, plot.width / denominator), height);
  }
  context.restore();
}

function indexForX(x, plot, length) {
  return clamp(Math.floor((x - plot.x) / plot.width * length), 0, length - 1);
}

function drawSelectedRoute(context, selectedTags, rows, plot, rowHeight) {
  if (!selectedTags.length || !rows.length) return;
  const rowIndexByTag = new Map(rows.map((row, index) => [row.tag, index]));
  const denominator = Math.max(1, selectedTags.length - 1);
  let drawing = false;
  let previousY = null;

  context.save();
  context.beginPath();
  context.rect(plot.x, plot.y, plot.width, plot.height);
  context.clip();
  context.beginPath();
  for (let index = 0; index < selectedTags.length; index += 1) {
    const rowIndex = rowIndexByTag.get(selectedTags[index]);
    if (rowIndex === undefined) {
      drawing = false;
      previousY = null;
      continue;
    }
    const x = plot.x + index / denominator * plot.width;
    const y = plot.y + (rowIndex + 0.5) * rowHeight;
    if (!drawing) context.moveTo(x, y);
    else {
      if (y !== previousY) context.lineTo(x, previousY);
      context.lineTo(x, y);
    }
    drawing = true;
    previousY = y;
  }
  context.lineJoin = "round";
  context.lineCap = "round";
  context.strokeStyle = "rgba(3, 14, 13, 0.9)";
  context.lineWidth = 4.5;
  context.stroke();
  context.strokeStyle = "#59e1c3";
  context.lineWidth = 2;
  context.stroke();
  context.restore();
}

function drawMatrix() {
  const prepared = prepareCanvas(elements.matrix);
  const result = state.result;
  if (!prepared || !result) return;
  const { context, width, height } = prepared;
  const labelWidth = width < 560 ? 87 : 128;
  const plot = chartPlot(width, height, { left: labelWidth, right: 18, top: 12, bottom: 34 });
  const rows = result.outbounds;
  const rowHeight = plot.height / rows.length;
  const successfulValues = rows.flatMap((row) => row.latency.filter(Number.isFinite));
  const maximum = clamp(percentile(successfulValues, 0.98) ?? 1000, 180, 5000);
  drawPlotBackground(context, plot);

  context.save();
  context.font = `${rowHeight < 22 ? 9 : 10}px "Cascadia Code", Consolas, monospace`;
  context.textAlign = "right";
  context.textBaseline = "middle";
  for (let rowIndex = 0; rowIndex < rows.length; rowIndex += 1) {
    const row = rows[rowIndex];
    const y = plot.y + rowIndex * rowHeight;
    const columnCount = Math.max(1, Math.min(row.latency.length, Math.floor(plot.width)));
    for (let column = 0; column < columnCount; column += 1) {
      const start = Math.floor(column / columnCount * row.latency.length);
      const end = Math.max(start + 1, Math.floor((column + 1) / columnCount * row.latency.length));
      let failed = false;
      let total = 0;
      let valid = 0;
      for (let index = start; index < end; index += 1) {
        if (!row.available[index]) failed = true;
        else {
          const latency = finite(row.latency[index], row.expectedLatency[index]);
          if (latency !== null) {
            total += latency;
            valid += 1;
          }
        }
      }
      context.fillStyle = failed ? "#481827" : latencyColor(valid ? total / valid : null, maximum);
      const x = plot.x + column / columnCount * plot.width;
      const nextX = plot.x + (column + 1) / columnCount * plot.width;
      context.fillRect(x, y, Math.ceil(nextX - x) + 0.25, Math.ceil(rowHeight));
    }
    context.fillStyle = rowIndex === state.selectedOutbound ? "#dfffa9" : "#93aaa2";
    const profile = row.profile.replaceAll("_", " ");
    const label = width < 560 ? row.tag : `${row.tag} · ${profile}`;
    context.fillText(label.length > 22 ? `${label.slice(0, 21)}…` : label, plot.x - 9, y + rowHeight / 2);
    context.strokeStyle = "rgba(7, 17, 16, 0.42)";
    context.beginPath();
    context.moveTo(plot.x, y + rowHeight);
    context.lineTo(plot.x + plot.width, y + rowHeight);
    context.stroke();
  }
  const selectedY = plot.y + state.selectedOutbound * rowHeight;
  context.strokeStyle = "#bdfc58";
  context.lineWidth = 1.5;
  context.strokeRect(plot.x - 0.5, selectedY + 0.5, plot.width + 1, Math.max(1, rowHeight - 1));
  context.restore();
  drawSelectedRoute(context, result.routing.selectedTag, rows, plot, rowHeight);
  drawTimeAxis(context, plot, result.durationS);

  state.hitTests.set(elements.matrix, (x, y) => {
    if (x < plot.x || x > plot.x + plot.width || y < plot.y || y > plot.y + plot.height) return null;
    const rowIndex = clamp(Math.floor((y - plot.y) / rowHeight), 0, rows.length - 1);
    const row = rows[rowIndex];
    const index = indexForX(x, plot, row.latency.length);
    const routeIndex = indexForX(x, plot, result.routing.selectedTag.length);
    const routedTag = result.routing.selectedTag[routeIndex] ?? "—";
    const observed = finite(row.latency[index]);
    const expected = finite(row.expectedLatency[index]);
    return {
      rowIndex,
      html: `<strong>${escapeHtml(row.tag)}</strong> · ${escapeHtml(row.profile.replaceAll("_", " "))}<br>${formatClock(index * result.stepS)} · ${row.available[index] ? formatMs(observed ?? expected) : "unavailable"}<br>selected route ${escapeHtml(routedTag)} · provider ${escapeHtml(row.provider ?? "—")}`,
    };
  });
}

function drawOutboundDetail() {
  const prepared = prepareCanvas(elements.detail);
  const result = state.result;
  const outbound = result?.outbounds[state.selectedOutbound];
  if (!prepared || !outbound) return;
  const { context, width, height } = prepared;
  const plot = chartPlot(width, height);
  const latencyValues = outbound.latency.map((value, index) => finite(value, outbound.available[index] ? outbound.expectedLatency[index] : null));
  const latencyMaximum = clamp((percentile(latencyValues, 0.98) ?? 300) * 1.2, 200, 5000);
  const rolling = rollingAvailability(outbound.available, result.windowPoints);
  drawPlotBackground(context, plot);
  drawYAxis(context, plot, latencyMaximum, (value) => (value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${value.toFixed(0)}`));
  drawYAxis(context, plot, 100, (value) => `${value.toFixed(0)}%`, 4, "right");
  drawTimeAxis(context, plot, result.durationS);
  drawFailureTicks(context, outbound.available, plot, 9);
  drawPath(context, latencyValues, plot, latencyMaximum, "#59e1c3", 1.2);
  drawPath(context, rolling, plot, 100, "#bdfc58", 2);

  state.hitTests.set(elements.detail, (x, y) => {
    if (x < plot.x || x > plot.x + plot.width || y < plot.y || y > plot.y + plot.height) return null;
    const index = indexForX(x, plot, outbound.latency.length);
    return {
      html: `<strong>${escapeHtml(outbound.tag)} · ${formatClock(index * result.stepS)}</strong><br>${outbound.available[index] ? `latency ${formatMs(finite(outbound.latency[index], outbound.expectedLatency[index]))}` : "connection failed"}<br>60 s availability ${formatPercent(rolling[index], 1)}`,
    };
  });
}

function drawResponse() {
  const prepared = prepareCanvas(elements.response);
  const result = state.result;
  if (!prepared || !result) return;
  const { context, width, height } = prepared;
  const plot = chartPlot(width, height, { right: 18 });
  const values = [...result.routing.latency.filter(Number.isFinite), ...result.routing.rollingEffective.filter(Number.isFinite)];
  const maximum = clamp((percentile(values, 0.99) ?? 1000) * 1.12, 300, 6000);
  drawPlotBackground(context, plot);
  drawYAxis(context, plot, maximum, (value) => (value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${value.toFixed(0)}`));
  drawTimeAxis(context, plot, result.durationS);
  drawFailureTicks(context, result.routing.success, plot, 8);
  drawPath(context, result.routing.latency, plot, maximum, "rgba(89, 225, 195, 0.7)", 1);
  drawPath(context, result.routing.rollingEffective, plot, maximum, "#ffad5b", 2.1);

  state.hitTests.set(elements.response, (x, y) => {
    if (x < plot.x || x > plot.x + plot.width || y < plot.y || y > plot.y + plot.height) return null;
    const index = indexForX(x, plot, result.routing.success.length);
    const route = result.routing.selectedTag[index];
    return {
      html: `<strong>${formatClock(index * result.stepS)} · ${escapeHtml(route)}</strong><br>${result.routing.success[index] ? `response ${formatMs(result.routing.latency[index])}` : "connection failed"}<br>60 s effective ${formatMs(result.routing.rollingEffective[index])}`,
    };
  });
}

function drawAvailability() {
  const prepared = prepareCanvas(elements.availability);
  const result = state.result;
  if (!prepared || !result) return;
  const { context, width, height } = prepared;
  const plot = chartPlot(width, height, { right: 18 });
  const rolling = result.routing.rollingAvailability;
  drawPlotBackground(context, plot);
  drawYAxis(context, plot, 100, (value) => `${value.toFixed(0)}%`);
  drawTimeAxis(context, plot, result.durationS);
  drawFailureTicks(context, result.routing.success, plot, 12);

  const referenceY = plot.y + plot.height - 0.99 * plot.height;
  context.save();
  context.strokeStyle = "rgba(181, 201, 193, 0.55)";
  context.setLineDash([4, 5]);
  context.beginPath();
  context.moveTo(plot.x, referenceY);
  context.lineTo(plot.x + plot.width, referenceY);
  context.stroke();
  context.restore();
  drawPath(context, rolling, plot, 100, "#bdfc58", 2.2);

  state.hitTests.set(elements.availability, (x, y) => {
    if (x < plot.x || x > plot.x + plot.width || y < plot.y || y > plot.y + plot.height) return null;
    const index = indexForX(x, plot, rolling.length);
    return {
      html: `<strong>${formatClock(index * result.stepS)}</strong><br>60 s availability ${formatPercent(rolling[index], 1)}<br>${result.routing.success[index] ? "attempt succeeded" : "attempt failed"} via ${escapeHtml(result.routing.selectedTag[index])}`,
    };
  });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderCharts() {
  state.renderRequest = 0;
  if (!state.result || elements.resultContent.hidden) return;
  drawMatrix();
  drawOutboundDetail();
  drawResponse();
  drawAvailability();
}

function queueCharts() {
  if (state.renderRequest) cancelAnimationFrame(state.renderRequest);
  state.renderRequest = requestAnimationFrame(renderCharts);
}

function wireChart(canvas) {
  const frame = canvas.closest(".chart-frame");
  const tooltip = frame.querySelector(".chart-tooltip");
  function locate(event) {
    const bounds = canvas.getBoundingClientRect();
    const x = event.clientX - bounds.left;
    const y = event.clientY - bounds.top;
    return { x, y, info: state.hitTests.get(canvas)?.(x, y) ?? null };
  }
  canvas.addEventListener("pointermove", (event) => {
    const { x, y, info } = locate(event);
    if (!info) {
      tooltip.hidden = true;
      return;
    }
    tooltip.innerHTML = info.html;
    tooltip.hidden = false;
    const maxX = frame.clientWidth - tooltip.offsetWidth - 8;
    const maxY = frame.clientHeight - tooltip.offsetHeight - 8;
    tooltip.style.left = `${clamp(x + 14, 8, maxX)}px`;
    tooltip.style.top = `${clamp(y + 14, 8, maxY)}px`;
  });
  canvas.addEventListener("pointerleave", () => { tooltip.hidden = true; });
  if (canvas === elements.matrix) {
    canvas.tabIndex = 0;
    canvas.addEventListener("click", (event) => {
      const { info } = locate(event);
      if (info?.rowIndex === undefined) return;
      state.selectedOutbound = info.rowIndex;
      updateOutboundDetailHeading();
      queueCharts();
    });
    canvas.addEventListener("keydown", (event) => {
      if (!state.result || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      state.selectedOutbound = clamp(state.selectedOutbound + direction, 0, state.result.outbounds.length - 1);
      updateOutboundDetailHeading();
      queueCharts();
    });
  }
}

elements.form.addEventListener("submit", runSimulation);
elements.scenario.addEventListener("change", () => {
  elements.summaryScope.value = elements.scenario.value;
  updateSelectionNotes();
});
elements.strategy.addEventListener("change", updateSelectionNotes);
elements.summaryScope.addEventListener("change", renderSummaryTable);
for (const button of elements.summarySortButtons) {
  button.addEventListener("click", () => {
    const key = button.dataset.summarySort;
    if (!key) return;
    if (state.summarySort.key === key) {
      state.summarySort.direction = state.summarySort.direction === "asc" ? "desc" : "asc";
    } else {
      state.summarySort = {
        key,
        direction: key === "availabilityPct" ? "desc" : "asc",
      };
    }
    renderSummaryTable();
  });
}
elements.count.addEventListener("change", () => {
  requestFromControls();
  renderSummaryTable();
});
elements.countDown.addEventListener("click", () => {
  elements.count.value = String(finite(elements.count.value, 0) - 1);
  requestFromControls();
  renderSummaryTable();
});
elements.countUp.addEventListener("click", () => {
  elements.count.value = String(finite(elements.count.value, 0) + 1);
  requestFromControls();
  renderSummaryTable();
});
elements.randomSeed.addEventListener("click", () => {
  const values = new Uint32Array(1);
  crypto.getRandomValues(values);
  elements.seed.value = String(values[0]);
});
elements.retryCatalog.addEventListener("click", loadCatalog);
elements.retrySimulation.addEventListener("click", runSimulation);
for (const canvas of chartCanvases) wireChart(canvas);

if ("ResizeObserver" in window) {
  const resizeObserver = new ResizeObserver(queueCharts);
  for (const canvas of chartCanvases) resizeObserver.observe(canvas.parentElement);
} else {
  window.addEventListener("resize", queueCharts, { passive: true });
}

loadCatalog();
