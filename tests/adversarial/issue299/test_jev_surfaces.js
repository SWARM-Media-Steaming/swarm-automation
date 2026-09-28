"use strict";

/**
 * Issue #299 UI contract: Jev is a decision engine on AI Configuration and
 * Feedback, not a fourth coding agent, and cost-first routing has no user toggle.
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..", "..");
const indexHtml = fs.readFileSync(path.join(root, "ui", "index.html"), "utf8");
const appJs = fs.readFileSync(path.join(root, "ui", "app.js"), "utf8");
const {
  statusLabel,
  tableRows,
  summaryCards,
  connectionStatus,
} = require(path.join(root, "ui", "jev-decision-ui.js"));

function section(html, id) {
  const start = html.indexOf(`id="${id}"`);
  assert.notEqual(start, -1, `${id} must exist`);
  return html.slice(Math.max(0, start - 200), start + 4000);
}

test("AI Configuration exposes a global Jev enable toggle and decision uses", () => {
  const panel = section(indexHtml, "jev-decision-panel");
  assert.match(panel, /data-config="jev_enabled"/);
  assert.match(panel, /Enable Jev Decision Engine/);
  for (const flag of [
    "jev_use_preflight",
    "jev_use_workflow",
    "jev_use_uat",
    "jev_use_cyber",
    "jev_use_rag",
    "jev_use_triage",
    "jev_use_completion",
  ]) {
    assert.match(panel, new RegExp(`data-config="${flag}"`));
  }
  assert.match(panel, /data-config="jev_confidence_automation"/);
  assert.match(panel, /data-config="jev_confidence_fallback"/);
  assert.match(panel, /data-config="jev_confidence_security"/);
  assert.match(panel, /data-config="jev_timeout_seconds"/);
  assert.match(panel, /data-config="jev_max_retries"/);
  assert.match(panel, /data-config="jev_fallback"/);
  assert.match(panel, /id="jev-connection-pill"/);
  assert.match(indexHtml, /data-config="jev_bin"/);
});

test("Optimize routing for cost is not a user-facing AI Configuration control", () => {
  const aiView = indexHtml.slice(indexHtml.indexOf('id="view-ai"'));
  const end = aiView.indexOf("<section");
  const view = end === -1 ? aiView : aiView.slice(0, end);
  assert.doesNotMatch(view, /Optimize routing for cost/);
  assert.doesNotMatch(indexHtml, /data-config="routing_optimization"/);
  assert.match(indexHtml, /cost-first after capability/);
});

test("Jev is not listed as a Claude\/Codex\/Grok implementation provider", () => {
  const meta = appJs.match(/const PROVIDER_META = \{[\s\S]*?\n  \};/);
  assert.ok(meta, "PROVIDER_META must exist");
  assert.match(meta[0], /claude:/);
  assert.match(meta[0], /codex:/);
  assert.match(meta[0], /grok:/);
  assert.doesNotMatch(meta[0], /\n    jev:/);
});

test("Feedback has a Jev scores tab with baseline-only copy and filters", () => {
  assert.match(indexHtml, /id="feedback-tab-jev"/);
  assert.match(indexHtml, /data-feedback-tab="jev"/);
  assert.match(indexHtml, /id="feedback-panel-jev"/);
  const panel = section(indexHtml, "feedback-panel-jev");
  assert.match(panel, /baseline-only/);
  assert.match(panel, /id="jev-feedback-status"/);
  assert.match(panel, /id="jev-feedback-routing"/);
  assert.match(panel, /id="jev-feedback-outcome"/);
  assert.match(panel, /id="jev-feedback-search"/);
  assert.match(panel, /id="jev-feedback-cards"/);
  assert.match(panel, /never counted on failed or retried work/);
  const intro = indexHtml.slice(indexHtml.indexOf('id="view-feedback"'), indexHtml.indexOf('id="view-feedback"') + 2500);
  assert.match(intro, /Jev scores/);
  assert.doesNotMatch(intro, /Three reports over AI work/);
});

test("disabled and fallback Feedback rows never render a zero Jev score", () => {
  assert.equal(statusLabel("disabled"), "Jev off — baseline only");
  const disabled = tableRows([
    {
      jevStatus: "disabled",
      baselineOnly: true,
      jevPresent: false,
      baselineScore: 0.8,
      jevScore: 0,
      modifiedScore: 0.8,
    },
  ]);
  assert.equal(disabled[0].jevScore, "Baseline only");
  const timeout = tableRows([{ jevStatus: "timeout", jevPresent: false, fallback: true, baselineScore: 0.5, jevScore: 0 }]);
  assert.equal(timeout[0].jevScore, "Baseline only");
  assert.equal(timeout[0].fallback, true);
});

test("summary cards lead with completion rate rather than implied savings on failure", () => {
  const cards = summaryCards({
    completionRate: 0.5,
    completed: 1,
    comparisons: 2,
    jevCost: 0.002,
    fallbackRate: 0.25,
    fallbackCount: 1,
    disabledCount: 1,
    outcomes: { retry: 3, failed: 1 },
  });
  assert.equal(cards[0].label, "Completion rate");
  assert.equal(cards[0].value, "50%");
  assert.match(cards[2].hint, /Not counted as cost savings/i);
});

test("connection status distinguishes missing, signed-out, and connected Jev", () => {
  assert.equal(connectionStatus(null).text, "Not checked");
  assert.equal(connectionStatus({ installed: false }).text, "Not installed");
  assert.equal(connectionStatus({ installed: true, authenticated: false }).text, "Sign-in required");
  assert.equal(connectionStatus({ installed: true, authenticated: true }).tone, "running");
});

test("help topics state that Jev recommends and Swarm remains the authority", () => {
  assert.match(appJs, /"jev-decision-engine"\s*:\s*\{/);
  assert.match(appJs, /Jev is a fast typed decision layer/);
  assert.match(appJs, /Swarm remains the orchestration and policy authority/);
  assert.match(appJs, /never suppresses a security finding/);
  assert.match(appJs, /"jev-feedback"\s*:\s*\{/);
  assert.match(appJs, /baseline is never overwritten/);
});
