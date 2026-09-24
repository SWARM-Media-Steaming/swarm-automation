"use strict";

/**
 * Issue #227 acceptance: a quota-resumed issue may no longer have its
 * original `Selected ... for this run.` entry in the bounded log buffer.
 * The worker's pinned-continuation boundary line is therefore the remaining
 * authoritative provider/model/effort record for the active resumed row.
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

const repos = [
  { id: "app", name: "acme/app", monitorActions: false },
  { id: "site", name: "acme/site", monitorActions: false },
];
const line = (repository, message) =>
  `[12:34:56] [Issue worker scheduler/stdout] [${repository}] [2026-09-24 12:34:56-0500] ${message}`;

function resumedRow(logs, issue) {
  const rows = deriveNowWorking({ workerState: "running", repositories: repos, logs });
  const row = rows.find((candidate) => candidate.kind === "issue" && candidate.issueNumber === String(issue));
  assert.ok(row, `resumed issue #${issue} must remain visible`);
  return row;
}

test("pinned continuation logs restore model and effort after selection rotation for every provider", () => {
  for (const { provider, model, effort, session } of [
    { provider: "Claude", model: "claude-sonnet-5", effort: "high", session: "claude:abc-1" },
    { provider: "Codex", model: "gpt-5.4", effort: "xhigh", session: "codex:abc-2" },
    { provider: "Grok", model: "grok-4-fast", effort: "low", session: "grok:abc-3" },
  ]) {
    const row = resumedRow([
      line("acme/app", `Preparing to resume saved session ${session} for issue #227.`),
      line("acme/app", `Pinned ${provider} model ${model} session ${session} with effort ${effort} for this continuation.`),
    ], 227);

    assert.equal(row.repository, "acme/app");
    assert.equal(row.state, "running");
    assert.equal(
      row.detail,
      `${provider} · ${model} · ${effort} effort · Resuming saved session`,
    );
  }
});

test("a malformed pinned line cannot overwrite another resumed issue's continuation metadata", () => {
  const logs = [
    line("acme/app", "Preparing to resume saved session app-session for issue #227."),
    line("acme/app", "Pinned Claude model claude-sonnet-5 session app-session with effort high for this continuation."),
    line("acme/site", "Preparing to resume saved session site-session for issue #228."),
    // The stable worker boundary line ends with this exact phrase. A stray
    // human/debug line must not replace the current issue's metadata.
    line("acme/site", "Pinned Grok model grok-4 session site-session with effort low for another continuation."),
  ];

  const app = resumedRow(logs, 227);
  const site = resumedRow(logs, 228);
  assert.equal(app.detail, "Claude · claude-sonnet-5 · high effort · Resuming saved session");
  assert.equal(site.detail, "Resuming saved session");
});
