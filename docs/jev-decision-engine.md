# Jev Decision Engine

Jev recommends bounded operational decisions. Swarm remains the orchestration
and policy authority.

## What Jev does

Jev is a fast, inexpensive typed decision layer. When enabled, it can:

- classify incoming GitHub issues (bug, feature, security, architecture, …)
- score pre-flight complexity, security sensitivity, RAG scope, UAT/Cyber need
- feed those structured scores into Dynamic Model Routing as extra inputs
- classify UAT and Cyber findings as in-scope / out-of-scope / fix-now / new issue
- score candidate RAG context before expensive LLM calls
- produce a structured completion assessment

Every result is a typed contract: enums, numeric scores, probabilities, and
reason codes. Prose is secondary metadata.

## What Jev does not do

Jev does not replace Claude, Codex, or Grok as implementation agents. It does
not replace the Swarm dynamic model router, benchmark/cost-based selection,
Adversarial UAT, Adversarial Cyber, or deterministic application rules.

Jev never independently:

- merges code or approves a pull request
- deletes data or changes repository permissions
- closes a blocking security issue
- suppresses a failed test
- disables Cyber or UAT that policy configured

Invalid or low-confidence output never directly controls an irreversible
workflow action. A low-confidence Cyber result never suppresses a finding.

## Architecture

```
GitHub → Swarm Orchestrator → Decision Engine (Jev / rules / LLM fallback)
       → decision signals → Swarm Router (policy + economics)
       → Claude / Codex / Grok → Implementation → UAT / Cyber
       → Jev advisory signals → Swarm final policy
```

The worker calls `DecisionEngine.evaluate(type, context)`. Jev is a CLI
adapter (`issue_worker/jev_cli.py`) following the same executable discovery,
timeout, retry, health check, structured output, and sanitized error handling
as the other AI CLIs. There is no direct HTTP client in this implementation.

## Confidence thresholds

Configurable on AI Configuration. Defaults:

- ≥ 0.90 — allow normal automated continuation
- 0.70–0.89 — Swarm policy decides whether to proceed or fall back
- < 0.70 — fall back to deterministic rules and/or a Claude/Codex/Grok oneshot
- Security-sensitive decisions use a higher configurable threshold (default 0.95)

## Fallback

Jev is optional. If it is disabled, missing, timing out, returning malformed
data, or producing low-confidence output, Swarm continues. Fallback order:

Jev → existing deterministic rules and/or the configured Claude/Codex/Grok
decision model.

When Jev is disabled, existing Swarm behavior is unchanged and Jev adds no
call, cost, or latency.

## Routing integration

Jev does not pick the final worker model. Pre-flight still runs the existing
Swarm grader/router. That result is the **baseline** score. Validated Jev
signals are stored separately. Swarm re-runs the existing scoring formula
with bounded Jev inputs to produce the **modified/combined** score. Cost-first
routing is the only automatic optimization mode: after capability,
expected-success, safety, and context-fit gates, the lowest estimated total
cost wins. Latency is a tie-breaker only.

## Persistence and Feedback

Every Jev decision is stored in `jev_decisions`. Baseline vs Jev vs combined
scores are stored in `jev_score_comparisons` without overwriting the baseline.
When Jev is off, rows are labelled baseline-only rather than showing a zero
Jev score. The Feedback **Jev scores** tab shows per-execution and aggregate
deltas, routing changes, completion, retries, fallback, latency, and
estimated cost. Success is measured first by task completion, then by lower
estimated cost per successfully completed task. Failures and retries are
never treated as savings.

GitHub execution comments include a concise `### Jev Decision Engine` section.
Raw prompts, credentials, and unredacted CLI output are not persisted or posted.

## Configuration

AI Configuration → Jev Decision Engine:

- Enable Jev Decision Engine
- Decision uses (pre-flight, workflow, UAT, Cyber, RAG, triage, completion)
- Confidence thresholds and fallback
- Timeout, retries, model/version, CLI path

Credentials stay with the Jev CLI (`JEV_API_KEY` / `TYPESAFE_API_KEY` or the
CLI's own sign-in). Swarm never logs secrets.
