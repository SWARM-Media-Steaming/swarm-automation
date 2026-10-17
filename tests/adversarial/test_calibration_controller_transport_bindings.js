"use strict";

/* Issue #467 follow-up: the calibration controller is extracted from ui/app.js
 * and runs inside a harness that supplies its enclosing bindings. The web
 * transport binding (`webMode`) must not change what calibration renders: the
 * hosted account block is web-only and calibration output is the same on both
 * transports. Only the DOM and Tauri transport are doubles.
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

// Loads the production slice with the given transport binding. Without a
// web account module on `window`, the web block must resolve to nothing.
function page(webMode) {
  const nodes = new Map();
  const byId = (id) => {
    if (!nodes.has(id)) nodes.set(id, new Element());
    return nodes.get(id);
  };
  const state = {
    config: { dynamic_model_routing: true, routing_optimization: "best" },
    modelCalibration: {
      status: data.active, lastResult: null, analysis: null, refreshing: false,
      analyzing: false,
      sort: { column: "provider", direction: "asc" }, progressTimer: null,
    },
  };
  const invoke = async (command) => {
    throw new Error(`Unexpected command ${command}`);
  };
  const document = {
    createElement: (tag) => new Element(tag),
    createTextNode: (text) => Object.assign(new Element("#text"), { textContent: text }),
  };
  const window = { SwarmModelCalibration: require(path.join(root, "ui/model-calibration-ui.js")) };
  const load = new Function("state", "window", "document", "byId", "invoke", "showToast", "formatIsoTimestamp", "errorText", "webMode",
    `${source.slice(start, end)}
     return { renderModelCalibrationFull };`);
  const controller = load(state, window, document, byId, invoke, () => {}, (value) => value, String, webMode);
  return { controller, byId };
}

test("the desktop and web transport bindings load the same calibration controller", () => {
  for (const webMode of [false, true]) {
    const app = page(webMode);
    assert.equal(typeof app.controller.renderModelCalibrationFull, "function", `webMode=${webMode} must expose the controller`);
  }
});

test("the transport binding does not change the rendered routing table", () => {
  const desktop = page(false);
  const web = page(true);
  desktop.controller.renderModelCalibrationFull();
  web.controller.renderModelCalibrationFull();
  const desktopRows = desktop.byId("model-routing-table").textContent;
  const webRows = web.byId("model-routing-table").textContent;
  assert.ok(desktopRows.length > 0, "The active calibration must render at least one routing row");
  assert.equal(webRows, desktopRows, "Web transport must render calibration identically to desktop");
  assert.match(webRows, /23\.5/, "The active calibrated price must still be visible under the web binding");
});
