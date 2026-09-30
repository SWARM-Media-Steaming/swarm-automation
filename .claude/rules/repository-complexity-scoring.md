# Repository-Aware Complexity Scoring Rules

Repository-aware complexity scoring is **always on**. It has no toggle, setting
or flag, for any repository or issue. Do not add one.

Pipeline (each stage has one owner; keep the boundaries):

    Repository -> deterministic analysis -> repository + component profiles
    -> issue -> relevant context -> Jev/AI complexity evaluation
    -> requirements vector -> dynamic model router -> worker
    -> adversarial UAT/Cyber -> actual execution metrics -> calibration

- **Measurement** (`issue_worker/repository_complexity.py`): local, pluggable
  analyzers (`Analyzer` protocol, `register_analyzer`) collect facts: files,
  LOC, languages, dependencies and graph depth, cyclomatic complexity, size
  distributions, tests, build/deploy/schema/API/infrastructure footprint,
  security-sensitive areas, Git churn and hotspots, Swarm history. Per-component
  profiles sit beside the repository profile. Never fabricate a metric: an
  unavailable one is recorded as unavailable and lowers confidence.
- **Lifecycle**: full scan when no compatible profile exists (schema or scoring
  version mismatch), diff-aware refresh when the default branch advances, and a
  weekly (or large-change-threshold) full verification. Profiles are cached and
  versioned (profile version, commit SHA, generation time, scoring version,
  analyzer versions). A historical score is never reinterpreted under a newer
  scoring version. Analysis failure must never block issue processing.
- **Interpretation** (`issue_worker/complexity_worker.py`): once per issue,
  before dynamic routing, Jev (when a valid candidate with usage) or the
  configured AI fallback receives a compact context (issue, relevant repository
  and component metrics, retrieved architecture context, similar-issue
  history) — never the repository. On failure a deterministic fallback vector is
  used with reduced confidence and the fallback is recorded.
- **Routing** (`dynamic_router.apply_complexity_requirements`, `model_router`):
  the router consumes the whole vector (implementation complexity, change
  surface, architecture risk, security risk, uncertainty, component complexity,
  confidence, context requirement), not one score. Capability, context and
  security floors are applied first; cost ranking happens only among models
  that meet them. Release upgrades must still satisfy the vector
  (`complexity_model_meets`). Manual selections stay pinned; Jev stays advisory
  (see `jev-decision-engine.md`); blacklist and no-static-table rules still
  apply (`model-blacklist.md`, `model-routing-no-static-tables.md`).
- **Audit**: the issue shows a `## Complexity Analysis` section followed by the
  `## Routing Decision`. The history database is the authoritative structured
  record (profiles, components, vector, requirements, scope, selected model,
  candidate scores, versions, commit) and the source for prediction-vs-actual
  outcomes.
- **Calibration** is deterministic, versioned and bounded: small historical
  samples must not dominate; no online self-modification of routing weights.

Any change to routing, the vector, or the scoring formula must bump the scoring
version, keep persisted data and the issue output in step, and update
`docs/complexity-scoring.md`, `skills/model-router/SKILL.md` and its tests.
