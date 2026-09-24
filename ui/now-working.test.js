const test = require("node:test");
const assert = require("node:assert/strict");
const { deriveNowWorking } = require("./now-working.js");

const line = (message, source = "Issue worker scheduler") =>
  `[12:34:56] [${source}/stdout] [2026-09-15 12:34:56-0500] ${message}`;
const repo = { id: "r1", name: "acme/app", monitorActions: false };

test("shows the issue being worked with its provider, model, effort, and phase", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [repo],
    logs: [
      line("Starting a cycle over 1 repositories"),
      line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
      line("Selected Claude model claude-sonnet-5 with effort high for this run."),
      line("Claude is working. Detailed implementation output is hidden."),
    ],
  });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].kind, "issue");
  assert.equal(rows[0].title, "#84 Reduce logs");
  assert.equal(rows[0].detail, "Claude · claude-sonnet-5 · high effort · Claude is writing the change");
  assert.equal(rows[0].repository, "acme/app");
  assert.equal(rows[0].state, "running");
  assert.equal(rows[0].provider, "Claude");
});

test("shows pinned continuation model and effort after the original selection rotates out", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [repo],
    logs: [
      line("Preparing to resume saved session abc for issue #84."),
      line("Pinned Claude model claude-sonnet-5 session abc with effort high for this continuation."),
    ],
  });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].detail, "Claude · claude-sonnet-5 · high effort · Resuming saved session");
});

test("drops an issue once it finishes instead of leaving a queue-check row", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [repo],
    logs: [
      line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
      line("Finished issue #84 with Claude: done"),
      line("Starting a cycle over 1 repositories"),
    ],
  });
  assert.deepEqual(rows, []);
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

test("finishing an issue preserves same-number work in another repository", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [repo, { ...repo, id: "r2", name: "acme/site" }],
    logs: [
      labeled("acme/app", "Selected oldest unprocessed assigned issue: #4 App work"),
      labeled("acme/app", "Adversarial UAT for issue #4: starting independent test run (round 0 of 6)."),
      labeled("acme/site", "Selected oldest unprocessed assigned issue: #4 Site work"),
      labeled("acme/site", "Adversarial UAT for issue #4: starting re-test for round 2 of 6."),
      labeled("acme/app", "Finished issue #4 with Codex: done"),
    ],
  });
  assert.deepEqual(rows.map((row) => `${row.kind}:${row.repository}${row.title}`), [
    "issue:acme/site#4 Site work",
    "adversarial:acme/siteFix/re-test round 2 of 6",
  ]);
});

test("shows a CI failure only while the worker is fixing it", () => {
  const checked = [
    line("GitHub Actions on ai-main are passing."),
    line("Failing pipeline(s) on ai-main (Build) already have an issue."),
    line("Could not check GitHub Actions; continuing with the issue queue: denied"),
  ];
  assert.deepEqual(deriveNowWorking({ workerState: "running", repositories: [{ ...repo, monitorActions: true }], logs: checked }), []);
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [{ ...repo, monitorActions: true }],
    logs: [line("Working CI failure issue #12 filed by the Actions monitor: Fix failing CI on ai-main: Build")],
  });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].kind, "ci");
  assert.equal(rows[0].state, "running");
  assert.equal(rows[0].title, "#12 Fix failing CI on ai-main: Build");
});

const labeled = (label, message) =>
  `[12:34:56] [Issue worker scheduler/stdout] [${label}] [2026-09-15 12:34:56-0500] ${message}`;

test("reads worker lines from parallel-repo runs that carry a repository label", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [repo, { ...repo, id: "r2", name: "acme/site" }],
    logs: [
      labeled("acme/app", "Selected oldest unprocessed assigned issue: #4 One"),
      labeled("acme/app", "Selected Claude model claude-x"),
      labeled("acme/site", "Selected oldest unprocessed assigned issue: #9 Two"),
    ],
  });
  assert.deepEqual(rows.map((row) => `${row.repository}${row.title}`), ["acme/app#4 One", "acme/site#9 Two"]);
  assert.equal(rows[0].detail, "Claude · Picked up from the queue");
});

test("keeps provider selection attached to its repository when parallel logs interleave", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [repo, { ...repo, id: "r2", name: "acme/site" }],
    logs: [
      labeled("acme/app", "Selected oldest unprocessed assigned issue: #4 One"),
      labeled("acme/site", "Selected oldest unprocessed assigned issue: #9 Two"),
      labeled("acme/app", "Selected Claude model claude-x with effort high for this run."),
      labeled("acme/site", "Selected Codex model gpt-x with effort medium for this run."),
    ],
  });
  const app = rows.find((row) => row.repository === "acme/app");
  const site = rows.find((row) => row.repository === "acme/site");
  assert.equal(app.provider, "Claude");
  assert.equal(site.provider, "Codex");
});

test("finishing an issue only removes that repository's matching issue number", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [repo, { ...repo, id: "r2", name: "acme/site" }],
    logs: [
      labeled("acme/app", "Selected oldest unprocessed assigned issue: #4 One"),
      labeled("acme/site", "Selected oldest unprocessed assigned issue: #4 Two"),
      labeled("acme/app", "Finished issue #4 with Claude: done"),
    ],
  });
  assert.deepEqual(rows.map((row) => `${row.repository}${row.title}`), ["acme/site#4 Two"]);
});

test("shows a new issue that starts after an earlier one finished", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [repo],
    logs: [
      line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
      line("Finished issue #84 with Claude: done"),
      line("Starting a cycle over 1 repositories"),
      line("Selected oldest unprocessed assigned issue: #85 Next one"),
      line("Codex is working. Detailed implementation output is hidden."),
    ],
  });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].title, "#85 Next one");
  assert.equal(rows[0].detail, "Codex · Codex is writing the change");
});

test("shows current adversarial UAT fix/re-test progress from worker boundary logs", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [repo],
    logs: [
      line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
      line("Adversarial UAT for issue #84: starting independent test run (round 0 of 6)."),
      line("Adversarial UAT for issue #84: starting fix/re-test round 3 of 6."),
      line("Adversarial UAT for issue #84: fix applied in round 3 of 6."),
      line("Adversarial UAT for issue #84: starting re-test for round 3 of 6."),
    ],
  });
  const adversarial = rows.find((row) => row.kind === "adversarial");
  assert.equal(adversarial.title, "Fix/re-test round 3 of 6");
  assert.equal(adversarial.detail, "Re-test in progress");
  assert.equal(adversarial.issueNumber, "84");
  assert.equal(adversarial.repository, "acme/app");
});

test("keeps adversarial UAT progress synchronized with an issue quota pause and resume", () => {
  const logs = [
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line("Adversarial UAT for issue #84: starting re-test for round 3 of 6."),
    line("Paused issue #84 because Codex usage is unavailable; session abc was preserved."),
  ];
  const pausedRows = deriveNowWorking({ workerState: "running", repositories: [repo], logs });
  assert.equal(pausedRows.find((row) => row.kind === "adversarial").state, "paused");

  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [repo],
    logs: [...logs,
      line("Codex usage is available again; preparing to resume session abc for issue #84."),
    ],
  });
  const adversarial = rows.find((row) => row.kind === "adversarial");
  assert.equal(adversarial.state, "running");
  assert.equal(adversarial.title, "Fix/re-test round 3 of 6");
});

test("clears a quota pause after a cold-restart session restore", () => {
  const baseLogs = [
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line("Adversarial UAT for issue #84: starting re-test for round 3 of 6."),
    line("Paused issue #84 because Codex usage is unavailable; session abc was preserved."),
  ];

  for (const restored of [
    "Codex restored session abc for issue #84.",
    "Codex restored quota-paused issue #84 on the existing branch.",
  ]) {
    const rows = deriveNowWorking({
      workerState: "running",
      repositories: [repo],
      logs: [...baseLogs, line(restored)],
    });
    assert.equal(rows.find((row) => row.kind === "issue").state, "running");
    assert.equal(rows.find((row) => row.kind === "adversarial").state, "running");
  }
});
