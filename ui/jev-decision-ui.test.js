const test = require("node:test");
const assert = require("node:assert/strict");
const {
  statusLabel,
  statusTone,
  percent,
  scoreCell,
  formatDelta,
  routeLabel,
  summaryCards,
  tableRows,
  connectionStatus,
} = require("./jev-decision-ui");

test("disabled Jev is labelled baseline-only rather than a zero score", () => {
  assert.equal(statusLabel("disabled"), "Jev off — baseline only");
  assert.equal(statusTone("disabled"), "stopped");
  assert.equal(scoreCell(null, { missing: true }), "Baseline only");
  const rows = tableRows([
    {
      jevStatus: "disabled",
      baselineOnly: true,
      jevPresent: false,
      baselineScore: 0.8,
      jevScore: null,
      modifiedScore: 0.8,
      scoreDelta: 0,
      modifiedProvider: "claude",
      modifiedModel: "claude-sonnet-5",
      modifiedEffort: "low",
    },
  ]);
  assert.equal(rows[0].jevScore, "Baseline only");
  assert.equal(rows[0].baselineOnly, true);
  assert.equal(rows[0].baselineScore, "0.80");
});

test("enabled Jev shows distinct baseline, Jev, and modified scores", () => {
  const rows = tableRows([
    {
      jevStatus: "enabled",
      jevPresent: true,
      baselineScore: 0.72,
      jevScore: 0.91,
      modifiedScore: 0.84,
      scoreDelta: 0.12,
      scoreDeltaPercent: 16.7,
      routingChanged: true,
      modifiedProvider: "codex",
      modifiedModel: "gpt-5.6-luna",
      modifiedEffort: "low",
    },
  ]);
  assert.equal(rows[0].jevScore, "0.91");
  assert.equal(rows[0].modifiedScore, "0.84");
  assert.equal(rows[0].scoreDelta, "+0.12 (+16.7%)");
  assert.equal(rows[0].routingChanged, true);
  assert.match(rows[0].selected, /codex/);
});

test("fallback and unavailable states are distinct from a genuine score", () => {
  assert.equal(statusLabel("timeout"), "Jev timed out");
  assert.equal(statusLabel("unavailable"), "Jev unavailable");
  assert.equal(statusLabel("low_confidence"), "Low confidence — fallback");
  const rows = tableRows([{ jevStatus: "timeout", jevPresent: false, fallback: true, baselineScore: 0.5 }]);
  assert.equal(rows[0].jevScore, "Baseline only");
  assert.equal(rows[0].fallback, true);
});

test("summary cards use completion and fallback, not implied zero Jev scores", () => {
  const cards = summaryCards({
    completionRate: 0.8,
    completed: 8,
    comparisons: 10,
    jevCost: 0.0012,
    fallbackRate: 0.2,
    fallbackCount: 2,
    disabledCount: 3,
    outcomes: { retry: 1 },
  });
  assert.equal(cards[0].value, "80%");
  assert.match(cards[1].value, /0.0012/);
  assert.equal(cards[2].value, "1");
  assert.equal(cards[3].value, "20%");
});

test("percent and route helpers tolerate empty values", () => {
  assert.equal(percent(null), "—");
  assert.equal(percent(0.94), "94%");
  assert.equal(routeLabel("", "", ""), "—");
  assert.equal(routeLabel("grok", "grok-4.6", "high"), "grok · grok-4.6 · high");
  assert.equal(formatDelta(null), "—");
  assert.equal(connectionStatus({ installed: false }).text, "Not installed");
  assert.equal(connectionStatus({ installed: true, authenticated: true }).tone, "running");
});
