"use strict";

/**
 * Issue #299 Feedback analysis wiring: filters must reach the query, and
 * retries/failures must not be dressed up as savings.
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..", "..");
const appJs = fs.readFileSync(path.join(root, "ui", "app.js"), "utf8");
const { summaryCards } = require(path.join(root, "ui", "jev-decision-ui.js"));

function refreshJevFeedbackSource() {
  const start = appJs.indexOf("async function refreshJevFeedback");
  assert.notEqual(start, -1, "refreshJevFeedback must exist");
  const sliceEnd = appJs.indexOf("\n  function renderExecutionHistory", start);
  assert.notEqual(sliceEnd, -1, "refreshJevFeedback body must be locatable");
  return appJs.slice(start, sliceEnd);
}

test("Jev Feedback refresh sends provider, date, delta, and cost to the backend", () => {
  const source = refreshJevFeedbackSource();
  assert.match(
    source,
    /get_jev_feedback_background/,
    "Feedback Jev tab must query get_jev_feedback_background",
  );
  assert.doesNotMatch(
    source,
    /provider:\s*""/,
    "refreshJevFeedback hardcodes provider: \"\" so the provider/model filter never reaches the query (AC 29). PAGE_SIZE is 10; client-side filtering of the current page hides matching rows on later pages.",
  );
  assert.match(
    source,
    /provider:\s*state\.jevFeedbackProvider/,
    "AC 29 requires the provider/model filter to be part of the backend query, not only a client-side hide of the current page",
  );
  assert.match(
    source,
    /fromDate:|createdAfter:|jevFrom:|dateFrom:/,
    "AC 29 requires a date-range filter on the query. The From date control currently re-renders the current page only.",
  );
  assert.match(
    source,
    /minDelta:|scoreDelta:|jevDelta:/,
    "AC 29 requires a score-delta filter on the query",
  );
  assert.match(
    source,
    /maxCost:|jevCost:|maxJevCost:/,
    "AC 29 requires a cost filter on the query",
  );
});

test("retries/failures summary counts failed outcomes, not only retry", () => {
  const cards = summaryCards({
    completionRate: 0.25,
    completed: 1,
    comparisons: 6,
    jevCost: 0.01,
    fallbackRate: 0,
    fallbackCount: 0,
    disabledCount: 0,
    outcomes: { retry: 1, retried: 1, failed: 3 },
  });
  const retries = cards.find((card) => /retry|failure/i.test(card.label));
  assert.ok(retries, "summary cards must include a retries/failures card (AC 26 / 30)");
  const value = Number(String(retries.value).replace(/[^\d.-]/g, ""));
  assert.ok(
    value >= 5,
    `Retries/failures card shows ${JSON.stringify(retries.value)} for outcomes retry=1, retried=1, failed=3. ` +
      "AC 30 requires failures to be visible and never treated as savings; counting only " +
      "the 'retry' key hides failed work.",
  );
});
