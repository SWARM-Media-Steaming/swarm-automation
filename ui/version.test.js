const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { formatBuildVersion } = require("./version.js");

test("shows the published build exactly, with one leading v", () => {
  assert.equal(formatBuildVersion("0.1.0"), "v0.1.0");
  assert.equal(formatBuildVersion("0.1.0-beta.42"), "v0.1.0-beta.42");
  assert.equal(formatBuildVersion("0.1.0+main.7"), "v0.1.0+main.7");
  assert.equal(formatBuildVersion("  v0.1.0+main.7  "), "v0.1.0+main.7");
  assert.equal(formatBuildVersion("V0.1.0-beta.3"), "V0.1.0-beta.3");
});

test("leaves the label blank when no build version is available", () => {
  assert.equal(formatBuildVersion(""), "");
  assert.equal(formatBuildVersion("   "), "");
  assert.equal(formatBuildVersion(null), "");
  assert.equal(formatBuildVersion(undefined), "");
});

test("places the running build next to the SWARM Automation name", () => {
  const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
  const brand = html.slice(html.indexOf('class="brand"'), html.indexOf("<nav"));
  const footer = html.slice(html.indexOf('class="sidebar-foot"'), html.indexOf("</aside>"));
  assert.match(brand, /id="app-version-label"/);
  assert.match(brand, /<strong>SWARM<\/strong><small>Automation<\/small>/);
  assert.doesNotMatch(footer, /app-version-label/);
  assert.match(html, /<script src="version\.js"><\/script>\s*<script src="app\.js"><\/script>/);
});
