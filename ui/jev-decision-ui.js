(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmJevDecision = api;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  "use strict";

  const STATUSES = {
    enabled: { label: "Jev applied", tone: "running" },
    disabled: { label: "Jev off — baseline only", tone: "stopped" },
    unavailable: { label: "Jev unavailable", tone: "paused" },
    timeout: { label: "Jev timed out", tone: "paused" },
    malformed: { label: "Jev response invalid", tone: "error" },
    authentication: { label: "Jev sign-in required", tone: "paused" },
    low_confidence: { label: "Low confidence — fallback", tone: "paused" },
    fallback: { label: "Fallback used", tone: "paused" },
  };

  function statusLabel(status) {
    const key = String(status || "disabled");
    return (STATUSES[key] || STATUSES.disabled).label;
  }

  function statusTone(status) {
    const key = String(status || "disabled");
    return (STATUSES[key] || STATUSES.disabled).tone;
  }

  function percent(value) {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    const ratio = number > 1 && number <= 100 ? number : number * 100;
    return `${Math.round(ratio)}%`;
  }

  function scoreCell(value, { missing = false } = {}) {
    if (missing) return "Baseline only";
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return number.toFixed(2);
  }

  function formatDelta(absolute, percentValue) {
    if (absolute === null || absolute === undefined || absolute === "") return "—";
    const number = Number(absolute);
    if (!Number.isFinite(number)) return "—";
    const sign = number > 0 ? "+" : "";
    const pct = Number(percentValue);
    if (Number.isFinite(pct)) {
      return `${sign}${number.toFixed(2)} (${sign}${pct.toFixed(1)}%)`;
    }
    return `${sign}${number.toFixed(2)}`;
  }

  function routeLabel(provider, model, effort) {
    const parts = [provider, model, effort].map((item) => String(item || "").trim()).filter(Boolean);
    return parts.length ? parts.join(" · ") : "—";
  }

  function summaryCards(summary) {
    const info = summary || {};
    const completion = info.completionRate;
    const fallback = info.fallbackRate;
    const cost = info.jevCost;
    const retries = info.outcomes && typeof info.outcomes === "object"
      ? Object.entries(info.outcomes)
        .filter(([outcome]) => /^(retry|retried|needs_retry|failed|failure|error)$/i.test(outcome))
        .reduce((total, [, count]) => total + (Number(count) || 0), 0)
      : 0;
    return [
      {
        label: "Completion rate",
        value: completion === null || completion === undefined ? "—" : percent(completion),
        hint: `${Number(info.completed || 0)} completed of ${Number(info.comparisons || 0)}`,
      },
      {
        label: "Estimated Jev cost",
        value: cost === null || cost === undefined ? "—" : `$${Number(cost).toFixed(4)}`,
        hint: "Estimate from Jev token pricing",
      },
      {
        label: "Retries / failures",
        value: String(retries),
        hint: "Not counted as cost savings",
      },
      {
        label: "Jev fallback rate",
        value: fallback === null || fallback === undefined ? "—" : percent(fallback),
        hint: `${Number(info.fallbackCount || 0)} fallbacks · ${Number(info.disabledCount || 0)} baseline-only`,
      },
    ];
  }

  function tableRows(records) {
    return (Array.isArray(records) ? records : []).map((record) => {
      const status = String((record && record.jevStatus) || "disabled");
      const baselineOnly = Boolean(record && (record.baselineOnly || status === "disabled"));
      const jevPresent = Boolean(record && record.jevPresent);
      return {
        executionId: String((record && record.executionId) || ""),
        repository: String((record && record.repository) || ""),
        issueNumber: record && record.issueNumber,
        issueTitle: String((record && record.issueTitle) || ""),
        jevStatus: status,
        statusLabel: statusLabel(status),
        statusTone: statusTone(status),
        baselineOnly,
        jevPresent,
        baselineScore: scoreCell(record && record.baselineScore),
        jevScore: jevPresent ? scoreCell(record && record.jevScore) : "Baseline only",
        modifiedScore: scoreCell(record && record.modifiedScore),
        scoreDelta: formatDelta(record && record.scoreDelta, record && record.scoreDeltaPercent),
        routingChanged: Boolean(record && record.routingChanged),
        selected: routeLabel(record && record.modifiedProvider, record && record.modifiedModel, record && record.modifiedEffort),
        baselineRoute: routeLabel(record && record.baselineProvider, record && record.baselineModel, record && record.baselineEffort),
        cost: record && record.estimatedJevCost != null ? `$${Number(record.estimatedJevCost).toFixed(4)}` : "—",
        outcome: String((record && record.outcome) || "—"),
        fallback: Boolean(record && record.fallback),
        detail: record,
      };
    });
  }

  function connectionStatus(tool) {
    if (!tool) return { text: "Not checked", tone: "stopped" };
    if (!tool.installed) return { text: "Not installed", tone: "stopped" };
    if (tool.authenticated === false) return { text: "Sign-in required", tone: "paused" };
    if (tool.authenticated) return { text: "Connected", tone: "running" };
    return { text: tool.status || "Installed", tone: "idle" };
  }

  return {
    STATUSES,
    statusLabel,
    statusTone,
    percent,
    scoreCell,
    formatDelta,
    routeLabel,
    summaryCards,
    tableRows,
    connectionStatus,
  };
});
