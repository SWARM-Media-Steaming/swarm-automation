"use strict";

/**
 * Issue #218: the Overview "AI agents" panel must show, per enabled
 * provider, whether it is "currently working an issue right now" — and the
 * status pill is the at-a-glance signal for that (acceptance criteria:
 * "Status ... Currently working: whether this provider is actively working
 * an issue right now, and on which repository/issue if so").
 *
 * `renderAiAgents()` in ui/app.js derives two independent things from the
 * same per-provider data for each row: a human `headline` string and a
 * color-coded `pillState` used for the `.status-pill` class/label. Both are
 * computed from the same `usage` (from the new `check_provider_usage`
 * probe) and `working` (from `aiAgentsWorkingByProvider`, i.e. the existing
 * Now Working log derivation) values, but with a *different* priority
 * order:
 *
 *   headline:  working.length > 0  is checked FIRST ("Working ...")
 *   pillState: usage.status === 2  is checked FIRST ("error")
 *
 * `check_provider_usage_background` polls every 60s independently of
 * whether the worker is mid-cycle for that same provider (see #218's own
 * "invoked on a timer ... not on every render" design). Because the usage
 * probe shells out to the provider's own CLI (`claude -p /usage ...`,
 * `codex_rate_limits.py`, `grok_rate_limits.py`) that CLI can be busy or
 * time out precisely while the provider is actively doing the real issue
 * work, yielding `usage.status === 2` (unavailable) *while the provider is
 * demonstrably working, per the logs*. In that overlap, the row's own text
 * says "Working #N ... in owner/repo" while its status pill simultaneously
 * renders red/"Usage unknown" — a direct self-contradiction of the one
 * thing this panel exists to show. This suite runs the live forEach body
 * out of ui/app.js (no product code is modified) and pins the pill and the
 * headline to agree with each other on "currently working" precedence.
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const appJs = fs.readFileSync(path.join(__dirname, "..", "..", "ui", "app.js"), "utf8");

function extractLiteral(constDeclaration, label) {
  const start = appJs.indexOf(constDeclaration);
  assert.notEqual(start, -1, `ui/app.js must still declare ${label}`);
  const end = appJs.indexOf(";", start) + 1;
  return appJs.slice(start, end);
}

const providerMetaLiteral = extractLiteral("const PROVIDER_META = {", "PROVIDER_META");
const aiAgentPillsLiteral = extractLiteral("const AI_AGENT_PILLS = ", "AI_AGENT_PILLS");

function extractAiAgentsForEachBlock() {
  const forEachStart = appJs.indexOf("enabled.forEach((provider) => {");
  assert.notEqual(forEachStart, -1, "renderAiAgents must walk the enabled-provider list");
  const appendAt = appJs.indexOf("list.appendChild(item);", forEachStart);
  assert.notEqual(appendAt, -1);
  const closeAt = appJs.indexOf("});", appendAt) + 3;
  return appJs.slice(forEachStart, closeAt);
}

function element(tag) {
  return {
    tagName: String(tag).toLowerCase(),
    className: "",
    textContent: "",
    children: [],
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    append(...nodes) {
      for (const node of nodes) {
        if (node && typeof node === "object") this.appendChild(node);
        else if (typeof node === "string") this.textContent += node;
      }
    },
  };
}

// Renders one row through the real ui/app.js forEach body and returns it.
function renderRow({ tool, usage, working }) {
  const list = { children: [], appendChild(child) { this.children.push(child); return child; } };
  const document = { createElement: element };
  const state = { tools: [tool] };
  const usageByProvider = new Map(usage ? [[usage.provider, usage]] : []);
  const workingByProvider = new Map(working && working.length ? [[tool.id, working]] : []);
  const run = new Function(
    "enabled",
    "document",
    "list",
    "state",
    "usageByProvider",
    "workingByProvider",
    `${providerMetaLiteral}
     ${aiAgentPillsLiteral}
     ${extractAiAgentsForEachBlock()}
     return list.children;`,
  );
  const rendered = run([{ id: tool.id, enabled: true }], document, list, state, usageByProvider, workingByProvider);
  assert.equal(rendered.length, 1);
  const row = rendered[0];
  const words = row.children.find((child) => child.className === "now-working-words");
  const pill = row.children.find((child) => String(child.className).startsWith("status-pill"));
  return {
    headline: words.children[0].textContent,
    pillClass: pill.className,
    pillText: pill.textContent,
  };
}

const readyTool = { id: "claude", installed: true, authenticated: true, status: "Installed & signed in" };

test("a provider with healthy usage and no active work renders an idle pill", () => {
  const { headline, pillClass, pillText } = renderRow({
    tool: readyTool,
    usage: { provider: "claude", status: 0, remainingPercent: 82, detail: "session 82% / week 95% remaining" },
    working: [],
  });
  assert.equal(headline, "Idle");
  assert.equal(pillClass, "status-pill idle");
  assert.equal(pillText, "Idle");
});

test("a provider whose usage probe fails while it is not working renders the error pill", () => {
  const { headline, pillClass, pillText } = renderRow({
    tool: readyTool,
    usage: { provider: "claude", status: 2, remainingPercent: null, detail: null },
    working: [],
  });
  assert.equal(headline, "Usage unknown");
  assert.equal(pillClass, "status-pill error");
  assert.equal(pillText, "Usage unknown");
});

test("a provider actively working an issue must show a Working pill, even if its own usage probe fails concurrently", () => {
  const working = [{ title: "#84 Reduce logs", repository: "acme/app" }];
  const { headline, pillClass, pillText } = renderRow({
    tool: readyTool,
    // Simulates the check_provider_usage race: the periodic quota probe
    // shells out to the same CLI the worker is using right now for real
    // issue work, and that probe times out/fails mid-work.
    usage: { provider: "claude", status: 2, remainingPercent: null, detail: null },
    working,
  });
  assert.equal(headline, "Working #84 Reduce logs in acme/app");
  // The pill must agree with the headline about "currently working" — a
  // provider the panel itself reports as working cannot simultaneously
  // render as an error/unknown-usage pill.
  assert.equal(pillClass, "status-pill running", `headline said "${headline}" but pill was "${pillClass}"`);
  assert.equal(pillText, "Working");
});

test("a provider actively working an issue must show a Working pill even when quota is merely low, not the low-quota pill", () => {
  const working = [{ title: "#12 Add retries", repository: "acme/site" }];
  const { headline, pillClass, pillText } = renderRow({
    tool: readyTool,
    usage: { provider: "claude", status: 1, remainingPercent: 3, detail: "session 3% / week 40% remaining" },
    working,
  });
  assert.equal(headline, "Working #12 Add retries in acme/site");
  assert.equal(pillClass, "status-pill running", `headline said "${headline}" but pill was "${pillClass}"`);
  assert.equal(pillText, "Working");
});
