const test = require("node:test");
const assert = require("node:assert/strict");
const { actionableLogEntries, actionableLogEntry } = require("./logging.js");

const line = (message, source = "Issue worker scheduler", stream = "stdout") =>
  `[12:34:56] [${source}/${stream}] [2026-09-15 12:34:56-0500] ${message}`;

test("turns issue lifecycle messages into concise actionable entries", () => {
  const messages = [
    "Selected oldest unprocessed assigned issue: #84 Reduce logs",
    "Selected issue #85 for rework after GitHub follow-up comment 123: Fix it",
    "Paused issue #84 because Codex usage is unavailable; session abc was preserved.",
    "Could not verify Claude usage for pinned issue #86; leaving state active and retrying later.",
    "Codex usage is available again; preparing to resume session abc for issue #84.",
    "Committed completed issue #84 work as deadbeef.",
  ];
  assert.deepEqual(
    messages.map((message) => actionableLogEntry(line(message)).message),
    [
      "Issue #84 work started",
      "Issue #85 rework started",
      "Issue #84 work paused — Codex usage unavailable",
      "Issue #86 work paused — Claude usage unavailable",
      "Issue #84 work resumed",
      "Issue #84 work completed",
    ],
  );
});

test("keeps errors, marks them red-ready, and removes the redundant prefix", () => {
  const entry = actionableLogEntry(line("ERROR: GitHub authentication failed", "Setup", "stderr"));
  assert.equal(entry.level, "error");
  assert.equal(entry.message, "GitHub authentication failed");
  assert.equal(entry.worker, false);

  assert.equal(actionableLogEntry(line("Issue worker exited with status 2.")).level, "error");
  assert.equal(actionableLogEntry(line("Could not fetch origin; deferring this run.")).level, "error");
});

test("shows queued work waiting for provider usage as an actionable pause", () => {
  const entry = actionableLogEntry(line(
    "No enabled provider (Claude, Codex) has at least 20% remaining in every active quota window; stopping.",
  ));
  assert.equal(entry.level, "info");
  assert.equal(entry.message, "Issue work paused — waiting for AI usage");
});

test("suppresses routine output and expected worker statuses", () => {
  assert.equal(actionableLogEntry(line("Starting a cycle over 3 repositories.")), null);
  assert.equal(actionableLogEntry(line("Codex remaining quota: 80%.")), null);
  assert.equal(actionableLogEntry(line("Issue worker exited with status 10.")), null);
  assert.equal(actionableLogEntry(line("Tests passed", "Test scheduler")), null);
});

test("collapses adjacent duplicate milestones", () => {
  const entries = actionableLogEntries([
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
  ]);
  assert.equal(entries.length, 1);
  assert.equal(entries[0].message, "Issue #84 work started");
});
