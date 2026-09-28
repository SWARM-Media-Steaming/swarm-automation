const { test } = require("node:test");
const assert = require("node:assert/strict");
const knowledge = require("./engineering-knowledge.js");

test("Ask SWARM defaults to the active repository when the header has one", () => {
  const scoped = knowledge.defaultScope("acme/checkout");
  assert.equal(scoped.kind, "repository");
  assert.equal(scoped.id, "acme/checkout");
  const options = knowledge.scopeOptions("acme/checkout");
  assert.equal(options[0].value, "all");
  assert.equal(options[1].value, "project");
  assert.equal(options[1].id, "acme");
  assert.equal(options[2].value, "repository");
});

test("Ask SWARM falls back to all connected knowledge without a repository", () => {
  const scoped = knowledge.defaultScope("");
  assert.equal(scoped.kind, "all");
  assert.equal(knowledge.scopeOptions("").length, 1);
});

test("knowledge status hides implementation details and reports counts", () => {
  const view = knowledge.formatStatus({
    enabled: true,
    automaticGeneration: false,
    lastRefresh: "2026-09-28T12:00:00+00:00",
    repositoriesIndexed: 3,
    issuesUnderstood: 12,
    relationshipsDiscovered: 40,
    generatedKnowledgeCount: 2,
  });
  assert.equal(view.enabledLabel, "On");
  assert.equal(view.repositoriesIndexed, 3);
  assert.equal(view.generatedEnabled, false);
  assert.equal(knowledge.formatStatus(null).lastRefresh, "Never");
});

test("citations keep provenance so answers stay explainable", () => {
  const label = knowledge.citationLabel({
    repository: "acme/checkout",
    title: "ADR 001 Kafka",
    provenanceKind: "source_fact",
  });
  assert.match(label, /acme\/checkout/);
  assert.match(label, /source fact/);
  const blocks = knowledge.answerBlocks({
    answer: "Kafka was selected for decoupling.",
    citations: [{ title: "Issue #417" }],
    sampleSize: 4,
    insufficientData: false,
    provider: "grok",
    model: "grok-4.6",
    effort: "low",
  });
  assert.equal(blocks.sample, "Sample size: 4.");
  assert.equal(blocks.model, "grok / grok-4.6 / low");
  assert.equal(blocks.citations.length, 1);
});
