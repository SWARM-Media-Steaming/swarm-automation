const test = require("node:test");
const assert = require("node:assert/strict");
const {
  defaultRouter,
  routingControlState,
  applyRoutingControlState,
  valuedToggleChecked,
  valuedToggleValue,
} = require("./dynamic-routing-ui.js");

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
    ".worker-model-label": element("worker-model-label"),
    ".worker-effort-label": element("worker-effort-label"),
    ".router-model-label": element("router-model-label"),
    ".router-effort-label": element("router-effort-label"),
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
  assert.equal(defaultRouter("grok").model, "grok-4.6");
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

test("a two-value checkbox is ticked only for its checked value", () => {
  assert.equal(valuedToggleChecked("cost", "cost"), true);
  assert.equal(valuedToggleChecked("best", "cost"), false);
  // A config written before the setting existed must not read as ticked.
  assert.equal(valuedToggleChecked(undefined, "cost"), false);
  assert.equal(valuedToggleChecked("", "cost"), false);
});

test("a two-value checkbox saves a string, never a boolean", () => {
  assert.equal(valuedToggleValue(true, "cost", "best"), "cost");
  assert.equal(valuedToggleValue(false, "cost", "best"), "best");
  assert.notEqual(typeof valuedToggleValue(true, "cost", "best"), "boolean");
});

test("cost routing toggle copy holds frontier models to complexity 9 or 10", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
  const match = html.match(/Optimize routing for cost<\/strong><small>(.*?)<\/small>/);
  assert.ok(match, "toggle blurb should sit under Optimize routing for cost");
  const copy = match[1];
  assert.match(copy, /least expensive model that can actually do the work/);
  assert.match(copy, /frontier model is a last resort/);
  assert.match(copy, /complexity is 9 or 10/);
  assert.match(copy, /capable mid-tier model/);
  assert.doesNotMatch(copy, /escalating only when risk is high/);
});

test("dynamic model routing help describes the cost frontier floor", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const source = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
  const topic = source.match(/"dynamic-model-routing":\s*\{[\s\S]*?html:\s*"([\s\S]*?)",\n\s*links:/);
  assert.ok(topic, "dynamic-model-routing help topic should exist");
  const html = topic[1];
  assert.match(html, /Optimize routing for cost/);
  assert.match(html, /least expensive model that can actually do the work/);
  assert.match(html, /frontier model is a last resort/);
  assert.match(html, /complexity is 9 or 10/);
  assert.match(html, /capable mid-tier model/);
  assert.doesNotMatch(html, /escalates to a stronger one when the graded risk is high/);
});
