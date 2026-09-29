(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmAdversarialUat = api;
})(typeof window === "undefined" ? globalThis : window, () => {
  "use strict";

  function roundCount(record) {
    return record.adversarialOutcome && record.adversarialOutcome !== "disabled"
      ? String(record.adversarialRoundCount || 0) : "—";
  }

  function aggregate(stats) {
    if (!stats?.loops) return "No adversarial UAT outcomes recorded yet.";
    const base = `Adversarial UAT · ${stats.averageRounds} average fix/re-test rounds per work-round · ${stats.cleanFirstPassPercent}% clean first pass · ${stats.capHitPercent}% cap hit (${stats.loops} work-rounds).`;
    if (stats.verifiedCleanCount == null && stats.bestEffortCount == null) return base;
    const verified = stats.verifiedCleanCount ?? 0;
    const best = stats.bestEffortCount ?? 0;
    return `${base} ${verified} verified-clean merge${verified === 1 ? "" : "s"} · ${best} best-effort merge${best === 1 ? "" : "s"}.`;
  }

  function capacity(value) {
    return typeof value === "number" && Number.isFinite(value)
      ? `${value.toFixed(1)} percentage points across providers; approximate remaining-quota snapshots, not token or dollar cost.`
      : "";
  }

  // Configured provider / model / reasoning effort only. Rows written before
  // effort was recorded have none and say so.
  function agent(provider, model, effort) {
    return `${provider} / ${model} / ${effort || "Not recorded"} reasoning`;
  }

  function roundDetail(round) {
    const epoch = round.epoch_number ? `epoch ${round.epoch_number} · ` : "";
    return `${epoch}fixer ${agent(round.fixer_provider, round.fixer_model, round.fixer_effort)} → tester ${agent(round.tester_provider, round.tester_model, round.tester_effort)}; ${round.tests_added} test files added, ${round.tests_modified} modified; failing suites ${round.tests_failing_before} → ${round.tests_failing_after}${round.disputed ? `; dispute: ${round.dispute_resolution || "upheld"}` : ""}.`;
  }

  return { roundCount, aggregate, capacity, roundDetail };
});
