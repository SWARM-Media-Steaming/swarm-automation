const test = require("node:test");
const assert = require("node:assert/strict");
const {
  UNAVAILABLE,
  USAGE_GROUPS,
  GROUP_COLUMNS,
  INVOCATION_COLUMNS,
  COVERAGE,
  coverageTone,
  coverageLabel,
  coverageChips,
  dominantCoverage,
  formatTokens,
  formatCost,
  formatCount,
  formatDurationMs,
  pricedLabel,
  nextSort,
  defaultFilters,
  hasActiveFilters,
  normalizeFilters,
  normalizeGroupBy,
  groupLabel,
  emptyStateMessage,
  groupRowLabel,
  pagerLabel,
  filtersForIssue,
  filtersForRouterSelection,
  filtersForExecution,
} = require("./usage-cost.js");

test("offers every grouping dimension the report supports", () => {
  assert.deepEqual(
    USAGE_GROUPS.map((entry) => entry.value),
    ["issue", "model", "provider", "grade", "effort", "agent", "prompt", "repository", "day", "week", "month"],
  );
  assert.equal(normalizeGroupBy("month"), "month");
  assert.equal(normalizeGroupBy("nope"), "issue");
  assert.equal(normalizeGroupBy(""), "issue");
  assert.equal(groupLabel("grade"), "Prompt grade");
  assert.equal(groupLabel("nope"), "Issue");
});

test("aggregate and invocation tables carry the columns the report specifies", () => {
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
});

test("missing values render as unavailable, never as zero", () => {
  assert.equal(formatTokens(null), UNAVAILABLE);
  assert.equal(formatTokens(undefined), UNAVAILABLE);
  assert.equal(formatTokens(""), UNAVAILABLE);
  assert.equal(formatCost(null), UNAVAILABLE);
  assert.equal(formatCost(undefined), UNAVAILABLE);
  assert.equal(formatDurationMs(null), UNAVAILABLE);
  // A genuine zero stays a zero — it is a real measurement, not a gap.
  assert.equal(formatTokens(0), "0");
  assert.equal(formatCost(0), "$0.00");
  assert.equal(formatDurationMs(0), "0 ms");
});

test("formats token counts, costs and durations for reading", () => {
  assert.equal(formatTokens(1234567), (1234567).toLocaleString());
  assert.equal(formatCount(undefined), "0");
  assert.equal(formatCost(12.5), "$12.50");
  // A sub-cent estimate keeps enough precision not to look free.
  assert.equal(formatCost(0.0021), "$0.0021");
  assert.equal(formatCost(3.5, "EUR"), "EUR 3.50");
  assert.equal(formatDurationMs(500), "500 ms");
  assert.equal(formatDurationMs(2500), "2.5s");
  assert.equal(formatDurationMs(125000), "2m 5s");
});

test("coverage statuses map onto the shared state palette", () => {
  assert.deepEqual(
    COVERAGE.map((entry) => entry.key),
    ["complete", "tokens_only", "partial", "unreported", "failed"],
  );
  assert.equal(coverageTone("complete"), "passed");
  assert.equal(coverageTone("tokens_only"), "running");
  assert.equal(coverageTone("partial"), "blocked");
  assert.equal(coverageTone("unreported"), "");
  assert.equal(coverageTone("failed"), "failed");
  assert.equal(coverageTone("nope"), "");
  assert.equal(coverageLabel("tokens_only"), "Tokens only");
  assert.equal(coverageLabel("weird"), "weird");
});

test("coverage chips drop statuses with nothing in them", () => {
  const chips = coverageChips({ complete: 3, tokens_only: 0, failed: 1 });
  assert.deepEqual(chips.map((chip) => chip.key), ["complete", "failed"]);
  assert.equal(chips[0].count, 3);
  assert.deepEqual(coverageChips(null), []);
});

test("a group's coverage cell reports its worst status, not its most common", () => {
  assert.equal(dominantCoverage({ complete: 99, failed: 1 }), "failed");
  assert.equal(dominantCoverage({ complete: 5, tokens_only: 2 }), "tokens_only");
  assert.equal(dominantCoverage({ complete: 5 }), "complete");
  assert.equal(dominantCoverage({}), "");
  assert.equal(dominantCoverage(null), "");
});

test("every estimated cost is accompanied by how much of it was priced", () => {
  assert.equal(pricedLabel(2, 5), "2 of 5 priced");
  assert.equal(pricedLabel(0, 3), "0 of 3 priced");
  assert.equal(pricedLabel(0, 0), "No invocations");
});

test("clicking a column header cycles sort the way the table reads", () => {
  assert.deepEqual(nextSort("cost", "desc", "cost"), { sort: "cost", direction: "asc" });
  assert.deepEqual(nextSort("cost", "asc", "cost"), { sort: "cost", direction: "desc" });
  // A new numeric column starts with the largest values first.
  assert.deepEqual(nextSort("cost", "desc", "total"), { sort: "total", direction: "desc" });
  // The label column starts alphabetical instead.
  assert.deepEqual(nextSort("cost", "desc", "group"), { sort: "group", direction: "asc" });
  // A column with no sort key leaves the current order alone.
  assert.deepEqual(nextSort("cost", "desc", "coverage"), { sort: "cost", direction: "desc" });
  assert.deepEqual(nextSort("cost", "desc", ""), { sort: "cost", direction: "desc" });
});

test("filter state starts neutral and knows when it is no longer neutral", () => {
  const filters = defaultFilters();
  assert.equal(filters.outcome, "all");
  assert.equal(filters.model, "");
  assert.equal(hasActiveFilters(filters), false);
  assert.equal(hasActiveFilters({ ...filters, model: "claude-sonnet-5" }), true);
  assert.equal(hasActiveFilters({ ...filters, outcome: "failure" }), true);
  // "all" is the outcome filter's neutral value, not an active choice.
  assert.equal(hasActiveFilters({ ...filters, outcome: "all" }), false);
  assert.equal(hasActiveFilters(null), false);
});

test("normalizing filters keeps known keys and repairs an unknown outcome", () => {
  const normalized = normalizeFilters({ model: "grok-4.6", outcome: "nope", nonsense: "x" });
  assert.equal(normalized.model, "grok-4.6");
  assert.equal(normalized.outcome, "all");
  assert.equal(normalized.nonsense, undefined);
  assert.equal(normalizeFilters(null).outcome, "all");
});

test("distinguishes no telemetry, unreported activity, and no matching rows", () => {
  assert.match(
    emptyStateMessage({ hasAnyUsage: false, hasAnyActivity: false }),
    /No AI activity recorded yet/,
  );
  const unavailable = emptyStateMessage({ hasAnyUsage: false, hasAnyActivity: true });
  assert.match(unavailable, /Usage unavailable/);
  assert.match(unavailable, /after #280/);
  assert.equal(
    emptyStateMessage({ hasAnyUsage: true, hasAnyActivity: true, filtered: true }),
    "No usage matches these filters.",
  );
  assert.equal(
    emptyStateMessage({ hasAnyUsage: true, hasAnyActivity: true, filtered: false }),
    "No usage recorded for this selection.",
  );
});

test("group rows label themselves per dimension and never render blank", () => {
  assert.equal(
    groupRowLabel({ group: "acme/app#7", issueTitle: "Add thing" }, "issue"),
    "acme/app#7 · Add thing",
  );
  assert.equal(groupRowLabel({ group: "acme/app#7" }, "issue"), "acme/app#7");
  assert.equal(groupRowLabel({ group: "claude-sonnet-5" }, "model"), "claude-sonnet-5");
  assert.equal(groupRowLabel({ group: "" }, "model"), "Model not recorded");
  assert.equal(groupRowLabel({ group: "" }, "grade"), "Not graded");
  assert.equal(groupRowLabel({ group: "" }, "day"), "Not recorded");
  assert.equal(groupRowLabel(null, "issue"), "Issue not recorded");
});

test("pager label only appears when there is more than one page", () => {
  assert.equal(pagerLabel(0, 0, 0), "");
  assert.equal(pagerLabel(0, 5, 5), "5 total");
  assert.equal(pagerLabel(0, 25, 42), "Showing 1-25 of 42");
  assert.equal(pagerLabel(25, 17, 42), "Showing 26-42 of 42");
});

test("cross-links from the other Feedback reports produce one filter shape", () => {
  const issue = filtersForIssue(295, "B+");
  assert.equal(issue.issueNumber, "295");
  assert.equal(issue.grade, "B+");
  assert.equal(issue.outcome, "all");
  assert.equal(issue.model, "");

  const router = filtersForRouterSelection("Claude", "claude-haiku-4-5");
  assert.equal(router.provider, "Claude");
  assert.equal(router.model, "claude-haiku-4-5");
  // Router Activity is about grading calls, so the link scopes to them.
  assert.equal(router.agentType, "router");

  const execution = filtersForExecution("exec-1");
  assert.equal(execution.executionId, "exec-1");
  assert.equal(execution.issueNumber, "");
});
