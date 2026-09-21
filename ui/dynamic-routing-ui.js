(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmDynamicRouting = api;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  "use strict";

  // Suggested router defaults, including what each tool is best at — the
  // router weighs those strengths when it picks which enabled tool gets an
  // issue. Keep in sync with provider_strengths_preset in src/config.rs. The
  // desktop saves whatever the user picks; these fill a provider card that has
  // no router settings yet.
  const DEFAULT_ROUTER = {
    claude: {
      model: "claude-haiku-4-5",
      effort: "low",
      strengths:
        "Multi-file refactors, following an existing codebase's conventions, careful review of someone else's work, and writing documentation or tests in the surrounding style.",
    },
    codex: {
      model: "gpt-5.6-luna",
      effort: "low",
      strengths:
        "Precise bug fixes, test-driven changes, and long autonomous edit-run-verify loops where the work is checked by running it.",
    },
    grok: {
      model: "grok-4.6",
      effort: "low",
      strengths:
        "Fast turnarounds on well-scoped changes, scripting and configuration work, and quick orientation in unfamiliar code.",
    },
  };

  function defaultRouter(providerId) {
    return DEFAULT_ROUTER[providerId] || { model: "", effort: "low", strengths: "" };
  }

  function routingControlState(dynamicRouting) {
    const enabled = Boolean(dynamicRouting);
    return {
      workerDisabled: enabled,
      routerHidden: !enabled,
      statusLabel: enabled ? "ON" : "OFF",
    };
  }

  function applyRoutingControlState(card, dynamicRouting) {
    const state = routingControlState(dynamicRouting);
    const model = card.querySelector(".provider-model");
    const effort = card.querySelector(".provider-effort");
    const routerModel = card.querySelector(".provider-router-model");
    const routerEffort = card.querySelector(".provider-router-effort");
    const strengths = card.querySelector(".provider-router-strengths");
    const workerModelLabel = card.querySelector(".worker-model-label");
    const workerEffortLabel = card.querySelector(".worker-effort-label");
    const routerModelLabel = card.querySelector(".router-model-label");
    const routerEffortLabel = card.querySelector(".router-effort-label");
    const strengthsLabel = card.querySelector(".router-strengths-label");
    const note = card.querySelector(".dynamic-routing-note");
    if (model) model.disabled = state.workerDisabled;
    if (effort) effort.disabled = state.workerDisabled;
    if (workerModelLabel) workerModelLabel.classList.toggle("is-disabled", state.workerDisabled);
    if (workerEffortLabel) workerEffortLabel.classList.toggle("is-disabled", state.workerDisabled);
    if (routerModelLabel) routerModelLabel.hidden = state.routerHidden;
    if (routerEffortLabel) routerEffortLabel.hidden = state.routerHidden;
    if (routerModel) routerModel.disabled = state.routerHidden;
    if (routerEffort) routerEffort.disabled = state.routerHidden;
    if (strengthsLabel) strengthsLabel.hidden = state.routerHidden;
    if (strengths) strengths.disabled = state.routerHidden;
    if (note) note.hidden = state.routerHidden;
    return state;
  }

  return { DEFAULT_ROUTER, defaultRouter, routingControlState, applyRoutingControlState };
});
