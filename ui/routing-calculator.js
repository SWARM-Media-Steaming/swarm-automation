(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmRoutingCalculator = api;
})(typeof window === "undefined" ? globalThis : window, () => {
  "use strict";

  // Pure helpers for the "Try the router" dialog. Drawing and `invoke()` calls
  // stay in app.js; everything here is derivation and wording, so it can be
  // unit tested without a document. The numbers themselves come from the
  // worker's own routing code (issue_worker/routing_calculator.py).

  // One-click starting points. Each fills every control so a person can see how
  // the router treats a familiar kind of work, then nudge one value at a time.
  const PRESETS = [
    { id: "typo", label: "Fix a typo in the docs", taskType: "documentation", complexity: 1, risk: "low" },
    { id: "feature", label: "A typical feature", taskType: "feature", complexity: 5, risk: "medium" },
    { id: "refactor", label: "Risky refactor", taskType: "large_refactor", complexity: 8, risk: "high" },
    { id: "bug", label: "Hard bug hunt", taskType: "deep_debugging", complexity: 9, risk: "medium" },
    { id: "security", label: "Security review", taskType: "security_analysis", complexity: 7, risk: "high" },
  ];

  const DEFAULTS = { taskType: "feature", complexity: 5, risk: "medium", provider: "" };

  function clampComplexity(value) {
    const number = Math.round(Number(value));
    if (!Number.isFinite(number)) return DEFAULTS.complexity;
    return Math.min(10, Math.max(1, number));
  }

  // The named band a complexity value falls in ("Standard", "Complex", …).
  function bandFor(bands, complexity) {
    const value = clampComplexity(complexity);
    const band = (Array.isArray(bands) ? bands : []).find((b) => value >= b.from && value <= b.to);
    return band ? band.label : "";
  }

  // What the form holds -> what the backend takes. Blank optional fields are
  // omitted so "no suggestion" is never sent as an empty suggestion.
  function inputsFromForm(form) {
    const values = form || {};
    const inputs = {
      taskType: String(values.taskType || DEFAULTS.taskType),
      complexity: clampComplexity(values.complexity),
      risk: ["low", "medium", "high"].includes(values.risk) ? values.risk : DEFAULTS.risk,
    };
    if (values.provider) inputs.provider = String(values.provider);
    const model = String(values.suggestedModel || "").trim();
    if (model) {
      inputs.suggestedModel = model;
      if (values.suggestedEffort) inputs.suggestedEffort = String(values.suggestedEffort);
    }
    return inputs;
  }

  // ----- Wording for results ---------------------------------------------

  function headline(result) {
    if (!result) return "";
    if (result.error) return `${result.providerName || "This tool"}: ${result.error}`;
    return `${result.providerName} · ${result.modelLabel} · ${result.effortLabel} effort`;
  }

  const SOURCE_LABELS = {
    scored: "Picked by the scoring router",
    tier: "Picked by the configured complexity tiers",
    suggested: "The AI router's suggestion, used as given",
  };

  function sourceLabel(source) {
    return SOURCE_LABELS[source] || "";
  }

  // 0.923 -> "92%". The router reports expected success as a 0-1 fraction.
  function present(value) {
    return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
  }

  function percent(value) {
    return present(value) ? `${Math.round(Number(value) * 100)}%` : "—";
  }

  // Estimated dollars for a reference task, or an honest dash when unpriced.
  function money(value) {
    if (!present(value)) return "unpriced";
    const number = Number(value);
    if (number === 0) return "$0";
    return number < 0.01 ? `$${number.toFixed(4)}` : `$${number.toFixed(number < 1 ? 3 : 2)}`;
  }

  // A fit score is only meaningful next to the others, so show it as a
  // three-decimal number.
  function score(value) {
    return present(value) ? Number(value).toFixed(3) : "—";
  }

  // Plain text for the clipboard: the inputs, then one line per tool.
  function copyText(payload) {
    const data = payload || {};
    const inputs = data.inputs || {};
    const lines = [
      `Dynamic Model Routing — ${inputs.taskType || "task"}, complexity ${inputs.complexity}/10, ${inputs.risk} risk`,
    ];
    (data.results || []).forEach((result) => lines.push(headline(result)));
    return lines.join("\n");
  }

  return {
    PRESETS,
    DEFAULTS,
    clampComplexity,
    bandFor,
    inputsFromForm,
    headline,
    sourceLabel,
    percent,
    money,
    score,
    copyText,
  };
});
