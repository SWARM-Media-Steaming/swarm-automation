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
    return stats?.loops
      ? `Adversarial UAT · ${stats.averageRounds} average fix/re-test rounds per work-round · ${stats.cleanFirstPassPercent}% clean first pass · ${stats.capHitPercent}% cap hit (${stats.loops} work-rounds).`
      : "No adversarial UAT outcomes recorded yet.";
  }

  function capacity(value) {
    return typeof value === "number" && Number.isFinite(value)
      ? `${value.toFixed(1)} percentage points across providers; approximate remaining-quota snapshots, not token or dollar cost.`
      : "";
  }

  function roundDetail(round) {
    return `${round.fixer_provider} / ${round.fixer_model} → ${round.tester_provider} / ${round.tester_model}; ${round.tests_added} test files added, ${round.tests_modified} modified; failing suites ${round.tests_failing_before} → ${round.tests_failing_after}${round.disputed ? `; dispute: ${round.dispute_resolution || "upheld"}` : ""}.`;
  }

  return { roundCount, aggregate, capacity, roundDetail };
});
