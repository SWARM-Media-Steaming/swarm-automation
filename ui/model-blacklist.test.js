"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const app = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");

function aiView() {
  const start = html.indexOf('<section id="view-ai"');
  return html.slice(start, html.indexOf('<section id="view-architecture"'));
}

test("AI Configuration has an add and remove blacklist", () => {
  const view = aiView();
  assert.match(view, /id="model-blacklist-rows"/);
  assert.match(view, /id="model-blacklist-form"/);
  assert.match(view, /id="model-blacklist-model"/);
  assert.match(view, /id="model-blacklist-successor"/);
  assert.match(view, /id="model-blacklist-add"/);
  assert.match(view, /data-help="model-blacklist"/);
  assert.ok(!view.includes("admin-blacklist"), "the platform admin table stays off this page");
});

test("the page saves the operator list with the rest of the configuration", () => {
  assert.match(app, /function renderModelBlacklist/);
  assert.match(app, /function addBlacklistEntry/);
  assert.match(app, /function removeBlacklistEntry/);
  assert.match(app, /state\.config\.model_blacklist = next/);
  assert.match(app, /renderModelBlacklist\(config\)/);
  assert.match(app, /"model-blacklist": \{/);
});
