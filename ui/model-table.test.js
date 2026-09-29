"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const api = require("./model-calibration-ui.js");
const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const app = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");

const model = (n, extra = {}) => ({
  key: `anthropic/m${String(n).padStart(2, "0")}`,
  provider: n % 2 ? "anthropic" : "openai",
  model: `m${String(n).padStart(2, "0")}`,
  status: n % 5 === 0 ? "DISCOVERED" : "ACTIVE",
  coding_score: n,
  ...extra,
});
const models = (count) => Array.from({ length: count }, (_, i) => model(i + 1));

test("search matches every term against provider, model, status and key", () => {
  const list = models(20);
  assert.equal(api.filterModels(list, "").length, 20);
  assert.equal(api.filterModels(list, "  ").length, 20);
  assert.deepEqual(api.filterModels(list, "m07").map((m) => m.model), ["m07"]);
  assert.equal(api.filterModels(list, "DISCOVERED").length, 4);
  assert.equal(api.filterModels(list, "openai active").every((m) => m.provider === "openai" && m.status === "ACTIVE"), true);
  assert.equal(api.filterModels(list, "no-such-model").length, 0);
  assert.deepEqual(api.filterModels(null, "x"), []);
});

test("paging defaults to ten per page and clamps the page into range", () => {
  assert.equal(api.DEFAULT_MODEL_PAGE_SIZE, 10);
  const first = api.paginate(models(34), 0, undefined);
  assert.equal(first.items.length, 10);
  assert.deepEqual([first.first, first.last, first.total, first.pages], [1, 10, 34, 4]);
  const last = api.paginate(models(34), 3, 10);
  assert.deepEqual([last.items.length, last.first, last.last], [4, 31, 34]);
  // A page past the end (a search just shrank the list) lands on the last page.
  assert.equal(api.paginate(models(34), 99, 10).page, 3);
  assert.equal(api.paginate(models(34), -4, 10).page, 0);
  const none = api.paginate([], 0, 10);
  assert.deepEqual([none.items.length, none.pages, none.first, none.last], [0, 1, 0, 0]);
  // Only offered page sizes are honoured.
  assert.equal(api.normalizePageSize("25"), 25);
  assert.equal(api.normalizePageSize("7"), 10);
});

test("every sortable column has a readable label and a direction in words", () => {
  for (const column of api.SORTABLE_COLUMNS) {
    assert.ok(api.SORT_LABELS[column], `${column} has a label`);
    assert.doesNotMatch(api.sortLabel(column), /_/);
  }
  assert.equal(api.sortSummary({ column: "coding_score", direction: "desc" }), "Sorted by Coding score, highest first.");
  assert.equal(api.sortSummary({ column: "provider", direction: "asc" }), "Sorted by Provider, A to Z.");
  assert.equal(api.sortSummary({ column: "last_updated", direction: "desc" }), "Sorted by Last updated, newest first.");
  assert.equal(api.sortSummary({ column: "input_cost", direction: "asc" }), "Sorted by Input cost, lowest first.");
});

test("the table has a search box, a per-page picker defaulting to 10, and a pager", () => {
  assert.match(html, /<input id="model-routing-search" type="search"/);
  assert.match(html, /<option value="10" selected>10<\/option>/);
  assert.match(html, /id="model-routing-pager"[^>]*aria-label="Model routing table pages"/);
  assert.match(html, /id="model-routing-sort-summary"[^>]*aria-live="polite"/);
  for (const id of ["model-routing-prev", "model-routing-next", "model-routing-page-label"]) {
    assert.ok(html.includes(`id="${id}"`), id);
  }
  for (const wiring of ["model-routing-search", "model-routing-page-size", "model-routing-prev", "model-routing-next"]) {
    assert.ok(app.includes(`byId("${wiring}")`), `${wiring} is wired`);
  }
  assert.match(app, /state\.modelCalibration\.page = 0/);
});

// ----- The real renderer, against a tiny DOM ---------------------------

class Node_ {
  constructor(tag = "div") {
    this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.classes = new Set(); this._text = "";
    this.disabled = false; this.title = "";
    this.classList = {
      toggle: (name, on) => { const enable = on === undefined ? !this.classes.has(name) : on; enable ? this.classes.add(name) : this.classes.delete(name); },
      add: (n) => this.classes.add(n), remove: (n) => this.classes.delete(n), contains: (n) => this.classes.has(n),
    };
  }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => (typeof c === "string" ? c : c.textContent)).join(""); }
  setAttribute(n, v) { this.attributes[n] = String(v); }
  append(...c) { this.children.push(...c); }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren(...c) { this._text = ""; this.children = c; }
}

function renderer(modelList, viewOverrides = {}) {
  const start = app.indexOf("  function renderModelRoutingTable()");
  const end = app.indexOf("  function renderModelCalibrationExamples()", start);
  assert.ok(start > 0 && end > start);
  const nodes = new Map();
  const byId = (id) => { if (!nodes.has(id)) nodes.set(id, new Node_()); return nodes.get(id); };
  const rows = [];
  const state = {
    modelCalibration: { sort: { column: "provider", direction: "asc" }, search: "", page: 0, pageSize: 10, ...viewOverrides },
  };
  const status = { active_calibration: { version: "v1", models: modelList }, has_newer_proposed: false };
  const document = { createElement: (t) => new Node_(t), createTextNode: (t) => Object.assign(new Node_("#text"), { _text: t }) };
  const build = new Function(
    "state", "window", "document", "byId", "modelCalibrationStatus", "buildModelRow",
    `${app.slice(start, end)}\nreturn renderModelRoutingTable;`,
  );
  const render = build(state, { SwarmModelCalibration: api }, document, byId, () => status,
    (m) => { rows.push(m.model); return Object.assign(new Node_(), { _text: m.model }); });
  return { render, byId, state, rows };
}

test("the first render shows ten rows, a page count and a plain-language sort summary", () => {
  const { render, byId, rows } = renderer(models(34));
  render();
  assert.equal(rows.length, 10);
  assert.equal(byId("model-routing-page-label").textContent, "Page 1 of 4");
  assert.equal(byId("model-routing-pager").classes.has("hidden"), false);
  assert.equal(byId("model-routing-prev").disabled, true);
  assert.equal(byId("model-routing-next").disabled, false);
  assert.match(byId("model-routing-sort-summary").textContent, /Sorted by Provider, A to Z\. Showing 1–10 of 34\./);
  assert.equal(byId("model-calibration-model-count").textContent, "34 models");
});

test("sort headers are labelled buttons that always show a direction", () => {
  const { render, byId } = renderer(models(12), { sort: { column: "coding_score", direction: "desc" } });
  render();
  const buttons = byId("model-routing-table-head").children;
  assert.equal(buttons.length, api.SORTABLE_COLUMNS.length);
  const active = buttons.find((b) => b.dataset.sortColumn === "coding_score");
  const idle = buttons.find((b) => b.dataset.sortColumn === "speed");
  assert.equal(active.classes.has("active"), true);
  assert.equal(active.attributes["aria-pressed"], "true");
  assert.equal(active.textContent, "Coding score▼");
  assert.match(active.attributes["aria-label"], /Coding score, sorted highest first\. Select to reverse\./);
  assert.equal(idle.classes.has("active"), false);
  assert.equal(idle.attributes["aria-pressed"], "false");
  assert.equal(idle.textContent, "Speed↕");
});

test("searching narrows the table and resets the count and pager", () => {
  const { render, byId, rows, state } = renderer(models(34), { search: "m0", page: 2 });
  render();
  assert.equal(rows.length, 9, "m01–m09 match; the out-of-range page was clamped back");
  assert.equal(state.modelCalibration.page, 0);
  assert.equal(byId("model-calibration-model-count").textContent, "9 of 34 models");
  assert.equal(byId("model-routing-pager").classes.has("hidden"), true, "one page needs no pager");
});

test("a search with no matches says so instead of showing an empty table", () => {
  const { render, byId, rows } = renderer(models(12), { search: "zzz" });
  render();
  assert.equal(rows.length, 0);
  assert.match(byId("model-routing-table").textContent, /No models match “zzz”\./);
});

test("a larger page size shows more rows at once", () => {
  const { render, rows, byId } = renderer(models(34), { pageSize: 25 });
  render();
  assert.equal(rows.length, 25);
  assert.equal(byId("model-routing-page-label").textContent, "Page 1 of 2");
});
