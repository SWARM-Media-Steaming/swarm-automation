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

- ≥ 0.90 — eligible for normal automated use, subject to Swarm policy gates
- 0.70–0.89 — usable as an advisory signal; Swarm policy decides whether to
  proceed, override it, or fall back
- < 0.70 — Jev is not actionable; fall back to deterministic rules and/or a
  Claude/Codex/Grok oneshot
- UAT and Cyber finding decisions are security-sensitive for actionability and
  use the higher configurable threshold (default 0.95)

These are not authority thresholds. Even a result at 0.99 cannot override a
deterministic safety gate. In particular, a blocking security finding cannot
be passed, filed away, or marked out of scope merely because Jev says `PASS`.
Failed tests cannot be marked complete, and required UAT/Cyber cannot be
skipped. A low-confidence security result never suppresses a finding.

A recommendation that is not actionable is never echoed as Swarm's own action
when Swarm supplied no default: an irreversible one (`COMPLETE`, `FAIL`,
`SKIP_UAT`, `SKIP_CYBER`, or a security `PASS`) becomes the conservative action
(human review, `RUN_UAT`, `RUN_CYBER`, or `FIX_NOW`).

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
with bounded Jev inputs to produce the **modified/combined** score. Jev is
asked which available model is the least expensive one that can do the work
well, but its `recommended_model` answer is advisory metadata; Swarm does not
blindly apply it. Jev's validated task type, complexity, security-risk, and
related scores are inputs to the existing router.

Cost-first routing is the only automatic optimization mode: after capability,
expected-success, safety, and context-fit gates, the lowest estimated total
cost wins. Automatic cost routing first removes candidates below the default
0.80 expected-success floor, then excludes candidates more than 0.03 behind
the strongest expected-success candidate before cost-aware scoring. Dollar
cost and token efficiency are considered separately; latency is a tie-breaker
only. Manual model selections and disabled dynamic routing are not replaced. Models on
the operator's blacklist (`skills/model-router/model-blacklist.json`) are never
offered to Jev, recommended by it, or routed to.

The full routing policy lives in `skills/model-router/SKILL.md` and
`skills/model-router/routing-rules.yaml`.

## Persistence and Feedback

Every Jev decision is stored in `jev_decisions`. Baseline vs Jev vs combined
scores are stored in `jev_score_comparisons` without overwriting the baseline.
When Jev is off, rows are labelled baseline-only rather than showing a zero
Jev score. The Feedback **Jev scores** tab shows per-execution and aggregate
deltas, routing changes, completion, retries, fallback, latency, and
estimated cost. Success is measured first by task completion, then by lower
estimated cost per successfully completed task. Failures and retries are
never treated as savings.
The tab's provider/model, date range (from/to, inclusive), minimum score delta
and maximum Jev cost filters run in the history query, so they cover every page
of history. The Retries / failures card counts retried and failed outcomes.

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

## Implementation authority

This document describes the contract. The exact enforcement remains in
`issue_worker/decision_engine.py` (`confidence_band`, `may_act_on`, and
`swarm_policy_action`) and `issue_worker/dynamic_router.py`
(`apply_jev_signals_to_decision`). When documentation and implementation
appear to disagree, update both and treat the safety gates in code as the
behavior that must not be weakened.
