"use strict";

/**
 * Issue #295 — Execution History / Prompt Grade / Router cross-links into
 * Usage & cost, independently of the helper-only UI suite.
 *
 * Spec:
 * - Execution History cards show invocation count, total tokens, estimated
 *   cost and coverage, or Usage unavailable for imported/pre-#280 runs.
 * - A card expands into its individual usage records.
 * - View usage on Prompt Grades opens Usage & cost filtered to that
 *   issue/grade.
 * - Router Activity provider/model selections open the filtered usage view.
 * - Usage execution identifiers link back to Execution History via the
 *   execution id (the history search box must accept that id).
 * - Estimated cost is always labelled as such; quota remaining stays off
 *   this tab. Missing values stay unavailable, including sub-cent vs $0.00.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const html = fs.readFileSync(path.join(root, "ui", "index.html"), "utf8");
const appJs = fs.readFileSync(path.join(root, "ui", "app.js"), "utf8");
const {
  UNAVAILABLE,
  formatCost,
  formatTokens,
  pricedLabel,
  filtersForIssue,
  filtersForRouterSelection,
  filtersForExecution,
} = require(path.join(root, "ui", "usage-cost.js"));

function extract(fnName) {
  const start = appJs.indexOf(`function ${fnName}(`);
  assert.notEqual(start, -1, `missing ${fnName}`);
  return start;
}

test("Execution History cards surface usage headlines or Usage unavailable", () => {
  const start = extract("buildExecutionRecordItem");
  const block = appJs.slice(start, start + 16000);
  assert.match(block, /\["Invocations"/);
  assert.match(block, /\["Total tokens"/);
  assert.match(block, /"Estimated cost"/);
  assert.match(block, /Usage unavailable/);
  assert.match(block, /tokenUsageSummary/);
  assert.match(block, /coverageChips/);
  assert.match(block, /pricedLabel/);
  assert.match(block, /View usage ↗/);
  assert.match(block, /dataset\.executionUsage/);
  assert.match(block, /Usage records \(/);
  assert.match(block, /createElement\("details"\)/);
  assert.match(
    block,
    /GitHub comments are never scraped/,
    "imported/pre-#280 copy must say GitHub comments are not backfilled",
  );
});

test("an execution without tokenUsageSummary is labelled unavailable rather than zero", () => {
  const start = appJs.indexOf('["Invocations"');
  const snippet = appJs.slice(start, start + 700);
  assert.match(snippet, /usage \? usageApiRef\.formatCount\(usage\.invocations\) : "Usage unavailable"/);
  assert.match(snippet, /usage \? usageApiRef\.formatTokens\(usage\.totalTokens\) : "Usage unavailable"/);
  assert.match(snippet, /usage \? usageApiRef\.formatCost\(usage\.estimatedCost, "USD"\) : "Usage unavailable"/);
  assert.doesNotMatch(snippet, /formatCount\(usage\.invocations\) : "0"/);
  assert.doesNotMatch(snippet, /formatCost\(usage\.estimatedCost.*: "\$0/);
});

test("Prompt Grades View usage carries the issue number and grade into the usage filters", () => {
  assert.match(appJs, /dataset\.gradeUsageIssue/);
  assert.match(appJs, /dataset\.gradeUsageGrade/);
  const start = appJs.indexOf('closest("[data-grade-usage-issue]")');
  assert.notEqual(start, -1);
  const block = appJs.slice(start, start + 500);
  assert.match(block, /filtersForIssue/);
  assert.match(block, /openUsageWithFilters/);
  const filters = filtersForIssue(295, "B+");
  assert.equal(filters.issueNumber, "295");
  assert.equal(filters.grade, "B+");
  assert.equal(filters.executionId, "");
});

test("Router Activity links scope usage to the router agent and selected model", () => {
  const start = appJs.indexOf('closest("[data-router-usage]")');
  assert.notEqual(start, -1);
  const block = appJs.slice(start, start + 600);
  assert.match(block, /filtersForRouterSelection/);
  assert.match(block, /dataset\.routerUsageModel/);
  const providerOnly = filtersForRouterSelection("claude", "");
  assert.equal(providerOnly.provider, "claude");
  assert.equal(providerOnly.agentType, "router");
  assert.equal(providerOnly.model, "");
  const withModel = filtersForRouterSelection("claude", "claude-haiku-4-5");
  assert.equal(withModel.model, "claude-haiku-4-5");
  assert.equal(withModel.agentType, "router");
});

test("History from an invocation searches Execution History by execution id", () => {
  const placeholder = /id="execution-history-search"[^>]*placeholder="([^"]+)"/.exec(html);
  assert.ok(placeholder, "execution history search is missing");
  assert.match(
    placeholder[1],
    /execution id/i,
    "the search box that History fills must advertise execution id as a searchable field",
  );
  const start = appJs.indexOf('closest("[data-usage-execution]")');
  const block = appJs.slice(start, start + 700);
  assert.match(block, /state\.executionHistorySearch = executionId/);
  assert.match(block, /searchInput\.value = executionId/);
  assert.match(block, /showFeedbackTab\("history"\)/);
  assert.match(block, /refreshExecutionHistory/);
  const filters = filtersForExecution("abc-def");
  assert.equal(filters.executionId, "abc-def");
});

test("sub-cent estimated cost is distinct from zero and from unavailable", () => {
  assert.equal(formatCost(null), UNAVAILABLE);
  assert.equal(formatCost(0), "$0.00");
  assert.notEqual(formatCost(0.0021), "$0.00");
  assert.notEqual(formatCost(0.0021), UNAVAILABLE);
  assert.equal(formatCost(0.0021), "$0.0021");
  assert.equal(formatTokens(null), UNAVAILABLE);
  assert.equal(formatTokens(0), "0");
  assert.equal(pricedLabel(0, 4), `${(0).toLocaleString()} of ${(4).toLocaleString()} priced`);
});

test("coverage chips are toggle buttons, not mute text", () => {
  const start = appJs.indexOf("function renderUsageCoverage");
  const block = appJs.slice(start, appJs.indexOf("function fillUsageSelect", start));
  assert.match(block, /type = "button"/);
  assert.match(block, /aria-pressed/);
  assert.match(block, /dataset\.usageCoverage/);
  assert.match(block, /chip\.help/);
});

test("the usage tab does not render provider quota remaining", () => {
  const start = html.indexOf('id="feedback-panel-usage"');
  const panel = html.slice(start, html.indexOf('id="view-debug"', start));
  assert.match(panel, /Provider quota remaining stays on the Overview page/);
  assert.doesNotMatch(panel, /id="usage-quota"/);
  const summary = appJs.slice(
    appJs.indexOf("function renderUsageSummary"),
    appJs.indexOf("function renderUsageCoverage"),
  );
  assert.doesNotMatch(summary, /quota/i);
  assert.doesNotMatch(summary, /remaining/i);
});
