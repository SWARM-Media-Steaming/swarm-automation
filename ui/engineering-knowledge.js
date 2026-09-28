(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmEngineeringKnowledge = api;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  "use strict";

  function defaultScope(activeRepository) {
    const repo = String(activeRepository || "").trim();
    if (!repo) return { kind: "all", id: "", label: "All connected knowledge" };
    const project = repo.split("/")[0] || "";
    return {
      kind: "repository",
      id: repo,
      project,
      label: repo,
    };
  }

  function scopeOptions(activeRepository) {
    const repo = String(activeRepository || "").trim();
    const project = repo.split("/")[0] || "";
    const options = [{ value: "all", id: "", label: "All connected knowledge" }];
    if (project) options.push({ value: "project", id: project, label: `Project / ${project}` });
    if (repo) options.push({ value: "repository", id: repo, label: `Repository / ${repo}` });
    return options;
  }

  function formatStatus(status) {
    const data = status && typeof status === "object" ? status : {};
    const enabled = Boolean(data.enabled);
    return {
      enabled,
      enabledLabel: enabled ? "On" : "Off",
      lastRefresh: data.lastRefresh || "Never",
      lastRefreshStatus: data.lastRefreshStatus || "",
      lastRefreshError: data.lastRefreshError || "",
      repositoriesIndexed: Number(data.repositoriesIndexed || 0),
      issuesUnderstood: Number(data.issuesUnderstood || 0),
      relationshipsDiscovered: Number(data.relationshipsDiscovered || 0),
      generatedEnabled: Boolean(data.automaticGeneration),
      generatedCount: Number(data.generatedKnowledgeCount || 0),
      lastGeneratedUpdate: data.lastGeneratedUpdate || "",
    };
  }

  function citationLabel(citation) {
    const item = citation && typeof citation === "object" ? citation : {};
    const repo = item.repository ? `${item.repository} · ` : "";
    const title = item.title || item.objectType || "Source";
    const provenance = item.provenanceKind ? ` (${String(item.provenanceKind).replace(/_/g, " ")})` : "";
    return `${repo}${title}${provenance}`;
  }

  function answerBlocks(result) {
    const data = result && typeof result === "object" ? result : {};
    const citations = Array.isArray(data.citations) ? data.citations : [];
    const sample = data.sampleSize == null ? "" : `Sample size: ${data.sampleSize}.`;
    const model = [data.provider, data.model, data.effort].filter(Boolean).join(" / ");
    return {
      answer: String(data.answer || "No answer."),
      citations,
      sample,
      model,
      insufficientData: Boolean(data.insufficientData),
    };
  }

  return {
    defaultScope,
    scopeOptions,
    formatStatus,
    citationLabel,
    answerBlocks,
  };
});
