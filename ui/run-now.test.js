const test = require("node:test");
const assert = require("node:assert/strict");
const { runNowMode } = require("./run-now.js");

test("starts one cycle when nothing is running", () => {
  assert.equal(runNowMode({ kind: "issue", processState: "stopped" }), "start");
  assert.equal(runNowMode({ kind: "uat", processState: "stopped" }), "start");
});

test("asks a running issue worker to scan now instead of starting a second one", () => {
  assert.equal(runNowMode({ kind: "issue", processState: "running" }), "request");
});

test("stays enabled for a running issue worker, so the button is clickable", () => {
  assert.notEqual(runNowMode({ kind: "issue", processState: "running" }), "disabled");
});

test("a paused issue worker has to be resumed first", () => {
  assert.equal(runNowMode({ kind: "issue", processState: "paused" }), "disabled");
});

test("the test scheduler keeps its start-only behavior", () => {
  assert.equal(runNowMode({ kind: "uat", processState: "running" }), "disabled");
  assert.equal(runNowMode({ kind: "uat", processState: "paused" }), "disabled");
});

test("an in-flight click or an unavailable repository disables the button", () => {
  assert.equal(runNowMode({ kind: "issue", processState: "running", busy: true }), "disabled");
  assert.equal(runNowMode({ kind: "issue", processState: "stopped", busy: true }), "disabled");
  assert.equal(runNowMode({ kind: "uat", processState: "stopped", available: false }), "disabled");
});

test("defaults to starting when no state is known yet", () => {
  assert.equal(runNowMode(), "start");
});
