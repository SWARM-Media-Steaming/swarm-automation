"use strict";

/**
 * Issue #213: Overview "Now Working" must show the selected AI provider,
 * model, and reasoning effort on the active issue row, and a live
 * Adversarial UAT row for the current fix/re-test round.
 *
 * The worker has no structured "current work" channel. The panel replays
 * issue-worker stdout. These tests feed the exact log lines the worker
 * emits (`Selected {provider} model {model} with effort {effort} for this
 * run.` and `Adversarial UAT for issue #N: ...`) through the live
 * `deriveNowWorking` parser and the Overview render path.
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { deriveNowWorking } = require(path.join(__dirname, "..", "..", "ui", "now-working.js"));

const line = (message, source = "Issue worker scheduler") =>
  `[12:34:56] [${source}/stdout] [2026-09-15 12:34:56-0500] ${message}`;
const labeled = (label, message) =>
  `[12:34:56] [Issue worker scheduler/stdout] [${label}] [2026-09-15 12:34:56-0500] ${message}`;

const repo = { id: "r1", name: "acme/app", monitorActions: false };
const site = { ...repo, id: "r2", name: "acme/site" };

function derive(logs, extra = {}) {
  return deriveNowWorking({
    workerState: "running",
    repositories: [repo],
    logs,
    ...extra,
  });
}

function selected(provider, model, effort) {
  return `Selected ${provider} model ${model} with effort ${effort} for this run.`;
}

function issueRow(rows, number = "84") {
  return rows.find(
    (row) => row.kind === "issue" && String(row.issueNumber || row.title).includes(String(number)),
  );
}

function adversarialRow(rows, number) {
  return rows.find((row) => {
    if (row.kind !== "adversarial") return false;
    if (number == null) return true;
    return String(row.issueNumber) === String(number);
  });
}

test("an active issue row shows provider, model, and effort from the worker selection log", () => {
  const rows = derive([
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line(selected("Claude", "claude-sonnet-5", "high")),
    line("Claude is working. Detailed implementation output is hidden."),
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].kind, "issue");
  assert.equal(rows[0].title, "#84 Reduce logs");
  assert.match(rows[0].detail, /\bClaude\b/);
  assert.match(rows[0].detail, /\bclaude-sonnet-5\b/);
  assert.match(rows[0].detail, /\bhigh effort\b/);
  assert.equal(
    rows[0].detail,
    "Claude · claude-sonnet-5 · high effort · Claude is writing the change",
  );
});

test("Codex and Grok selection logs populate model and effort the same way", () => {
  for (const [provider, model, effort] of [
    ["Codex", "gpt-5.4", "medium"],
    ["Grok", "grok-4-fast", "low"],
  ]) {
    const rows = derive([
      line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
      line(selected(provider, model, effort)),
    ]);
    assert.equal(rows.length, 1, provider);
    assert.match(rows[0].detail, new RegExp(`\\b${provider}\\b`));
    assert.match(rows[0].detail, new RegExp(`\\b${model.replace(/\./g, "\\.")}\\b`));
    assert.match(rows[0].detail, new RegExp(`\\b${effort} effort\\b`));
    assert.doesNotMatch(rows[0].detail, / ·  · /);
  }
});

test("xhigh effort and hyphenated model ids survive the detail join", () => {
  const rows = derive([
    line("Selected oldest unprocessed assigned issue: #7 Fix it"),
    line(selected("Claude", "claude-sonnet-4-5", "xhigh")),
  ]);
  assert.match(rows[0].detail, /\bclaude-sonnet-4-5\b/);
  assert.match(rows[0].detail, /\bxhigh effort\b/);
});

test("selection without a later 'is working' line still shows model and effort", () => {
  const rows = derive([
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line(selected("Claude", "claude-sonnet-5", "high")),
  ]);
  assert.match(rows[0].detail, /\bClaude\b/);
  assert.match(rows[0].detail, /\bclaude-sonnet-5\b/);
  assert.match(rows[0].detail, /\bhigh effort\b/);
  assert.match(rows[0].detail, /Picked up from the queue/);
});

test("a later phase update keeps the already-captured model and effort", () => {
  const rows = derive([
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line(selected("Claude", "claude-sonnet-5", "high")),
    line("Created issue branch ai/claude/issue-84"),
  ]);
  assert.equal(
    rows[0].detail,
    "Claude · claude-sonnet-5 · high effort · Issue branch ready",
  );
});

test("a follow-up rework row also shows the selection's model and effort", () => {
  const rows = derive([
    line("Selected issue #85 for rework after GitHub follow-up comment 123: Fix it"),
    line(selected("Grok", "grok-4", "high")),
  ]);
  assert.equal(rows[0].kind, "issue");
  assert.match(rows[0].detail, /\bGrok\b/);
  assert.match(rows[0].detail, /\bgrok-4\b/);
  assert.match(rows[0].detail, /\bhigh effort\b/);
});

test("independent adversarial assessment (round 0 of 6) is a live adversarial row", () => {
  const rows = derive([
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line("Adversarial UAT for issue #84: starting independent test run (round 0 of 6)."),
  ]);
  const adversarial = adversarialRow(rows, "84");
  assert.ok(adversarial, "round-zero independent UAT must appear in Now Working");
  assert.equal(adversarial.kind, "adversarial");
  assert.match(`${adversarial.title} ${adversarial.detail}`, /independent/i);
  assert.match(`${adversarial.title} ${adversarial.detail}`, /0 of 6/);
  assert.equal(adversarial.repository, "acme/app");
  assert.equal(adversarial.state, "running");
});

test("fix/re-test progress uses the current round in the row title", () => {
  const rows = derive([
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line("Adversarial UAT for issue #84: starting fix/re-test round 3 of 6."),
  ]);
  const adversarial = adversarialRow(rows, "84");
  assert.ok(adversarial);
  assert.equal(adversarial.title, "Fix/re-test round 3 of 6");
  assert.ok(String(adversarial.detail || "").trim(), "live progress needs a current-step detail");
});

test("the latest adversarial boundary log wins, and each step is distinguishable", () => {
  const startLogs = [
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line("Adversarial UAT for issue #84: starting independent test run (round 0 of 6)."),
    line("Adversarial UAT for issue #84: starting fix/re-test round 3 of 6."),
  ];
  const appliedLogs = [
    ...startLogs,
    line("Adversarial UAT for issue #84: fix applied in round 3 of 6."),
  ];
  const retestLogs = [
    ...appliedLogs,
    line("Adversarial UAT for issue #84: starting re-test for round 3 of 6."),
  ];
  const started = adversarialRow(derive(startLogs), "84");
  const applied = adversarialRow(derive(appliedLogs), "84");
  const retest = adversarialRow(derive(retestLogs), "84");
  assert.equal(started.title, "Fix/re-test round 3 of 6");
  assert.equal(applied.title, "Fix/re-test round 3 of 6");
  assert.equal(retest.title, "Fix/re-test round 3 of 6");
  assert.notEqual(started.detail, applied.detail);
  assert.notEqual(applied.detail, retest.detail);
  assert.notEqual(started.detail, retest.detail);
  assert.match(String(retest.detail), /re-test/i);
});

test("first counted round and the six-round cap both parse", () => {
  const first = adversarialRow(
    derive([
      line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
      line("Adversarial UAT for issue #84: starting fix/re-test round 1 of 6."),
    ]),
    "84",
  );
  const last = adversarialRow(
    derive([
      line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
      line("Adversarial UAT for issue #84: starting re-test for round 6 of 6."),
    ]),
    "84",
  );
  assert.equal(first.title, "Fix/re-test round 1 of 6");
  assert.equal(last.title, "Fix/re-test round 6 of 6");
});

test("an adversarial row sits alongside the issue row, not in place of it", () => {
  const rows = derive([
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line(selected("Claude", "claude-sonnet-5", "high")),
    line("Adversarial UAT for issue #84: starting re-test for round 3 of 6."),
  ]);
  const issue = issueRow(rows, "84");
  const adversarial = adversarialRow(rows, "84");
  assert.ok(issue, "the issue row must remain while UAT is in progress");
  assert.ok(adversarial, "UAT progress must be its own row kind");
  assert.equal(issue.kind, "issue");
  assert.equal(adversarial.kind, "adversarial");
  assert.match(issue.detail, /\bclaude-sonnet-5\b/);
  assert.equal(adversarial.title, "Fix/re-test round 3 of 6");
});

test("finishing the issue drops the adversarial progress row with it", () => {
  const rows = derive([
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line("Adversarial UAT for issue #84: starting re-test for round 3 of 6."),
    line("Finished issue #84 with Claude: done"),
  ]);
  assert.equal(adversarialRow(rows, "84"), undefined);
  assert.equal(issueRow(rows, "84"), undefined);
});

test("a stopped worker hides adversarial progress the same way it hides issues", () => {
  const logs = [
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line("Adversarial UAT for issue #84: starting fix/re-test round 2 of 6."),
  ];
  assert.deepEqual(derive(logs, { workerState: "stopped" }), []);
});

test("quota-pause of an issue in UAT must not leave adversarial UAT marked running", () => {
  const rows = derive([
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line("Adversarial UAT for issue #84: starting fix/re-test round 3 of 6."),
    line("Paused issue #84 because Codex usage is unavailable; session abc was preserved."),
  ]);
  const issue = issueRow(rows, "84");
  const adversarial = adversarialRow(rows, "84");
  assert.ok(issue);
  assert.equal(issue.state, "paused");
  assert.match(issue.detail, /Waiting for Codex usage/);
  assert.ok(
    adversarial,
    "the current UAT round is still the work in progress; hiding it loses the live round",
  );
  assert.equal(
    adversarial.state,
    "paused",
    `adversarial row stayed ${adversarial.state} after the issue was quota-paused; Now Working would show UAT as currently running`,
  );
  assert.equal(adversarial.title, "Fix/re-test round 3 of 6");
});

test("resuming that issue returns the adversarial row to running on the same round", () => {
  const rows = derive([
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line("Adversarial UAT for issue #84: starting fix/re-test round 3 of 6."),
    line("Paused issue #84 because Codex usage is unavailable; session abc was preserved."),
    line("Codex usage is available again; preparing to resume session abc for issue #84."),
  ]);
  const issue = issueRow(rows, "84");
  const adversarial = adversarialRow(rows, "84");
  assert.equal(issue.state, "running");
  assert.ok(adversarial, "resume must keep the current UAT round visible");
  assert.equal(adversarial.state, "running");
  assert.equal(adversarial.title, "Fix/re-test round 3 of 6");
});

test("a later 'is working' line updates the issue phase and leaves the UAT round intact", () => {
  const rows = derive([
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line(selected("Claude", "claude-sonnet-5", "high")),
    line("Adversarial UAT for issue #84: starting fix/re-test round 2 of 6."),
    line("Claude is working. Detailed implementation output is hidden."),
  ]);
  const issue = issueRow(rows, "84");
  const adversarial = adversarialRow(rows, "84");
  assert.match(issue.detail, /\bclaude-sonnet-5\b/);
  assert.match(issue.detail, /Claude is writing the change/);
  assert.equal(adversarial.kind, "adversarial");
  assert.equal(adversarial.title, "Fix/re-test round 2 of 6");
  assert.doesNotMatch(String(adversarial.detail), /writing the change/i);
});

test("CI failure rows still appear, and still receive model/effort from the selection log", () => {
  const rows = derive(
    [
      line("Working CI failure issue #12 filed by the Actions monitor: Fix failing CI on ai-main: Build"),
      line(selected("Codex", "gpt-5.4", "high")),
    ],
    { repositories: [{ ...repo, monitorActions: true }] },
  );
  assert.equal(rows.length, 1);
  assert.equal(rows[0].kind, "ci");
  assert.equal(rows[0].title, "#12 Fix failing CI on ai-main: Build");
  assert.equal(rows[0].state, "running");
  assert.match(rows[0].detail, /\bCodex\b/);
  assert.match(rows[0].detail, /\bgpt-5\.4\b/);
  assert.match(rows[0].detail, /\bhigh effort\b/);
});

test("legacy test-scheduler state is inert when the worker is stopped", () => {
  const rows = deriveNowWorking({
    workerState: "stopped",
    repositories: [{ ...repo, uatState: "running" }],
    testRuns: {
      r1: [
        {
          startedAt: 10,
          finishedAt: null,
          trigger: "manual",
          suites: [
            { name: "API", state: "Passed" },
            { name: "UI", state: "Running" },
            { name: "E2E", state: "Not executed" },
          ],
        },
      ],
    },
    logs: [
      line("Adversarial UAT for issue #84: starting fix/re-test round 3 of 6."),
    ],
  });
  assert.deepEqual(
    rows,
    [],
    "removed scheduler snapshots must not recreate a tests row, and a stopped issue worker has no live adversarial row",
  );
});

test("parallel-repo adversarial logs stay attached to their own repository", () => {
  const rows = deriveNowWorking({
    workerState: "running",
    repositories: [repo, site],
    logs: [
      labeled("acme/app", "Selected oldest unprocessed assigned issue: #4 One"),
      labeled("acme/app", "Adversarial UAT for issue #4: starting fix/re-test round 2 of 6."),
      labeled("acme/site", "Selected oldest unprocessed assigned issue: #9 Two"),
      labeled("acme/site", "Adversarial UAT for issue #9: starting independent test run (round 0 of 6)."),
    ],
  });
  const appUat = rows.find((row) => row.kind === "adversarial" && row.repository === "acme/app");
  const siteUat = rows.find((row) => row.kind === "adversarial" && row.repository === "acme/site");
  assert.ok(appUat);
  assert.ok(siteUat);
  assert.equal(appUat.title, "Fix/re-test round 2 of 6");
  assert.match(`${siteUat.title} ${siteUat.detail}`, /independent/i);
  assert.equal(
    rows.filter((row) => row.kind === "issue").map((row) => `${row.repository}${row.title}`).join(","),
    "acme/app#4 One,acme/site#9 Two",
  );
});

test("non-worker sources and malformed lookalikes do not crash or invent a UAT row", () => {
  assert.doesNotThrow(() => deriveNowWorking({}));
  assert.doesNotThrow(() => derive(null));
  const rows = derive([
    line("Selected oldest unprocessed assigned issue: #84 Reduce logs"),
    line("Adversarial UAT for issue #84: starting fix/re-test round 3 of 6.", "Test scheduler"),
    line("Adversarial UAT for issue #84: not a real boundary"),
    line("Adversarial UAT for issue potato: starting fix/re-test round 1 of 6."),
    line("starting fix/re-test round 3 of 6."),
    undefined,
    "",
    42,
  ]);
  assert.equal(adversarialRow(rows), undefined);
  assert.equal(rows.filter((row) => row.kind === "issue").length, 1);
});

test("Overview renders an adversarial row with the Adversarial UAT kind label", () => {
  const appJs = fs.readFileSync(path.join(__dirname, "..", "..", "ui", "app.js"), "utf8");
  const kindsStart = appJs.indexOf("const NOW_WORKING_KINDS = ");
  assert.notEqual(kindsStart, -1, "renderNowWorking must keep a kind-label map");
  const kindsLiteral = appJs.slice(kindsStart, appJs.indexOf(";", kindsStart) + 1);
  const pillsStart = appJs.indexOf("const NOW_WORKING_PILLS = ");
  assert.notEqual(pillsStart, -1);
  const pillsLiteral = appJs.slice(pillsStart, appJs.indexOf(";", pillsStart) + 1);
  assert.match(
    kindsLiteral,
    /adversarial:\s*"Adversarial UAT"/,
    "the Overview kind pill must say Adversarial UAT, not the raw kind id",
  );
  assert.match(kindsLiteral, /issue:\s*"Issue"/);
  assert.doesNotMatch(
    kindsLiteral,
    /tests:\s*"Tests"/,
    "Issue #215 removes the tests row kind in favor of Adversarial UAT",
  );
  assert.match(kindsLiteral, /ci:\s*"CI\/CD"/);

  const classAssign = appJs.indexOf("item.className = `now-working-row ${row.kind}`");
  assert.notEqual(classAssign, -1, "renderNowWorking must put row.kind on the row class");
  const forEachStart = appJs.lastIndexOf("rows.forEach((row) => {", classAssign);
  assert.notEqual(forEachStart, -1, "renderNowWorking must walk deriveNowWorking rows");
  const appendAt = appJs.indexOf("list.appendChild(item);", forEachStart);
  assert.notEqual(appendAt, -1);
  const forEachBlock = appJs.slice(forEachStart, appJs.indexOf("});", appendAt) + 3);

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

  const list = {
    children: [],
    appendChild(child) {
      this.children.push(child);
      return child;
    },
  };
  const document = { createElement: element };
  const run = new Function(
    "rows",
    "document",
    "list",
    "multiple",
    `${kindsLiteral}
     ${pillsLiteral}
     ${forEachBlock}
     return list.children;`,
  );
  const rendered = run(
    [
      {
        kind: "adversarial",
        title: "Fix/re-test round 3 of 6",
        detail: "Re-test in progress",
        repository: "acme/app",
        state: "running",
        since: "12:34",
      },
    ],
    document,
    list,
    false,
  );
  assert.equal(rendered.length, 1);
  assert.equal(rendered[0].className, "now-working-row adversarial");
  const kind = rendered[0].children.find((child) => child.className === "now-working-kind");
  assert.ok(kind);
  assert.equal(kind.textContent, "Adversarial UAT");
  const words = rendered[0].children.find((child) => child.className === "now-working-words");
  assert.ok(words);
  assert.equal(words.children[0].textContent, "Fix/re-test round 3 of 6");
  assert.match(words.children[1].textContent, /Re-test in progress/);
});
