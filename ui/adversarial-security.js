(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmAdversarialSecurity = api;
})(typeof window === "undefined" ? globalThis : window, () => {
  "use strict";

  const SEVERITIES = ["Critical", "High", "Medium", "Low"];

  // A review that could not run and a review that ran and found nothing must
  // never render the same way, so an execution with no recorded status reads
  // as "—" rather than silently as a pass. securityReviewStatus is the
  // authoritative verdict whenever it is set — it is written on both the
  // success path and the early-failure path (setup/auth/session failures
  // before any round completes, where securityOutcome is never set at all).
  // Only fall back to securityOutcome when no status was ever recorded: a
  // known-but-unstatused outcome still must not silently read as blank/pass.
  function reviewStatus(record) {
    if (record.securityReviewStatus) return record.securityReviewStatus;
    const outcome = record.securityOutcome;
    if (!outcome || outcome === "disabled") return "—";
    return "FAILED";
  }

  function roundCount(record) {
    return record.securityOutcome && record.securityOutcome !== "disabled"
      ? String(record.securityRoundCount || 0) : "—";
  }

  function severityLine(findings) {
    const counts = (findings && findings.severity) || {};
    return SEVERITIES.map((name) => `${name} ${counts[name] || 0}`).join(" · ");
  }

  function findingsSummary(record) {
    const findings = record.securityFindings;
    if (!findings || typeof findings !== "object" || !Object.keys(findings).length) return "";
    const discovered = findings.inScopeDiscovered || 0;
    const fixed = findings.inScopeFixed || 0;
    const open = findings.inScopeOpen || 0;
    const created = findings.issuesCreated || 0;
    return `${discovered} in-scope finding${discovered === 1 ? "" : "s"} · ${fixed} fixed · ${open} unresolved · `
      + `${created} follow-up issue${created === 1 ? "" : "s"} · ${severityLine(findings)}`;
  }

  function aggregate(stats) {
    return stats?.reviews
      ? `Adversarial Cybersecurity · ${stats.averageRounds} average fix/re-test rounds per review · ${stats.passPercent}% clean · ${stats.fixedPercent}% remediated · ${stats.failedPercent}% unresolved or failed (${stats.reviews} reviews, ${stats.findingsFixed}/${stats.findingsFound} in-scope findings fixed).`
      : "No adversarial cybersecurity reviews recorded yet.";
  }

  // Configured provider / model / reasoning effort only. Rows written before
  // effort was recorded have none and say so.
  function agent(provider, model, effort) {
    return `${provider} / ${model} / ${effort || "Not recorded"} reasoning`;
  }

  function roundDetail(round) {
    const epoch = round.epoch_number ? `epoch ${round.epoch_number} · ` : "";
    return `${epoch}fixer ${agent(round.fixer_provider, round.fixer_model, round.fixer_effort)} → tester ${agent(round.tester_provider, round.tester_model, round.tester_effort)}; `
      + `${round.findings_found || 0} finding(s) open, ${round.findings_fixed || 0} verified fixed, `
      + `${round.findings_filed || 0} filed separately; ${round.tests_added} security test files added, `
      + `${round.tests_modified} modified; failing suites ${round.tests_failing_before} → ${round.tests_failing_after}.`;
  }

  return { reviewStatus, roundCount, severityLine, findingsSummary, aggregate, roundDetail, SEVERITIES };
});
