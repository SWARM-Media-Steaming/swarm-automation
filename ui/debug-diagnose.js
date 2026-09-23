(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmDebugDiagnose = api;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  "use strict";

  // Pure render/format helpers for the "What's wrong?" diagnostic result
  // (see src/main.rs's DiagnosticResult/DiagnosticProblem and
  // issue_worker/diagnose.py). No DOM access here so these are unit-tested
  // directly; app.js does the actual rendering and wiring.

  const CONFIDENCE_LABELS = {
    high: "High confidence",
    medium: "Medium confidence",
    low: "Low confidence — take with a grain of salt",
  };

  function confidenceLabel(problem) {
    const key = String(problem && problem.confidence || "").toLowerCase();
    return CONFIDENCE_LABELS[key] || "Confidence unknown";
  }

  function hasNoActiveProblems(result) {
    return Boolean(result) && Array.isArray(result.problems) && result.problems.length === 0;
  }

  function unavailableMessage(result) {
    const reason = result && result.unavailableReason;
    return reason
      ? `AI explanation is unavailable right now: ${reason}`
      : "AI explanation is unavailable right now.";
  }

  function isFromCache(problem) {
    return Boolean(problem) && problem.source === "cache";
  }

  function isCanned(problem) {
    return Boolean(problem) && problem.source === "canned";
  }

  function isUnavailable(problem) {
    return Boolean(problem) && problem.source === "unavailable";
  }

  function canFileIssue(problem) {
    return Boolean(problem) && problem.isBug === true && !problem.filedIssueUrl;
  }

  function confirmFileIssuePrompt(problem) {
    const repository = (problem && problem.repository) || "swarm-automation";
    return (
      `File an unassigned GitHub issue in SWARM-Media-Steaming/swarm-automation about the ` +
      `problem diagnosed for ${repository}? Someone who owns swarm-automation will need to ` +
      `triage it.`
    );
  }

  function actionableList(problem) {
    return Array.isArray(problem && problem.actionableItems) ? problem.actionableItems : [];
  }

  function evidenceList(problem) {
    return Array.isArray(problem && problem.evidence) ? problem.evidence : [];
  }

  return {
    confidenceLabel,
    hasNoActiveProblems,
    unavailableMessage,
    isFromCache,
    isCanned,
    isUnavailable,
    canFileIssue,
    confirmFileIssuePrompt,
    actionableList,
    evidenceList,
  };
});
