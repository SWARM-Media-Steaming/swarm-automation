"use strict";

/**
 * Issue #295 — Feedback Usage & cost UI, independently of the helper unit tests.
 *
 * Spec-derived checks of markup, wiring, and pure formatters:
 * - Feedback has a fourth Usage & cost tab that reuses the global repository filter.
 * - Monetary values are labelled Estimated cost; quota remaining stays on Overview.
 * - Missing values render as unavailable, never as zero.
 * - Filters, grouping, drill-down, pagination, and the three empty states exist.
 * - Prompt Grades, Router Activity, and Execution History link into the usage view.
 * - Invocation History links must use the execution identifier.
 * - Usage filters persist across Feedback tabs for the session.
 * - The desktop never totals the usage table itself.
 * - Tab, filters, tables, and expandable details are keyboard-accessible.
 * - No budgets, alerts, or GitHub-comment backfill.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const html = fs.readFileSync(path.join(root, "ui", "index.html"), "utf8");
const appJs = fs.readFileSync(path.join(root, "ui", "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "ui", "style.css"), "utf8");
const usageJs = fs.readFileSync(path.join(root, "ui", "usage-cost.js"), "utf8");
const {
  UNAVAILABLE,
  USAGE_GROUPS,
  GROUP_COLUMNS,
  INVOCATION_COLUMNS,
  COVERAGE,
  FILTER_KEYS,
  formatTokens,
  formatCost,
  formatCount,
  pricedLabel,
  emptyStateMessage,
  defaultFilters,
  hasActiveFilters,
  normalizeFilters,
  filtersForIssue,
  filtersForRouterSelection,
  filtersForExecution,
  coverageLabel,
} = require(path.join(root, "ui", "usage-cost.js"));
const { FEEDBACK_TABS } = require(path.join(root, "ui", "prompt-grades.js"));

function tagWithId(markup, id) {
  const match = new RegExp(`<[a-z][^>]*\\bid="${id}"[^>]*>`, "i").exec(markup);
  assert.ok(match, `no element with id="${id}"`);
  return match[0];
}

function usagePanel() {
  const start = html.indexOf('id="feedback-panel-usage"');
  assert.notEqual(start, -1, "Usage & cost panel is missing");
  return html.slice(start, html.indexOf("</section>", start));
}

test("Feedback has four tabs including Usage & cost, sharing the repository chips", () => {
  assert.deepEqual(FEEDBACK_TABS, ["grades", "routing", "jev", "history", "usage"]);
  const tabs = [...html.matchAll(/data-feedback-tab="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(tabs, ["grades", "routing", "jev", "history", "usage"]);
  const tab = tagWithId(html, "feedback-tab-usage");
  assert.match(tab, /role="tab"/);
  assert.match(tab, /aria-controls="feedback-panel-usage"/);
  assert.match(html, /id="feedback-tab-usage"[^>]*>Usage &amp; cost/);
  assert.match(html, /id="feedback-repo-chips"/);
  assert.match(html, /aria-label="Filter Feedback by repository"/);
  assert.doesNotMatch(usagePanel(), /data-usage-filter="repositories"/);
});

test("summary cards, filters, grouping, and both pagers are in the panel", () => {
  const panel = usagePanel();
  assert.match(panel, /id="usage-summary"/);
  assert.match(panel, /id="usage-coverage"/);
  assert.match(panel, /aria-label="Reporting coverage"/);
  [
    "usage-start-date",
    "usage-end-date",
    "usage-issue",
    "usage-grade",
    "usage-provider",
    "usage-model",
    "usage-effort",
    "usage-agent-type",
    "usage-prompt-type",
    "usage-outcome",
    "usage-coverage-filter",
    "usage-search",
    "usage-group-by",
  ].forEach((id) => {
    assert.match(panel, new RegExp(`id="${id}"`));
  });
  const bound = [...panel.matchAll(/data-usage-filter="([^"]+)"/g)].map((m) => m[1]);
  [
    "startDate", "endDate", "issueNumber", "grade", "provider", "model", "effort",
    "agentType", "promptType", "outcome", "coverage", "search",
  ].forEach((key) => {
    assert.ok(bound.includes(key), `no control bound to ${key}`);
  });
  assert.match(panel, /id="usage-groups-pager"/);
  assert.match(panel, /id="usage-detail-pager"/);
  assert.match(panel, /aria-label="Usage aggregate pages"/);
  assert.match(panel, /aria-label="Usage invocation pages"/);
  assert.match(panel, /id="usage-clear-filters"/);
});

test("every grouping dimension and required table column is offered", () => {
  assert.deepEqual(
    USAGE_GROUPS.map((entry) => entry.value),
    ["issue", "model", "provider", "grade", "effort", "agent", "prompt", "repository", "day", "week", "month"],
  );
  assert.deepEqual(
    GROUP_COLUMNS.map((column) => column.label),
    ["Group", "Issues", "Invocations", "Input", "Cached", "Reasoning", "Output", "Total", "Estimated cost", "Coverage"],
  );
  assert.deepEqual(
    INVOCATION_COLUMNS.map((column) => column.label),
    [
      "Agent", "Provider / Model", "Prompt", "Effort", "Attempt", "Input", "Cached",
      "Reasoning", "Output", "Total", "Estimated cost", "Duration", "Result",
    ],
  );
  assert.equal(COVERAGE.length, 5);
  ["Complete", "Tokens only", "Partial", "Unreported", "Failed"].forEach((label) => {
    assert.ok(COVERAGE.some((entry) => entry.label === label), `missing coverage ${label}`);
  });
});

test("every monetary label is Estimated cost and quota remaining is not on this tab", () => {
  const panel = usagePanel();
  assert.match(panel, /<strong>Estimated cost<\/strong>/);
  assert.match(appJs, /"Estimated cost"/);
  assert.equal(GROUP_COLUMNS.filter((column) => column.cost).every((column) => column.label === "Estimated cost"), true);
  assert.equal(INVOCATION_COLUMNS.filter((column) => column.cost).every((column) => column.label === "Estimated cost"), true);
  assert.match(panel, /Provider quota remaining stays on the Overview page/);
  assert.doesNotMatch(panel, /\bbudget\b/i);
  assert.doesNotMatch(panel, /\balert\b/i);
  assert.doesNotMatch(panel, /\benforc/i);
  assert.doesNotMatch(panel, /id="usage-quota"/);
});

test("missing values render as unavailable; zero stays zero; priced coverage is visible", () => {
  assert.equal(formatTokens(null), UNAVAILABLE);
  assert.equal(formatTokens(undefined), UNAVAILABLE);
  assert.equal(formatCost(null), UNAVAILABLE);
  assert.equal(formatCost(undefined), UNAVAILABLE);
  assert.equal(formatTokens(0), "0");
  assert.equal(formatCost(0), "$0.00");
  assert.notEqual(formatCost(0), formatCost(null));
  assert.equal(pricedLabel(3, 11), `${formatCount(3)} of ${formatCount(11)} priced`);
  assert.match(appJs, /pricedLabel/);
  assert.match(appJs, /formatCost\(summary && summary\.estimatedCost/);
});

test("empty states distinguish no telemetry, usage unavailable, and no matches", () => {
  assert.match(
    emptyStateMessage({ hasAnyUsage: false, hasAnyActivity: false, filtered: false }),
    /Store AI execution history/,
  );
  const unavailable = emptyStateMessage({ hasAnyUsage: false, hasAnyActivity: true, filtered: false });
  assert.match(unavailable, /Usage unavailable/);
  assert.match(unavailable, /after #280/);
  assert.match(unavailable, /never scraped/);
  assert.equal(
    emptyStateMessage({ hasAnyUsage: true, hasAnyActivity: true, filtered: true }),
    "No usage matches these filters.",
  );
});

test("cross-links from the other Feedback reports produce the usage filter shape", () => {
  const issue = filtersForIssue(295, "B+");
  assert.equal(issue.issueNumber, "295");
  assert.equal(issue.grade, "B+");
  const router = filtersForRouterSelection("Claude", "claude-haiku-4-5");
  assert.equal(router.provider, "Claude");
  assert.equal(router.model, "claude-haiku-4-5");
  assert.equal(router.agentType, "router");
  const execution = filtersForExecution("exec-1");
  assert.equal(execution.executionId, "exec-1");
  assert.match(appJs, /data-grade-usage-issue/);
  assert.match(appJs, /View usage ↗/);
  assert.match(appJs, /filtersForIssue/);
  assert.match(appJs, /filtersForRouterSelection/);
  assert.match(appJs, /filtersForExecution/);
  assert.match(appJs, /data-execution-usage/);
  assert.match(appJs, /data-router-usage/);
});

test("an invocation History link uses the execution identifier instead of an unfiltered tab switch", () => {
  assert.match(appJs, /dataset\.usageExecution/);
  const start = appJs.indexOf('closest("[data-usage-execution]")');
  assert.notEqual(start, -1, "Usage & cost must wire a History control for each invocation");
  const block = appJs.slice(start, start + 650);
  assert.match(
    block,
    /dataset\.usageExecution/,
    "clicking History must read the execution id; switching to Execution history while clearing search and ignoring the id does not take the user to that execution",
  );
});

test("usage filters persist when moving among Feedback tabs during the session", () => {
  const start = appJs.indexOf("function showFeedbackTab(");
  assert.notEqual(start, -1);
  const end = appJs.indexOf("function showRepositoryTab(", start);
  const body = appJs.slice(start, end);
  assert.doesNotMatch(
    body,
    /state\.usage\.filters\s*=/,
    "showFeedbackTab must not reset usage filters; they survive tab changes for the session",
  );
  assert.match(body, /state\.feedbackTab === "usage"/);
  const clear = appJs.indexOf('event.target.id === "usage-clear-filters"');
  assert.notEqual(clear, -1);
  assert.match(appJs.slice(clear, clear + 400), /defaultFilters\(\)/);
});

test("the desktop queries a paginated backend report and never totals the usage table", () => {
  assert.match(appJs, /invoke\("get_usage_report_background"/);
  assert.doesNotMatch(appJs, /invoke\("get_token_usage/);
  assert.doesNotMatch(usageJs, /reduce\(/);
  assert.match(usageJs, /never pull the usage table into the desktop to add it up/);
  assert.match(appJs, /this code formats, it never totals/);
  assert.doesNotMatch(appJs, /token_usage_totals/);
});

test("tables, tabs, filters, and expandable details are named for assistive technology and keyboard use", () => {
  const tab = tagWithId(html, "feedback-tab-usage");
  assert.match(tab, /role="tab"/);
  assert.match(html, /role="tablist"/);
  assert.match(html, /aria-label="Feedback reports"/);
  assert.match(appJs, /ArrowRight: 1, ArrowLeft: -1, Home: "first", End: "last"/);
  assert.match(appJs, /showFeedbackTab\(feedbackTabs\[index]\.dataset\.feedbackTab, \{ focus: true \}\)/);
  assert.match(appJs, /caption\.className = "sr-only"/);
  assert.match(appJs, /cell\.scope = "col"/);
  assert.match(appJs, /type = "button"/);
  assert.match(appJs, /dataset\.usageSort/);
  assert.match(appJs, /dataset\.usageGroup/);
  assert.match(appJs, /createElement\("details"\)/);
  assert.match(appJs, /Usage records \(/);
  const panel = usagePanel();
  [...panel.matchAll(/<(select|input)[^>]*data-usage-filter="([^"]+)"[^>]*>/g)].forEach((match) => {
    const tag = match[0];
    const named = /aria-label=/.test(tag) || /<\/label>/.test(panel.slice(panel.indexOf(tag) - 80, panel.indexOf(tag) + tag.length));
    const wrapped = panel.slice(Math.max(0, panel.indexOf(tag) - 120), panel.indexOf(tag)).includes("<label>");
    assert.ok(named || wrapped, `${match[2]} filter is missing an accessible name`);
  });
  assert.match(css, /\.sr-only/);
});

test("help copy explains estimated cost, coverage, and the #280 telemetry window", () => {
  assert.match(appJs, /"usage-cost"\s*:/);
  assert.match(appJs, /Estimated cost/);
  assert.match(appJs, /Tokens only/);
  assert.match(appJs, /Usage unavailable/);
  assert.match(html, /data-help="usage-cost"/);
});

test("no GitHub-comment backfill path exists in the usage UI", () => {
  const panel = usagePanel();
  assert.doesNotMatch(panel, /scrape/i);
  assert.doesNotMatch(appJs, /AI Usage/);
  assert.match(emptyStateMessage({ hasAnyUsage: false, hasAnyActivity: true }), /never scraped/);
});

test("session filter helpers treat empty outcome as inactive and keep unknown keys out", () => {
  const defaults = defaultFilters();
  assert.equal(defaults.outcome, "all");
  assert.equal(hasActiveFilters(defaults), false);
  assert.equal(hasActiveFilters({ ...defaults, model: "claude-sonnet-5" }), true);
  const normalized = normalizeFilters({ outcome: "maybe", extra: "drop", issueNumber: 295 });
  assert.equal(normalized.outcome, "all");
  assert.equal(normalized.issueNumber, "295");
  assert.equal("extra" in normalized, false);
  assert.equal(coverageLabel("tokens_only"), "Tokens only");
  FILTER_KEYS.forEach((key) => assert.ok(key in defaults));
});
