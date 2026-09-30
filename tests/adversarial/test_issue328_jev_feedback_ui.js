"use strict";

/** Adversarial UI contracts for issue #328's Feedback analysis findings. */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const appJs = fs.readFileSync(path.join(root, "ui", "app.js"), "utf8");
const indexHtml = fs.readFileSync(path.join(root, "ui", "index.html"), "utf8");
const { summaryCards } = require(path.join(root, "ui", "jev-decision-ui.js"));

function functionSource(name, nextName) {
  const start = appJs.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `${name} must exist`);
  const end = appJs.indexOf(`\n  function ${nextName}`, start + 1);
  assert.notEqual(end, -1, `${name} must have a stable following function`);
  return appJs.slice(start, end);
}

test("Feedback sends every full-history filter, including the inclusive upper date", () => {
  const source = functionSource("refreshJevFeedback", "renderExecutionHistory");
  for (const [field, state] of [
    ["provider", "jevFeedbackProvider"],
    ["createdAfter", "jevFeedbackFrom"],
    ["createdBefore", "jevFeedbackTo"],
    ["minDelta", "jevFeedbackDelta"],
    ["maxCost", "jevFeedbackCost"],
  ]) {
    assert.match(
      source,
      new RegExp(`${field}:\\s*state\\.${state}`),
      `${field} must reach get_jev_feedback_background rather than filtering only the current page`,
    );
  }
  assert.match(indexHtml, /id="jev-feedback-to"\s+type="date"/);
  assert.match(
    appJs,
    /byId\("jev-feedback-to"\)\?\.addEventListener\("change"[\s\S]*?refreshJevFilters\("jevFeedbackTo"/,
    "changing the upper date must reset pagination and refresh the backend query",
  );
});

test("Retries / failures reports failed work even when there are no retries", () => {
  const cards = summaryCards({
    comparisons: 3,
    outcomes: { completed: 1, failed: 2 },
  });
  const card = cards.find((entry) => entry.label === "Retries / failures");
  assert.ok(card, "Feedback must expose a Retries / failures summary card");
  assert.equal(card.value, "2", "failed outcomes were hidden when retry count was zero");
});

test("Retries / failures aggregates retry and failure outcome spellings exactly", () => {
  const cards = summaryCards({
    outcomes: {
      retry: 1,
      retried: 2,
      needs_retry: 3,
      failed: 4,
      failure: 5,
      error: 6,
      completed: 99,
    },
  });
  const card = cards.find((entry) => entry.label === "Retries / failures");
  assert.equal(card.value, "21");
  assert.match(card.hint, /Not counted as cost savings/i);
});
