"use strict";

/**
 * Issue #229: a session that was quota-paused before a worker restart is
 * active again once either cold-restart restore boundary is emitted. The
 * Overview consumes the worker's unstructured stdout, so this test exercises
 * the real parser with the exact worker-owned messages rather than a
 * hand-built state transition.
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const { deriveNowWorking } = require(path.join(
  __dirname,
  "..",
  "..",
  "ui",
  "now-working.js",
));

const app = { id: "app", name: "acme/app", monitorActions: false };
const site = { id: "site", name: "acme/site", monitorActions: false };
const line = (repository, message) =>
  `[12:34:56] [Issue worker scheduler/stdout] [${repository}] [2026-09-24 12:34:56-0500] ${message}`;

function rowsFor(logs) {
  return deriveNowWorking({
    workerState: "running",
    repositories: [app, site],
    logs,
  });
}

function row(rows, repository, kind) {
  const result = rows.find((candidate) => candidate.repository === repository && candidate.kind === kind);
  assert.ok(result, `expected ${kind} row for ${repository}`);
  return result;
}

const appPausedUat = [
  line("acme/app", "Selected oldest unprocessed assigned issue: #229 Persist resumed state"),
  line("acme/app", "Adversarial UAT for issue #229: starting re-test for round 3 of 6."),
  line("acme/app", "Paused issue #229 because Codex usage is unavailable; session codex:restart-229 was preserved."),
];

test("both cold-restart restore boundary lines clear the paused issue and its current UAT round", () => {
  for (const restored of [
    "Codex usage is available again; restored session codex:restart-229 for issue #229.",
    "Codex restored quota-paused issue #229 on the existing branch.",
  ]) {
    const rows = rowsFor([...appPausedUat, line("acme/app", restored)]);
    const issue = row(rows, "acme/app", "issue");
    const adversarial = row(rows, "acme/app", "adversarial");

    assert.equal(issue.state, "running", restored);
    assert.equal(issue.detail, "Resuming saved session", restored);
    assert.equal(adversarial.state, "running", restored);
    assert.equal(adversarial.title, "Fix/re-test round 3 of 6", restored);
    assert.equal(adversarial.detail, "Re-test in progress", restored);
  }
});

test("a repository-scoped cold restart cannot resume a same-number paused issue in another repository", () => {
  const rows = rowsFor([
    ...appPausedUat,
    line("acme/site", "Selected oldest unprocessed assigned issue: #229 Unrelated site work"),
    line("acme/site", "Adversarial UAT for issue #229: starting independent test run (round 0 of 6)."),
    line("acme/site", "Paused issue #229 because Claude usage is unavailable; session claude:site-229 was preserved."),
    line("acme/app", "Codex restored quota-paused issue #229 on the existing branch."),
  ]);

  assert.equal(row(rows, "acme/app", "issue").state, "running");
  assert.equal(row(rows, "acme/app", "adversarial").state, "running");
  assert.equal(row(rows, "acme/site", "issue").state, "paused");
  assert.equal(row(rows, "acme/site", "adversarial").state, "paused");
});
