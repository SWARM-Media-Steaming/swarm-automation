"use strict";

/**
 * Issue #314: Overview must "show the adversarial stage, provider, model,
 * and reasoning effort for active UAT and cybersecurity work" and never
 * silently drop a real configured value.
 *
 * `AdversarialStage.attribution_log` (issue_worker/adversarial_core.py)
 * renders a missing model/effort as the literal word "default":
 *
 *   f"... model {choice.model or 'default'} with effort {choice.effort or 'default'}."
 *
 * `adversarialAttribution` in ui/now-working.js then undoes that by treating
 * the literal token "default" as the missing-value sentinel and converting
 * it back to "":
 *
 *   model: found[4] === "default" ? "" : found[4]
 *   effort: found[5] === "default" ? "" : found[5]
 *
 * A provider whose *actual configured* model or reasoning-effort name is the
 * word "default" (a plausible catalog/spec name, not merely hypothetical:
 * this repo's own model_router.py already uses the bare group name
 * "default" elsewhere) is therefore indistinguishable on the wire from a
 * provider with no model/effort configured at all. The real value is
 * silently discarded and the Overview row renders as if that information
 * were never recorded, which is exactly what the issue's acceptance
 * criteria say must not happen ("Show provider, model, and reasoning effort
 * ... for active work").
 *
 * These tests pin the *correct* round-trip: a genuinely configured model or
 * effort literally named "default" must still show up as "default" on the
 * Overview row, not be swallowed into an empty/absent value indistinguishable
 * from nothing being configured. They fail against the current sentinel
 * collision and must pass once the log/parse protocol disambiguates a real
 * "default" value from a genuinely missing one (e.g. by never falling back to
 * a plain English word that can itself be a legitimate value).
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { deriveNowWorking } = require(path.join(__dirname, "..", "..", "ui", "now-working.js"));

const repo = { id: "r1", name: "acme/app", monitorActions: false };
const line = (message) =>
  `[12:34:56] [Issue worker scheduler/stdout] [2026-09-15 12:34:56-0500] ${message}`;

function derive(logs) {
  return deriveNowWorking({ workerState: "running", repositories: [repo], logs });
}

function uatRow(rows) {
  return rows.find((row) => row.kind === "adversarial");
}

test("a UAT fixer whose configured model is literally named 'default' is not shown as if no model were configured", () => {
  const rows = derive([
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line("Adversarial UAT for issue #84: starting fix/re-test round 1 of 3."),
    // Worker-authentic attribution line for a provider whose *real* model
    // name is "default" (e.g. a spec entry named "default" in the model
    // catalog), not a missing value.
    line("Adversarial UAT for issue #84: fixer Codex model default with effort medium."),
  ]);
  const row = uatRow(rows);
  assert.ok(row, "expected an active UAT row");
  assert.equal(row.attribution.provider, "Codex");
  assert.equal(
    row.attribution.model,
    "default",
    "a real model literally named 'default' must survive the round trip, not collapse to an empty value",
  );
  assert.match(
    row.detail,
    /default/,
    "the Overview detail text must name the real 'default' model rather than silently omitting it",
  );
});

test("a UAT tester whose configured reasoning effort is literally named 'default' is not shown as unrecorded", () => {
  const rows = derive([
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line("Adversarial UAT for issue #84: starting re-test for round 1 of 3."),
    line("Adversarial UAT for issue #84: tester Grok model grok-4.6 with effort default."),
  ]);
  const row = uatRow(rows);
  assert.ok(row, "expected an active UAT row");
  assert.equal(
    row.attribution.effort,
    "default",
    "a real reasoning effort literally named 'default' must survive the round trip, not collapse to an empty value",
  );
});
