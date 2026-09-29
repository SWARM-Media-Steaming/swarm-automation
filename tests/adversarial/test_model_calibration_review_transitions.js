"use strict";

/* Issue #205 AC 9, 12–13 and the review workflow: pending changes remain
 * inspectable after an unchanged check; after activation the summary must
 * describe the now-active version. Run the real UI controller against real
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

function page(initialStatus, { lastResult = null, afterActivation = null } = {}) {
  let backendStatus = initialStatus;
  const nodes = new Map();
  const byId = (id) => {
    if (!nodes.has(id)) nodes.set(id, new Element());
    return nodes.get(id);
  };
  const state = {
    config: { dynamic_model_routing: true, routing_optimization: "best" },
    modelCalibration: {
      status: initialStatus, lastResult, analysis: null, refreshing: false,
      analyzing: false, activating: false, approvingKeys: new Set(),
      sort: { column: "provider", direction: "asc" }, progressTimer: null,
    },
  };
  const calls = [], toasts = [];
  const invoke = async (command, args) => {
    calls.push({ command, args });
    if (command === "get_model_calibration_status_background") return backendStatus;
    if (command === "activate_model_calibration_background") {
      assert.ok(afterActivation, "The test must supply a real post-activation response");
      assert.equal(args.version, afterActivation.active_version);
      backendStatus = afterActivation;
      return afterActivation.active_calibration;
    }
    throw new Error(`Unexpected command ${command}`);
  };
  const document = {
    createElement: (tag) => new Element(tag),
    createTextNode: (text) => Object.assign(new Element("#text"), { textContent: text }),
  };
  const window = { SwarmModelCalibration: require(path.join(root, "ui/model-calibration-ui.js")) };
  const load = new Function("state", "window", "document", "byId", "invoke", "showToast", "formatIsoTimestamp", "errorText",
    `${source.slice(start, end)}
     return { renderModelCalibrationFull, activateProposedCalibration };`);
  const controller = load(state, window, document, byId, invoke,
    (...args) => toasts.push(args), (value) => value, String);
  return { controller, byId, calls, toasts };
}

test("a proposal retains its detailed changes after a no-change startup check", () => {
  assert.equal(data.repeated.status, "no_change");
  assert.equal(data.rechecked.proposed_version, data.proposed.proposed_version);
  assert.ok(data.rechecked.proposed_calibration.diff.pricing_changes.length);
  const app = page(data.rechecked);
  app.controller.renderModelCalibrationFull();
  assert.equal(app.byId("model-calibration-activate").classList.contains("hidden"), false);
  assert.equal(app.byId("model-calibration-changes").classList.contains("hidden"), false,
    "An unchanged observation hid the detailed changes of the still-pending proposal");
  assert.match(app.byId("model-calibration-changes-body").textContent, /23\.5/);
});

test("activating after manual refresh reconciles the cached review summary", async () => {
  const app = page(data.manual_proposed, {
    lastResult: data.manual_refresh, afterActivation: data.manual_activated,
  });
  app.controller.renderModelCalibrationFull();
  assert.match(app.byId("model-calibration-result").textContent, /available for review/);
  await app.controller.activateProposedCalibration();
  assert.ok(app.calls.some((call) => call.command === "activate_model_calibration_background"));
  assert.ok(app.toasts.some(([message]) => message.includes("Activated calibration")));
  assert.equal(app.byId("model-calibration-activate").classList.contains("hidden"), true);
  assert.doesNotMatch(app.byId("model-calibration-result").textContent, /available for review/i,
    "The manual-refresh cache still says review is pending after this exact version was activated");
});

test("freshly opened configuration shows the newly proposed price details", () => {
  const app = page(data.proposed);
  app.controller.renderModelCalibrationFull();
  assert.equal(app.byId("model-calibration-changes").classList.contains("hidden"), false);
  assert.match(app.byId("model-calibration-changes-body").textContent, /23\.5/);
  assert.match(app.byId("model-calibration-changes-body").textContent, /Routing impact/);
});
