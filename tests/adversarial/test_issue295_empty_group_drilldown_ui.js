"use strict";

/**
 * Issue #295 — empty aggregate group values are real drill-downs.
 *
 * Grouping by prompt grade / model / effort / agent produces a row whose
 * `group` is "" when that field was never recorded. The Usage & cost UI
 * labels those rows ("Not graded", "Model not recorded", …) and the query
 * API treats `group_value=""` as "this bucket" (distinct from no selection).
 *
 * A truthy check on `state.usage.groupValue` collapses that bucket back
 * into "every recorded invocation", hides the Clear selection control, and
 * reports the wrong empty state — so the labelled row cannot actually be
 * opened. Spec: selecting an aggregate row drills through to its
 * invocations; all large result sets stay paginated; tables and expandable
 * details stay keyboard-reachable.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const html = fs.readFileSync(path.join(root, "ui", "index.html"), "utf8");
const appJs = fs.readFileSync(path.join(root, "ui", "app.js"), "utf8");
const {
  groupRowLabel,
  defaultFilters,
  hasActiveFilters,
  normalizeFilters,
  FILTER_KEYS,
} = require(path.join(root, "ui", "usage-cost.js"));

function sliceFn(name, length = 2500) {
  const start = appJs.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing ${name}`);
  return appJs.slice(start, start + length);
}

test("empty group values get an explicit label so the aggregate row is clickable", () => {
  assert.equal(groupRowLabel({ group: "" }, "grade"), "Not graded");
  assert.equal(groupRowLabel({ group: "" }, "model"), "Model not recorded");
  assert.equal(groupRowLabel({ group: "" }, "effort"), "Effort not recorded");
  assert.equal(groupRowLabel({ group: "" }, "agent"), "Agent not recorded");
  assert.equal(groupRowLabel({ group: "" }, "prompt"), "Prompt type not recorded");
  assert.equal(groupRowLabel({ group: "" }, "provider"), "Provider not recorded");
  assert.equal(groupRowLabel({ group: "" }, "repository"), "Repository not recorded");
  assert.equal(groupRowLabel({ group: "" }, "day"), "Not recorded");
});

test("the drill-down button writes the raw group value, including empty string", () => {
  const block = sliceFn("renderUsageGroups", 4000);
  assert.match(
    block,
    /dataset\.usageGroup = row\.group/,
    "drill-down must send the aggregate's group value verbatim so an empty "
      + "bucket (Not graded) is distinguishable from no selection",
  );
  assert.match(block, /type = "button"/);
  assert.match(block, /dataset\.usageGroup/);
});

test("renderUsageDetail heading treats an empty groupValue as a selected bucket", () => {
  const block = sliceFn("renderUsageDetail", 1800);
  // Truthiness on groupValue (`state.usage.groupValue ? …`) makes "" look
  // like "no drill-down": the heading reverts to "Every recorded invocation".
  assert.doesNotMatch(
    block,
    /heading\.textContent = state\.usage\.groupValue\s*\?/,
    "empty-string groupValue is a real selection (Not graded / Model not "
      + "recorded). A truthy check on it reports 'Every recorded invocation'",
  );
  assert.match(
    block,
    /groupRowLabel|groupValue !== null|groupValue != null|selectedGroup/,
    "the invocation heading must name the selected bucket, including the "
      + "empty one, rather than falling back to the unfiltered caption",
  );
});

test("Clear selection stays available while the empty group is selected", () => {
  const block = sliceFn("renderUsageDetail", 1800);
  assert.doesNotMatch(
    block,
    /clear\.classList\.toggle\("hidden", !state\.usage\.groupValue\)/,
    "Clear selection must stay available while the empty group is selected",
  );
});

test("empty-state copy counts an empty groupValue as a filter/selection", () => {
  const block = sliceFn("renderUsageDetail", 1800);
  assert.doesNotMatch(
    block,
    /Boolean\(state\.usage\.groupValue\)/,
    "empty-state 'filtered' must count an empty groupValue as a selection",
  );
});

test("the usage query actually sends empty string group values to the backend", () => {
  const block = sliceFn("refreshUsageReport", 1600);
  assert.match(block, /groupValue:\s*state\.usage\.groupValue/);
  // `groupValue: state.usage.groupValue || null` would turn "" into null
  // and the backend would return every invocation again.
  assert.doesNotMatch(
    block,
    /groupValue:\s*state\.usage\.groupValue\s*\|\|/,
    "must not coerce empty groupValue to null before the query; that is "
      + "how Not graded becomes 'every invocation'",
  );
});

test("clicking a selected empty group still toggles drill-down off", () => {
  const start = appJs.indexOf('closest("[data-usage-group]")');
  assert.notEqual(start, -1);
  const block = appJs.slice(start, start + 450);
  assert.match(block, /state\.usage\.groupValue === value \? null : value/);
});

test("Clear selection exists and is a keyboard-reachable button", () => {
  const match = /<button[^>]*id="usage-clear-drilldown"[^>]*>/.exec(html);
  assert.ok(match, "missing usage-clear-drilldown");
  assert.match(match[0], /type="button"/);
  assert.match(html, /id="usage-detail-heading"/);
  assert.match(html, /id="usage-groups"/);
  assert.match(html, /id="usage-detail"/);
});

test("openUsageWithFilters resets drill-down without dropping FILTER_KEYS", () => {
  const block = sliceFn("openUsageWithFilters", 900);
  assert.match(block, /state\.usage\.groupValue = null/);
  assert.match(block, /normalizeFilters\(filters\)/);
  assert.match(block, /showFeedbackTab\("usage"\)/);
  const scoped = normalizeFilters({ issueNumber: 295, grade: "B+", extra: "drop" });
  FILTER_KEYS.forEach((key) => assert.ok(key in scoped));
  assert.equal(scoped.issueNumber, "295");
  assert.equal(scoped.grade, "B+");
  assert.equal("extra" in scoped, false);
  assert.equal(hasActiveFilters(defaultFilters()), false);
});
