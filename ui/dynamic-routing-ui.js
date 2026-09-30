(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmDynamicRouting = api;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  "use strict";

  // A provider card with no router settings yet shows no model: the desktop
  // fills an empty router model from the live catalog (the worker's
  // routing_calculator.py defaults), so no model is named here. The desktop
  // saves whatever the user picks.
  function defaultRouter() {
    return { model: "", effort: "low" };
  }

  // A few settings are a two-value string rather than a boolean, but are still
  // a single checkbox (e.g. routing_optimization: "cost" when ticked, "best"
  // when not). The generic [data-config] loop in app.js reads the pair off the
  // element's data-checked-value/data-unchecked-value attributes and maps it
  // through these, so such a setting stays part of that one loop instead of
  // earning a handler of its own.
  function valuedToggleChecked(value, checkedValue) {
    return String(value ?? "") === String(checkedValue ?? "");
  }

  function valuedToggleValue(checked, checkedValue, uncheckedValue) {
    return checked ? String(checkedValue ?? "") : String(uncheckedValue ?? "");
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

  return {
    defaultRouter,
    valuedToggleChecked,
    valuedToggleValue,
    routingControlState,
    applyRoutingControlState,
  };
});
