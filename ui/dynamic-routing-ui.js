(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmDynamicRouting = api;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  "use strict";

  // Suggested router defaults for a provider card that has no router settings
  // yet. Keep in sync with router_preset in src/config.rs. The desktop saves
  // whatever the user picks.
  const DEFAULT_ROUTER = {
    claude: { model: "claude-haiku-4-5", effort: "low" },
    codex: { model: "gpt-5.6-luna", effort: "low" },
    grok: { model: "grok-4.6", effort: "low" },
  };

  function defaultRouter(providerId) {
    return DEFAULT_ROUTER[providerId] || { model: "", effort: "low" };
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
    const workerModelLabel = card.querySelector(".worker-model-label");
    const workerEffortLabel = card.querySelector(".worker-effort-label");
    const routerModelLabel = card.querySelector(".router-model-label");
    const routerEffortLabel = card.querySelector(".router-effort-label");
    const note = card.querySelector(".dynamic-routing-note");
    if (model) model.disabled = state.workerDisabled;
    if (effort) effort.disabled = state.workerDisabled;
    if (workerModelLabel) workerModelLabel.classList.toggle("is-disabled", state.workerDisabled);
    if (workerEffortLabel) workerEffortLabel.classList.toggle("is-disabled", state.workerDisabled);
    if (routerModelLabel) routerModelLabel.hidden = state.routerHidden;
    if (routerEffortLabel) routerEffortLabel.hidden = state.routerHidden;
    if (routerModel) routerModel.disabled = state.routerHidden;
    if (routerEffort) routerEffort.disabled = state.routerHidden;
    if (note) note.hidden = state.routerHidden;
    return state;
  }

  return { DEFAULT_ROUTER, defaultRouter, routingControlState, applyRoutingControlState };
});
