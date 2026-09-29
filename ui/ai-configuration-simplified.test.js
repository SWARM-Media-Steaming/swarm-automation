"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const app = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");

function aiView() {
  const start = html.indexOf('<section id="view-ai"');
  return html.slice(start, html.indexOf("</section>", start));
}

test("removed panels are gone from AI Configuration", () => {
  const view = aiView();
  for (const gone of ["App updates", "CLI path overrides", "PROVIDER EXECUTABLES", "SOFTWARE UPDATE",
    "Execution history", "Allow prompt feedback upload", "Typed operational decisions"]) {
    assert.ok(!view.includes(gone), `${gone} must not appear in AI Configuration`);
  }
});

test("execution history has no toggle and feedback upload is not a setting", () => {
  for (const key of ["ai_execution_history_enabled", "prompt_feedback_upload_enabled"]) {
    assert.ok(!html.includes(key) && !app.includes(key), `${key} must not be in the UI`);
  }
  assert.ok(!html.includes("Store AI execution history"));
  assert.ok(!app.includes("Store AI execution history"));
});

test("the in-app updater UI and its backend calls are gone", () => {
  for (const id of ["update-banner", "check-update", "update-version-pill", "update-candidates"]) {
    assert.ok(!html.includes(`id="${id}`), `${id} removed from the page`);
  }
  for (const command of ["check_for_update", "install_update", "list_update_candidates", "install_update_candidate"]) {
    assert.ok(!app.includes(command), `${command} is no longer invoked`);
  }
  assert.ok(!app.includes('"update-available"'));
  // The running build label survives; it is unrelated to updating.
  assert.match(app, /invoke\("app_version"\)/);
  assert.match(html, /id="app-version-label"/);
});

test("provider CLI paths are auto-detected, with no override fields", () => {
  assert.ok(!html.includes("data-provider-bin"));
  assert.ok(!app.includes("data-provider-bin"));
  assert.ok(!html.includes('data-config="jev_bin"'));
});

test("Jev has a single toggle in the Dynamic Model Routing panel", () => {
  const view = aiView();
  const routing = view.slice(view.indexOf("dynamic-routing-panel"), view.indexOf('id="provider-cards"'));
  assert.match(routing, /data-config="allow_usage_credit_models"/);
  assert.match(routing, /data-config="jev_enabled"/);
  assert.equal((html.match(/data-config="jev_/g) || []).length, 1);
});
