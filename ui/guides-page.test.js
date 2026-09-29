"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const app = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");

function view(id) {
  const start = html.indexOf(`<section id="view-${id}"`);
  assert.notEqual(start, -1, `view-${id} exists`);
  const end = html.indexOf("</section>", start);
  return html.slice(start, end);
}

// Every routing-explainer panel that used to sit in AI Configuration.
const MOVED_IDS = [
  "model-calibration-health-pill",
  "model-calibration-fields",
  "model-calibration-strategy",
  "model-calibration-flow",
  "model-routing-table",
  "model-calibration-examples",
  "refresh-model-data",
];

test("Guides is a nav page with a title, a view, and a Dynamic Routing tab", () => {
  assert.match(html, /data-view-target="guides"[^>]*>[^<]*<span[^>]*>[^<]*<\/span>Guides</);
  assert.match(app, /guides: "Guides"/);
  const guides = view("guides");
  assert.match(guides, /class="eyebrow">GUIDES</);
  assert.match(guides, /role="tablist"/);
  assert.match(guides, /data-guides-tab="routing"[^>]*>Dynamic Routing</);
  assert.match(guides, /data-guides-panel="routing"/);
  assert.match(guides, /role="tabpanel"[^>]*aria-labelledby="guides-tab-routing"/);
});

test("the routing panels live on Guides and no longer on AI Configuration", () => {
  const guides = view("guides");
  const ai = view("ai");
  for (const id of MOVED_IDS) {
    assert.ok(guides.includes(`id="${id}"`), `${id} is on Guides`);
    assert.ok(!ai.includes(`id="${id}"`), `${id} is gone from AI Configuration`);
  }
  for (const title of ["Calibration health", "Current routing strategy"]) {
    assert.ok(!html.includes(title), `${title} was consolidated into the status panel`);
  }
});

test("Dynamic Routing is four short panels, not six", () => {
  const panel = view("guides");
  const panels = panel.match(/<article class="panel/g) || [];
  assert.equal(panels.length, 4);
  for (const title of ["Routing data &amp; strategy", "How Dynamic Routing works", "Model routing table", "Example routing decisions"]) {
    assert.ok(panel.includes(title), title);
  }
  // The single-item FAQ list is gone.
  assert.ok(!html.includes("model-calibration-modes"));
});

test("AI Configuration keeps the refresh settings and points to Guides", () => {
  const ai = view("ai");
  assert.match(ai, /REFRESH SETTINGS/);
  assert.match(ai, /id="model-data-key"/);
  assert.match(ai, /data-view-jump="guides"/);
});

test("opening Guides refreshes the routing data, and its tabs are keyboard reachable", () => {
  assert.match(app, /if \(view === "guides"\) void refreshModelCalibration/);
  const show = app.slice(app.indexOf("function showGuidesTab"), app.indexOf("// The Repository view is a tablist"));
  assert.match(show, /aria-selected/);
  assert.match(show, /button\.tabIndex = active \? 0 : -1/);
  assert.match(app, /showGuidesTab\(guidesTabs\[index\]\.dataset\.guidesTab, \{ focus: true \}\)/);
});

test("the Guides page adds no inline styles", () => {
  assert.ok(!/style="/.test(view("guides")));
});
