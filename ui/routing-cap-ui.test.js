"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { CAP_PROVIDERS, capOptions, capEffortState } = require("./dynamic-routing-ui.js");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const app = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "style.css"), "utf8");

const models = [
  { value: "sonnet-x", label: "Sonnet X", efforts: ["low", "high"], defaultEffort: "high" },
  { value: "haiku-x", label: "Haiku X", efforts: ["low"] },
];

test("the cap model list is Uncapped plus the discovered models", () => {
  assert.deepEqual(capOptions(models, "").map((o) => o.value), ["", "sonnet-x", "haiku-x"]);
  assert.equal(capOptions(models, "")[0].label, "Uncapped");
});

test("a saved model the CLI no longer offers stays selectable", () => {
  const options = capOptions(models, "gone");
  assert.equal(options.at(-1).value, "gone");
  assert.match(options.at(-1).label, /not offered/);
});

test("uncapped has no effort; a capped model keeps a supported saved effort", () => {
  assert.deepEqual(capEffortState(models, "", "high"), { efforts: [], effort: "", disabled: true });
  assert.equal(capEffortState(models, "sonnet-x", "low").effort, "low");
  assert.equal(capEffortState(models, "haiku-x", "high").effort, "high");
  assert.equal(capEffortState(models, "sonnet-x", "").effort, "high");
});

test("the repository settings carry one data-repo-config model and effort select per provider", () => {
  for (const id of CAP_PROVIDERS) {
    assert.match(html, new RegExp(`data-repo-config="routing_cap_${id}_model"`));
    assert.match(html, new RegExp(`data-repo-config="routing_cap_${id}_effort"`));
    assert.match(app, new RegExp(`routing_cap_${id}_model: ""`));
  }
  assert.match(html, /data-help="routing-cap"/);
  assert.match(app, /"routing-cap": \{/);
  assert.doesNotMatch(html, /routing-cap-panel[^>]*style=/);
});

test("the cap grid collapses at both existing breakpoints", () => {
  assert.match(css, /@media \(max-width: 1060px\) \{[^@]*\.routing-cap-grid/);
  assert.match(css, /@media \(max-width: 820px\) \{[\s\S]*?\.routing-cap-grid \{ grid-template-columns: 1fr; \}/);
});
