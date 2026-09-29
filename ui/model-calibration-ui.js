(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmModelCalibration = api;
})(typeof window === "undefined" ? globalThis : window, () => {
  "use strict";

  // Pure helpers for the Guides page's "Dynamic Routing
  // Calibration" section (issue #205). DOM building and `invoke()` calls stay
  // in app.js; everything here is formatting/derivation that is worth unit
  // testing without a document.

  // ----- Health pill --------------------------------------------------

  // Maps calibration status onto the app's existing status-pill vocabulary
  // (idle/running/ok/error/paused) instead of inventing a new one. See
  // .claude/rules/ui-design-system.md.
  function healthPill(status) {
    const info = status || {};
    if (info.refresh_running) return { text: "Refreshing…", tone: "running" };
    if (!info.active_version) return { text: "Not yet calibrated", tone: "idle" };
    if (!info.healthy) return { text: "Needs attention", tone: "error" };
    if (info.last_attempted_status === "failed") return { text: "Refresh failed", tone: "error" };
    if (info.has_newer_proposed) return { text: "Update available", tone: "paused" };
    return { text: "Current", tone: "ok" };
  }

  // ----- Status field grid ---------------------------------------------

  // One entry per field the issue calls out for plain-language visibility.
  // `config` is the AppConfig the page already has bound; `status` is
  // `get_model_calibration_status`'s response.
  function statusFields(config, status) {
    const cfg = config || {};
    const info = status || {};
    // Seven fields: what an operator checks at a glance. Health is the pill,
    // and a failed attempt shows in Refresh status and Source status.
    return [
      { label: "Dynamic routing", value: cfg.dynamic_model_routing ? "Enabled" : "Disabled" },
      { label: "Active calibration", value: info.active_version || "None yet" },
      { label: "Last successful refresh", value: info.last_successful_refresh_at || "Never" },
      { label: "Refresh status", value: refreshStatusLabel(info.last_attempted_status) },
      { label: "Source status", value: info.source_status || "—" },
      {
        label: "Models",
        value: `${info.active_model_count ?? 0} active · ${info.discovered_model_count ?? 0} discovered`,
      },
      { label: "Proposed update", value: info.has_newer_proposed ? "Awaiting review" : "None" },
    ];
  }

  function refreshStatusLabel(value) {
    switch (value) {
      case "changed":
        return "Changed";
      case "no_change":
        return "No change";
      case "failed":
        return "Failed";
      case "skipped_interval":
        return "Skipped (interval)";
      default:
        return "Not yet run";
    }
  }

  // ----- Refresh result summary -----------------------------------------

  // Exact-shape text blocks the issue mocks up under "Refresh Result
  // Summary". `result` is a `refresh_model_data` response.
  function refreshResult(status, cachedResult) {
    const info = status || {};
    const useStored = info.last_attempted_status && (
      !cachedResult || (cachedResult.attempted_at && info.last_attempted_refresh_at > cachedResult.attempted_at)
    );
    const result = useStored ? {
      status: info.last_attempted_status,
      error: info.last_error,
      attempted_at: info.last_attempted_refresh_at,
      diff: info.last_diff,
      calibration_version: info.last_refresh_calibration_version,
    } : cachedResult;
    if (!result) return null;
    // Older status documents did not retain the refresh's version. Avoid
    // attributing their diff to whichever calibration happens to be active.
    if (result.status === "changed" && !result.calibration_version) return null;
    if (result.status !== "changed" || !info.active_version) return result;
    // Activation does not change the refresh timestamp. Resolve the version
    // against current status even when the cached refresh is still newest.
    const version = result.calibration_version;
    return {
      ...result,
      activated: version === info.active_version,
      calibration_state: version === info.active_version ? "active"
        : info.has_newer_proposed && version === info.proposed_version ? "proposed" : "historical",
    };
  }

  function resultHeadline(result) {
    const status = (result || {}).status;
    if (status === "changed") return "Model data refreshed successfully";
    if (status === "no_change") return "Model data is current. No routing changes were required.";
    if (status === "failed") return "Model data refresh failed. Existing routing configuration remains active.";
    if (status === "skipped_interval") return "Refresh skipped: the minimum refresh interval has not elapsed.";
    if (status === "already_running") return "A model data refresh is already running.";
    return "";
  }

  function costImpactText(percent) {
    if (percent === null || percent === undefined) return "No estimate available";
    const sign = percent > 0 ? "+" : "";
    return `${sign}${percent}%`;
  }

  // Line items for the "Model data refreshed successfully" summary block.
  // Returns [] (nothing to show beyond the headline) for no_change/failed.
  function resultSummaryLines(result) {
    const info = result || {};
    if (info.status !== "changed") return [];
    const diff = info.diff || {};
    const lines = [
      { label: "Models checked", value: String(diff.models_checked ?? info.models_checked ?? 0) },
      { label: "New models", value: String((diff.newly_discovered_models || diff.discovered_models || []).length) },
      { label: "Pricing changes", value: String((diff.pricing_changes || []).length) },
      { label: "Benchmark changes", value: String((diff.benchmark_changes || []).length) },
      { label: "Routing changes", value: `${(diff.routing_changes || []).length} workload categor${(diff.routing_changes || []).length === 1 ? "y" : "ies"}` },
      { label: "Estimated cost impact", value: costImpactText(diff.estimated_cost_change_percent) },
      {
        label: "Calibration",
        value: info.calibration_state === "active"
          ? `Active calibration (version ${info.calibration_version})`
          : info.calibration_state === "historical"
          ? `Historical calibration (version ${info.calibration_version})`
          : info.activated
          ? `Active immediately (version ${info.calibration_version})`
          : `New calibration available for review (version ${info.calibration_version})`,
      },
    ];
    if ((diff.removed_models || []).length) {
      lines.push({ label: "Removed models", value: String(diff.removed_models.length) });
    }
    if ((diff.performance_changes || []).length) {
      lines.push({ label: "Performance changes", value: String(diff.performance_changes.length) });
    }
    if ((diff.routing_input_changes || []).length) {
      lines.push({ label: "Routing input changes", value: String(diff.routing_input_changes.length) });
    }
    if ((diff.cost_comparison_categories || []).length) {
      lines.push({
        label: "Cost estimate coverage",
        value: `${diff.cost_comparison_categories.length} workloads with prices in both calibrations`,
      });
    }
    return lines;
  }

  function failureDetail(result) {
    const info = result || {};
    if (info.status !== "failed") return "";
    return info.error ? String(info.error) : "";
  }

  // ----- "What changed" detail (pricing/benchmark/routing/discovered) ---

  function changeDetailLines(diff) {
    const info = diff || {};
    const lines = [];
    (info.discovered_models || []).forEach((key) => {
      lines.push({ heading: key, detail: "New model discovered" });
    });
    (info.removed_models || []).forEach((key) => {
      lines.push({ heading: key, detail: "Removed from the updated model catalog" });
    });
    (info.status_changes || []).forEach((change) => {
      lines.push({ heading: change.key, detail: `Status ${change.previous_status} → ${change.new_status}` });
    });
    (info.pricing_changes || []).forEach((change) => {
      const details = [];
      [
        ["input_cost", "Input price"], ["output_cost", "Output price"],
        ["reasoning_cost", "Reasoning price"], ["cost_rank", "Cost rank"],
      ].forEach(([field, label]) => {
        const before = change[`previous_${field}`] ?? null;
        const after = change[`new_${field}`] ?? null;
        if (before === after) return;
        let detail = `${label} ${before ?? "unavailable"} → ${after ?? "unavailable"}`;
        if (field !== "cost_rank") {
          if (before && after != null) {
            const delta = Math.round(((after - before) / before) * 1000) / 10;
            detail += ` (${delta > 0 ? "+" : ""}${delta}%)`;
          }
          detail += " USD per million tokens";
        }
        details.push(detail);
      });
      lines.push({ heading: `${change.provider}/${change.model}`, detail: details.join("; ") });
    });
    [...(info.benchmark_changes || []), ...(info.performance_changes || []),
      ...(info.routing_input_changes || [])].forEach((change) => {
      lines.push({
        heading: `${change.provider}/${change.model}`,
        detail: `${change.field}: ${change.previous ?? "—"} → ${change.new ?? "—"}`,
      });
    });
    return lines;
  }

  function routingImpactLines(diff) {
    const changes = (diff || {}).routing_changes || [];
    if (!changes.length) return [{ heading: "All workload categories", detail: "No change" }];
    return changes.map((change) => {
      const previous = change.previous ? `${change.previous.model || "none"} (${change.previous.effort || "—"})` : "none";
      const next = `${(change.new || {}).model || "none"} (${(change.new || {}).effort || "—"})`;
      return { heading: change.label || change.category, detail: `${previous} → ${next}` };
    });
  }

  // ----- How Dynamic Routing works (static, plain-language) -------------

  const ALGORITHM_FLOW_STEPS = [
    "Prompt",
    "Pre-flight analysis",
    "Task type + complexity",
    "Eligible models",
    "Lowest cost that fits",
    "Model + reasoning effort",
  ];

  const ROUTING_FACTORS = [
    { name: "Task Complexity", detail: "How difficult the request appears to be." },
    {
      name: "Task Type",
      detail: "Whether the work involves coding, debugging, architecture, reasoning, research, simple tasks, or agentic workflows.",
    },
    { name: "Model Capability", detail: "Benchmark and historical performance for each model." },
    { name: "Expected Cost", detail: "Estimated input, output, and reasoning token costs for completing the task." },
    { name: "Reasoning Level", detail: "Whether the task is expected to require low, medium, or high reasoning effort." },
    { name: "Performance", detail: "Model speed, latency, reliability, and historical success." },
  ];

  // ----- Current strategy summary ----------------------------------------

  function currentStrategy(status) {
    const info = status || {};
    const costOn = true;
    const weights = info.weights || {};
    return {
      mode: "Cost Aware",
      costOptimizationEnabled: costOn,
      factors: [
        { name: "Capability", level: weights.capability || "high" },
        { name: "Task Fit", level: weights.task_fit || "high" },
        { name: "Cost Efficiency", level: weights.cost_efficiency || (costOn ? "high" : "medium") },
        { name: "Performance", level: weights.performance || "medium" },
        { name: "Reliability", level: weights.reliability || "medium" },
      ],
    };
  }

  // ----- Model routing table ---------------------------------------------

  const SORTABLE_COLUMNS = [
    "provider",
    "model",
    "status",
    "coding_score",
    "agentic_score",
    "reasoning_score",
    "input_cost",
    "output_cost",
    "speed",
    "cost_efficiency",
    "last_updated",
  ];

  // Stable sort (index tiebreak) over a model list. Unknown columns fall back
  // to "provider" so a bad/removed column name never throws.
  function sortModels(models, column, direction) {
    const list = Array.isArray(models) ? models : [];
    const key = SORTABLE_COLUMNS.includes(column) ? column : "provider";
    const sign = direction === "desc" ? -1 : 1;
    return list
      .map((model, index) => ({ model, index }))
      .sort((a, b) => {
        const left = a.model ? a.model[key] : undefined;
        const right = b.model ? b.model[key] : undefined;
        if (left == null && right == null) return a.index - b.index;
        if (left == null) return 1;
        if (right == null) return -1;
        if (typeof left === "number" && typeof right === "number") {
          return (left - right) * sign || a.index - b.index;
        }
        return String(left).localeCompare(String(right)) * sign || a.index - b.index;
      })
      .map((entry) => entry.model);
  }

  function toggleSort(current, column) {
    if (!SORTABLE_COLUMNS.includes(column)) return current;
    if (current.column !== column) return { column, direction: "asc" };
    return { column, direction: current.direction === "asc" ? "desc" : "asc" };
  }

  // ----- Example routing decisions ---------------------------------------

  // Built from the active calibration's own recorded routing decisions
  // (`ModelCalibrationService.recalculate_routing`), never hard-coded, per
  // the issue's "generated from the actual current calibration" requirement.
  function exampleRoutingDecisions(status) {
    const routing = ((status || {}).active_calibration || {}).routing || {};
    return Object.values(routing).map((decision) => ({
      label: decision.label,
      text: decision.provider && decision.model
        ? `${decision.provider}/${decision.model} · ${decision.effort || "medium"} reasoning`
        : "No eligible model for this workload yet",
    }));
  }

  return {
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
    ALGORITHM_FLOW_STEPS,
    ROUTING_FACTORS,
    currentStrategy,
    SORTABLE_COLUMNS,
    sortModels,
    toggleSort,
    exampleRoutingDecisions,
  };
});
