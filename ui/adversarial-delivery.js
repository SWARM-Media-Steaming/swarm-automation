(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmAdversarialDelivery = api;
})(typeof window === "undefined" ? globalThis : window, () => {
  "use strict";

  const BEST_EFFORT_LABEL = "Best-effort merge with unresolved adversarial results";

  function names(items, key) {
    return (Array.isArray(items) ? items : [])
      .map((item) => (item && typeof item === "object" ? item[key] : item))
      .filter((value) => value != null && String(value).trim() !== "")
      .map((value) => String(value));
  }

  function describeUnresolved(unresolved) {
    const source = unresolved && typeof unresolved === "object" ? unresolved : {};
    const suites = names(source.suites, "id");
    const findings = names(source.findings, "title");
    const suiteText = suites.length ? suites.join(", ") : "none";
    const findingText = findings.length ? findings.join("; ") : "none";
    return `${suites.length} failing suite${suites.length === 1 ? "" : "s"} (${suiteText}); `
      + `${findings.length} open finding${findings.length === 1 ? "" : "s"} (${findingText})`;
  }

  function policyText(record) {
    if (record.adversarialMergePolicy === "best_effort") {
      return "Allow best-effort adversarial merge after 3 rounds";
    }
    if (record.adversarialMergePolicy === "strict") {
      return "Strict — merge only after adversarial acceptance";
    }
    return "";
  }

  function paragraphs(record) {
    const lines = [];
    const policy = policyText(record);
    if (policy) lines.push(["Adversarial merge policy", policy]);
    const epochs = [];
    if (record.adversarialEpochCount) epochs.push(`UAT ${record.adversarialEpochCount}`);
    if (record.securityEpochCount) epochs.push(`cybersecurity ${record.securityEpochCount}`);
    if (epochs.length) lines.push(["Adversarial epochs", epochs.join(" · ")]);
    if (record.adversarialDelivery === "best_effort") {
      lines.push(["Delivery", BEST_EFFORT_LABEL]);
      const unresolved = record.adversarialUnresolved && typeof record.adversarialUnresolved === "object"
        ? record.adversarialUnresolved : {};
      lines.push(["Unresolved before merge", describeUnresolved(unresolved.before_merge)]);
      const after = unresolved.after_merge && typeof unresolved.after_merge === "object"
        ? unresolved.after_merge : {};
      const promotion = after.promotion || record.promotionStatus || "";
      const url = after.promotion_url || record.promotionUrl || "";
      const merged = after.merged
        ? `merged into ${after.integration_branch || "the integration branch"}`
          + (after.merged_sha ? ` as ${String(after.merged_sha).slice(0, 12)}` : "")
        : "";
      const promo = promotion ? `promotion ${promotion}${url ? ` ${url}` : ""}` : "";
      lines.push(["Unresolved after merge", [describeUnresolved(after), merged, promo].filter(Boolean).join("; ")]);
    } else if (record.adversarialDelivery === "verified_clean") {
      lines.push(["Delivery", "Verified-clean merge"]);
      if (record.promotionStatus) {
        lines.push(["Promotion", [record.promotionStatus, record.promotionUrl].filter(Boolean).join(" ")]);
      }
    } else if (record.promotionStatus) {
      lines.push(["Promotion", [record.promotionStatus, record.promotionUrl].filter(Boolean).join(" ")]);
    }
    const epochRows = Array.isArray(record.adversarialEpochs) ? record.adversarialEpochs : [];
    if (epochRows.length) {
      const text = epochRows.map((epoch) => {
        const stage = epoch.stage || "uat";
        const number = epoch.epoch_number || "?";
        const outcome = epoch.outcome || "recorded";
        const reason = epoch.escalation_reason
          || (epoch.next_escalation && epoch.next_escalation.reason)
          || "";
        return `${stage} epoch ${number}: ${outcome}${reason ? ` (${reason})` : ""}`;
      }).join("; ");
      lines.push(["Epoch history", text]);
    }
    return lines;
  }

  function summaryChip(record) {
    if (record.adversarialDelivery === "best_effort") {
      return { text: BEST_EFFORT_LABEL, warning: true };
    }
    if (record.adversarialDelivery === "verified_clean") {
      return { text: "Verified-clean merge", warning: false };
    }
    if (record.adversarialMergePolicy === "strict") {
      return { text: "Strict adversarial merge", warning: false };
    }
    return null;
  }

  function repositoryPolicy(enabled) {
    return enabled
      ? "Allow best-effort adversarial merge after 3 rounds"
      : "Strict adversarial merge — blocking failures stay unmerged";
  }

  return {
    BEST_EFFORT_LABEL,
    describeUnresolved,
    paragraphs,
    summaryChip,
    repositoryPolicy,
  };
});
