"use strict";

/**
 * Issue #291: Knowledge / Ask SWARM is a first-class view with settings,
 * refresh/rebuild, and help topics that explain how SWARM uses engineering data.
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const appJs = fs.readFileSync(path.join(root, "ui", "app.js"), "utf8");
const indexHtml = fs.readFileSync(path.join(root, "ui", "index.html"), "utf8");
const knowledgeJs = fs.readFileSync(path.join(root, "ui", "engineering-knowledge.js"), "utf8");

function section(html, id) {
  const start = html.indexOf(`<section id="${id}"`);
  assert.notEqual(start, -1, `${id} must remain a top-level view section`);
  const end = html.indexOf("</section>", start);
  assert.notEqual(end, -1, `${id} must have a closing section tag`);
  return html.slice(start, end + "</section>".length);
}

test("Knowledge is a top-level view with Ask SWARM, refresh, and rebuild", () => {
  assert.match(indexHtml, /data-view-target="knowledge"/);
  const view = section(indexHtml, "view-knowledge");
  assert.match(view, /id="ask-swarm-question"/);
  assert.match(view, /id="ask-swarm-scope"/);
  assert.match(view, /id="refresh-knowledge"/);
  assert.match(view, /id="rebuild-knowledge"/);
  assert.match(view, /data-config="engineering_knowledge_enabled"/);
  assert.match(view, /data-config="automatic_knowledge_generation"/);
  assert.match(appJs, /knowledge:\s*"Knowledge"/);
  assert.match(appJs, /ask_swarm_background/);
  assert.match(appJs, /refresh_knowledge_background/);
});

test("help topics explain what knowledge is and how SWARM uses engineering data", () => {
  assert.match(appJs, /"engineering-knowledge"\s*:\s*\{/);
  assert.match(appJs, /What is SWARM Engineering Knowledge\?/);
  assert.match(appJs, /How does SWARM use my engineering data\?|uses this data locally on this Mac/);
  assert.match(appJs, /\["Ask SWARM",\s*"ask-swarm"\]/);
  assert.match(knowledgeJs, /defaultScope/);
});
