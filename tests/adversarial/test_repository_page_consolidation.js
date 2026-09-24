"use strict";

/**
 * Issue #216: Repository page becomes the single home for every
 * `data-repo-config` setting (plus the relocated Promotion queue), tabbed
 * instead of one long scroll; AI Configuration becomes the home for every
 * `data-config` setting that used to live in Advanced; the Advanced nav
 * item/page and the branch-tree visual/raw Git graph are removed outright.
 *
 * These checks read markup/text statically (app.js executes against
 * `window.__TAURI__` at load time and cannot be required under plain Node),
 * matching the convention already used by test_test_scheduler_removal_ui.js
 * and test_help_async_refresh_relocation.js for this repository.
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const indexHtml = fs.readFileSync(path.join(root, "ui", "index.html"), "utf8");
const appJs = fs.readFileSync(path.join(root, "ui", "app.js"), "utf8");
const styleCss = fs.readFileSync(path.join(root, "ui", "style.css"), "utf8");

function section(html, id) {
  const start = html.indexOf(`<section id="${id}"`);
  assert.notEqual(start, -1, `${id} must exist as a top-level view section`);
  const end = html.indexOf("</section>", start);
  assert.notEqual(end, -1, `${id} must have a closing section tag`);
  return html.slice(start, end + "</section>".length);
}

function repoConfigKeys(html) {
  return [...html.matchAll(/data-repo-config="([^"]+)"/g)].map((m) => m[1]);
}

function appConfigKeys(html) {
  return [...html.matchAll(/data-config="([^"]+)"/g)].map((m) => m[1]);
}

const repositorySection = section(indexHtml, "view-repository");
// Everything outside the repository <section>...</section> slice, so
// misplaced settings elsewhere in the document are still caught.
const outsideRepository = indexHtml.slice(0, indexHtml.indexOf(repositorySection))
  + indexHtml.slice(indexHtml.indexOf(repositorySection) + repositorySection.length);

test("the Advanced nav item and page no longer exist anywhere in the UI", () => {
  assert.doesNotMatch(indexHtml, /data-view-target=["']advanced["']/i);
  assert.doesNotMatch(indexHtml, /id=["']view-advanced["']/i);
  assert.doesNotMatch(appJs, /\badvanced\s*:\s*["']Advanced["']/, "pageTitles must drop its advanced entry");
  assert.doesNotMatch(indexHtml, />\s*Advanced\s*</);
});

test("every data-repo-config setting lives inside the Repository page, and none leak outside it", () => {
  const insideRepo = repoConfigKeys(repositorySection);
  const outsideRepo = repoConfigKeys(outsideRepository);
  assert.equal(outsideRepo.length, 0, `data-repo-config found outside Repository: ${outsideRepo.join(", ")}`);
  // Sanity: the moved fields actually made it in, not merely "not elsewhere".
  for (const key of [
    "base_branch", "integration_branch", "remote_name", "branch_prefix",
    "require_issue_tests", "adversarial_uat_enabled",
    "update_claude_assets_enabled", "allow_environment_only_summary",
  ]) {
    assert.ok(insideRepo.includes(key), `Repository page must contain data-repo-config="${key}"`);
  }
});

test("every data-config setting lives outside the Repository page", () => {
  const insideRepoAppConfig = appConfigKeys(repositorySection);
  assert.equal(insideRepoAppConfig.length, 0, `data-config found inside Repository: ${insideRepoAppConfig.join(", ")}`);
  // The app-wide settings relocated out of Advanced must exist somewhere else.
  for (const key of ["auto_update", "ai_execution_history_enabled", "prompt_feedback_upload_enabled"]) {
    assert.ok(outsideRepository.includes(`data-config="${key}"`), `data-config="${key}" must exist outside Repository`);
  }
  assert.match(outsideRepository, /data-provider-bin="claude"/, "provider CLI overrides must exist outside Repository");
  assert.doesNotMatch(repositorySection, /data-provider-bin=/, "provider CLI overrides must not live on the Repository page");
});

test("the Repository page is a tablist, not one long scroll", () => {
  const tabs = [...repositorySection.matchAll(/data-repository-tab="([^"]+)"/g)].map((m) => m[1]);
  assert.ok(tabs.length >= 3, "Repository page must expose multiple tabs instead of a single scroll");
  assert.equal(new Set(tabs).size, tabs.length, "tab keys must be unique");
  const panels = [...repositorySection.matchAll(/data-repository-panel="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(new Set(panels), new Set(tabs), "every tab must have a matching panel and vice versa");
  assert.match(appJs, /showRepositoryTab/, "app.js must drive the tablist, mirroring showFeedbackTab");
});

test("the branch-tree visual and raw Git graph no longer exist anywhere in the UI", () => {
  // Note: "raw-graph-panel" / "branch-graph" CSS classes are intentionally
  // excluded — rawTextPanel() in app.js reuses that same collapsible-text
  // component for execution-history detail rows, unrelated to branch trees.
  for (const needle of [
    "branch-tree", "raw-git-graph", "branch-map", "branch-node", "branchOverview",
    "renderBranchOverview", "refreshBranches", "refresh-branches",
    "id=\"branch-warning\"",
  ]) {
    assert.equal(indexHtml.includes(needle), false, `index.html still contains removed branch-tree artifact: ${needle}`);
    assert.equal(appJs.includes(needle), false, `app.js still contains removed branch-tree artifact: ${needle}`);
  }
  assert.doesNotMatch(styleCss, /\.branch-tree\s*\{/);
  assert.doesNotMatch(styleCss, /\.branch-map\s*\{/);
  assert.doesNotMatch(styleCss, /\.branch-node\s*\{/);
});

test("the Promotion queue actions are relocated onto the Repository page and still wired up", () => {
  assert.match(repositorySection, /id="promotion-panel"/, "Promotion queue panel must live on the Repository page");
  assert.match(repositorySection, /id="promotion-list"/);
  assert.doesNotMatch(section(indexHtml, "view-overview"), /id="promotion-panel"/, "Promotion queue must not remain on Overview");
  assert.match(appJs, /invoke\("open_integration_pr"/, "Create PR action must still call open_integration_pr");
  assert.match(appJs, /invoke\("promote_integration_branch_background"/, "Merge to Main action must still call promote_integration_branch_background");
  assert.match(appJs, /view === "repository"[\s\S]{0,120}refreshPromotions/, "navigating to Repository must refresh the promotion queue");
});

test("help text and empty-state copy do not point users at the removed Advanced page", () => {
  // Real bug: 'Store AI execution history' moved to AI Configuration, but its
  // help topic and its empty-state message were left saying "Advanced".
  assert.doesNotMatch(appJs, /\(see Advanced\)/i, "execution-history help topic must not cite the deleted Advanced page");
  assert.doesNotMatch(appJs, /\bin Advanced\b/i, "no UI copy may direct users to the deleted Advanced page");
  assert.doesNotMatch(appJs, /\bunder Advanced\b/i, "no UI copy may direct users to the deleted Advanced page");
});
