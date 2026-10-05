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

  // Routing cap (per repository, per provider): the select for a provider's
  // model lists only what the CLI discovered and the catalog prices (the same
  // list the provider card uses), preceded by "Uncapped". A saved model that is
  // no longer offered stays selectable so the setting is never silently lost.
  const CAP_PROVIDERS = ["claude", "codex", "grok"];

  function capOptions(models, savedModel) {
    const options = [{ value: "", label: "Uncapped" }];
    const seen = new Set();
    (models || []).forEach((entry) => {
      if (!entry || !entry.value || seen.has(entry.value)) return;
      seen.add(entry.value);
      options.push({ value: entry.value, label: entry.label || entry.value });
    });
    if (savedModel && !seen.has(savedModel)) {
      options.push({ value: savedModel, label: `${savedModel} (not offered)` });
    }
    return options;
  }

  // An effort is only meaningful with a model. Uncapped leaves it empty and
  // disabled; otherwise it keeps the saved effort when the model supports it.
  function capEffortState(models, model, savedEffort) {
    if (!model) return { efforts: [], effort: "", disabled: true };
    const spec = (models || []).find((entry) => entry.value === model);
    const efforts = spec && Array.isArray(spec.efforts) ? [...new Set(spec.efforts)] : [];
    if (savedEffort && !efforts.includes(savedEffort)) efforts.push(savedEffort);
    const effort = efforts.includes(savedEffort)
      ? savedEffort
      : (spec && spec.defaultEffort) || (efforts.includes("high") ? "high" : efforts[0] || "");
    return { efforts, effort, disabled: false };
  }

  return {
    CAP_PROVIDERS,
    capOptions,
    capEffortState,
    defaultRouter,
    valuedToggleChecked,
    valuedToggleValue,
    routingControlState,
    applyRoutingControlState,
  };
});
