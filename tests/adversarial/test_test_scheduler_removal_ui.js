"use strict";

/**
 * Issue #215 removes the deterministic scheduler from every UI entry point.
 * Its old data shape is intentionally ignored, while #213's independent
 * Adversarial UAT progress remains a first-class Now Working row.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const indexHtml = fs.readFileSync(path.join(root, "ui", "index.html"), "utf8");
const appJs = fs.readFileSync(path.join(root, "ui", "app.js"), "utf8");
const nowWorkingJs = fs.readFileSync(path.join(root, "ui", "now-working.js"), "utf8");
const { deriveNowWorking } = require(path.join(root, "ui", "now-working.js"));

const line = (message, source = "Issue worker scheduler") =>
  `[12:34:56] [${source}/stdout] [2026-09-23 12:34:56-0500] ${message}`;

test("navigation, Overview, and page markup expose no Test Scheduler surface", () => {
  for (const forbidden of [
    /data-view-target=["']scheduler["']/i,
    /id=["']view-scheduler["']/i,
    /id=["']uat-service-card["']/i,
    /id=["']uat-hour["']/i,
    /id=["']uat-schedule-summary["']/i,
    /data-action=["'](?:start|run|pause|stop)-uat["']/i,
    />\s*Test Scheduler\s*</i,
  ]) {
    assert.doesNotMatch(indexHtml, forbidden);
  }
});

test("frontend behavior contains no scheduler IPC or deleted repository settings", () => {
  for (const identifier of [
    "start_uat_scheduler",
    "get_test_plan",
    "get_test_plan_background",
    "detect_test_definition",
    "create_test_definition",
    "audit_test_coverage",
    "get_test_runs",
    "get_test_runs_background",
    "save_test_input",
    "choose_test_input_path",
    "uat_hour",
    "allow_disruptive_tests",
    "uat_ai_test_data_enabled",
    "uat_triage_enabled",
    "test_inputs",
  ]) {
    assert.equal(appJs.includes(identifier), false, `deleted UI contract remains: ${identifier}`);
  }
});

test("legacy scheduler snapshots cannot recreate a tests row", () => {
  const rows = deriveNowWorking({
    workerState: "stopped",
    repositories: [{
      id: "legacy",
      name: "acme/legacy",
      uatState: "running",
      monitorActions: false,
    }],
    testRuns: {
      legacy: [{
        startedAt: 1,
        finishedAt: null,
        trigger: "manual",
        suites: [{ id: "old", name: "Old suite", state: "Running" }],
      }],
    },
  });

  assert.deepEqual(rows, []);
  assert.doesNotMatch(nowWorkingJs, /function\s+testRows\s*\(/);
  assert.doesNotMatch(appJs, /\btests\s*:\s*["']Tests["']/);
});

test("Adversarial UAT replaces the removed tests kind without losing issue progress", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [{ id: "r1", name: "acme/app", monitorActions: false }],
    logs: [
      line("Selected oldest unprocessed assigned issue: #215 Remove scheduler"),
      line("Adversarial UAT for issue #215: starting independent test run (round 0 of 6)."),
    ],
    // Malformed legacy data must be irrelevant rather than crashing parsing.
    testRuns: { r1: { suites: "not-an-array" } },
  });

  assert.deepEqual(rows.map((row) => row.kind), ["issue", "adversarial"]);
  assert.equal(rows.some((row) => row.kind === "tests"), false);
  const uat = rows.find((row) => row.kind === "adversarial");
  assert.equal(uat.repository, "acme/app");
  assert.equal(uat.title, "Independent test run");
  assert.match(uat.detail, /Round 0 of 6/);

  const kinds = appJs.match(/const NOW_WORKING_KINDS\s*=\s*\{[^}]+\}/)?.[0] || "";
  assert.match(kinds, /adversarial:\s*["']Adversarial UAT["']/);
  assert.doesNotMatch(kinds, /\btests\s*:/);
});
