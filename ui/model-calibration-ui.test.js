const test = require("node:test");
const assert = require("node:assert/strict");
const {
  healthPill,
  statusFields,
  refreshStatusLabel,
  refreshResult,
  resultHeadline,
  costImpactText,
  resultSummaryLines,
  failureDetail,
  changeDetailLines,
  routingImpactLines,
  currentStrategy,
  sortModels,
  toggleSort,
  exampleRoutingDecisions,
} = require("./model-calibration-ui.js");

test("health pill reflects a running refresh over any other state", () => {
  assert.deepEqual(healthPill({ refresh_running: true, healthy: false }), {
    text: "Refreshing…",
    tone: "running",
  });
});

test("health pill reports not-yet-calibrated before anything is active", () => {
  assert.deepEqual(healthPill({ active_version: null }), {
    text: "Not yet calibrated",
    tone: "idle",
  });
});

test("health pill flags an unhealthy active calibration", () => {
  assert.deepEqual(healthPill({ active_version: "2026-01-01-001", healthy: false }), {
    text: "Needs attention",
    tone: "error",
  });
});

test("health pill surfaces a newer proposed calibration awaiting review", () => {
  assert.deepEqual(
    healthPill({ active_version: "2026-01-01-001", healthy: true, has_newer_proposed: true }),
    { text: "Update available", tone: "paused" },
  );
});

test("health pill is current when healthy with nothing pending", () => {
  assert.deepEqual(healthPill({ active_version: "2026-01-01-001", healthy: true }), {
    text: "Current",
    tone: "ok",
  });
});

test("failed refresh remains visible while the active calibration is healthy", () => {
  assert.deepEqual(healthPill({
    active_version: "2026-01-01-001", healthy: true, last_attempted_status: "failed",
  }), { text: "Refresh failed", tone: "error" });
});

test("status fields cover every plain-language item the issue calls for", () => {
  const fields = statusFields(
    { dynamic_model_routing: true, routing_optimization: "cost" },
    {
      active_version: "2026-01-01-001",
      last_successful_refresh_at: "2026-01-01T00:00:00Z",
      last_attempted_refresh_at: "2026-01-01T00:00:00Z",
      last_attempted_status: "changed",
      source_status: "ok",
      active_model_count: 12,
      discovered_model_count: 2,
      healthy: true,
      has_newer_proposed: true,
    },
  );
  const byLabel = Object.fromEntries(fields.map((field) => [field.label, field.value]));
  assert.equal(byLabel["Dynamic routing"], "Enabled");
  assert.equal(byLabel["Active calibration"], "2026-01-01-001");
  assert.equal(byLabel["Models"], "12 active · 2 discovered");
  assert.equal(byLabel["Proposed update"], "Awaiting review");
  assert.equal(fields.length, 7);
});

test("status fields fall back to honest defaults before anything has run", () => {
  const fields = statusFields({}, {});
  const byLabel = Object.fromEntries(fields.map((field) => [field.label, field.value]));
  assert.equal(byLabel["Dynamic routing"], "Disabled");
  assert.equal(byLabel["Active calibration"], "None yet");
  assert.equal(byLabel["Last successful refresh"], "Never");
  assert.equal(byLabel["Refresh status"], "Not yet run");
  assert.equal(byLabel["Models"], "0 active · 0 discovered");
  assert.equal(byLabel["Proposed update"], "None");
});

test("refresh status label covers every value the service returns", () => {
  assert.equal(refreshStatusLabel("changed"), "Changed");
  assert.equal(refreshStatusLabel("no_change"), "No change");
  assert.equal(refreshStatusLabel("failed"), "Failed");
  assert.equal(refreshStatusLabel("skipped_interval"), "Skipped (interval)");
  assert.equal(refreshStatusLabel(undefined), "Not yet run");
});

test("result headline matches the issue's mocked copy exactly", () => {
  assert.equal(resultHeadline({ status: "changed" }), "Model data refreshed successfully");
  assert.equal(
    resultHeadline({ status: "no_change" }),
    "Model data is current. No routing changes were required.",
  );
  assert.equal(
    resultHeadline({ status: "failed" }),
    "Model data refresh failed. Existing routing configuration remains active.",
  );
});

test("cost impact text signs a positive change and handles missing data", () => {
  assert.equal(costImpactText(-8.4), "-8.4%");
  assert.equal(costImpactText(3), "+3%");
  assert.equal(costImpactText(null), "No estimate available");
});

test("result summary lines are empty unless the refresh actually changed something", () => {
  assert.deepEqual(resultSummaryLines({ status: "no_change" }), []);
  assert.deepEqual(resultSummaryLines({ status: "failed" }), []);
});

test("result summary lines cover models/new/pricing/benchmark/routing/cost/calibration", () => {
  const lines = resultSummaryLines({
    status: "changed",
    calibration_version: "2026-01-02-001",
    activated: false,
    diff: {
      models_checked: 37,
      discovered_models: ["xai/grok-5", "openai/gpt-7"],
      pricing_changes: [{}, {}, {}, {}],
      benchmark_changes: new Array(7).fill({}),
      routing_changes: [{}, {}, {}],
      estimated_cost_change_percent: -8.4,
    },
  });
  const byLabel = Object.fromEntries(lines.map((line) => [line.label, line.value]));
  assert.equal(byLabel["Models checked"], "37");
  assert.equal(byLabel["New models"], "2");
  assert.equal(byLabel["Pricing changes"], "4");
  assert.equal(byLabel["Benchmark changes"], "7");
  assert.equal(byLabel["Routing changes"], "3 workload categories");
  assert.equal(byLabel["Estimated cost impact"], "-8.4%");
  assert.match(byLabel["Calibration"], /available for review/);
});

test("an auto-activated result says the calibration is already active", () => {
  const lines = resultSummaryLines({
    status: "changed",
    calibration_version: "2026-01-02-001",
    activated: true,
    diff: { discovered_models: [], pricing_changes: [], benchmark_changes: [], routing_changes: [] },
  });
  const calibration = lines.find((line) => line.label === "Calibration");
  assert.match(calibration.value, /Active immediately/);
});

test("already-discovered models awaiting review do not inflate the new-model count", () => {
  const lines = resultSummaryLines({
    status: "changed", diff: { discovered_models: ["fixture/pending"], newly_discovered_models: [] },
  });
  assert.equal(lines.find((line) => line.label === "New models").value, "0");
});

test("activation and rollback reconcile a cached manual result without another refresh", () => {
  const cached = {
    status: "changed", calibration_version: "2026-01-02-001", activated: false,
    attempted_at: "2026-01-02T00:00:00Z", diff: {},
  };
  const status = {
    active_version: cached.calibration_version, has_newer_proposed: false,
    last_attempted_status: "changed", last_attempted_refresh_at: cached.attempted_at,
  };
  const active = refreshResult(status, cached);
  assert.equal(active.activated, true);
  assert.match(resultSummaryLines(active).find((line) => line.label === "Calibration").value, /Active calibration/);
  const rolledBack = refreshResult({ ...status, active_version: "2026-01-01-001" }, cached);
  assert.equal(rolledBack.activated, false);
  assert.match(resultSummaryLines(rolledBack).find((line) => line.label === "Calibration").value, /Historical calibration/);
  assert.equal(cached.activated, false);
});

test("a later stored source failure supersedes the previous successful manual result", () => {
  const result = refreshResult({
    active_version: "2026-01-01-001", last_attempted_status: "failed",
    last_attempted_refresh_at: "2026-01-02T00:00:00Z", last_error: "Source unavailable",
  }, { status: "changed", attempted_at: "2026-01-01T00:00:00Z" });
  assert.equal(result.status, "failed");
  assert.equal(failureDetail(result), "Source unavailable");
});

test("reopened summaries use the refresh version after rollback", () => {
  const result = refreshResult({
    active_version: "2026-01-01-001", last_attempted_status: "changed",
    last_refresh_calibration_version: "2026-01-02-001", last_diff: {},
  });
  assert.equal(result.calibration_version, "2026-01-02-001");
  assert.equal(result.calibration_state, "historical");
  assert.match(resultSummaryLines(result).find((line) => line.label === "Calibration").value,
    /Historical calibration.*2026-01-02-001/);
});

test("legacy status without refresh provenance does not misattribute a diff", () => {
  assert.equal(refreshResult({
    active_version: "2026-01-01-001", last_attempted_status: "changed", last_diff: {},
  }), null);
});

test("task-fit changes remain visible when example routes do not change", () => {
  const diff = { routing_input_changes: [{
    provider: "fixture", model: "m1", field: "strengths", previous: ["coding"], new: ["documentation"],
  }] };
  assert.equal(resultSummaryLines({ status: "changed", diff })
    .find((line) => line.label === "Routing input changes").value, "1");
  assert.deepEqual(changeDetailLines(diff), [{
    heading: "fixture/m1", detail: "strengths: coding → documentation",
  }]);
  assert.match(routingImpactLines(diff)[0].detail, /No change/);
});

test("removed models are counted and named even when representative routes do not change", () => {
  const diff = { removed_models: ["fixture/retired"], routing_changes: [] };
  const summary = resultSummaryLines({ status: "changed", diff });
  assert.equal(summary.find((line) => line.label === "Removed models").value, "1");
  assert.deepEqual(changeDetailLines(diff), [{
    heading: "fixture/retired", detail: "Removed from the updated model catalog",
  }]);
  assert.match(routingImpactLines(diff)[0].detail, /No change/);
});

test("failure detail is only populated for a failed result", () => {
  assert.equal(failureDetail({ status: "failed", error: "timed out" }), "timed out");
  assert.equal(failureDetail({ status: "changed", error: "should not show" }), "");
});

test("change detail lines cover discovered, pricing, and benchmark changes", () => {
  const lines = changeDetailLines({
    discovered_models: ["xai/grok-5"],
    pricing_changes: [
      { provider: "anthropic", model: "claude-x", previous_output_cost: 10, new_output_cost: 8 },
    ],
    benchmark_changes: [
      { provider: "openai", model: "gpt-x", field: "coding_score", previous: 70, new: 82 },
    ],
  });
  assert.equal(lines.length, 3);
  assert.equal(lines[0].detail, "New model discovered");
  assert.match(lines[1].detail, /Output price 10 → 8 \(-20%\)/);
  assert.match(lines[2].detail, /coding_score: 70 → 82/);
});

test("routing impact lines report no change when nothing moved", () => {
  assert.deepEqual(routingImpactLines({ routing_changes: [] }), [
    { heading: "All workload categories", detail: "No change" },
  ]);
});

test("routing impact lines show the before/after model and effort", () => {
  const lines = routingImpactLines({
    routing_changes: [
      {
        label: "Complex Coding",
        previous: { model: "gpt-5.6-luna", effort: "medium" },
        new: { model: "gpt-6-astra", effort: "high" },
      },
    ],
  });
  assert.equal(lines[0].heading, "Complex Coding");
  assert.equal(lines[0].detail, "gpt-5.6-luna (medium) → gpt-6-astra (high)");
});

test("current strategy always reports Cost Aware for automatic routing", () => {
  const costAware = currentStrategy({ routing_mode: "cost_aware", weights: { cost_efficiency: "high" } });
  assert.equal(costAware.mode, "Cost Aware");
  assert.equal(costAware.costOptimizationEnabled, true);

  const stillCost = currentStrategy({ routing_mode: "quality", weights: {} });
  assert.equal(stillCost.mode, "Cost Aware");
  assert.equal(stillCost.costOptimizationEnabled, true);
  assert.ok(stillCost.factors.some((factor) => factor.name === "Capability" && factor.level === "high"));
});

test("sorting models is stable and tolerates an unknown column", () => {
  const models = [
    { provider: "a", model: "m1", relative_cost: 3 },
    { provider: "b", model: "m2", relative_cost: 1 },
    { provider: "c", model: "m3", relative_cost: 2 },
  ];
  const byProvider = sortModels(models, "provider", "desc");
  assert.deepEqual(byProvider.map((m) => m.provider), ["c", "b", "a"]);

  const unknownColumn = sortModels(models, "not-a-real-column", "asc");
  assert.deepEqual(unknownColumn.map((m) => m.provider), ["a", "b", "c"]);
});

test("toggling sort on the same column flips direction, a new column resets to ascending", () => {
  const first = toggleSort({ column: "provider", direction: "asc" }, "provider");
  assert.deepEqual(first, { column: "provider", direction: "desc" });
  const second = toggleSort(first, "model");
  assert.deepEqual(second, { column: "model", direction: "asc" });
  assert.deepEqual(toggleSort(second, "not-a-column"), second);
});

test("example routing decisions are read from the active calibration, not hard-coded", () => {
  const examples = exampleRoutingDecisions({
    active_calibration: {
      routing: {
        simple_coding: { label: "Simple coding task", provider: "claude", model: "claude-haiku-4-5", effort: "low" },
        architecture: { label: "Architecture design", provider: null, model: null, effort: null },
      },
    },
  });
  assert.equal(examples.length, 2);
  assert.equal(examples[0].text, "claude/claude-haiku-4-5 · low reasoning");
  assert.match(examples[1].text, /No eligible model/);
});
