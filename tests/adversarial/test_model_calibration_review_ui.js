"use strict";

/**
 * Issue #205 UI integration: render real calibration-service responses in
 * the app's real rendering functions. Expected from the issue: a discovered
 * model can be inspected before activation, model detail exposes history,
 * and startup failures include useful error information while active routing
 * remains visibly available. The DOM and Tauri transport are local doubles;
 * no implementation is re-created here.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const root = path.resolve(__dirname, "../..");
const helpers = require(path.join(root, "ui/model-calibration-ui.js"));
const source = fs.readFileSync(path.join(root, "ui/app.js"), "utf8");
const start = source.indexOf("  function modelCalibrationStatus()");
const end = source.indexOf("  async function initialize()", start);
assert.ok(start >= 0 && end > start, "Load the real calibration controller, never a copied implementation");
const fixtureRun = spawnSync("python3", [path.join(__dirname, "calibration_uat_fixture.py")], {
  cwd: root, encoding: "utf8", timeout: 20000,
});
assert.equal(fixtureRun.status, 0, fixtureRun.stderr || String(fixtureRun.error || "Fixture generation failed"));
const snapshots = JSON.parse(fixtureRun.stdout);

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this._text = "";
    this.classes = new Set();
    this.classList = {
      add: (...names) => names.forEach((name) => this.classes.add(name)),
      remove: (...names) => names.forEach((name) => this.classes.delete(name)),
      toggle: (name, enabled) => {
        const on = enabled === undefined ? !this.classes.has(name) : enabled;
        if (on) this.classes.add(name); else this.classes.delete(name);
        return on;
      },
      contains: (name) => this.classes.has(name),
    };
  }
  set className(value) { this.classes = new Set(value.split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(" "); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => typeof child === "string" ? child : child.textContent).join(" "); }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this._text = ""; this.children = [...children]; }
}

function page(status, lastResult = null) {
  const nodes = new Map();
  const byId = (id) => {
    if (!nodes.has(id)) nodes.set(id, new Element());
    return nodes.get(id);
  };
  const state = {
    config: { dynamic_model_routing: true, routing_optimization: "best" },
    modelCalibration: {
      status, lastResult, analysis: null, refreshing: false, analyzing: false,
      activating: false, approvingKeys: new Set(), sort: { column: "provider", direction: "asc" },
      progressTimer: null,
    },
  };
  const calls = [];
  const invoke = async (command, args) => {
    calls.push({ command, args });
    if (command === "get_model_calibration_status_background") return status;
    if (command === "refresh_model_data_background") return snapshots.result;
    throw new Error(`Unexpected Tauri call: ${command}`);
  };
  const document = {
    createElement: (tag) => new Element(tag),
    createTextNode: (text) => { const node = new Element("#text"); node.textContent = text; return node; },
  };
  const window = { SwarmModelCalibration: helpers, setInterval: () => 1, clearInterval: () => {} };
  const load = new Function("state", "window", "document", "byId", "invoke", "showToast", "formatIsoTimestamp", "errorText",
    `${source.slice(start, end)}
     return { renderModelCalibrationFull, refreshModelCalibration, refreshModelData, onModelCalibrationRefreshed };`);
  const controller = load(state, window, document, byId, invoke, () => {}, (value) => value, String);
  return { controller, byId, calls, nodes, state };
}

test("manual refresh bypasses the interval and displays backend change counts", async () => {
  const app = page(snapshots.proposal);
  await app.controller.refreshModelData();
  assert.ok(app.calls.some(({ command, args }) => command === "refresh_model_data_background" && args.force === true));
  assert.match(app.byId("model-calibration-result").textContent, /Model data refreshed successfully/);
  assert.match(app.byId("model-calibration-result").textContent, /New models/);
  assert.equal(app.byId("refresh-model-data").disabled, false);
});

test("a newly discovered remote model is inspectable before activating its proposal", () => {
  assert.equal(snapshots.result.activated, false);
  assert.equal(snapshots.proposal.proposed_calibration.discovered_models[0].model, "newly-discovered");
  const app = page(snapshots.proposal, snapshots.result);
  app.controller.renderModelCalibrationFull();
  const rows = app.byId("model-routing-table").textContent;
  assert.match(rows, /newly-discovered/, "The proposed model exists in backend data but has no model-detail row for review");
  assert.match(rows, /DISCOVERED/);
});

test("model detail exposes stored pricing and benchmark history", () => {
  const status = snapshots.failed_status;
  const model = status.active_calibration.models[0];
  assert.ok(model.pricing_history.length > 0);
  assert.ok(model.benchmark_history.some((item) => item.field.includes("fixture_coding")));
  const app = page(status);
  app.controller.renderModelCalibrationFull();
  const rows = app.byId("model-routing-table").textContent;
  assert.match(rows, /17\.75/, "Expanding a model must expose its previous output price, not only the current price");
  assert.match(rows, /63\.125/, "Stored historical benchmark values must reach model detail");
});

test("startup failure shows useful source error while keeping the active version visible", async () => {
  const app = page(snapshots.failed_status);
  app.controller.onModelCalibrationRefreshed(snapshots.failure);
  // Finish the resolved Tauri status promise; no timer or scheduling sleep.
  await Promise.resolve();
  await Promise.resolve();
  const text = [...app.nodes.values()].map((node) => node.textContent).join("\n");
  assert.ok(text.includes(snapshots.failed_status.active_version));
  assert.match(text, /Fixture source unavailable/, "Startup failure details must not disappear because lastResult is only populated by manual refresh");
});
