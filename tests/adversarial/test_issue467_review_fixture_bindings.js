"use strict";

/* Issue #467: keep the independently authored calibration review harnesses
 * executable with the production controller's enclosing webMode binding. */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const root = path.resolve(__dirname, "../..");

function runReviewSuite(filename, minimumCases) {
  const env = { ...process.env };
  delete env.NODE_TEST_CONTEXT;
  const result = spawnSync("node", ["--test", `tests/adversarial/${filename}`], {
    cwd: root,
    env,
    encoding: "utf8",
    timeout: 30000,
  });
  const output = `${result.stdout || ""}\n${result.stderr || ""}`;
  assert.equal(result.error, undefined, `${filename} must start: ${result.error || ""}`);
  assert.equal(result.status, 0, `${filename} must pass:\n${output}`);

  const count = output.match(/^ℹ tests (\d+)$/m);
  assert.ok(count, `${filename} must report its collected Node test count:\n${output}`);
  assert.ok(Number(count[1]) >= minimumCases,
    `${filename} must collect at least ${minimumCases} cases, got ${count[1]}`);
}

test("calibration review UI fixtures supply the enclosing webMode binding", () => {
  runReviewSuite("test_model_calibration_review_ui.js", 4);
});

test("calibration review transition fixtures supply the enclosing webMode binding", () => {
  runReviewSuite("test_model_calibration_review_transitions.js", 3);
});
