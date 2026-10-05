"use strict";

/**
 * Issue #381 — the cache columns of the Usage & cost tables, exercised at runtime.
 *
 * The other UI suites check source text and pure formatters. This one runs the
 * real `renderUsageGroups` and `buildUsageInvocationTable` from ui/app.js
 * against a minimal DOM and asserts what the user would actually read:
 *
 * - every table row has exactly one cell per header (the cache columns were
 *   added to the column lists and to the hand-built row renderers separately,
 *   so a drift shifts every value under the wrong heading);
 * - the cell under each cache heading carries that heading's value;
 * - a statistic the provider never reported reads as unavailable, while a
 *   genuinely measured zero (0.0%, $0.00, "No") stays zero — and a negative net
 *   saving keeps its sign.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const appSource = fs.readFileSync(path.join(root, "ui", "app.js"), "utf8");
const api = require(path.join(root, "ui", "usage-cost.js"));

class FakeNode {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.className = "";
    this.dataset = {};
    this.attributes = {};
    this.title = "";
    this.type = "";
    this._text = "";
    this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
  }

  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  append(...nodes) {
    nodes.forEach((node) => this.children.push(typeof node === "string" ? textNode(node) : node));
  }

  replaceChildren(...nodes) {
    this.children = nodes;
    this._text = "";
  }

  setAttribute(name, value) {
    this.attributes[name] = value;
  }
}

function textNode(value) {
  const node = new FakeNode("#text");
  node._text = String(value);
  return node;
}

const fakeDocument = {
  createElement: (tag) => new FakeNode(tag),
  createTextNode: textNode,
};

function extractFunction(name) {
  const start = appSource.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `ui/app.js has no function ${name}`);
  let index = appSource.indexOf("(", start);
  let depth = 0;
  for (; index < appSource.length; index += 1) {
    if (appSource[index] === "(") depth += 1;
    if (appSource[index] === ")" && (depth -= 1) === 0) break;
  }
  const open = appSource.indexOf("{", index);
  depth = 0;
  for (let at = open; at < appSource.length; at += 1) {
    if (appSource[at] === "{") depth += 1;
    if (appSource[at] === "}" && (depth -= 1) === 0) return appSource.slice(start, at + 1);
  }
  throw new Error(`unbalanced braces in ${name}`);
}

const externalLink = (label) => Object.assign(new FakeNode("a"), { _text: label });

function buildInvocationTable(rows, options) {
  const factory = new Function(
    "document", "usageApi", "externalLink",
    `${extractFunction("buildUsageInvocationTable")}\nreturn buildUsageInvocationTable;`,
  );
  return factory(fakeDocument, () => api, externalLink)(rows, options);
}

function renderGroupTable(rows, groupBy = "model") {
  const box = new FakeNode("div");
  const state = { usage: { groupBy, sort: "cost", direction: "desc", groupValue: null } };
  const factory = new Function(
    "document", "byId", "usageApi", "usageReportView", "usageFilters", "usageCurrency",
    "renderUsagePager", "externalLink", "state",
    `${extractFunction("renderUsageGroups")}\nreturn renderUsageGroups;`,
  );
  factory(
    fakeDocument,
    () => box,
    () => api,
    () => ({ groups: { rows, total: rows.length, limit: 25, offset: 0 }, hasAnyUsage: true, hasAnyActivity: true }),
    () => api.defaultFilters(),
    () => "USD",
    () => {},
    externalLink,
    state,
  )();
  return box.children.find((child) => child.tag === "table");
}

function headers(table) {
  const head = table.children.find((child) => child.tag === "thead");
  return head.children[0].children.map((cell) => cell.textContent.replace(/\s*[▲▼]$/, ""));
}

function bodyRows(table) {
  const body = table.children.find((child) => child.tag === "tbody");
  return body.children.map((row) => row.children.map((cell) => cell.textContent));
}

function cellUnder(table, row, label) {
  const at = headers(table).indexOf(label);
  assert.notEqual(at, -1, `no column headed "${label}" in [${headers(table).join(" | ")}]`);
  return bodyRows(table)[row][at];
}

const LEGACY_INVOCATION = {
  agentType: "primary", provider: "Claude", model: "claude-sonnet-5", promptType: "initial",
  reasoningEffort: "high", attemptNumber: 1, inputTokens: 100, cachedInputTokens: null,
  reasoningTokens: null, outputTokens: 50, totalTokens: 150, estimatedCost: 0.01, currency: "USD",
  durationMs: 1500, coverage: "complete",
  cacheReadTokens: null, cacheWriteTokens: null, reportedCost: null, cacheSavingsEstimate: null,
  sessionId: "", sessionReused: null,
};

const FULL_INVOCATION = {
  ...LEGACY_INVOCATION,
  inputTokens: 1111, cacheReadTokens: 2222, cacheWriteTokens: 3333, cachedInputTokens: 4444,
  reasoningTokens: 5555, outputTokens: 6666, totalTokens: 7777, estimatedCost: 1.25,
  reportedCost: 2.5, cacheSavingsEstimate: 0.75, sessionId: "11111111-2222-4333-8444-555555555555",
  sessionReused: 1,
};

const ZERO_INVOCATION = {
  ...LEGACY_INVOCATION, cacheReadTokens: 0, cacheWriteTokens: 0, reportedCost: 0,
  cacheSavingsEstimate: 0, sessionReused: 0, sessionId: "22222222-2222-4333-8444-555555555555",
};

test("every invocation row has one cell per header, with and without the issue column", () => {
  for (const showIssue of [false, true]) {
    const table = buildInvocationTable([LEGACY_INVOCATION, FULL_INVOCATION, ZERO_INVOCATION], { showIssue });
    const width = headers(table).length;
    assert.equal(width, api.INVOCATION_COLUMNS.length + (showIssue ? 1 : 0));
    bodyRows(table).forEach((cells, index) => {
      assert.equal(cells.length, width, `row ${index} (showIssue=${showIssue}) has ${cells.length} cells for ${width} headers`);
    });
  }
});

test("each invocation cache cell carries the value its heading names", () => {
  const table = buildInvocationTable([FULL_INVOCATION]);
  const number = (value) => Number(value).toLocaleString();
  assert.equal(cellUnder(table, 0, "Input"), number(1111));
  assert.equal(cellUnder(table, 0, "Cache read"), number(2222));
  assert.equal(cellUnder(table, 0, "Cache write"), number(3333));
  assert.equal(cellUnder(table, 0, "Cached"), number(4444));
  assert.equal(cellUnder(table, 0, "Total"), number(7777));
  assert.equal(cellUnder(table, 0, "Estimated cost"), "$1.25");
  assert.equal(cellUnder(table, 0, "Reported cost"), "$2.50");
  assert.equal(cellUnder(table, 0, "API-equivalent cache savings"), "$0.75");
  assert.equal(cellUnder(table, 0, "Session"), FULL_INVOCATION.sessionId);
  assert.equal(cellUnder(table, 0, "Session reused"), "Yes");
});

test("invocations: unreported cache statistics are unavailable and measured zeros stay zero", () => {
  const table = buildInvocationTable([LEGACY_INVOCATION, ZERO_INVOCATION]);
  const labels = ["Cache read", "Cache write", "Reported cost", "API-equivalent cache savings", "Session", "Session reused"];
  labels.forEach((label) => {
    assert.equal(cellUnder(table, 0, label), api.UNAVAILABLE, `legacy row: ${label} must read as unavailable`);
  });
  assert.equal(cellUnder(table, 1, "Cache read"), "0");
  assert.equal(cellUnder(table, 1, "Cache write"), "0");
  assert.equal(cellUnder(table, 1, "Reported cost"), "$0.00");
  assert.equal(cellUnder(table, 1, "API-equivalent cache savings"), "$0.00");
  assert.equal(cellUnder(table, 1, "Session reused"), "No");
});

test("invocations: negative net savings keep their sign and a sub-cent amount is not shown as zero", () => {
  const table = buildInvocationTable([
    { ...FULL_INVOCATION, cacheSavingsEstimate: -1.5, reportedCost: 0.0042 },
    { ...FULL_INVOCATION, cacheSavingsEstimate: 0.0031 },
  ]);
  assert.equal(cellUnder(table, 0, "API-equivalent cache savings"), "-$1.50");
  assert.equal(cellUnder(table, 0, "Reported cost"), "$0.0042");
  assert.equal(cellUnder(table, 1, "API-equivalent cache savings"), "$0.0031");
});

const LEGACY_GROUP = {
  group: "claude-sonnet-5", issues: 1, invocations: 2, inputTokens: 10, cachedTokens: null,
  reasoningTokens: null, outputTokens: 5, totalTokens: 15, estimatedCost: 0.02, pricedInvocations: 2,
  coverage: { complete: 2 }, cacheHitEfficiency: null, sessionReused: null, cacheSavingsEstimate: null,
};

test("aggregate rows have one cell per header and the cache cells sit under their headings", () => {
  const table = renderGroupTable([
    LEGACY_GROUP,
    { ...LEGACY_GROUP, group: "measured", cacheHitEfficiency: 0.8, sessionReused: 3, cacheSavingsEstimate: 0.0042 },
    { ...LEGACY_GROUP, group: "zero", cacheHitEfficiency: 0, sessionReused: 0, cacheSavingsEstimate: 0 },
    { ...LEGACY_GROUP, group: "net-loss", cacheHitEfficiency: 0.05, sessionReused: 1, cacheSavingsEstimate: -1.5 },
  ]);
  const width = headers(table).length;
  assert.equal(width, api.GROUP_COLUMNS.length);
  bodyRows(table).forEach((cells, index) => {
    assert.equal(cells.length, width, `aggregate row ${index} has ${cells.length} cells for ${width} headers`);
  });
  // Unavailable is not zero.
  assert.equal(cellUnder(table, 0, "Cache hit efficiency"), api.UNAVAILABLE);
  assert.equal(cellUnder(table, 0, "Sessions reused"), api.UNAVAILABLE);
  assert.equal(cellUnder(table, 0, "API-equivalent cache savings"), api.UNAVAILABLE);
  // Measured values.
  assert.equal(cellUnder(table, 1, "Cache hit efficiency"), "80.0%");
  assert.equal(cellUnder(table, 1, "Sessions reused"), "3");
  assert.equal(cellUnder(table, 1, "API-equivalent cache savings"), "$0.0042");
  // A genuine zero is still a zero.
  assert.equal(cellUnder(table, 2, "Cache hit efficiency"), "0.0%");
  assert.equal(cellUnder(table, 2, "Sessions reused"), "0");
  assert.equal(cellUnder(table, 2, "API-equivalent cache savings"), "$0.00");
  // A cache-write premium can make the net estimate negative.
  assert.equal(cellUnder(table, 3, "API-equivalent cache savings"), "-$1.50");
  // The pre-existing columns did not move.
  assert.match(cellUnder(table, 0, "Estimated cost"), /^\$0\.02/);
  assert.equal(cellUnder(table, 0, "Invocations"), "2");
});

test("the Coverage badge is still the last aggregate cell", () => {
  const table = renderGroupTable([{ ...LEGACY_GROUP, coverage: { failed: 1, complete: 1 } }]);
  assert.equal(headers(table).pop(), "Coverage");
  assert.equal(bodyRows(table)[0].pop(), api.coverageLabel("failed"));
});
