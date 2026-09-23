"use strict";

/**
 * Issue #210: Info & Debug only shows lines that survive `actionableLogEntry`.
 * The new filing logs interpolate a tester title on the local-marker retry
 * path. `isError` runs before the filing allowlist and treats any message
 * containing `\bfailed\b` as an error. A finding titled "... failed ..."
 * must still be the filing milestone, not a red worker failure.
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const {
  actionableLogEntry,
  actionableLogEntries,
} = require(path.join(__dirname, "..", "..", "ui", "logging.js"));

const line = (message, source = "Issue worker scheduler", stream = "stdout") =>
  `[12:34:56] [${source}/${stream}] [2026-09-15 12:34:56-0500] ${message}`;

test("local-marker already-filed log with 'failed' in the title is an info milestone", () => {
  const entry = actionableLogEntry(
    line(
      "Out-of-scope adversarial UAT finding already filed for #180: Parser failed on nested input",
    ),
  );
  assert.ok(
    entry,
    "Info & Debug drops non-actionable lines; a null entry hides the retry",
  );
  assert.equal(
    entry.level,
    "info",
    `tester titles routinely contain the word "failed"; classifying this as ${entry.level} makes a successful dedup look like a worker crash. message=${JSON.stringify(entry.message)}`,
  );
  assert.match(entry.message, /already filed/i);
  assert.match(entry.message, /180/);
});

test("GitHub-search already-filed log with a URL stays an info milestone", () => {
  const entry = actionableLogEntry(
    line(
      "Out-of-scope adversarial UAT finding already filed for #180: https://example.invalid/issues/182",
    ),
  );
  assert.ok(entry);
  assert.equal(entry.level, "info");
  assert.match(entry.message, /182/);
});

test("two different filed issue URLs are two Info & Debug milestones", () => {
  const entries = actionableLogEntries([
    line("Filed out-of-scope adversarial UAT finding for #180: https://example.invalid/issues/182"),
    line("Filed out-of-scope adversarial UAT finding for #180: https://example.invalid/issues/183"),
  ]);
  assert.equal(entries.length, 2, JSON.stringify(entries.map((entry) => entry.message)));
  assert.equal(entries[0].level, "info");
  assert.equal(entries[1].level, "info");
  assert.match(entries[0].message, /182/);
  assert.match(entries[1].message, /183/);
});
