const test = require("node:test");
const assert = require("node:assert/strict");
const { GRADES, gradeTone, distributionBars, toggleGrade, summaryLine } = require("./prompt-grades.js");

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
