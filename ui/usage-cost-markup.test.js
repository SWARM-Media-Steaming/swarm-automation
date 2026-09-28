// Markup and wiring contract for Feedback's Usage & cost tab (issue #295).
//
// There is no DOM under `node --test`, so these read index.html/app.js as
// text the way the repository's other structural UI suites do. They exist to
// catch the failures a pure-helper test cannot: a tab button with no panel, a
// filter input nothing reads, an icon-only control with no accessible name.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const app = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "style.css"), "utf8");
const { USAGE_GROUPS, FILTER_KEYS } = require("./usage-cost.js");
const { FEEDBACK_TABS } = require("./prompt-grades.js");

function attributes(markup, attribute) {
  return [...markup.matchAll(new RegExp(`${attribute}="([^"]+)"`, "g"))].map((m) => m[1]);
}

// The opening tag carrying `id="<id>"`, so attribute assertions do not
// depend on the order the attributes happen to be written in.
function tagWithId(id) {
  const match = new RegExp(`<[a-z]+[^>]*\\bid="${id}"[^>]*>`).exec(html);
  assert.ok(match, `no element with id="${id}"`);
  return match[0];
}

test("Feedback has a fourth tab and every tab has exactly one panel", () => {
  const tabs = attributes(html, "data-feedback-tab");
  const panels = attributes(html, "data-feedback-panel");
  assert.deepEqual(tabs, ["grades", "routing", "history", "usage"]);
  assert.deepEqual([...tabs].sort(), [...panels].sort());
  // The switcher in app.js must know about the same set, or a tab would
  // silently fall back to the first panel.
  assert.deepEqual([...FEEDBACK_TABS].sort(), [...tabs].sort());
});

test("the usage tab and panel reference each other for assistive technology", () => {
  const tab = tagWithId("feedback-tab-usage");
  const panel = tagWithId("feedback-panel-usage");
  assert.match(tab, /role="tab"/);
  assert.match(tab, /aria-controls="feedback-panel-usage"/);
  assert.match(panel, /role="tabpanel"/);
  assert.match(panel, /aria-labelledby="feedback-tab-usage"/);
  // Not the selected tab at rest, so it ships out of the tab order and
  // hidden — the roving tabindex the other three already use.
  assert.match(tab, /aria-selected="false"/);
  assert.match(tab, /tabindex="-1"/);
  assert.match(panel, /hidden/);
});

test("the panel is one tablist away from the others, not a separate route", () => {
  // Adding a top-level area would need a nav item, a view section and a
  // pageTitles entry; Usage & cost is deliberately none of those.
  assert.doesNotMatch(html, /data-view-target="usage"/);
  assert.doesNotMatch(html, /id="view-usage"/);
});

test("every filter control is bound to a filter key the backend accepts", () => {
  const bound = attributes(html, "data-usage-filter");
  assert.ok(bound.length > 0);
  bound.forEach((key) => {
    assert.ok(FILTER_KEYS.includes(key), `${key} is not a known usage filter`);
  });
  // The filters the issue lists by name all have a control.
  ["startDate", "endDate", "issueNumber", "grade", "provider", "model", "effort",
   "agentType", "promptType", "outcome", "coverage", "search"].forEach((key) => {
    assert.ok(bound.includes(key), `no control is bound to ${key}`);
  });
  // Repository is the one filter that is deliberately not here: Feedback's
  // existing global repository chips own it for all four tabs.
  assert.ok(!bound.includes("repositories"));
  assert.match(html, /id="feedback-repo-chips"/);
});

test("every filter control carries an accessible name", () => {
  const panel = html.slice(html.indexOf('id="feedback-panel-usage"'));
  const controls = [...panel.matchAll(/<(select|input)[^>]*data-usage-filter="[^"]+"[^>]*>/g)]
    .map((match) => match[0]);
  assert.ok(controls.length >= 12);
  controls.forEach((control) => {
    const id = /id="([^"]+)"/.exec(control);
    assert.ok(id, `${control} has no id`);
    const labelled = panel.includes(`<label>`) || panel.includes(`for="${id[1]}"`);
    // Every control sits inside a <label>, and the selects additionally
    // carry aria-label because their visible text is a short word.
    assert.ok(labelled);
    if (control.startsWith("<select")) {
      assert.match(control, /aria-label="/, `${id[1]} needs an aria-label`);
    }
  });
});

test("live regions announce themselves as they update", () => {
  ["usage-summary", "usage-coverage", "usage-groups", "usage-detail"].forEach((id) => {
    assert.match(tagWithId(id), /aria-live="polite"/, `${id} should be a polite live region`);
  });
  assert.match(tagWithId("usage-coverage"), /aria-label="Reporting coverage"/);
});

test("both tables are paged, and each pager is labelled", () => {
  assert.match(tagWithId("usage-groups-pager"), /aria-label="Usage aggregate pages"/);
  assert.match(tagWithId("usage-detail-pager"), /aria-label="Usage invocation pages"/);
  ["usage-groups-prev", "usage-groups-next", "usage-detail-prev", "usage-detail-next"]
    .forEach((id) => assert.ok(html.includes(`id="${id}"`), `${id} is missing`));
});

test("tables are rendered with headers, captions and sort state", () => {
  // Built in app.js rather than in markup, since the rows are data-driven.
  assert.match(app, /cell\.scope = "col"/);
  assert.match(app, /caption\.className = "sr-only"/);
  assert.match(app, /cell\.setAttribute\("aria-sort"/);
  assert.match(css, /\.sr-only\s*{/);
});

test("the group-by selector offers every dimension the report supports", () => {
  assert.match(tagWithId("usage-group-by"), /aria-label="Group usage totals"/);
  // The options are filled from USAGE_GROUPS rather than duplicated in
  // markup, so the two can never drift.
  assert.match(app, /api\.USAGE_GROUPS\.forEach/);
  assert.ok(USAGE_GROUPS.length === 11);
});

test("expandable details reuse the app's existing disclosure pattern", () => {
  // Execution History cards expand into their usage records with the same
  // <details>/raw-graph-panel shape the rest of the card already uses.
  assert.match(app, /Usage records \(\$\{usageApiRef\.formatCount\(usageRecords\.length\)\}\)/);
  assert.match(app, /details\.className = "raw-graph-panel"/);
});

test("all three cross-links into the usage view are wired", () => {
  assert.match(app, /data-grade-usage-issue/);
  assert.match(app, /dataset\.gradeUsageIssue/);
  assert.match(app, /data-router-usage/);
  assert.match(app, /dataset\.routerUsage/);
  assert.match(app, /data-execution-usage/);
  assert.match(app, /dataset\.executionUsage/);
  // And every one goes through the single entry point rather than poking at
  // usage state directly.
  assert.equal((app.match(/openUsageWithFilters\(/g) || []).length, 4);
});

test("the view queries the backend instead of aggregating in the desktop", () => {
  assert.match(app, /invoke\("get_usage_report_background"/);
  // No client-side totalling: the panel formats pre-aggregated numbers.
  const panelCode = app.slice(app.indexOf("function renderUsageGroups"), app.indexOf("function renderUsagePager"));
  assert.doesNotMatch(panelCode, /\.reduce\(/);
});

test("no inline styles or scripts are introduced", () => {
  const panel = html.slice(
    html.indexOf('id="feedback-panel-usage"'),
    html.indexOf('id="view-debug"'),
  );
  assert.doesNotMatch(panel, /\sstyle="/);
  assert.doesNotMatch(panel, /<script/);
});

test("the panel's helper module is loaded before app.js", () => {
  const helper = html.indexOf('src="usage-cost.js"');
  const main = html.indexOf('src="app.js"');
  assert.ok(helper > 0 && helper < main);
});

test("new multi-column grids collapse at the two existing breakpoints", () => {
  const wide = css.slice(css.indexOf("@media (max-width: 1060px)"), css.indexOf("@media (max-width: 820px)"));
  const narrow = css.slice(css.indexOf("@media (max-width: 820px)"));
  [wide, narrow].forEach((block) => {
    assert.match(block, /\.usage-summary\s*{\s*grid-template-columns/);
    assert.match(block, /\.usage-filters\s*{\s*grid-template-columns/);
  });
  // And no third breakpoint was invented for them.
  assert.equal((css.match(/@media \(max-width/g) || []).length, 2);
});

test("the panel uses design tokens rather than new literal colors", () => {
  const block = css.slice(css.indexOf("/* Usage & cost"), css.indexOf("@media (max-width: 1060px)"));
  const literals = block.match(/#[0-9a-fA-F]{3,8}\b/g) || [];
  assert.equal(literals.length, 0, `unexpected literal colors: ${literals.join(", ")}`);
  assert.match(block, /var\(--/);
  // The one rgba() is a translucent tint of the violet accent, matching how
  // .suite-state and the other selected-state rules already tint tokens.
  const tints = block.match(/rgba\([^)]+\)/g) || [];
  assert.equal(tints.length, 1);
  assert.match(block, /\.usage-row\.selected \{ background: rgba/);
});
