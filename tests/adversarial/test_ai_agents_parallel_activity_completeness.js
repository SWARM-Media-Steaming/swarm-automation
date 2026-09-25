"use strict";

/**
 * Issue #218 requires the consolidated panel to say which repository and
 * issue every provider is working across all configured repositories.
 * `--parallel-repos` launches one worker per repository, so two important
 * consequences follow from the repository's own scheduler contract:
 *
 * 1. One repository worker exiting must not erase a sibling repository that
 *    is still active.
 * 2. The same enabled provider can be selected concurrently by more than one
 *    repository worker, and every active repository/issue location must be
 *    visible rather than replaced by an anonymous count.
 *
 * These tests replay deterministic production-shaped log lines through the
 * real Now Working parser and execute the AI-agent row renderer from app.js.
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.join(__dirname, "..", "..");
const { deriveNowWorking } = require(path.join(repoRoot, "ui", "now-working.js"));
const appJs = fs.readFileSync(path.join(repoRoot, "ui", "app.js"), "utf8");

const repositories = [
  { id: "app", name: "acme/app", monitorActions: false },
  { id: "site", name: "acme/site", monitorActions: false },
];

function childLine(repository, message, second) {
  const time = String(second).padStart(2, "0");
  return `[12:34:${time}] [Issue worker scheduler/stdout] [${repository}] [2026-09-23 12:34:${time}-0500] ${message}`;
}

function schedulerLine(message, second) {
  return `[12:34:${String(second).padStart(2, "0")}] [Issue worker scheduler/stdout] ${message}`;
}

function issue(rows, repository, number) {
  return rows.find((row) => row.kind === "issue"
    && row.repository === repository
    && String(row.issueNumber) === String(number));
}

test("one parallel repository exiting does not erase a sibling repository that is still working", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories,
    logs: [
      childLine("acme/app", "Selected oldest unprocessed assigned issue: #21 App work", 1),
      childLine("acme/app", "Selected Claude model claude-sonnet-5 with effort high for this run.", 2),
      childLine("acme/site", "Selected oldest unprocessed assigned issue: #34 Site work", 3),
      childLine("acme/site", "Selected Codex model gpt-5.6-luna with effort medium for this run.", 4),
      // Runner.work_repo emits this repository-qualified line as soon as one
      // parallel child exits; the other child can still be running.
      schedulerLine("acme/app: worker exited with status 1; will retry.", 5),
    ],
  });

  assert.equal(issue(rows, "acme/app", 21), undefined, "the exited repository is no longer active");
  const site = issue(rows, "acme/site", 34);
  assert.ok(site, "an exit from acme/app must not clear acme/site's active work");
  assert.equal(site.provider, "Codex");
  assert.equal(site.state, "running");
});

function extractLiteral(declaration, label) {
  const start = appJs.indexOf(declaration);
  assert.notEqual(start, -1, `ui/app.js must declare ${label}`);
  const end = appJs.indexOf(";", start);
  assert.notEqual(end, -1, `${label} declaration must be terminated`);
  return appJs.slice(start, end + 1);
}

function extractAiAgentsForEachBlock() {
  const start = appJs.indexOf("enabled.forEach((provider) => {");
  assert.notEqual(start, -1, "renderAiAgents must render each enabled provider");
  const append = appJs.indexOf("list.appendChild(item);", start);
  assert.notEqual(append, -1, "renderAiAgents must append the provider row");
  const end = appJs.indexOf("});", append);
  assert.notEqual(end, -1, "enabled-provider renderer must close");
  return appJs.slice(start, end + 3);
}

function element(tagName) {
  return {
    tagName,
    className: "",
    textContent: "",
    children: [],
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    append(...children) {
      children.forEach((child) => this.appendChild(child));
    },
  };
}

function renderClaudeHeadline(working) {
  const document = { createElement: element };
  const list = { children: [], appendChild(child) { this.children.push(child); return child; } };
  const state = {
    tools: [{ id: "claude", installed: true, authenticated: true, status: "Signed in" }],
  };
  const usageByProvider = new Map([[
    "claude",
    { provider: "claude", status: 0, remainingPercent: 82, detail: "session 82% / week 95% remaining" },
  ]]);
  const workingByProvider = new Map([["claude", working]]);
  const render = new Function(
    "enabled",
    "document",
    "list",
    "state",
    "usageByProvider",
    "workingByProvider",
    `${extractLiteral("const PROVIDER_META = {", "PROVIDER_META")}
     ${extractLiteral("const AI_AGENT_PILLS = ", "AI_AGENT_PILLS")}
     ${extractAiAgentsForEachBlock()}
     return list.children;`,
  );
  const rows = render(
    [{ id: "claude", enabled: true }],
    document,
    list,
    state,
    usageByProvider,
    workingByProvider,
  );
  assert.equal(rows.length, 1);
  const words = rows[0].children.find((child) => child.className === "now-working-words");
  return words.children[0].textContent;
}

test("one provider working parallel repositories identifies every repository and issue", () => {
  const headline = renderClaudeHeadline([
    { title: "#21 App work", repository: "acme/app" },
    { title: "#34 Site work", repository: "acme/site" },
  ]);

  assert.match(headline, /#21\b/, "the first active issue number must be named");
  assert.match(headline, /acme\/app\b/, "the first active repository must be named");
  assert.match(headline, /#34\b/, "the second active issue number must be named, not reduced to '+1 more'");
  assert.match(headline, /acme\/site\b/, "the second active repository must be named");
});
