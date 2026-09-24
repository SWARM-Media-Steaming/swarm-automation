"use strict";

/**
 * Issue #218 requires the AI agents summary to derive "currently working"
 * across every configured repository, not only the active picker selection.
 * The supported --parallel-repos scheduler mode makes those repositories'
 * labeled worker lines interleave. These tests exercise that production log
 * shape through the real Now Working parser that feeds the new panel.
 *
 * Repository issue numbers are local to a repository. Two repositories can
 * therefore both be working #84 at the same time; neither provider ownership
 * nor a completion event may leak across that repository boundary.
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { deriveNowWorking } = require(path.join(__dirname, "..", "..", "ui", "now-working.js"));

const repositories = [
  { id: "app", name: "acme/app", monitorActions: false },
  { id: "site", name: "acme/site", monitorActions: false },
];

// install_swarm_issue_cron.py prefixes every child line with [owner/repo]
// when --parallel-repos is enabled. The desktop log transport adds the first
// two bracket groups around that exact payload.
function workerLine(repository, message, second) {
  return `[12:34:${String(second).padStart(2, "0")}] [Issue worker scheduler/stdout] [${repository}] [2026-09-23 12:34:${String(second).padStart(2, "0")}-0500] ${message}`;
}

function rowsFor(logs) {
  return deriveNowWorking({ logs, workerState: "running", repositories });
}

function issue(rows, repository, number) {
  return rows.find((row) =>
    row.kind === "issue"
      && row.repository === repository
      && String(row.issueNumber) === String(number));
}

test("interleaved parallel-repository selection lines retain the provider for their own issue", () => {
  const rows = rowsFor([
    workerLine("acme/app", "Selected oldest unprocessed assigned issue: #84 App change", 1),
    workerLine("acme/site", "Selected oldest unprocessed assigned issue: #17 Site change", 2),
    // A real parallel run can reach provider selection in either order after
    // both issue-selection lines have already arrived.
    workerLine("acme/app", "Selected Claude model claude-sonnet-5 with effort high for this run.", 3),
    workerLine("acme/site", "Selected Codex model gpt-5.4 with effort medium for this run.", 4),
  ]);

  const app = issue(rows, "acme/app", 84);
  const site = issue(rows, "acme/site", 17);
  assert.ok(app, "the first configured repository must remain represented");
  assert.ok(site, "the second configured repository must remain represented");
  assert.equal(app.provider, "Claude", "Claude must be shown working acme/app #84");
  assert.equal(site.provider, "Codex", "Codex must be shown working acme/site #17");
  assert.match(app.detail, /claude-sonnet-5/);
  assert.match(site.detail, /gpt-5\.4/);
});

test("finishing the same issue number in one repository does not hide another repository's active issue", () => {
  const rows = rowsFor([
    workerLine("acme/app", "Selected oldest unprocessed assigned issue: #84 App change", 1),
    workerLine("acme/app", "Selected Claude model claude-sonnet-5 with effort high for this run.", 2),
    workerLine("acme/site", "Selected oldest unprocessed assigned issue: #84 Site change", 3),
    workerLine("acme/site", "Selected Codex model gpt-5.4 with effort medium for this run.", 4),
    workerLine("acme/app", "Finished issue #84 with Claude: done", 5),
  ]);

  assert.equal(issue(rows, "acme/app", 84), undefined, "the completed repository must disappear");
  const site = issue(rows, "acme/site", 84);
  assert.ok(site, "issue numbers are repository-local; acme/site #84 is still running");
  assert.equal(site.provider, "Codex");
  assert.equal(site.state, "running");
});

