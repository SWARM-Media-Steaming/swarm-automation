const test = require("node:test");
const assert = require("node:assert/strict");
const {
  confidenceLabel,
  hasNoActiveProblems,
  unavailableMessage,
  isFromCache,
  isCanned,
  isUnavailable,
  canFileIssue,
  confirmFileIssuePrompt,
  actionableList,
  evidenceList,
} = require("./debug-diagnose.js");

const problem = (overrides = {}) => ({
  problemId: "p1",
  repository: "SWARM-Media-Steaming/swarm",
  source: "ai",
  provider: "Claude",
  model: "claude-sonnet-5",
  explanation: "It broke.",
  confidence: "high",
  evidence: [{ source: "cron.log", excerpt: "ERROR: boom" }],
  actionableItems: ["Retry"],
  isBug: false,
  suggestedIssueTitle: null,
  suggestedIssueBody: null,
  filedIssueUrl: null,
  ...overrides,
});

test("confidenceLabel maps each known level to a readable label", () => {
  assert.equal(confidenceLabel(problem({ confidence: "high" })), "High confidence");
  assert.equal(confidenceLabel(problem({ confidence: "medium" })), "Medium confidence");
  assert.equal(
    confidenceLabel(problem({ confidence: "low" })),
    "Low confidence — take with a grain of salt"
  );
  assert.equal(confidenceLabel(problem({ confidence: "" })), "Confidence unknown");
});

test("hasNoActiveProblems is true only for an empty problems array", () => {
  assert.equal(hasNoActiveProblems({ problems: [] }), true);
  assert.equal(hasNoActiveProblems({ problems: [problem()] }), false);
  assert.equal(hasNoActiveProblems({}), false);
  assert.equal(hasNoActiveProblems(null), false);
});

test("unavailableMessage includes the reason when given", () => {
  assert.equal(
    unavailableMessage({ unavailableReason: "No AI provider currently has usage remaining." }),
    "AI explanation is unavailable right now: No AI provider currently has usage remaining."
  );
  assert.equal(
    unavailableMessage({ unavailableReason: null }),
    "AI explanation is unavailable right now."
  );
});

test("source helpers distinguish cache/canned/unavailable problems", () => {
  assert.equal(isFromCache(problem({ source: "cache" })), true);
  assert.equal(isFromCache(problem({ source: "ai" })), false);
  assert.equal(isCanned(problem({ source: "canned" })), true);
  assert.equal(isCanned(problem({ source: "ai" })), false);
  assert.equal(isUnavailable(problem({ source: "unavailable" })), true);
  assert.equal(isUnavailable(problem({ source: "ai" })), false);
});

test("canFileIssue requires is_bug true and no existing filed URL", () => {
  assert.equal(canFileIssue(problem({ isBug: true, filedIssueUrl: null })), true);
  assert.equal(canFileIssue(problem({ isBug: false, filedIssueUrl: null })), false);
  assert.equal(
    canFileIssue(problem({ isBug: true, filedIssueUrl: "https://example.invalid/issues/1" })),
    false
  );
});

test("confirmFileIssuePrompt names the affected repository", () => {
  const message = confirmFileIssuePrompt(problem({ repository: "SWARM-Media-Steaming/swarm" }));
  assert.match(message, /SWARM-Media-Steaming\/swarm/);
  assert.match(message, /SWARM-Media-Steaming\/swarm-automation/);
  assert.match(message, /unassigned/i);
});

test("actionableList and evidenceList default to empty arrays", () => {
  assert.deepEqual(actionableList(problem({ actionableItems: undefined })), []);
  assert.deepEqual(actionableList(problem({ actionableItems: ["Retry", "Wait"] })), [
    "Retry",
    "Wait",
  ]);
  assert.deepEqual(evidenceList(problem({ evidence: undefined })), []);
  assert.deepEqual(evidenceList({}), []);
});
