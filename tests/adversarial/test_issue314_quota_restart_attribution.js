"use strict";

/**
 * Issue #314 requires a quota pause and worker restart to preserve the active
 * adversarial stage's provider/model/effort without leaving stale attribution.
 *
 * These are the real boundary lines emitted across two scheduler cycles:
 *
 *   - the child worker emits the stage boundary, attribution, and quota pause;
 *   - install_swarm_issue_cron.py reports saved progress and starts another
 *     cycle (exit 11 is one of its PROGRESS_EXIT_CODES);
 *   - swarm_issue_worker.py restores the issue and emits its pinned (or
 *     replacement) provider selection before continuing the active stage.
 *
 * The Overview replays that complete stream. A replacement provider selection
 * on resume is the current adversarial agent, not merely primary-implementation
 * attribution. An unexpected child exit does clear volatile rows, so the next
 * run must reconstruct the active checkpoint instead of leaving the stage
 * invisible until it finishes.
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

const repo = { id: "r1", name: "acme/app", monitorActions: false };
const line = (message) =>
  `[12:34:56] [Issue worker scheduler/stdout] [2026-09-29 12:34:56-0500] ${message}`;

const stages = [
  {
    label: "Adversarial UAT",
    kind: "adversarial",
    start: "starting re-test for round 1 of 3",
  },
  {
    label: "Adversarial Cybersecurity",
    kind: "security",
    start: "starting re-test for round 1 of 3",
  },
];

function rows(logs) {
  return deriveNowWorking({ workerState: "running", repositories: [repo], logs });
}

function restartedLogs(stage, restoreLine, selectionLine, terminal = "") {
  const result = [
    line("Selected oldest unprocessed assigned issue: #314 Show attribution"),
    line(`${stage.label} for issue #314: ${stage.start}.`),
    line(`${stage.label} for issue #314: tester Codex model gpt-5.6 with effort medium.`),
    line("Paused issue #314 because Codex usage is unavailable; session codex:314 was preserved."),
    // Exact non-parallel scheduler message for QUOTA_PAUSED_EXIT_CODE (11),
    // which install_swarm_issue_cron.py classifies as saved progress.
    line("acme/app: made progress; will re-check on the next cycle."),
    line("Starting a cycle over 1 repository(ies)."),
    line(restoreLine),
    line("Selected oldest unprocessed assigned issue: #314 Show attribution"),
    line(selectionLine),
  ];
  if (terminal) result.push(line(terminal));
  return result;
}

function crashRestartLogs(stage) {
  return [
    line("Selected oldest unprocessed assigned issue: #314 Show attribution"),
    line(`${stage.label} for issue #314: ${stage.start}.`),
    line(`${stage.label} for issue #314: tester Codex model gpt-5.6 with effort medium.`),
    // A transient child failure/process restart clears live rows. The durable
    // adversarial checkpoint is still active and run_selected_issue emits the
    // pinned phase owner on the retry.
    line("acme/app: worker exited with status 1; will retry."),
    line("Starting a cycle over 1 repository(ies)."),
    line("Selected oldest unprocessed assigned issue: #314 Show attribution"),
    line("Pinned Codex model gpt-5.6 session codex:314 with effort medium for this continuation."),
  ];
}

for (const stage of stages) {
  test(`quota checkpoint and cold restart retain current ${stage.label} attribution`, () => {
    const actual = rows(restartedLogs(
      stage,
      "Codex usage is available again; restored session codex:314 for issue #314.",
      "Pinned Codex model gpt-5.6 session codex:314 with effort medium for this continuation.",
    ));
    const row = actual.find((candidate) => candidate.kind === stage.kind);

    assert.ok(row, `${stage.label} disappeared after the expected quota exit/restart`);
    assert.equal(row.state, "running", stage.label);
    assert.equal(row.title, "Fix/re-test round 1 of 3", stage.label);
    assert.deepEqual(
      row.attribution,
      { role: "tester", provider: "Codex", model: "gpt-5.6", effort: "medium" },
      `${stage.label} must retain the configured current invocation, not a blank/stale row`,
    );
    assert.match(row.detail, /Tester .* Codex .* gpt-5\.6 .* medium reasoning .* Re-test in progress/);
  });

  test(`quota-resume provider handoff replaces stale ${stage.label} attribution`, () => {
    const actual = rows(restartedLogs(
      stage,
      "Grok restored quota-paused issue #314 on the existing branch.",
      "Selected Grok model grok-4.6 with effort high for this run.",
    ));
    const row = actual.find((candidate) => candidate.kind === stage.kind);

    assert.ok(row, `${stage.label} disappeared during the quota handoff`);
    assert.deepEqual(
      row.attribution,
      { role: "tester", provider: "Grok", model: "grok-4.6", effort: "high" },
      `${stage.label} kept the exhausted provider after the worker selected its replacement`,
    );
    assert.doesNotMatch(row.detail, /Codex|gpt-5\.6|medium reasoning/);
    assert.match(row.detail, /Tester .* Grok .* grok-4\.6 .* high reasoning .* Re-test in progress/);
  });

  test(`active ${stage.label} is reconstructed with attribution after an unexpected worker restart`, () => {
    const actual = rows(crashRestartLogs(stage));
    const row = actual.find((candidate) => candidate.kind === stage.kind);

    assert.ok(row, `${stage.label} was not reconstructed from its active checkpoint`);
    assert.equal(row.title, "Fix/re-test round 1 of 3", stage.label);
    assert.deepEqual(
      row.attribution,
      { role: "tester", provider: "Codex", model: "gpt-5.6", effort: "medium" },
      `${stage.label} restart lost the current configured invocation`,
    );
  });

  test(`terminal completion clears ${stage.label} retained across quota restart`, () => {
    const actual = rows(restartedLogs(
      stage,
      "Codex usage is available again; restored session codex:314 for issue #314.",
      "Pinned Codex model gpt-5.6 session codex:314 with effort medium for this continuation.",
      `${stage.label} for issue #314: review completed with status PASS.`,
    ));
    assert.equal(
      actual.find((candidate) => candidate.kind === stage.kind),
      undefined,
      `${stage.label} remained visible after terminal completion`,
    );
  });
}
