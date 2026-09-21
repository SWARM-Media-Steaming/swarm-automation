const test = require("node:test");
const assert = require("node:assert/strict");
const { defaultRouter, routingControlState, applyRoutingControlState } = require("./dynamic-routing-ui.js");

function element(className) {
  return {
    className,
    disabled: false,
    hidden: false,
    classList: {
      names: new Set(),
      toggle(name, on) {
        if (on) this.names.add(name);
        else this.names.delete(name);
      },
      contains(name) {
        return this.names.has(name);
      },
    },
  };
}

function card() {
  const nodes = {
    ".provider-model": element("provider-model"),
    ".provider-effort": element("provider-effort"),
    ".provider-router-model": element("provider-router-model"),
    ".provider-router-effort": element("provider-router-effort"),
    ".provider-router-strengths": element("provider-router-strengths"),
    ".worker-model-label": element("worker-model-label"),
    ".worker-effort-label": element("worker-effort-label"),
    ".router-model-label": element("router-model-label"),
    ".router-effort-label": element("router-effort-label"),
    ".router-strengths-label": element("router-strengths-label"),
    ".dynamic-routing-note": element("dynamic-routing-note"),
  };
  return {
    nodes,
    querySelector(selector) {
      return nodes[selector] || null;
    },
  };
}

test("dynamic routing defaults name the inexpensive router for each provider", () => {
  assert.equal(defaultRouter("claude").model, "claude-haiku-4-5");
  assert.equal(defaultRouter("claude").effort, "low");
  assert.equal(defaultRouter("codex").model, "gpt-5.6-luna");
  assert.equal(defaultRouter("grok").model, "grok-4.3");
});

test("every provider ships a description of what it is best at", () => {
  ["claude", "codex", "grok"].forEach((id) => {
    assert.ok(defaultRouter(id).strengths.length > 20, `${id} has no strengths text`);
  });
  const texts = ["claude", "codex", "grok"].map((id) => defaultRouter(id).strengths);
  assert.equal(new Set(texts).size, 3);
  assert.equal(defaultRouter("unknown").strengths, "");
});

test("routing off leaves the worker selectors usable and hides the router selectors", () => {
  const state = routingControlState(false);
  assert.equal(state.workerDisabled, false);
  assert.equal(state.routerHidden, true);
  assert.equal(state.statusLabel, "OFF");

  const host = card();
  applyRoutingControlState(host, false);
  assert.equal(host.nodes[".provider-model"].disabled, false);
  assert.equal(host.nodes[".provider-effort"].disabled, false);
  assert.equal(host.nodes[".worker-model-label"].classList.contains("is-disabled"), false);
  assert.equal(host.nodes[".router-model-label"].hidden, true);
  assert.equal(host.nodes[".router-effort-label"].hidden, true);
  assert.equal(host.nodes[".provider-router-model"].disabled, true);
  assert.equal(host.nodes[".router-strengths-label"].hidden, true);
  assert.equal(host.nodes[".provider-router-strengths"].disabled, true);
  assert.equal(host.nodes[".dynamic-routing-note"].hidden, true);
});

test("routing on disables worker model and effort and shows the router selectors", () => {
  const state = routingControlState(true);
  assert.equal(state.workerDisabled, true);
  assert.equal(state.routerHidden, false);
  assert.equal(state.statusLabel, "ON");

  const host = card();
  applyRoutingControlState(host, true);
  assert.equal(host.nodes[".provider-model"].disabled, true);
  assert.equal(host.nodes[".provider-effort"].disabled, true);
  assert.equal(host.nodes[".worker-model-label"].classList.contains("is-disabled"), true);
  assert.equal(host.nodes[".worker-effort-label"].classList.contains("is-disabled"), true);
  assert.equal(host.nodes[".router-model-label"].hidden, false);
  assert.equal(host.nodes[".router-effort-label"].hidden, false);
  assert.equal(host.nodes[".provider-router-model"].disabled, false);
  assert.equal(host.nodes[".provider-router-effort"].disabled, false);
  assert.equal(host.nodes[".router-strengths-label"].hidden, false);
  assert.equal(host.nodes[".provider-router-strengths"].disabled, false);
  assert.equal(host.nodes[".dynamic-routing-note"].hidden, false);
});

test("turning routing back off restores the worker selectors", () => {
  const host = card();
  applyRoutingControlState(host, true);
  applyRoutingControlState(host, false);
  assert.equal(host.nodes[".provider-model"].disabled, false);
  assert.equal(host.nodes[".worker-model-label"].classList.contains("is-disabled"), false);
  assert.equal(host.nodes[".router-model-label"].hidden, true);
});
