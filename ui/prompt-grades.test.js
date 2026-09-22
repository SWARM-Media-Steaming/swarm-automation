const test = require("node:test");
const assert = require("node:assert/strict");
const {
  GRADES,
  FEEDBACK_TABS,
  activeTab,
  gradeTone,
  distributionBars,
  toggleGrade,
  routerRows,
  toggleRouter,
  toggleRouterModel,
  routerSummary,
  modelLabel,
  routerLine,
  summaryLine,
} = require("./prompt-grades.js");

test("maps each letter onto the shared state palette", () => {
  assert.equal(gradeTone("A+"), "passed");
  assert.equal(gradeTone("B-"), "running");
  assert.equal(gradeTone("C"), "blocked");
  assert.equal(gradeTone("D+"), "failed");
  assert.equal(gradeTone("F"), "failed");
  assert.equal(gradeTone(""), "");
  assert.equal(gradeTone(undefined), "");
});

test("scales bars against the most common grade and keeps empty grades", () => {
  const bars = distributionBars({ distribution: { A: 4, B: 2, F: 1 } });
  assert.equal(bars.length, GRADES.length);
  assert.equal(bars.find((bar) => bar.grade === "A").percent, 100);
  assert.equal(bars.find((bar) => bar.grade === "B").percent, 50);
  assert.equal(bars.find((bar) => bar.grade === "F").percent, 25);
  assert.equal(bars.find((bar) => bar.grade === "C").count, 0);
  assert.equal(bars.find((bar) => bar.grade === "B-").selected, false);
  const selected = distributionBars({ distribution: { "B-": 2 } }, "B-");
  assert.equal(selected.find((bar) => bar.grade === "B-").selected, true);
  assert.equal(selected.find((bar) => bar.grade === "A").selected, false);
  assert.ok(distributionBars(null).every((bar) => bar.percent === 0 && bar.selected === false));
});

test("clicking a grade selects it and clicking it again clears the filter", () => {
  assert.equal(toggleGrade("", "B-"), "B-");
  assert.equal(toggleGrade("B-", "B-"), "");
  assert.equal(toggleGrade("B-", "A"), "A");
  assert.equal(toggleGrade("A", "nope"), "A");
  assert.equal(toggleGrade("", ""), "");
});

test("summarizes how many prompts earned a B or better", () => {
  assert.equal(summaryLine(null), "No graded prompts yet.");
  assert.equal(summaryLine({ graded: 1, distribution: { A: 1 } }), "1 of 1 graded prompt earned a B or better.");
  assert.equal(
    summaryLine({ graded: 5, distribution: { "A-": 1, B: 2, C: 2 } }),
    "3 of 5 graded prompts earned a B or better.",
  );
});

test("falls back to the first tab for an unknown feedback tab", () => {
  assert.deepEqual(FEEDBACK_TABS, ["grades", "routing", "history"]);
  assert.equal(activeTab("routing"), "routing");
  assert.equal(activeTab("history"), "history");
  assert.equal(activeTab("nope"), "grades");
  assert.equal(activeTab(""), "grades");
  assert.equal(activeTab(undefined), "grades");
});

const MATRIX = [
  {
    router: "claude",
    graded: 3,
    models: [
      { model: "opus", count: 2, percent: 66.7 },
      { model: "haiku", count: 1, percent: 33.3 },
    ],
    selections: [
      { provider: "codex", count: 2, percent: 66.7 },
      { provider: "claude", count: 1, percent: 33.3 },
    ],
  },
  {
    router: "grok",
    graded: 1,
    models: [{ model: "grok-4.7", count: 1, percent: 100 }],
    selections: [{ provider: "grok", count: 1, percent: 100 }],
  },
];

test("sizes each grading platform's picks against its own graded total", () => {
  const rows = routerRows(MATRIX, "claude");
  assert.equal(rows.length, 2);
  assert.equal(rows[0].router, "claude");
  assert.equal(rows[0].graded, 3);
  assert.equal(rows[0].selected, true);
  assert.equal(rows[0].interactive, true);
  assert.deepEqual(rows[0].models.map((entry) => entry.model), ["opus", "haiku"]);
  assert.equal(rows[0].models[0].percent, 66.7);
  assert.deepEqual(rows[0].selections.map((entry) => entry.provider), ["codex", "claude"]);
  assert.equal(rows[0].selections[0].percent, 66.7);
  assert.equal(rows[1].selected, false);
  assert.deepEqual(routerRows(null, ""), []);
});

test("keeps a router-less row visible but not selectable", () => {
  const rows = routerRows([{ router: "", graded: 2, selections: [{ provider: "codex", count: 2 }] }], "");
  assert.equal(rows[0].interactive, false);
  assert.equal(rows[0].selected, false);
  // percent is derived when the payload predates the rounded value.
  assert.equal(rows[0].selections[0].percent, 100);
});

test("clicking a grading platform selects it and clicking it again clears the filter", () => {
  assert.equal(toggleRouter("", "claude", MATRIX), "claude");
  assert.equal(toggleRouter("claude", "claude", MATRIX), "");
  assert.equal(toggleRouter("claude", "grok", MATRIX), "grok");
  assert.equal(toggleRouter("claude", "codex", MATRIX), "claude");
  assert.equal(toggleRouter("claude", "", MATRIX), "claude");
});

test("selects a grading model within its platform and clears back to the platform", () => {
  assert.deepEqual(toggleRouterModel("", "", "claude", "opus", MATRIX), {
    router: "claude", model: "opus",
  });
  assert.deepEqual(toggleRouterModel("claude", "opus", "claude", "opus", MATRIX), {
    router: "claude", model: "",
  });
  assert.deepEqual(toggleRouterModel("claude", "opus", "grok", "missing", MATRIX), {
    router: "claude", model: "opus",
  });
  assert.equal(routerRows(MATRIX, "claude", "opus")[0].models[0].selected, true);
});

test("counts recorded platforms and model rows without treating history gaps as platforms", () => {
  const matrix = [...MATRIX, {
    router: "", graded: 1, models: [{ model: "legacy-model", count: 1 }],
    selections: [{ provider: "claude", count: 1 }],
  }];
  assert.deepEqual(routerSummary(matrix), { platforms: 2, models: 3 });
});

test("presents common router model IDs as friendly names", () => {
  assert.equal(modelLabel("claude-haiku-4-5"), "Haiku 4.5");
  assert.equal(modelLabel("gpt-5.6-luna"), "GPT-5.6 Luna");
  assert.equal(modelLabel("grok-4.7"), "Grok 4.7");
  assert.equal(modelLabel("opus"), "Opus");
  assert.equal(modelLabel("custom/model"), "custom/model");
  assert.equal(modelLabel(""), "Model not recorded");
});

test("summarizes who a grading platform picks most often", () => {
  assert.equal(routerLine(routerRows(MATRIX, "")[0], (id) => id.toUpperCase()),
    "3 graded · picked CODEX 67% of the time");
  assert.equal(routerLine({ graded: 0, selections: [] }), "No graded prompts yet.");
});
