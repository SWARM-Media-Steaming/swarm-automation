const { test } = require("node:test");
const assert = require("node:assert/strict");
const docs = require("./architecture-docs.js");

function entity(overrides) {
  return {
    id: "api",
    section: "components",
    kind: "component",
    name: "API service",
    summary: "Serves requests.",
    personas: {},
    responsibilities: ["Routing"],
    depends_on: [],
    technologies: ["Rust"],
    steps: [],
    risks: [],
    evidence: [{ type: "path", ref: "src/api.rs", line: 12 }],
    provenance: "observed",
    confidence: 0.9,
    ...overrides,
  };
}

function model(extra) {
  return {
    enabled: true,
    freshness: "current",
    documentedThrough: "abcdef1234567",
    sections: [{ key: "components", title: "Components" }, { key: "risks", title: "Risks" }, { key: "data_flows", title: "Data flows" }],
    pending: [],
    entities: [
      entity({}),
      entity({ id: "db", name: "Store", kind: "datastore", section: "data_stores", depends_on: [] }),
      entity({ id: "web", name: "Web", depends_on: ["api"], personas: { executive: "Customer-facing site." } }),
      entity({ id: "flow1", kind: "flow", section: "data_flows", name: "Request path", steps: ["web", "api", "db"] }),
      entity({ id: "r1", kind: "risk", section: "risks", name: "Single point", provenance: "inferred", confidence: 0.5 }),
    ],
    ...(extra || {}),
  };
}

test("persona selection changes emphasis but reads the same canonical entities", () => {
  const data = model();
  const engineer = docs.sectionsFor(data, "engineer").map((s) => s.key);
  const executive = docs.sectionsFor(data, "executive").map((s) => s.key);
  assert.notDeepEqual(engineer, executive);
  assert.deepEqual([...engineer].sort(), [...executive].sort());
  assert.equal(docs.personaText(data.entities[2], "executive"), "Customer-facing site.");
  assert.equal(docs.personaText(data.entities[2], "engineer"), "Serves requests.");
});

test("all five audiences are available and unknown ones fall back", () => {
  assert.deepEqual(docs.PERSONAS.map((p) => p.id), ["engineer", "architect", "security", "product", "executive"]);
  assert.equal(docs.normalizePersona("nope"), "engineer");
});

test("empty sections are hidden and executive detail is bounded", () => {
  const keys = docs.sectionsFor({ entities: [entity({})] }, "engineer").map((s) => s.key);
  assert.deepEqual(keys, ["components"]);
  const many = { entities: Array.from({ length: 20 }, (_, i) => entity({ id: `c${i}`, name: `C${i}` })) };
  const section = docs.sectionsFor(many, "executive")[0];
  assert.equal(section.entities.length, 6);
  assert.equal(section.hidden, 14);
});

test("detail fields differ by persona and expose provenance and confidence", () => {
  const data = model();
  const engineer = docs.renderDetail(data, "api", "engineer");
  const executive = docs.renderDetail(data, "api", "executive");
  assert.match(engineer, /Evidence/);
  assert.match(engineer, /data-copy-ref="src\/api.rs"/);
  assert.doesNotMatch(executive, /Evidence/);
  assert.match(engineer, /Observed/);
  assert.match(engineer, /Confidence: High/);
});

test("rendered output escapes untrusted text and rejects unsafe ids", () => {
  const evil = model();
  evil.entities.push(entity({ id: "x", name: "<img src=x onerror=alert(1)>", summary: "<script>1</script>" }));
  evil.entities.push(entity({ id: 'bad"id', name: "Bad" }));
  const page = docs.renderPage(evil, "engineer", "", "");
  assert.doesNotMatch(page, /<img/);
  assert.doesNotMatch(page, /<script>/);
  assert.doesNotMatch(page, /data-entity-id="bad"id"/);
  assert.match(page, /&lt;img/);
});

test("diagrams are interactive, keyboard reachable and cycle-safe", () => {
  const data = model();
  const svg = docs.renderDiagram(docs.architectureGraph(data), { label: "Arch" });
  assert.match(svg, /role="button" tabindex="0" data-entity-id="web"/);
  const cyclic = { nodes: [entity({ id: "a" }), entity({ id: "b" })], edges: [{ from: "a", to: "b" }, { from: "b", to: "a" }] };
  assert.ok(docs.renderDiagram(cyclic, {}).includes("<svg"));
  assert.equal(docs.renderDiagram({ nodes: [], edges: [] }, {}), "");
  const flow = docs.flowGraph(data);
  assert.deepEqual(flow.nodes.map((n) => n.id), ["web", "api", "db"]);
});

test("freshness distinguishes current, pending, baseline and unavailable", () => {
  assert.equal(docs.freshnessSummary({ freshness: "current", documentedThrough: "abcdef1234" }).label, "Current");
  const pending = docs.freshnessSummary({ freshness: "pending", pending: [{}, {}] });
  assert.equal(pending.label, "Pending changes");
  assert.match(pending.detail, /2 changes/);
  assert.equal(docs.freshnessSummary({ freshness: "baseline" }).label, "Baseline");
  assert.equal(docs.freshnessSummary({ freshness: "empty" }).label, "Not available");
});

test("pending changes are shown apart from current architecture", () => {
  const data = model({ freshness: "pending", pending: [{ issue: 7, reason: "New queue", touches: ["Queue"], pullRequest: "9" }] });
  const page = docs.renderPage(data, "architect", "", "");
  assert.match(page, /Pending, not yet current architecture/);
  assert.match(page, /Issue #7/);
});

test("a disabled repository shows the off state, not speculative content", () => {
  const page = docs.renderPage({ enabled: false, freshness: "empty", entities: [] }, "engineer", "", "");
  assert.match(page, /Maintain interactive architecture documentation/);
});

test("persona keyboard navigation wraps and supports Home/End", () => {
  assert.equal(docs.nextPersona("engineer", "ArrowRight"), "architect");
  assert.equal(docs.nextPersona("engineer", "ArrowLeft"), "executive");
  assert.equal(docs.nextPersona("architect", "End"), "executive");
  assert.equal(docs.nextPersona("architect", "x"), null);
  const bar = docs.renderPersonaBar("security");
  assert.match(bar, /role="radiogroup"/);
  assert.match(bar, /aria-checked="true" tabindex="0" class="active" data-persona="security"/);
});
