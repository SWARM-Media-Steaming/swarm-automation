(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmArchitectureDocs = api;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  "use strict";

  // One canonical model (entities with provenance/confidence/evidence). The
  // persona only changes ordering, density and emphasis; it never changes the
  // content the model holds. Everything is rendered as escaped strings so the
  // module is unit-testable without a DOM.

  const PERSONAS = [
    { id: "engineer", label: "Software engineer", blurb: "Files, entry points, modules, dependencies and tests." },
    { id: "architect", label: "Software architect", blurb: "Boundaries, responsibilities, coupling and trade-offs." },
    { id: "security", label: "Cybersecurity engineer", blurb: "Trust boundaries, controls, attack surface and findings." },
    { id: "product", label: "Product owner", blurb: "Capabilities, ownership, delivery dependencies and risks." },
    { id: "executive", label: "Executive", blurb: "Purpose, value, operational health and major risks." },
  ];

  const PERSONA_IDS = PERSONAS.map((persona) => persona.id);

  // Section order and which sections start expanded, per audience.
  const EMPHASIS = {
    engineer: {
      order: ["components", "architecture", "data_flows", "technologies", "data_stores", "testing_ci", "integrations", "security", "deployment", "decisions", "risks", "system_context"],
      open: ["components", "architecture", "technologies", "testing_ci"],
      fields: ["responsibilities", "depends_on", "technologies", "steps", "evidence", "risks"],
      limit: 40,
    },
    architect: {
      order: ["architecture", "components", "data_flows", "integrations", "data_stores", "deployment", "decisions", "security", "technologies", "testing_ci", "risks", "system_context"],
      open: ["architecture", "components", "decisions", "deployment"],
      fields: ["responsibilities", "depends_on", "technologies", "risks", "evidence"],
      limit: 30,
    },
    security: {
      order: ["security", "data_flows", "integrations", "data_stores", "architecture", "components", "deployment", "risks", "technologies", "testing_ci", "decisions", "system_context"],
      open: ["security", "integrations", "risks"],
      fields: ["risks", "evidence", "depends_on", "responsibilities", "technologies"],
      limit: 30,
    },
    product: {
      order: ["system_context", "components", "data_flows", "integrations", "risks", "decisions", "technologies", "architecture", "deployment", "testing_ci", "security", "data_stores"],
      open: ["system_context", "components", "risks"],
      fields: ["responsibilities", "risks", "depends_on"],
      limit: 12,
    },
    executive: {
      order: ["system_context", "risks", "integrations", "components", "deployment", "decisions", "technologies", "architecture", "data_flows", "security", "testing_ci", "data_stores"],
      open: ["system_context", "risks"],
      fields: ["risks"],
      limit: 6,
    },
  };

  const PROVENANCE = {
    observed: { label: "Observed source fact", short: "Observed" },
    inferred: { label: "Inferred relationship", short: "Inferred" },
    ai_generated: { label: "AI-generated explanation", short: "AI-generated" },
    human: { label: "Human-authored decision or assumption", short: "Human" },
  };

  const FIELD_LABELS = {
    responsibilities: "Responsibilities",
    depends_on: "Depends on",
    technologies: "Technologies",
    steps: "Flow steps",
    evidence: "Evidence",
    risks: "Risks",
  };

  const ID_RE = /^[a-z0-9][a-z0-9_.-]{0,63}$/;

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function safeId(value) {
    return ID_RE.test(String(value || "")) ? String(value) : "";
  }

  function normalizePersona(persona) {
    return PERSONA_IDS.includes(persona) ? persona : "engineer";
  }

  function personaText(entity, persona) {
    const texts = (entity && entity.personas) || {};
    return String(texts[normalizePersona(persona)] || entity.summary || "");
  }

  function confidenceLabel(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "Unknown";
    if (number >= 0.85) return "High";
    if (number >= 0.6) return "Medium";
    return "Low";
  }

  function provenanceInfo(value) {
    return PROVENANCE[value] || { label: "Unspecified", short: "Unspecified" };
  }

  function entityIndex(model) {
    const index = new Map();
    ((model && model.entities) || []).forEach((entity) => index.set(entity.id, entity));
    return index;
  }

  function freshnessSummary(model) {
    const data = model || {};
    const pending = (data.pending || []).length;
    const through = data.documentedThrough ? String(data.documentedThrough).slice(0, 10) : "";
    switch (data.freshness) {
      case "current":
        return { label: "Current", tone: "running", detail: `Verified through commit ${through}` };
      case "pending":
        return {
          label: "Pending changes",
          tone: "paused",
          detail: `${pending} change${pending === 1 ? "" : "s"} not yet in the integration branch${through ? `; current through ${through}` : ""}`,
        };
      case "baseline":
        return { label: "Baseline", tone: "paused", detail: "Generated from file names only; no issue has been reviewed yet" };
      default:
        return { label: "Not available", tone: "stopped", detail: "No architecture documentation has been recorded yet" };
    }
  }

  function sectionsFor(model, persona) {
    const emphasis = EMPHASIS[normalizePersona(persona)];
    const titles = new Map(((model && model.sections) || []).map((section) => [section.key, section.title]));
    const grouped = new Map();
    ((model && model.entities) || []).forEach((entity) => {
      if (!grouped.has(entity.section)) grouped.set(entity.section, []);
      grouped.get(entity.section).push(entity);
    });
    return emphasis.order
      .filter((key) => grouped.has(key))
      .map((key) => {
        const all = grouped.get(key).slice().sort((a, b) => Number(b.confidence || 0) - Number(a.confidence || 0) || String(a.name).localeCompare(String(b.name)));
        return {
          key,
          title: titles.get(key) || key,
          open: emphasis.open.includes(key),
          total: all.length,
          entities: all.slice(0, emphasis.limit),
          hidden: Math.max(0, all.length - emphasis.limit),
        };
      });
  }

  function overview(model, persona) {
    const entities = (model && model.entities) || [];
    const byKind = (kind, count) =>
      entities.filter((entity) => entity.kind === kind).sort((a, b) => Number(b.confidence || 0) - Number(a.confidence || 0)).slice(0, count);
    const risks = entities.filter((entity) => ["risk", "assumption", "question"].includes(entity.kind)).slice(0, 5);
    return {
      freshness: freshnessSummary(model),
      purpose: byKind("purpose", 1)[0] || null,
      technologies: byKind("technology", normalizePersona(persona) === "executive" ? 4 : 8),
      components: byKind("component", normalizePersona(persona) === "executive" ? 5 : 10),
      risks,
      hasArchitecture: entities.some((entity) => entity.kind === "component"),
      hasFlow: entities.some((entity) => entity.kind === "flow"),
    };
  }

  // -- diagrams ------------------------------------------------------------

  function architectureGraph(model) {
    const entities = ((model && model.entities) || []).filter((entity) => ["component", "datastore", "integration"].includes(entity.kind));
    const ids = new Set(entities.map((entity) => entity.id));
    const edges = [];
    entities.forEach((entity) => (entity.depends_on || []).forEach((target) => {
      if (ids.has(target) && target !== entity.id) edges.push({ from: entity.id, to: target });
    }));
    return { nodes: entities.slice(0, 24), edges };
  }

  function flowGraph(model, flowId) {
    const index = entityIndex(model);
    const flows = ((model && model.entities) || []).filter((entity) => entity.kind === "flow");
    const flow = flows.find((entity) => entity.id === flowId) || flows[0];
    if (!flow) return { flow: null, nodes: [], edges: [] };
    const steps = (flow.steps || []).filter((id) => index.has(id)).slice(0, 12);
    const edges = [];
    for (let i = 0; i + 1 < steps.length; i += 1) edges.push({ from: steps[i], to: steps[i + 1] });
    return { flow, nodes: steps.map((id) => index.get(id)), edges };
  }

  // Layered layout: a node sits one column right of its deepest predecessor.
  // Bounded relaxation makes cycles terminate.
  function layoutGraph(graph) {
    const depth = new Map(graph.nodes.map((node) => [node.id, 0]));
    for (let pass = 0; pass < graph.nodes.length; pass += 1) {
      let changed = false;
      graph.edges.forEach((edge) => {
        const next = (depth.get(edge.from) || 0) + 1;
        if (depth.has(edge.to) && next > (depth.get(edge.to) || 0) && next < graph.nodes.length) {
          depth.set(edge.to, next);
          changed = true;
        }
      });
      if (!changed) break;
    }
    const columns = new Map();
    graph.nodes.forEach((node) => {
      const column = depth.get(node.id) || 0;
      if (!columns.has(column)) columns.set(column, []);
      columns.get(column).push(node);
    });
    const width = 150;
    const height = 46;
    const gapX = 52;
    const gapY = 20;
    const placed = new Map();
    let maxRows = 1;
    columns.forEach((nodes, column) => {
      maxRows = Math.max(maxRows, nodes.length);
      nodes.forEach((node, row) => placed.set(node.id, { node, x: 16 + column * (width + gapX), y: 16 + row * (height + gapY) }));
    });
    const columnCount = columns.size || 1;
    return {
      placed,
      width: 32 + columnCount * width + (columnCount - 1) * gapX,
      height: 32 + maxRows * height + (maxRows - 1) * gapY,
      nodeWidth: width,
      nodeHeight: height,
    };
  }

  function truncate(text, length) {
    const value = String(text || "");
    return value.length > length ? `${value.slice(0, length - 1)}…` : value;
  }

  function renderDiagram(graph, options) {
    const config = options || {};
    if (!graph.nodes.length) return "";
    const layout = layoutGraph(graph);
    const label = escapeHtml(config.label || "Diagram");
    const selected = config.selectedId || "";
    const edges = graph.edges
      .filter((edge) => layout.placed.has(edge.from) && layout.placed.has(edge.to))
      .map((edge) => {
        const from = layout.placed.get(edge.from);
        const to = layout.placed.get(edge.to);
        const x1 = from.x + layout.nodeWidth;
        const y1 = from.y + layout.nodeHeight / 2;
        const x2 = to.x;
        const y2 = to.y + layout.nodeHeight / 2;
        const back = x2 <= x1;
        const path = back
          ? `M${x1} ${y1} C${x1 + 30} ${y1 - 40}, ${x2 - 30} ${y2 - 40}, ${x2} ${y2}`
          : `M${x1} ${y1} C${x1 + 26} ${y1}, ${x2 - 26} ${y2}, ${x2} ${y2}`;
        return `<path class="diagram-edge" d="${path}" marker-end="url(#arrow)" />`;
      })
      .join("");
    const nodes = Array.from(layout.placed.values())
      .map(({ node, x, y }) => {
        const id = safeId(node.id);
        if (!id) return "";
        const classes = ["diagram-node", `kind-${escapeHtml(node.kind)}`, id === selected ? "selected" : ""].filter(Boolean).join(" ");
        return (
          `<g class="${classes}" role="button" tabindex="0" data-entity-id="${escapeHtml(id)}" ` +
          `aria-label="${escapeHtml(`${node.name}, ${node.kind}. Open details`)}" transform="translate(${x} ${y})">` +
          `<title>${escapeHtml(node.name)}</title>` +
          `<rect width="${layout.nodeWidth}" height="${layout.nodeHeight}" rx="9" />` +
          `<text x="${layout.nodeWidth / 2}" y="${layout.nodeHeight / 2 + 4}" text-anchor="middle">${escapeHtml(truncate(node.name, 22))}</text>` +
          `</g>`
        );
      })
      .join("");
    return (
      `<svg class="arch-diagram" viewBox="0 0 ${layout.width} ${layout.height}" role="group" aria-label="${label}" ` +
      `preserveAspectRatio="xMinYMin meet"><defs><marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">` +
      `<path d="M0 0 L8 4 L0 8 z" class="diagram-arrow" /></marker></defs>${edges}${nodes}</svg>`
    );
  }

  // -- details -------------------------------------------------------------

  function evidenceLabel(item) {
    const kinds = { path: "File", symbol: "Symbol", issue: "Issue #", pull_request: "PR #", commit: "Commit", test: "Test" };
    const ref = item.type === "commit" ? String(item.ref).slice(0, 10) : String(item.ref);
    return `${kinds[item.type] || "Ref"}${item.type === "issue" || item.type === "pull_request" ? "" : " "}${ref}${item.line ? `:${item.line}` : ""}`;
  }

  function renderEvidence(items) {
    if (!items || !items.length) return "";
    return (
      `<ul class="arch-evidence">` +
      items
        .map((item) => {
          const text = evidenceLabel(item);
          return `<li><button type="button" class="text-button compact" data-copy-ref="${escapeHtml(item.ref)}" title="Copy reference" aria-label="${escapeHtml(`Copy ${text}`)}">${escapeHtml(text)}</button></li>`;
        })
        .join("") +
      `</ul>`
    );
  }

  function renderDetail(model, entityId, persona) {
    const index = entityIndex(model);
    const entity = index.get(entityId);
    if (!entity) return `<p class="panel-copy">Select a component, flow, technology or diagram node to see details.</p>`;
    const active = normalizePersona(persona);
    const provenance = provenanceInfo(entity.provenance);
    const fields = EMPHASIS[active].fields;
    const pieces = fields
      .map((field) => {
        const value = entity[field];
        if (!Array.isArray(value) || !value.length) return "";
        if (field === "evidence") return `<h4>${FIELD_LABELS.evidence}</h4>${renderEvidence(value)}`;
        const items = value.map((item) => {
          if (field === "depends_on" || field === "steps") {
            const target = index.get(item);
            const id = safeId(item);
            return target && id
              ? `<li><button type="button" class="text-button compact" data-entity-id="${escapeHtml(id)}">${escapeHtml(target.name)}</button></li>`
              : `<li>${escapeHtml(item)}</li>`;
          }
          return `<li>${escapeHtml(item)}</li>`;
        });
        return `<h4>${FIELD_LABELS[field]}</h4><ul>${items.join("")}</ul>`;
      })
      .join("");
    const updated = entity.updatedBy && entity.updatedBy.issue ? `Last changed by issue #${escapeHtml(entity.updatedBy.issue)}` : "";
    return (
      `<div class="arch-detail-head"><p class="eyebrow">${escapeHtml(entity.kind)}</p><h3>${escapeHtml(entity.name)}</h3></div>` +
      `<p>${escapeHtml(personaText(entity, active))}</p>` +
      `<p class="arch-meta"><span class="status-pill ${entity.provenance === "observed" ? "running" : "paused"}" title="${escapeHtml(provenance.label)}">${escapeHtml(provenance.short)}</span> ` +
      `<span class="status-pill stopped">Confidence: ${escapeHtml(confidenceLabel(entity.confidence))}</span></p>` +
      pieces +
      (updated ? `<p class="panel-copy">${updated}</p>` : "")
    );
  }

  // -- page ----------------------------------------------------------------

  function renderEntityButton(entity, persona, selectedId) {
    const id = safeId(entity.id);
    if (!id) return "";
    const provenance = provenanceInfo(entity.provenance);
    return (
      `<li><button type="button" class="arch-item${id === selectedId ? " selected" : ""}" data-entity-id="${escapeHtml(id)}">` +
      `<strong>${escapeHtml(entity.name)}</strong>` +
      `<span>${escapeHtml(truncate(personaText(entity, persona), 140))}</span>` +
      `<small>${escapeHtml(provenance.short)} · ${escapeHtml(confidenceLabel(entity.confidence))}</small></button></li>`
    );
  }

  function renderOverview(model, persona, selectedId, flowId) {
    const view = overview(model, persona);
    const architecture = renderDiagram(architectureGraph(model), { label: "High-level architecture diagram", selectedId });
    const flow = flowGraph(model, flowId);
    const flowDiagram = renderDiagram(flow, { label: "Primary data-flow diagram", selectedId });
    const list = (title, entities) =>
      entities.length
        ? `<article class="panel"><div class="panel-header"><div><h3>${escapeHtml(title)}</h3></div></div><ul class="arch-list">${entities
            .map((entity) => renderEntityButton(entity, persona, selectedId))
            .join("")}</ul></article>`
        : "";
    return (
      `<div class="arch-overview">` +
      (view.purpose
        ? `<article class="panel span-two"><div class="panel-header"><div><p class="eyebrow">PURPOSE AND BOUNDARY</p><h3>${escapeHtml(view.purpose.name)}</h3></div></div><p class="panel-copy">${escapeHtml(personaText(view.purpose, persona))}</p></article>`
        : "") +
      (architecture ? `<article class="panel span-two"><div class="panel-header"><div><p class="eyebrow">DIAGRAM</p><h3>High-level architecture</h3></div></div>${architecture}</article>` : "") +
      (flowDiagram
        ? `<article class="panel span-two"><div class="panel-header"><div><p class="eyebrow">DIAGRAM</p><h3>${escapeHtml(flow.flow ? flow.flow.name : "Primary data flow")}</h3></div></div>${flowDiagram}</article>`
        : "") +
      list("Main components", view.components) +
      list("Primary technologies", view.technologies) +
      list("Top risks, assumptions and open questions", view.risks) +
      `</div>`
    );
  }

  function renderSections(model, persona, selectedId) {
    return sectionsFor(model, persona)
      .map((section) => {
        const items = section.entities.map((entity) => renderEntityButton(entity, persona, selectedId)).join("");
        const more = section.hidden ? `<p class="panel-copy">${section.hidden} more not shown for this audience. Switch audience for full detail.</p>` : "";
        return (
          `<details class="arch-section"${section.open ? " open" : ""} data-section="${escapeHtml(section.key)}">` +
          `<summary>${escapeHtml(section.title)} <span class="opt">${section.total}</span></summary>` +
          `<ul class="arch-list">${items}</ul>${more}</details>`
        );
      })
      .join("");
  }

  function renderPending(model) {
    const pending = (model && model.pending) || [];
    if (!pending.length) return "";
    const rows = pending
      .map((record) => {
        const touches = (record.touches || []).map(escapeHtml).join(", ");
        return `<li>Issue #${escapeHtml(record.issue)}${record.pullRequest ? ` (PR #${escapeHtml(record.pullRequest)})` : ""}: ${escapeHtml(record.reason || "Architecture change")}${touches ? ` — ${touches}` : ""}</li>`;
      })
      .join("");
    return `<div class="banner warning"><strong>Pending, not yet current architecture.</strong> These completed issues are not in the integration branch yet:<ul>${rows}</ul></div>`;
  }

  function renderPersonaBar(persona) {
    const active = normalizePersona(persona);
    return (
      `<div class="segmented arch-personas" role="radiogroup" aria-label="Documentation audience">` +
      PERSONAS.map(
        (item) =>
          `<button type="button" role="radio" aria-checked="${item.id === active}" tabindex="${item.id === active ? 0 : -1}" ` +
          `class="${item.id === active ? "active" : ""}" data-persona="${item.id}" title="${escapeHtml(item.blurb)}">${escapeHtml(item.label)}</button>`,
      ).join("") +
      `</div>`
    );
  }

  function nextPersona(current, key) {
    const index = PERSONA_IDS.indexOf(normalizePersona(current));
    const step = key === "ArrowRight" || key === "ArrowDown" ? 1 : key === "ArrowLeft" || key === "ArrowUp" ? -1 : 0;
    if (key === "Home") return PERSONA_IDS[0];
    if (key === "End") return PERSONA_IDS[PERSONA_IDS.length - 1];
    if (!step) return null;
    return PERSONA_IDS[(index + step + PERSONA_IDS.length) % PERSONA_IDS.length];
  }

  function renderPage(model, persona, selectedId, flowId) {
    const data = model || {};
    const freshness = freshnessSummary(data);
    const hasContent = (data.entities || []).length > 0;
    const header =
      `<div class="arch-status"><span class="status-pill ${freshness.tone}">${escapeHtml(freshness.label)}</span> ` +
      `<span class="panel-copy">${escapeHtml(freshness.detail)}${data.documentedAt ? ` · ${escapeHtml(data.documentedAt)}` : ""}</span></div>`;
    if (!data.enabled) {
      return `${header}<div class="banner policy"><strong>Off</strong><span>Turn on “Maintain interactive architecture documentation” in Repository settings to build this page.</span></div>`;
    }
    if (!hasContent) {
      return `${header}<p class="panel-copy">Nothing is documented for this repository yet. Sections appear here as evidence is recorded.</p>${renderPending(data)}`;
    }
    return header + renderPending(data) + renderOverview(data, persona, selectedId, flowId) + `<div class="arch-sections">${renderSections(data, persona, selectedId)}</div>`;
  }

  return {
    PERSONAS,
    EMPHASIS,
    escapeHtml,
    safeId,
    normalizePersona,
    personaText,
    confidenceLabel,
    provenanceInfo,
    freshnessSummary,
    sectionsFor,
    overview,
    architectureGraph,
    flowGraph,
    layoutGraph,
    renderDiagram,
    renderDetail,
    renderPage,
    renderPersonaBar,
    renderPending,
    nextPersona,
  };
});
