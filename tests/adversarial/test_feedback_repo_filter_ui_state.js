"use strict";

/**
 * Issue #211: the Feedback page's repository filter is independent app
 * state (`state.feedbackRepoFilter`), never `state.activeRepoId`. This
 * suite executes the live `feedbackRepositories`, `restoreFeedbackRepoFilter`,
 * and `feedbackRepoIdsForQuery` functions straight out of ui/app.js against a
 * minimal fake `state`, so a regression to the "empty selection means every
 * repository" contract, the persisted-filter restore, or the stale-id
 * cleanup fails the UAT instead of only surfacing in the running app.
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const appJs = fs.readFileSync(
  path.join(__dirname, "..", "..", "ui", "app.js"),
  "utf8",
);

function extractFeedbackFilterBlock() {
  const start = appJs.indexOf("function feedbackRepositories() {");
  assert.notEqual(
    start,
    -1,
    "ui/app.js must define feedbackRepositories() for the Feedback repo filter",
  );
  const end = appJs.indexOf("function renderFeedbackRepoFilter() {", start);
  assert.notEqual(
    end,
    -1,
    "feedbackRepoIdsForQuery/restoreFeedbackRepoFilter must sit right before renderFeedbackRepoFilter",
  );
  return appJs.slice(start, end);
}

function repo(id, githubRepository) {
  return { id, github_repository: githubRepository };
}

function makeFilter(config) {
  const state = { config, feedbackRepoFilter: [] };
  const run = new Function(
    "state",
    `${extractFeedbackFilterBlock()}
     return { feedbackRepositories, restoreFeedbackRepoFilter, feedbackRepoIdsForQuery, feedbackRepoFilterSignature, state };`,
  );
  return run(state);
}

test("restoring with no persisted filter selects every configured repository", () => {
  const { restoreFeedbackRepoFilter, feedbackRepoIdsForQuery, state } = makeFilter({
    repositories: [repo("a", "octocat/a"), repo("b", "octocat/b")],
  });
  restoreFeedbackRepoFilter(state.config);
  assert.deepEqual(state.feedbackRepoFilter.slice().sort(), ["a", "b"]);
  // Every repo selected must serialize as the empty/global sentinel, not an
  // explicit list — the backend treats [] as "no WHERE repository clause".
  assert.deepEqual(feedbackRepoIdsForQuery(), []);
});

test("an older config with no feedback_repo_filter field at all still defaults to all repos", () => {
  const { restoreFeedbackRepoFilter, state } = makeFilter({
    repositories: [repo("a", "octocat/a")],
    // feedback_repo_filter intentionally omitted, as an upgraded config would have it.
  });
  restoreFeedbackRepoFilter(state.config);
  assert.deepEqual(state.feedbackRepoFilter, ["a"]);
});

test("a persisted single-repo selection restores exactly that repo, not all repos", () => {
  const { restoreFeedbackRepoFilter, feedbackRepoIdsForQuery, state } = makeFilter({
    repositories: [repo("a", "octocat/a"), repo("b", "octocat/b")],
    feedback_repo_filter: ["b"],
  });
  restoreFeedbackRepoFilter(state.config);
  assert.deepEqual(state.feedbackRepoFilter, ["b"]);
  assert.deepEqual(feedbackRepoIdsForQuery(), ["b"]);
});

test("a persisted filter naming a repository removed from config falls back to all remaining repos", () => {
  const { restoreFeedbackRepoFilter, state } = makeFilter({
    repositories: [repo("a", "octocat/a"), repo("b", "octocat/b")],
    // "deleted-repo" was checked before it was removed from config.repositories.
    feedback_repo_filter: ["deleted-repo"],
  });
  restoreFeedbackRepoFilter(state.config);
  assert.deepEqual(state.feedbackRepoFilter.slice().sort(), ["a", "b"]);
});

test("a persisted filter with one live id and one stale id keeps only the live one, not all repos", () => {
  const { restoreFeedbackRepoFilter, feedbackRepoIdsForQuery, state } = makeFilter({
    repositories: [repo("a", "octocat/a"), repo("b", "octocat/b"), repo("c", "octocat/c")],
    feedback_repo_filter: ["b", "deleted-repo"],
  });
  restoreFeedbackRepoFilter(state.config);
  assert.deepEqual(state.feedbackRepoFilter, ["b"]);
  assert.deepEqual(feedbackRepoIdsForQuery(), ["b"]);
});

test("feedbackRepoIdsForQuery ignores repo ids in the filter that config no longer has", () => {
  const { feedbackRepoIdsForQuery, state } = makeFilter({
    repositories: [repo("a", "octocat/a"), repo("b", "octocat/b")],
  });
  // Simulate state drifting out of sync with config without going through restore.
  state.feedbackRepoFilter = ["a", "removed-repo"];
  assert.deepEqual(feedbackRepoIdsForQuery(), ["a"]);
});

test("repositories missing an id or a github_repository are excluded from the filterable set", () => {
  const { feedbackRepositories, state } = makeFilter({
    repositories: [
      repo("a", "octocat/a"),
      { id: "b", github_repository: "" },
      { id: "", github_repository: "octocat/c" },
    ],
  });
  assert.deepEqual(
    feedbackRepositories().map((entry) => entry.id),
    ["a"],
  );
});

test("feedbackRepoFilterSignature is order-independent so re-rendering after a shuffle is not treated as a filter change", () => {
  const { feedbackRepoFilterSignature, state } = makeFilter({ repositories: [] });
  state.feedbackRepoFilter = ["b", "a"];
  const first = feedbackRepoFilterSignature();
  state.feedbackRepoFilter = ["a", "b"];
  const second = feedbackRepoFilterSignature();
  assert.equal(first, second);
});
