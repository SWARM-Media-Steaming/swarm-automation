"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const app = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");

test("model data sources and activation are fixed, not user decisions", () => {
  for (const removed of [
    "model_data_source",
    "model_data_source_url",
    "model_calibration_auto_activate",
    "model_calibration_apply_to_routing",
  ]) {
    assert.ok(!html.includes(removed), `${removed} must not be a UI setting`);
    assert.ok(!app.includes(removed), `${removed} must not be read by the UI`);
  }
  assert.ok(!/Automatically activate clean refreshes/.test(html));
  assert.ok(!/Apply calibrated model data to live routing/.test(html));
});

test("the optional Artificial Analysis key is a masked, non-autofilled field", () => {
  const input = /<input[^>]*id="model-data-key"[^>]*>/.exec(html);
  assert.ok(input, "key input exists");
  assert.match(input[0], /type="password"/);
  assert.match(input[0], /autocomplete="off"/);
  assert.match(input[0], /aria-describedby="model-data-key-status"/);
  assert.match(html, /<label for="model-data-key">/);
  assert.match(html, /id="model-data-key-status"[^>]*aria-live="polite"/);
});

test("the key is not a config setting and is never shown back", () => {
  const input = /<input[^>]*id="model-data-key"[^>]*>/.exec(html)[0];
  assert.ok(!/data-config/.test(input), "the key must not ride the generic config loop");
  assert.ok(!/value=/.test(input), "the saved key is never rendered into the page");
  assert.match(app, /invoke\("save_model_data_key", \{ key: value \}\)/);
  // After saving, the field is cleared so the secret does not linger in the DOM.
  assert.match(app, /input\.value = ""/);
});

test("Artificial Analysis is attributed next to the key field", () => {
  assert.match(html, /data-external="https:\/\/artificialanalysis\.ai\/"/);
  assert.match(html, /stored in the macOS Keychain/i);
});

test("saving a key refreshes model data so benchmarks appear without a restart", () => {
  const save = app.slice(app.indexOf("async function saveModelDataKey"), app.indexOf("async function clearModelDataKey"));
  assert.match(save, /await refreshModelData\(\)/);
});

test("the settings panel adds no inline styles", () => {
  const panel = html.slice(html.indexOf("REFRESH SETTINGS"), html.indexOf('id="view-knowledge"'));
  assert.ok(!/style="/.test(panel));
});
