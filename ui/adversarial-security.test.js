const { test } = require("node:test");
const assert = require("node:assert/strict");
const security = require("./adversarial-security.js");
const { deriveNowWorking } = require("./now-working.js");

const line = (message, source = "Issue worker scheduler") =>
  `[12:34:56] [${source}/stdout] [2026-09-15 12:34:56-0500] ${message}`;
const repo = { id: "r1", name: "acme/app", monitorActions: false };

test("a review that could not run never renders like a clean pass", () => {
  assert.equal(security.reviewStatus({}), "—");
  assert.equal(security.reviewStatus({securityOutcome: "disabled"}), "—");
  assert.equal(security.reviewStatus({securityOutcome: "clean_first_pass", securityReviewStatus: "PASS"}), "PASS");
  assert.equal(security.reviewStatus({securityOutcome: "cap_hit", securityReviewStatus: "FAILED"}), "FAILED");
  // A recorded review with no status is treated as failed, not as a pass.
  assert.equal(security.reviewStatus({securityOutcome: "resolved_after_n"}), "FAILED");
});

test("legacy and disabled history stay distinct from a zero-round clean review", () => {
  assert.equal(security.roundCount({}), "—");
  assert.equal(security.roundCount({securityOutcome: "disabled", securityRoundCount: 0}), "—");
  assert.equal(security.roundCount({securityOutcome: "clean_first_pass", securityRoundCount: 0}), "0");
  assert.equal(security.roundCount({securityOutcome: "cap_hit", securityRoundCount: 6}), "6");
});

test("finding counts carry severity and separate fixed from still-open", () => {
  assert.equal(security.findingsSummary({}), "");
  const text = security.findingsSummary({securityFindings: {
    inScopeDiscovered: 3, inScopeFixed: 2, inScopeOpen: 1, issuesCreated: 1,
    severity: {Critical: 1, High: 1, Medium: 1, Low: 0},
  }});
  assert.match(text, /3 in-scope findings/);
  assert.match(text, /2 fixed/);
  assert.match(text, /1 unresolved/);
  assert.match(text, /1 follow-up issue\b/);
  assert.match(text, /Critical 1 · High 1 · Medium 1 · Low 0/);
});

test("the aggregate reports remediation and failure rates, not just a count", () => {
  assert.match(security.aggregate(null), /No adversarial cybersecurity/);
  const text = security.aggregate({reviews: 20, averageRounds: 0.8, passPercent: 70,
    fixedPercent: 20, findingsCreatedPercent: 5, failedPercent: 5, findingsFound: 9, findingsFixed: 8});
  assert.match(text, /0.8 average fix\/re-test rounds per review/);
  assert.match(text, /70% clean/);
  assert.match(text, /5% unresolved or failed \(20 reviews, 8\/9 in-scope findings fixed\)/);
});

test("round evidence shows findings open, verified fixed, and filed separately", () => {
  const text = security.roundDetail({fixer_provider: "Claude", fixer_model: "fix",
    tester_provider: "Codex", tester_model: "review", findings_found: 2, findings_fixed: 1,
    findings_filed: 3, tests_added: 1, tests_modified: 0,
    tests_failing_before: 2, tests_failing_after: 0});
  assert.match(text, /Claude \/ fix → Codex \/ review/);
  assert.match(text, /2 finding\(s\) open, 1 verified fixed, 3 filed separately/);
  assert.match(text, /failing suites 2 → 0/);
});

test("Overview tracks a security review as its own row beside the issue", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [repo],
    logs: [
      line("Selected oldest unprocessed assigned issue: #278 Add a client"),
      line("Adversarial Cybersecurity for issue #278: starting independent security review (round 0 of 6)."),
    ],
  });
  assert.equal(rows.length, 2);
  const review = rows.find((row) => row.kind === "security");
  assert.ok(review, "the security review is its own Overview row");
  assert.equal(review.title, "Independent security review");
  assert.match(review.detail, /Round 0 of 6/);
  assert.equal(review.state, "running");
  assert.equal(rows.find((row) => row.kind === "issue").title, "#278 Add a client");
});

test("UAT and security reviews of one issue are separate rows, not one overwriting the other", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [repo],
    logs: [
      line("Selected oldest unprocessed assigned issue: #278 Add a client"),
      line("Adversarial UAT for issue #278: starting independent test run (round 0 of 6)."),
      line("Adversarial Cybersecurity for issue #278: starting fix/re-test round 2 of 6."),
    ],
  });
  assert.deepEqual(rows.map((row) => row.kind).sort(), ["adversarial", "issue", "security"]);
  const review = rows.find((row) => row.kind === "security");
  assert.equal(review.title, "Fix/re-test round 2 of 6");
  assert.match(review.detail, /Fix in progress/);
});

test("a quota pause on the issue pauses its security review row too", () => {
  const rows = deriveNowWorking({
    workerState: "paused",
    repositories: [repo],
    logs: [
      line("Selected oldest unprocessed assigned issue: #278 Add a client"),
      line("Adversarial Cybersecurity for issue #278: starting re-test for round 1 of 6."),
      line("Paused issue #278 because Claude usage is unavailable"),
    ],
  });
  assert.equal(rows.find((row) => row.kind === "security").state, "paused");
});
