const test = require("node:test");
const assert = require("node:assert/strict");
const { deriveNowWorking } = require("./now-working.js");

const line = (message, source = "Issue worker scheduler") =>
  `[12:34:56] [${source}/stdout] [2026-09-15 12:34:56-0500] ${message}`;
const repo = { id: "r1", name: "acme/app", uatState: "stopped", monitorActions: false };

test("shows the issue being worked with its provider and phase", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [repo],
    logs: [
      line("Starting a cycle over 1 repositories"),
      line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
      line("Selected Claude model claude-x"),
      line("Claude is working. Detailed implementation output is hidden."),
    ],
  });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].kind, "issue");
  assert.equal(rows[0].title, "#84 Reduce logs");
  assert.equal(rows[0].detail, "Claude · Claude is writing the change");
  assert.equal(rows[0].repository, "acme/app");
  assert.equal(rows[0].state, "running");
});

test("drops an issue once it finishes and falls back to an idle row", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [repo],
    logs: [
      line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
      line("Finished issue #84 with Claude: done"),
    ],
  });
  assert.deepEqual(rows.map((row) => [row.key, row.state]), [["issue:idle", "idle"]]);
});

test("marks quota-paused issues as paused and shows nothing when the worker is stopped", () => {
  const logs = [
    line("Selected oldest unprocessed assigned issue: #7 Fix it"),
    line("Paused issue #7 because Codex usage is unavailable; session abc was preserved."),
  ];
  const [paused] = deriveNowWorking({ workerState: "running", repositories: [repo], logs });
  assert.equal(paused.state, "paused");
  assert.equal(paused.detail, "Waiting for Codex usage");
  assert.deepEqual(deriveNowWorking({ workerState: "stopped", repositories: [repo], logs }), []);
});

test("keeps issues of different repositories apart", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [repo, { ...repo, id: "r2", name: "acme/site" }],
    logs: [
      line("=== repo: acme/app ==="),
      line("Selected oldest unprocessed assigned issue: #1 One"),
      line("=== repo: acme/site ==="),
      line("Selected oldest unprocessed assigned issue: #1 Other"),
    ],
  });
  assert.deepEqual(rows.map((row) => `${row.repository}${row.title}`), ["acme/app#1 One", "acme/site#1 Other"]);
});

test("reports an in-flight test run and an idle scheduler", () => {
  const rows = deriveNowWorking({
    workerState: "stopped",
    repositories: [{ ...repo, uatState: "running" }, { ...repo, id: "r2", name: "acme/site", uatState: "running" }],
    testRuns: {
      r1: [
        { startedAt: 1, finishedAt: 5, suites: [] },
        { startedAt: 10, finishedAt: null, trigger: "manual", suites: [{ name: "API", state: "Passed" }, { name: "UI", state: "Running" }, { name: "E2E", state: "Not executed" }] },
      ],
      r2: [{ startedAt: 1, finishedAt: 5, suites: [] }],
    },
  });
  assert.equal(rows[0].title, "Running UI");
  assert.equal(rows[0].detail, "Manual run · 1 of 3 suites finished");
  assert.equal(rows[0].state, "running");
  assert.equal(rows[1].state, "idle");
});

test("reports the latest CI result only for repositories monitored for Actions", () => {
  const logs = [line("GitHub Actions on ai-main are passing."), line("Failing pipeline(s) on ai-main (Build) already have an issue.")];
  assert.deepEqual(deriveNowWorking({ workerState: "stopped", repositories: [repo], logs }), []);
  const rows = deriveNowWorking({ workerState: "stopped", repositories: [{ ...repo, monitorActions: true }], logs });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].kind, "ci");
  assert.equal(rows[0].state, "error");
});
