"use strict";

/**
 * Issue #210: filing an out-of-scope adversarial UAT finding must be visible
 * in the app's Info & Debug actionable log, not only as a raw stdout line.
 *
 * The desktop never renders automation.log verbatim in that panel. `renderLogs`
 * in ui/app.js feeds every captured worker line through
 * `SwarmLogging.actionableLogEntries`. Lines that do not become an entry are
 * treated as routine command output and replaced with "Waiting for actionable
 * output…". A `log()` call that the allowlist drops therefore still leaves the
 * user with zero indication in Info & Debug — the original bug.
 *
 * Worker stdout is wrapped as `[unix] [Issue worker scheduler/stdout] [ts] msg`
 * (see src/processes.rs emit_log). That is the shape this suite feeds the
 * filter, using the same helper as ui/logging.test.js.
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { actionableLogEntry } = require(path.join(__dirname, "..", "..", "ui", "logging.js"));

const line = (message, source = "Issue worker scheduler", stream = "stdout") =>
  `[12:34:56] [${source}/${stream}] [2026-09-15 12:34:56-0500] ${message}`;

const FILED_URL = "https://example.invalid/issues/182";
const FILED = `Filed out-of-scope adversarial UAT finding for #180: ${FILED_URL}`;
const ALREADY = `Out-of-scope adversarial UAT finding already filed for #180: ${FILED_URL}`;
const ALREADY_TITLE = "Out-of-scope adversarial UAT finding already filed for #180: Separate parser bug";
const NO_URL =
  "GitHub did not return an issue URL for the out-of-scope adversarial UAT finding: '(no url returned)'";

test("a newly filed out-of-scope finding is an info milestone that names the issue URL", () => {
  const entry = actionableLogEntry(line(FILED));
  assert.ok(
    entry,
    "Info & Debug filters worker stdout through actionableLogEntry; a null entry means the filing is hidden as routine output even though log() ran",
  );
  assert.equal(entry.level, "info");
  assert.equal(entry.worker, true);
  assert.match(
    entry.message,
    /182/,
    `actionable message ${JSON.stringify(entry.message)} must still identify the filed issue so the user can open it`,
  );
  assert.match(entry.message, /https:\/\/example\.invalid\/issues\/182|issues\/182|#182/);
});

test("a GitHub-deduplicated filing is a distinct visible milestone that still names the URL", () => {
  const entry = actionableLogEntry(line(ALREADY));
  assert.ok(
    entry,
    "the already-filed-via-dedup log line must also survive the actionable-log allowlist; otherwise retries look like the app did nothing",
  );
  assert.notEqual(entry.level, "error");
  assert.match(entry.message, /182|already filed|already/i);
});

test("local-marker dedup that logs the finding title is still visible", () => {
  const entry = actionableLogEntry(line(ALREADY_TITLE));
  assert.ok(
    entry,
    "file_adversarial_findings logs the tester title when the marker is already in loop state; that line must not vanish from Info & Debug",
  );
});

test("missing GitHub issue URL is visible rather than silently dropped", () => {
  const entry = actionableLogEntry(line(NO_URL));
  assert.ok(
    entry,
    "the CI-monitor-style 'GitHub did not return an issue URL' warning must appear in the actionable log so a failed capture is not indistinguishable from never filing",
  );
});
