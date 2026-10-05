"use strict";

/* Issue #374 migration of #205 UI transition coverage: activation details are
 * inspectable immediately; unchanged checks do not resurrect review state.
 * Run the real UI controller against real
 * backend snapshots. Only DOM and Tauri transport are doubles.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const root = path.resolve(__dirname, "../..");
const source = fs.readFileSync(path.join(root, "ui/app.js"), "utf8");
const start = source.indexOf("  function modelCalibrationStatus()");
const end = source.indexOf("  async function initialize()", start);
assert.ok(start >= 0 && end > start, "The production calibration controller must load");
const fixture = spawnSync("python3", [path.join(__dirname, "calibration_transition_fixture.py")], {
  cwd: root, encoding: "utf8", timeout: 20000,
});
assert.equal(fixture.status, 0, fixture.stderr || String(fixture.error));
const data = JSON.parse(fixture.stdout);

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.classes = new Set();
    this._text = "";
    this.classList = {
      add: (...names) => names.forEach((name) => this.classes.add(name)),
      remove: (...names) => names.forEach((name) => this.classes.delete(name)),
      contains: (name) => this.classes.has(name),
      toggle: (name, force) => {
        const on = force === undefined ? !this.classes.has(name) : Boolean(force);
        if (on) this.classes.add(name); else this.classes.delete(name);
        return on;
      },
    };
  }
  set className(value) { this.classes = new Set(value.split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(" "); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return [this._text, ...this.children.map((child) => child.textContent)].join(" "); }
  setAttribute(name, value) { (this.attributes ||= {})[name] = String(value); }
  getAttribute(name) { return (this.attributes || {})[name] ?? null; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this._text = ""; this.children = [...children]; }
}

function page(initialStatus, { lastResult = null } = {}) {
  const nodes = new Map();
  const byId = (id) => {
    if (!nodes.has(id)) nodes.set(id, new Element());
    return nodes.get(id);
  };
  const state = {
    config: { dynamic_model_routing: true, routing_optimization: "best" },
    modelCalibration: {
      status: initialStatus, lastResult, analysis: null, refreshing: false,
      analyzing: false,
      sort: { column: "provider", direction: "asc" }, progressTimer: null,
    },
  };
  const calls = [], toasts = [];
  const invoke = async (command, args) => {
    calls.push({ command, args });
    if (command === "get_model_calibration_status_background") return initialStatus;
    throw new Error(`Unexpected command ${command}`);
  };
  const document = {
    createElement: (tag) => new Element(tag),
    createTextNode: (text) => Object.assign(new Element("#text"), { textContent: text }),
  };
  const window = { SwarmModelCalibration: require(path.join(root, "ui/model-calibration-ui.js")) };
  const load = new Function("state", "window", "document", "byId", "invoke", "showToast", "formatIsoTimestamp", "errorText",
    `${source.slice(start, end)}
     return { renderModelCalibrationFull };`);
  const controller = load(state, window, document, byId, invoke,
    (...args) => toasts.push(args), (value) => value, String);
  return { controller, byId, calls, toasts };
}

test("an unchanged startup check keeps the automatically activated version current", () => {
  assert.equal(data.repeated.status, "no_change");
  assert.equal(data.rechecked.active_version, data.active.active_version);
  assert.equal(data.rechecked.has_newer_proposed, false);
  const app = page(data.rechecked);
  app.controller.renderModelCalibrationFull();
  assert.match(app.byId("model-calibration-result").textContent, /No routing changes were required/);
});

test("automatic refresh summary describes the active version and detailed price", () => {
  assert.equal(data.refresh.activated, true);
  const app = page(data.active, { lastResult: data.refresh });
  app.controller.renderModelCalibrationFull();
  assert.match(app.byId("model-calibration-result").textContent, /Active calibration/);
  assert.equal(app.byId("model-calibration-changes").classList.contains("hidden"), false);
  assert.match(app.byId("model-calibration-changes-body").textContent, /23\.5/);
});

test("freshly opened configuration shows the active calibrated price", () => {
  const app = page(data.active);
  app.controller.renderModelCalibrationFull();
  assert.match(app.byId("model-routing-table").textContent, /23\.5/);
  assert.doesNotMatch(source, /function activateProposedCalibration/);
});
