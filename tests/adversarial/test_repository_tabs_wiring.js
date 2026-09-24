"use strict";

/**
 * Issue #216: the Repository page is a tablist "like the Feedback page
 * already has," not one long scroll. test_repository_page_consolidation.js
 * already checks that a tablist exists and that config fields land in the
 * right place; this suite targets the tab *wiring* itself, which is where a
 * later edit to these four tabs (adding a field, splitting a tab, moving a
 * setting between tabs again) is most likely to silently regress:
 *
 *  - showRepositoryTab() falls back to REPOSITORY_TABS[0] ("source") for any
 *    tab key it does not recognize (ui/app.js:1755). A tab button added to
 *    index.html without a matching entry in the REPOSITORY_TABS array would
 *    render, be clickable, update its own aria-selected/active class via the
 *    generic querySelectorAll loops — but clicking it would silently snap
 *    state.repositoryTab back to "source" and show the Source panel instead,
 *    a confusing "my click did nothing" bug that static regex on "at least
 *    3 tabs exist" would not catch.
 *  - Non-active tab panels must ship already `hidden` in the static markup
 *    (matching the existing Feedback tab convention), not only hidden via
 *    the CSS `.tab-panel.active` rule, so screen readers and no-JS/slow-load
 *    states don't see four stacked panels worth of duplicate settings.
 *  - Every tab<->panel pair must be linked in both directions
 *    (aria-controls / aria-labelledby), and every data-repo-config /
 *    data-config key must appear exactly once in the document — a
 *    regression here would mean a setting got left behind in an old
 *    location AND added to its new tab, so the generic
 *    `[data-repo-config]` binding loop in ui/app.js binds two inputs to one
 *    config key and only one of them wins on save.
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const indexHtml = fs.readFileSync(path.join(root, "ui", "index.html"), "utf8");
const appJs = fs.readFileSync(path.join(root, "ui", "app.js"), "utf8");

function repositoryTabsArray() {
  const match = appJs.match(/const REPOSITORY_TABS = \[([^\]]*)\];/);
  assert.notEqual(match, null, "ui/app.js must define a REPOSITORY_TABS array driving showRepositoryTab");
  return match[1].split(",").map((entry) => entry.trim().replace(/^["']|["']$/g, "")).filter(Boolean);
}

function tabButtons() {
  return [...indexHtml.matchAll(
    /<button[^>]*role="tab"[^>]*id="([^"]+)"[^>]*data-repository-tab="([^"]+)"[^>]*aria-controls="([^"]+)"/g,
  )].map((m) => ({ id: m[1], tab: m[2], controls: m[3] }));
}

function tabPanels() {
  return [...indexHtml.matchAll(
    /<div[^>]*id="(repository-panel-[^"]+)"[^>]*role="tabpanel"[^>]*aria-labelledby="([^"]+)"[^>]*data-repository-panel="([^"]+)"([^>]*)>/g,
  )].map((m) => ({ id: m[1], labelledby: m[2], panel: m[3], restOfTag: m[4] }));
}

test("REPOSITORY_TABS in app.js exactly matches the set of data-repository-tab values in the markup", () => {
  const jsTabs = repositoryTabsArray();
  const htmlTabs = tabButtons().map((b) => b.tab);
  assert.deepEqual([...jsTabs].sort(), [...htmlTabs].sort(),
    "a tab key present in one of index.html/app.js but not the other means clicking it silently falls back to the default tab");
  assert.equal(new Set(jsTabs).size, jsTabs.length, "REPOSITORY_TABS must not list the same tab key twice");
});

test("every repository tab button and panel are linked in both directions", () => {
  const buttons = tabButtons();
  const panels = tabPanels();
  assert.ok(buttons.length >= 3, "expected multiple repository tab buttons");
  assert.equal(buttons.length, panels.length, "every tab button must have exactly one matching panel");
  for (const button of buttons) {
    const panel = panels.find((p) => p.id === button.controls);
    assert.ok(panel, `tab button #${button.id} has aria-controls="${button.controls}" but no panel with that id exists`);
    assert.equal(panel.labelledby, button.id, `panel #${panel.id} must set aria-labelledby="${button.id}" to match its controlling tab`);
    assert.equal(panel.panel, button.tab, `panel #${panel.id}'s data-repository-panel must equal its tab's data-repository-tab ("${button.tab}")`);
  }
});

test("every non-active repository tab panel ships hidden in the static markup", () => {
  const panels = tabPanels();
  const active = panels.filter((p) => !/\bhidden\b/.test(p.restOfTag));
  assert.equal(active.length, 1, `exactly one repository panel should be visible before JS runs; found visible: ${active.map((p) => p.id).join(", ")}`);
});

test("no data-repo-config or data-config key is bound by more than one element in index.html", () => {
  for (const attr of ["data-repo-config", "data-config"]) {
    const keys = [...indexHtml.matchAll(new RegExp(`${attr}="([^"]+)"`, "g"))].map((m) => m[1]);
    const seen = new Map();
    for (const key of keys) seen.set(key, (seen.get(key) || 0) + 1);
    const dupes = [...seen.entries()].filter(([, count]) => count > 1).map(([key]) => key);
    assert.deepEqual(dupes, [], `${attr} keys must be unique in index.html; duplicated: ${dupes.join(", ")}`);
  }
});
