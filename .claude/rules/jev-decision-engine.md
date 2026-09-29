# Jev Decision Engine Rules

Jev is an advisory, typed decision layer inside Swarm Automation. It is not a
fourth implementation agent and it never owns the final workflow or model
selection decision.

## Authority and confidence

- Swarm remains the orchestration, policy, safety, and execution authority.
- Jev confidence below `0.70` is not actionable; use the configured rules/LLM
  fallback.
- Confidence from `0.70` through `0.89` may influence policy as an advisory
  signal.
- Confidence at or above `0.90` is eligible for normal automation, but still
  cannot override deterministic Swarm gates.
- UAT and Cyber finding decisions are security-sensitive and use the stricter
  default threshold of `0.95` for actionability.
- A high-confidence Jev `PASS` must never suppress a blocking security finding.
  Required UAT/Cyber, failed-test, completion, and other irreversible-action
  gates remain authoritative.

When changing Jev behavior, preserve the distinction between a recommendation
being recorded, a recommendation being actionable, and the final
`swarm_action` selected by policy.

## Model and cost routing

- Jev may be asked to recommend the least expensive available model capable of
  doing the task, using the supplied model capability and relative-cost data.
- Jev's `recommended_model` is advisory metadata. Do not apply it directly as
  the final provider/model/effort selection.
- Swarm's router must score available provider/model/effort candidates after
  capability, expected-success, safety, and context-fit gates.
- Automatic routing is cost-first. The default expected-success floor is
  `0.80`; candidates more than `0.03` behind the strongest expected-success
  candidate are excluded from cost optimization. Among the remaining capable
  candidates, lower estimated total dollar/token cost wins; latency is only a
  tie-breaker.
- Never let Jev, the router or an upgrade name a model on the operator's
  blacklist (`skills/model-router/model-blacklist.json`); see
  `model-blacklist.md`. A `recommended_model` that is blacklisted is treated as
  not routable.
- An automatic upgrade to a newer release of the same family (`latest_release`)
  is not a scoring override: it applies only to a model with a price in the
  pricing catalog, only when it is no dearer and not measurably weaker, and never
  to a session that has already started.
- Preserve explicit manual model selections and the no-Jev behavior when
  dynamic routing is disabled.
- Preserve baseline, Jev, and modified routing records separately. Never
  overwrite the Swarm baseline.

## Context and data handling

- Jev requests must remain structured typed state and questions, not raw
  unrestricted prompt dumps.
- Sanitize issue text, findings, and RAG candidates before transmission.
- Do not persist credentials, raw prompts, or unredacted Jev CLI output.
- Persist sanitized fingerprints, typed results, confidence, scores, reason
  codes, fallback state, final Swarm action, and eventual outcome.

## Source of truth

Keep these synchronized when changing the policy:

- `docs/jev-decision-engine.md` — public contract and behavior.
- `skills/model-router/SKILL.md` — routing algorithm and Jev interaction.
- `skills/model-router/routing-rules.yaml` — routing thresholds and weights.
- `skills/model-router/model-blacklist.json` — models that are never offered.
- `issue_worker/decision_engine.py` — confidence and safety enforcement.
- `issue_worker/dynamic_router.py` — Jev/routing combination.

The implementation is authoritative for exact enforcement. Any change that
weakens a deterministic safety gate requires corresponding tests and explicit
documentation updates.
