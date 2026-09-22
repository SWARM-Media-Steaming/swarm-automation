const { test } = require("node:test");
const assert = require("node:assert/strict");
const uat = require("./adversarial-uat.js");

test("legacy and disabled history stay distinct from a zero-round clean pass", () => {
  assert.equal(uat.roundCount({}), "—");
  assert.equal(uat.roundCount({adversarialOutcome: "disabled", adversarialRoundCount: 0}), "—");
  assert.equal(uat.roundCount({adversarialOutcome: "clean_first_pass", adversarialRoundCount: 0}), "0");
  assert.equal(uat.roundCount({adversarialOutcome: "cap_hit", adversarialRoundCount: 6}), "6");
});

test("history aggregates explicitly describe the work-round denominator", () => {
  assert.match(uat.aggregate(null), /No adversarial/);
  const text = uat.aggregate({loops: 20, averageRounds: 1.2, cleanFirstPassPercent: 75, capHitPercent: 5});
  assert.match(text, /1.2 average fix\/re-test rounds per work-round/);
  assert.match(text, /75% clean first pass/);
  assert.match(text, /5% cap hit \(20 work-rounds\)/);
});

test("quota snapshots never masquerade as metered monetary or token costs", () => {
  assert.equal(uat.capacity(null), "");
  assert.equal(uat.capacity(NaN), "");
  assert.match(uat.capacity(0), /^0.0 percentage points/);
  assert.match(uat.capacity(120.25), /120.3 percentage points across providers; approximate/);
  assert.match(uat.capacity(4), /not token or dollar cost/);
});

test("round evidence shows provider pairing, suite failures and dispute outcome", () => {
  const text = uat.roundDetail({fixer_provider: "Claude", fixer_model: "fix", tester_provider: "Codex", tester_model: "test",
    tests_added: 2, tests_modified: 1, tests_failing_before: 3, tests_failing_after: 0, disputed: true,
    dispute_resolution: "revised against issue specification"});
  assert.match(text, /Claude \/ fix → Codex \/ test/);
  assert.match(text, /failing suites 3 → 0/);
  assert.match(text, /dispute: revised against issue specification/);
});
