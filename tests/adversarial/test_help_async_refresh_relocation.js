"use strict";

/**
 * Issue #214: the async-refresh explanation belongs to Help > Concepts,
 * rather than the Info & Debug introduction.  Help's concepts are data-driven:
 * a topic alone is not discoverable unless HELP_CONCEPTS registers it, and a
 * registered key without a topic leaves the UI unable to open it.  Check both
 * halves of that contract as well as the source section boundary.
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const appJs = fs.readFileSync(path.join(root, "ui", "app.js"), "utf8");
const indexHtml = fs.readFileSync(path.join(root, "ui", "index.html"), "utf8");
const note = "Tool detection and logs refresh independently so slow checks do not block the rest of the interface.";

function section(html, id) {
  const start = html.indexOf(`<section id="${id}"`);
  assert.notEqual(start, -1, `${id} must remain a top-level view section`);
  const end = html.indexOf("</section>", start);
  assert.notEqual(end, -1, `${id} must have a closing section tag`);
  return html.slice(start, end + "</section>".length);
}

test("the async-refresh note is a uniquely registered Help Concepts topic", () => {
  const occurrences = appJs.split(note).length - 1 + indexHtml.split(note).length - 1;
  assert.equal(occurrences, 1, "the explanatory sentence must have one canonical UI home, preventing stale copies");

  const topic = appJs.match(/"async-refresh"\s*:\s*\{([\s\S]*?)\n\s*\},/);
  assert.ok(topic, "HELP_TOPICS must define an async-refresh entry for the help modal");
  assert.match(topic[1], /title:\s*"Independent refresh"/, "the topic needs a short Concepts label");
  assert.ok(topic[1].includes(note), "the Help topic must preserve the required refresh explanation");

  assert.match(
    appJs,
    /\["Independent refresh",\s*"async-refresh"\]/,
    "HELP_CONCEPTS must register the topic so it is visible in Help > Concepts",
  );
  assert.match(
    indexHtml,
    /id="help-concepts"/,
    "the Help page must retain the Concepts container that renderHelpConcepts populates",
  );
});

test("Info & Debug no longer presents the async-refresh explanation or loses its diagnostic controls", () => {
  const debug = section(indexHtml, "view-debug");
  assert.doesNotMatch(debug, new RegExp(note.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.match(debug, /id="diagnose-button"/);
  assert.match(debug, /id="refresh-tools"/);
  assert.match(debug, /id="tool-grid"/);
  assert.match(debug, /id="full-log"/);
});
