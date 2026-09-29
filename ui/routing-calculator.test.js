"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const api = require("./routing-calculator.js");
const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const app = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");

// ----- Pure helpers -------------------------------------------------------

test("every preset fills every control with a value the router accepts", () => {
  assert.ok(api.PRESETS.length >= 4);
  const ids = new Set();
  for (const preset of api.PRESETS) {
    assert.ok(preset.label && preset.taskType);
    assert.ok(preset.complexity >= 1 && preset.complexity <= 10);
    assert.ok(["low", "medium", "high"].includes(preset.risk));
    assert.ok(!ids.has(preset.id), "ids are unique");
    ids.add(preset.id);
  }
});

test("a complexity value maps to its named band", () => {
  const bands = [
    { label: "Trivial", from: 1, to: 1 }, { label: "Simple", from: 2, to: 3 },
    { label: "Standard", from: 4, to: 6 }, { label: "Complex", from: 7, to: 8 },
    { label: "Very complex", from: 9, to: 9 }, { label: "Extreme", from: 10, to: 10 },
  ];
  assert.equal(api.bandFor(bands, 1), "Trivial");
  assert.equal(api.bandFor(bands, 5), "Standard");
  assert.equal(api.bandFor(bands, 9), "Very complex");
  assert.equal(api.bandFor(bands, 99), "Extreme", "out-of-range input is clamped, never blank");
  assert.equal(api.bandFor(null, 5), "");
});

test("form values become backend inputs, dropping blank optional fields", () => {
  assert.deepEqual(
    api.inputsFromForm({ taskType: "refactor", complexity: "7", risk: "high", provider: "", suggestedModel: "  ", suggestedEffort: "high" }),
    { taskType: "refactor", complexity: 7, risk: "high" },
    "an effort without a model is meaningless and is not sent",
  );
  assert.deepEqual(
    api.inputsFromForm({ taskType: "feature", complexity: 5, risk: "low", provider: "claude", suggestedModel: " claude-opus-5 ", suggestedEffort: "xhigh" }),
    { taskType: "feature", complexity: 5, risk: "low", provider: "claude", suggestedModel: "claude-opus-5", suggestedEffort: "xhigh" },
  );
  assert.deepEqual(api.inputsFromForm({}), { taskType: "feature", complexity: 5, risk: "medium" });
  assert.equal(api.inputsFromForm({ complexity: 0 }).complexity, 1);
  assert.equal(api.inputsFromForm({ complexity: 40 }).complexity, 10);
  assert.equal(api.inputsFromForm({ complexity: "x" }).complexity, 5);
  assert.equal(api.inputsFromForm({ risk: "extreme" }).risk, "medium");
});

test("numbers are worded for people, and missing prices are honest", () => {
  assert.equal(api.percent(0.923), "92%");
  assert.equal(api.percent(null), "—");
  assert.equal(api.score(0.71724), "0.717");
  assert.equal(api.score(null), "—");
  assert.equal(api.percent(0), "0%", "a real zero is still shown as zero");
  assert.equal(api.percent(undefined), "—");
  assert.equal(api.money(null), "unpriced");
  assert.equal(api.money(0.0281), "$0.028");
  assert.equal(api.money(0.00512), "$0.0051");
  assert.equal(api.money(1.5), "$1.50");
  assert.equal(api.money(0), "$0");
});

test("results read as one line, and copy as text", () => {
  const result = { providerName: "Claude", modelLabel: "Claude Opus 5.5", effortLabel: "XHigh" };
  assert.equal(api.headline(result), "Claude · Claude Opus 5.5 · XHigh effort");
  assert.equal(api.headline({ providerName: "Grok", error: "No eligible model" }), "Grok: No eligible model");
  const text = api.copyText({ inputs: { taskType: "feature", complexity: 7, risk: "high" }, results: [result] });
  assert.match(text, /feature, complexity 7\/10, high risk/);
  assert.match(text, /Claude · Claude Opus 5\.5 · XHigh effort/);
  assert.equal(api.sourceLabel("suggested"), "The AI router's suggestion, used as given");
});

// ----- Markup and accessibility -------------------------------------------

function modal() {
  const start = html.indexOf('<div id="routing-calculator-modal"');
  assert.ok(start > 0);
  return html.slice(start, html.indexOf('<div id="diagnose-modal"', start));
}

test("the button lives in the Dynamic Model Routing panel", () => {
  const panel = html.slice(html.indexOf("dynamic-routing-panel"), html.indexOf('id="provider-cards"'));
  assert.match(panel, /id="open-routing-calculator"[^>]*aria-haspopup="dialog"/);
  assert.match(panel, />Try the router</);
});

test("the dialog follows the app's modal pattern and is labelled", () => {
  const dialog = modal();
  assert.match(dialog, /class="modal-overlay" hidden role="dialog" aria-modal="true" aria-labelledby="routing-calculator-title"/);
  assert.match(dialog, /id="routing-calculator-close" class="modal-close" aria-label="Close"/);
  assert.match(dialog, /id="routing-calculator-title"/);
  assert.ok(!/style="/.test(dialog), "no inline styles");
});

test("every control has a label and the slider spans 1 to 10", () => {
  const dialog = modal();
  assert.match(dialog, /<input id="calc-complexity" type="range" min="1" max="10" step="1"/);
  assert.match(dialog, /<label for="calc-complexity">/);
  assert.match(dialog, /<fieldset[^>]*calc-risk">\s*<legend>Risk<\/legend>/);
  for (const id of ["calc-task-type", "calc-provider", "calc-suggested-model", "calc-suggested-effort"]) {
    assert.ok(new RegExp(`<label[^>]*>[^<]*(?:<[^>]+>[^<]*)*<[^>]*id="${id}"`).test(dialog), `${id} sits inside a label`);
  }
  assert.match(dialog, /id="calc-status"[^>]*role="status" aria-live="polite"/);
  assert.match(dialog, /id="calc-results"[^>]*aria-live="polite"/);
});

test("the dialog is wired for keyboard and mouse", () => {
  assert.match(app, /event\.key === "Escape" && !byId\("routing-calculator-modal"\)\.hidden\) closeRoutingCalculator\(\)/);
  assert.match(app, /event\.target === byId\("routing-calculator-modal"\)\) closeRoutingCalculator\(\)/);
  assert.match(app, /if \(!byId\("routing-calculator-modal"\)\.hidden\) trapCalcFocus\(event\)/);
  // Focus goes to the close button on open and back to the opener on close.
  assert.match(app, /calcEl\("routing-calculator-close"\)\.focus\(\)/);
  assert.match(app, /calculator\.opener\.focus\(\)/);
  assert.match(app, /invoke\("describe_routing_calculator"\)/);
  assert.match(app, /invoke\("simulate_routing", \{ inputs: readCalcForm\(\) \}\)/);
  assert.ok(html.indexOf("routing-calculator.js") < html.indexOf('src="app.js"'), "helpers load before app.js");
});

// ----- The real rendering and live-update code, against a small DOM -------

class Node_ {
  constructor(tag = "div") {
    this.tag = tag; this.children = []; this.dataset = {}; this._text = ""; this.value = ""; this.open = false;
    this.classes = new Set();
    this.classList = {
      toggle: (n, on) => { const enable = on === undefined ? !this.classes.has(n) : on; enable ? this.classes.add(n) : this.classes.delete(n); },
      add: (n) => this.classes.add(n), remove: (n) => this.classes.delete(n), contains: (n) => this.classes.has(n),
    };
  }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(" "); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  append(...items) { items.forEach((i) => this.children.push(typeof i === "string" ? Object.assign(new Node_("#t"), { _text: i }) : i)); }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren(...items) { this._text = ""; this.children = []; this.append(...items); }
  get childElementCount() { return this.children.length; }
  find(predicate, out = []) { if (predicate(this)) out.push(this); this.children.forEach((c) => c.find && c.find(predicate, out)); return out; }
}

function harness(invoke) {
  const start = app.indexOf("  function calcEl(id)");
  const end = app.indexOf("  async function openRoutingCalculator()", start);
  assert.ok(start > 0 && end > start, "the calculator controller is extractable");
  const nodes = new Map();
  const byId = (id) => { if (!nodes.has(id)) nodes.set(id, new Node_()); return nodes.get(id); };
  byId("calc-task-type").value = "feature";
  byId("calc-complexity").value = "7";
  byId("calc-provider").value = "";
  byId("calc-suggested-model").value = "";
  byId("calc-suggested-effort").value = "";
  const state = { routingCalculator: { options: null, request: 0, timer: null, opener: null, payload: null } };
  const document = {
    createElement: (t) => new Node_(t), createTextNode: (t) => Object.assign(new Node_("#t"), { _text: t }),
    querySelector: () => ({ value: "high" }), querySelectorAll: () => [],
  };
  const window = { SwarmRoutingCalculator: api, clearTimeout: () => {}, setTimeout: () => 1 };
  const shown = [];
  const build = new Function(
    "state", "window", "document", "byId", "invoke", "errorText", "button", "showToast",
    `${app.slice(start, end)}\nreturn { runRoutingCalculator, renderCalcResults, readCalcForm, setCalcStatus, buildCalcResult };`,
  );
  const controller = build(state, window, document, byId, invoke, (e) => String(e), (t) => Object.assign(new Node_("button"), { _text: t }), (m) => shown.push(m));
  return { controller, byId, state, shown };
}

const claude = {
  provider: "claude", providerName: "Claude", model: "claude-opus-5-5", modelLabel: "Claude Opus 5.5",
  effort: "xhigh", effortLabel: "XHigh", source: "scored",
  explanation: "Complexity 7/10 falls in Claude's Complex band.",
  steps: ["Complexity 7/10 is the Complex band.", "11 combinations were scored."],
  upgrade: { from: "claude-opus-5", to: "claude-opus-5-5", reason: "the latest opus release" },
  alternatives: [
    { model: "claude-opus-5-5", modelLabel: "Claude Opus 5.5", effort: "xhigh", effortLabel: "XHigh", score: 0.6068, expectedSuccess: 0.92, estimatedCost: 0.156 },
    { model: "claude-opus-5", modelLabel: "Claude Opus 5", effort: "xhigh", effortLabel: "XHigh", score: 0.6064, expectedSuccess: 0.92, estimatedCost: null },
  ],
};

test("a result card shows the answer, why, the upgrade, and the alternatives", () => {
  const { controller, byId } = harness(async () => ({}));
  controller.renderCalcResults({ results: [claude], catalog: { label: "Active calibration 2026-09-29-003" } });
  const cards = byId("calc-results").children;
  assert.equal(cards.length, 1);
  const text = cards[0].textContent;
  assert.match(text, /Claude Opus 5\.5/);
  assert.match(text, /XHigh effort/);
  assert.match(text, /Picked by the scoring router/);
  assert.match(text, /Upgraded.*claude-opus-5 → claude-opus-5-5/);
  assert.match(text, /How it got there/);
  assert.match(text, /Other options it scored \(2\)/);
  assert.match(text, /92%/);
  assert.match(text, /unpriced/, "an unpriced option is not shown as free");
  const chosen = cards[0].find((n) => n.classes.has("chosen"));
  assert.equal(chosen.length, 1, "exactly one row is marked as the pick");
  assert.equal(byId("calc-copy").classes.has("hidden"), false);
  assert.equal(byId("calc-catalog").textContent, "Using: Active calibration 2026-09-29-003.");
  assert.deepEqual(byId("calc-model-options").children.map((o) => o.value), ["claude-opus-5-5", "claude-opus-5"]);
});

test("one tool opens its steps; several tools keep them folded", () => {
  const { controller } = harness(async () => ({}));
  assert.equal(controller.buildCalcResult(claude, true).find((n) => n.tag === "details" && n.open).length, 1);
  assert.equal(controller.buildCalcResult(claude, false).find((n) => n.tag === "details" && n.open).length, 0);
});

test("a tool that cannot be routed shows its reason instead of a blank card", () => {
  const { controller, byId } = harness(async () => ({}));
  controller.renderCalcResults({ results: [{ provider: "grok", providerName: "Grok", error: "No eligible model for Grok." }] });
  const card = byId("calc-results").children[0];
  assert.equal(card.classes.has("error"), true);
  assert.match(card.textContent, /Grok/);
  assert.match(card.textContent, /No eligible model for Grok\./);
});

test("the form is sent to the router as the inputs it needs", async () => {
  const calls = [];
  const { controller, byId } = harness(async (command, args) => { calls.push([command, args]); return { results: [], catalog: {} }; });
  byId("calc-provider").value = "codex";
  byId("calc-suggested-model").value = "gpt-5.6-sol";
  await controller.runRoutingCalculator();
  assert.equal(calls[0][0], "simulate_routing");
  assert.deepEqual(calls[0][1].inputs, {
    taskType: "feature", complexity: 7, risk: "high", provider: "codex", suggestedModel: "gpt-5.6-sol",
  });
});

test("a slow older answer never overwrites a newer one", async () => {
  const pending = [];
  const { controller, byId } = harness((command) => new Promise((resolve) => pending.push(resolve)));
  const first = controller.runRoutingCalculator();
  const second = controller.runRoutingCalculator();
  pending[1]({ results: [{ ...claude, modelLabel: "Newest answer" }], catalog: {} });
  await second;
  pending[0]({ results: [{ ...claude, modelLabel: "Stale answer" }], catalog: {} });
  await first;
  assert.match(byId("calc-results").textContent, /Newest answer/);
  assert.doesNotMatch(byId("calc-results").textContent, /Stale answer/);
});

test("a failed run explains itself and leaves the last result alone", async () => {
  let fail = false;
  const { controller, byId } = harness(async () => {
    if (fail) throw new Error("Complexity must be a whole number from 1 to 10.");
    return { results: [claude], catalog: {} };
  });
  await controller.runRoutingCalculator();
  fail = true;
  await controller.runRoutingCalculator();
  assert.match(byId("calc-status").textContent, /Complexity must be a whole number/);
  assert.equal(byId("calc-status").dataset.tone, "error");
  assert.match(byId("calc-results").textContent, /Claude Opus 5\.5/);
});
