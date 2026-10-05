"use strict";

/**
 * Issue #381 — cache efficiency in the Feedback "Usage & cost" view.
 *
 * Oracle (from the issue): a missing statistic is shown as unavailable, never
 * as an invented zero; a genuine zero stays zero; the net estimated savings can
 * be negative and must read as an ordinary signed amount; estimates and
 * provider-reported costs are labelled apart from realized subscription billing.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const appJs = fs.readFileSync(path.join(root, "ui", "app.js"), "utf8");
const api = require(path.join(root, "ui", "usage-cost.js"));

test("efficiency renders only genuine ratios and treats everything else as unavailable", () => {
  for (const value of [null, undefined, "", "abc", {}, NaN, Infinity, -Infinity, -0.01, 1.01, 2, true, false, []]) {
    assert.equal(api.formatEfficiency(value), api.UNAVAILABLE, `${JSON.stringify(value)} is not a measured ratio`);
  }
  assert.equal(api.formatEfficiency(0), "0.0%");
  assert.equal(api.formatEfficiency(1), "100.0%");
  assert.equal(api.formatEfficiency(0.8), "80.0%");
  assert.equal(api.formatEfficiency(0.12345), "12.3%");
});

test("negative net savings read as a signed amount, not a malformed currency string", () => {
  const text = api.formatCost(-1.5, "USD");
  assert.doesNotMatch(text, /\$-/, `got ${text}`);
  assert.match(text, /1\.50/);
  assert.equal(api.formatCost(null, "USD"), api.UNAVAILABLE);
  assert.notEqual(api.formatCost(0, "USD"), api.UNAVAILABLE, "a reported zero is not unavailable");
});

test("session reuse and savings stay unavailable when nothing was reported", () => {
  assert.equal(api.formatTokens(null), api.UNAVAILABLE);
  assert.equal(api.formatCost(undefined, "USD"), api.UNAVAILABLE);
  assert.notEqual(api.formatTokens(0), api.UNAVAILABLE);
});

test("the view separates estimated savings and reported cost from realized billing", () => {
  const lowered = appJs.toLowerCase();
  assert.match(lowered, /not realized subscription savings/);
  assert.match(lowered, /not verified subscription charges/);
  assert.doesNotMatch(lowered, /you saved|realized savings/);
});

test("aggregate and invocation tables expose the cache columns once each", () => {
  const labels = (columns) => columns.map((column) => column.label);
  for (const columns of [api.GROUP_COLUMNS, api.INVOCATION_COLUMNS]) {
    const names = labels(columns);
    assert.equal(new Set(names).size, names.length, "column labels must be unique");
    const keys = columns.map((column) => column.key);
    assert.equal(new Set(keys).size, keys.length, "column keys must be unique");
  }
  assert.ok(api.GROUP_COLUMNS.some((column) => column.key === "cacheHitEfficiency"));
  assert.ok(api.INVOCATION_COLUMNS.some((column) => column.key === "sessionId"));
  assert.ok(api.INVOCATION_COLUMNS.some((column) => column.key === "reportedCost"));
});
