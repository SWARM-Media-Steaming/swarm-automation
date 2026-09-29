const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const delivery = require("./adversarial-delivery.js");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const app = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "style.css"), "utf8");

function panel(id, until) {
  const start = html.indexOf(`id="${id}"`);
  const end = html.indexOf(`id="${until}"`);
  assert.ok(start >= 0 && end > start, `${id} panel is present`);
  return html.slice(start, end);
}

test("the best-effort toggle lives on pull request automation and defaults off", () => {
  const deliveryPanel = panel("repository-panel-delivery", "view-ai");
  const queuePanel = panel("repository-panel-queue", "repository-panel-delivery");
  assert.match(deliveryPanel, /data-repo-config="adversarial_best_effort_merge"/);
  assert.match(deliveryPanel, /Allow best-effort adversarial merge after 3 rounds/);
  assert.match(deliveryPanel, /Enabling this may merge code with unresolved adversarial tests or actionable security findings\./);
  assert.doesNotMatch(queuePanel, /adversarial_best_effort_merge/);
  assert.match(app, /adversarial_best_effort_merge: false/);
  assert.match(css, /\.best-effort-toggle:has\(input:checked\)/);
  assert.match(css, /\.best-effort-toggle \.warning-copy/);
});

test("execution history can separate verified-clean merges from best-effort merges", () => {
  const history = panel("view-feedback", "view-guides");
  assert.match(history, /id="execution-history-delivery"/);
  assert.match(history, /value="verified_clean"/);
  assert.match(history, /value="best_effort"/);
  assert.match(app, /executionHistoryDelivery: "all"/);
  assert.match(app, /delivery: state\.executionHistoryDelivery \|\| "all"/);
  assert.match(app, /No executions match this merge filter\./);
});

test("a best-effort execution lists unresolved suites and findings before and after merge", () => {
  const lines = delivery.paragraphs({
    adversarialMergePolicy: "best_effort",
    adversarialDelivery: "best_effort",
    adversarialEpochCount: 1,
    securityEpochCount: 1,
    promotionStatus: "promoted",
    promotionUrl: "https://example.invalid/pull/9",
    adversarialUnresolved: {
      before_merge: {
        suites: [{ id: "adversarial-180", stage: "uat" }],
        findings: [{ title: "Service token is hardcoded", severity: "High", stage: "security" }],
      },
      after_merge: {
        merged: true,
        integration_branch: "ai-main",
        merged_sha: "abcdef1234567890",
        promotion: "promoted",
        promotion_url: "https://example.invalid/pull/9",
        suites: [{ id: "adversarial-180" }],
        findings: [{ title: "Service token is hardcoded" }],
      },
    },
    adversarialEpochs: [{
      stage: "uat", epoch_number: 1, outcome: "best_effort", escalation_reason: "epoch_exhausted",
    }],
  });
  const text = lines.map(([label, value]) => `${label}: ${value}`).join("\n");
  assert.match(text, /Adversarial merge policy: Allow best-effort adversarial merge after 3 rounds/);
  assert.match(text, /Delivery: Best-effort merge with unresolved adversarial results/);
  assert.match(text, /Unresolved before merge: 1 failing suite \(adversarial-180\); 1 open finding \(Service token is hardcoded\)/);
  assert.match(text, /Unresolved after merge: 1 failing suite \(adversarial-180\); 1 open finding \(Service token is hardcoded\)/);
  assert.match(text, /merged into ai-main as abcdef123456/);
  assert.match(text, /promotion promoted https:\/\/example\.invalid\/pull\/9/);
  assert.match(text, /UAT 1 · cybersecurity 1/);
  assert.equal(delivery.summaryChip({ adversarialDelivery: "best_effort" }).text,
    "Best-effort merge with unresolved adversarial results");
  assert.equal(delivery.summaryChip({ adversarialDelivery: "best_effort" }).warning, true);
});

test("a verified-clean execution is labelled separately from strict policy", () => {
  const lines = delivery.paragraphs({
    adversarialMergePolicy: "strict",
    adversarialDelivery: "verified_clean",
    adversarialEpochCount: 2,
    promotionStatus: "not_configured",
  });
  const text = lines.map(([label, value]) => `${label}: ${value}`).join("\n");
  assert.match(text, /Strict — merge only after adversarial acceptance/);
  assert.match(text, /Delivery: Verified-clean merge/);
  assert.match(text, /Adversarial epochs: UAT 2/);
  assert.match(text, /Promotion: not_configured/);
  assert.equal(delivery.summaryChip({ adversarialDelivery: "verified_clean" }).warning, false);
  assert.match(delivery.repositoryPolicy(false), /Strict adversarial merge/);
  assert.match(delivery.repositoryPolicy(true), /Allow best-effort adversarial merge after 3 rounds/);
});
