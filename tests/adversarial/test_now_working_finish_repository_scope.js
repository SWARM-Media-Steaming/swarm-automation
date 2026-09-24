"use strict";

/**
 * Issue #228: Finishing issue #N in repository A must only remove rows that
 * belong to A/#N. Issue numbers are local to a repository, so an identical
 * number in repository B is unrelated active work. A terminal line without a
 * repository identity is similarly unsafe to apply when more than one
 * repository is configured.
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { deriveNowWorking } = require(path.join(__dirname, "..", "..", "ui", "now-working.js"));

const app = { id: "app", name: "acme/app", monitorActions: true };
const site = { id: "site", name: "acme/site", monitorActions: true };
const labeled = (repository, message) =>
  `[12:34:56] [Issue worker scheduler/stdout] [${repository}] [2026-09-24 12:34:56-0500] ${message}`;
const unlabeled = (message) =>
  `[12:34:56] [Issue worker scheduler/stdout] [2026-09-24 12:34:56-0500] ${message}`;

function derive(logs) {
  return deriveNowWorking({
    workerState: "running",
    repositories: [app, site],
    logs,
  });
}

function identities(rows) {
  return rows.map((row) => `${row.kind}:${row.repository}:${row.issueNumber}:${row.title}`);
}

test("a repository-scoped finish removes all of its #N rows but preserves every same-number row in another repository", () => {
  const rows = derive([
    labeled("acme/app", "Selected oldest unprocessed assigned issue: #4 App implementation"),
    labeled("acme/app", "Working CI failure issue #4 filed by the Actions monitor: App pipeline"),
    labeled("acme/app", "Adversarial UAT for issue #4: starting re-test for round 2 of 6."),
    labeled("acme/site", "Selected oldest unprocessed assigned issue: #4 Site implementation"),
    labeled("acme/site", "Working CI failure issue #4 filed by the Actions monitor: Site pipeline"),
    labeled("acme/site", "Adversarial UAT for issue #4: starting independent test run (round 0 of 6)."),
    labeled("acme/app", "Finished issue #4 with Codex: delivered"),
  ]);

  assert.deepEqual(identities(rows), [
    "issue:acme/site:4:#4 Site implementation",
    "ci:acme/site:4:#4 Site pipeline",
    "adversarial:acme/site:4:Independent test run",
  ]);
});

test("an ambiguous finish line cannot clear same-number work from configured repositories", () => {
  const rows = derive([
    labeled("acme/app", "Selected oldest unprocessed assigned issue: #4 App implementation"),
    labeled("acme/site", "Selected oldest unprocessed assigned issue: #4 Site implementation"),
    unlabeled("Finished issue #4 with Codex: malformed parallel-worker output"),
  ]);

  assert.deepEqual(identities(rows), [
    "issue:acme/app:4:#4 App implementation",
    "issue:acme/site:4:#4 Site implementation",
  ]);
});
