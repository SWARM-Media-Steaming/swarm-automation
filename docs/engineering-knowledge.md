# SWARM Engineering Knowledge

Persistent, source-backed engineering memory for SWARM Automation (issue #291).

## What is SWARM Engineering Knowledge?

It is local memory of the software systems you have connected to SWARM:
repositories, issues, pull requests, agent executions, adversarial findings,
and the decisions those records support. It is not a separate product
database. It is an index and relationship layer over the data SWARM already
stores.

## How does SWARM use my engineering data?

SWARM reads that data on this Mac. It does not upload your repositories or
prompts as part of Engineering Knowledge. When an agent starts an issue,
SWARM retrieves a small relevant **Knowledge Context Pack** and adds it to
the prompt. You can also ask questions in **Ask SWARM**. Optional generated
summaries spend extra AI tokens only when you turn them on.

## Architecture

```text
Connected repositories + swarm-automation.sqlite3 (system of record)
        │
        ▼
Knowledge providers (swarm execution, filesystem, git, future externals)
        │
        ▼
KnowledgeStore (objects, relationships, revisions, provenance)
        │
        ├── KnowledgeRetriever  → Ask SWARM / agent context pack
        ├── KnowledgeIndexer    → Refresh / Rebuild
        └── KnowledgeGeneration → optional summaries
```

The desktop (`src/main.rs`) shells out to `issue_worker/engineering_knowledge.py`
the same way it queries execution history. The issue worker uses the same
module in-process.

## Data flow

1. **Existing records.** `ai_executions`, `adversarial_rounds`, and
   `ai_token_usage` stay authoritative. Knowledge objects point at them with
   `source_kind` + `source_ref`.
2. **Refresh.** Incremental upsert of objects and relationships from those
   tables plus README/docs/ADRs/dependency manifests and a bounded git log.
3. **Rebuild.** Recreates derived/generated knowledge from source-of-truth
   data. Source-backed objects are upserted; historical revisions are kept.
4. **Ask SWARM.** Retrieve → compose a source-backed answer → optionally
   synthesize with the existing model router.
5. **Agent start.** Retrieve a bounded context pack, inject it, record the
   object ids and approximate context tokens.

## Knowledge object model

Each object has type, ownership/scope, repository, project, title/summary/body,
provenance, source provider/kind/ref/url, timestamps, commit/branch/revision,
and status (`active` / superseded).

Typical types: `environment`, `project`, `repository`, `issue`,
`pull_request`, `commit`, `file`, `component`, `decision`, `finding`,
`agent_execution`, `agent_round`, `documentation`, `generated_summary`,
`dependency`.

Provenance is one of:

- `source_fact`
- `generated_summary`
- `inferred_relationship`
- `generated_recommendation`
- `human_authored`

Generated interpretations never silently become facts.

## Relationship model

One generalized table. Types include `modifies`, `related_to`, `implements`,
`belongs_to`, `depends_on`, `affects`, `originated_from`, `discovered_by`,
`corrected_by`, `worked_on`, `produced`, `responded_to`, `verified_by`.
Relationships are queryable with bounded graph traversal.

## Retrieval

`KnowledgeRetriever` scores title/summary/search text, optional FTS5, and
type boosts. Scope filters: one repository, a project (GitHub owner), or all
connected repositories. A later vector implementation should replace this
class only.

## Source provenance

Every object records where it came from. Ask SWARM citations expose
repository, title, URL, and provenance kind so a user can see why SWARM said
something.

## Automatic context injection

When Engineering Knowledge is enabled, `Worker.build_prompt` appends a
bounded pack (default ~2500 tokens). Selection prefers similar issues,
decisions, and adversarial findings on related components. Failures are
warnings; they never block delivery. `finish_execution_history` incrementally
re-indexes the completed run, including UAT/security rounds.

Historical sample-backed signals (cost, models, prior findings) are passed
into the existing router prompt. They do not replace Dynamic Model Routing.

## Generated knowledge

Off by default (`automatic_knowledge_generation`). When on, refresh may write
repository, architecture, decision, component, risk, and issue-theme
summaries. Each generated object stores generation date, supporting sources,
and provenance `generated_summary`.

## Configuration

App-wide, on the Knowledge page:

- Engineering Knowledge (default on)
- Automatic knowledge generation (default off)
- Per-kind generate checkboxes
- `knowledge_context_token_limit` (clamped 200–20000)
- `knowledge_owner_scope_id` (default `local`)

## Extension

Implement `KnowledgeProvider` (`collect_objects`, `collect_relationships`)
and `register_provider`. A provider may contribute objects, relationships,
source references, timestamps, and ownership/scope. Do not add a
source-specific schema for Jira, Confluence, Slack, or similar.

## Historical preservation

Updates write a revision row before changing the live object. `get_object(..., as_of=)`
returns the snapshot that was live at that time.

## Token / cost history

Cost questions read `ai_token_usage` and matching `ai_executions`. Answers
include sample size. No estimate is invented when the sample is empty.

## Phase 2

Richer component/architecture graphs, issue clustering quality, cross-repository
impact analysis, deeper cost analytics, and optional semantic retrieval if it
proves useful — still without replacing the SQLite system of record in Phase 1.
