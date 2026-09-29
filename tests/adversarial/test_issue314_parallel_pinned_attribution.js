"use strict";

// Parallel scheduler output can interleave issues with the same number in
// different repositories. A pinned continuation belongs to its labeled
// repository, even when another repository was selected more recently.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { deriveNowWorking } = require("../../ui/now-working.js");

const repositories = [
  { id: "app", name: "acme/app", monitorActions: false },
  { id: "site", name: "acme/site", monitorActions: false },
];
const line = (repository, message) =>
  `[12:34:56] [Issue worker scheduler/stdout] [${repository}] [2026-09-29 12:34:56-0500] ${message}`;

for (const stage of [
  { label: "Adversarial UAT", kind: "adversarial" },
  { label: "Adversarial Cybersecurity", kind: "security" },
]) {
  for (const restarted of [false, true]) {
    test(`pinned ${stage.label} continuation updates only its labeled repository${restarted ? " after restart" : ""}`, () => {
      const logs = [
        line("acme/app", "Selected oldest unprocessed assigned issue: #314 App work"),
        line("acme/app", `${stage.label} for issue #314: starting re-test for round 1 of 3.`),
        line("acme/app", `${stage.label} for issue #314: tester Codex model gpt-5.6 with effort medium.`),
        ...(restarted ? [line("acme/app", "acme/app: worker exited with status 1; will retry."),
          line("acme/app", "Starting a cycle over 1 repository(ies)."),
          line("acme/app", "Selected oldest unprocessed assigned issue: #314 App work")] : []),
        line("acme/site", "Selected oldest unprocessed assigned issue: #314 Site work"),
        line("acme/site", `${stage.label} for issue #314: starting re-test for round 2 of 3.`),
        line("acme/site", `${stage.label} for issue #314: tester Grok model grok-4.6 with effort high.`),
        // The app worker resumes after the site worker's selection. The source
        // label identifies its repository; the issue number alone cannot.
        line("acme/app", "Pinned Claude model claude-sonnet-5 session claude:314 with effort low for this continuation."),
      ];
      const rows = deriveNowWorking({ workerState: "running", repositories, logs });
      const app = rows.find((row) => row.kind === stage.kind && row.repository === "acme/app");
      const site = rows.find((row) => row.kind === stage.kind && row.repository === "acme/site");
      assert.ok(app, "app stage must remain visible");
      assert.ok(site, "site stage must remain visible");
      assert.deepEqual(app.attribution,
        { role: "tester", provider: "Claude", model: "claude-sonnet-5", effort: "low" });
      assert.deepEqual(site.attribution,
        { role: "tester", provider: "Grok", model: "grok-4.6", effort: "high" });
      assert.match(app.detail, /Claude · claude-sonnet-5 · low reasoning/);
      assert.match(site.detail, /Grok · grok-4\.6 · high reasoning/);
    });
  }
}
