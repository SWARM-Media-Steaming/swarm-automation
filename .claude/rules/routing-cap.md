# Routing cap rules

A repository may cap, per provider (Claude, Codex, Grok), the model and effort
Dynamic Model Routing may select (`routing_cap_<provider>_model` / `_effort` in
the repo config, sent as `--routing-caps` JSON). Logic lives in
`issue_worker/routing_cap.py`; do not add a parallel clamp.

- The cap is a final clamp after the router's pick, never a replacement for it.
  Compare with `model_router.estimated_dollar_cost` (effort aware); no rank or
  model-name table. Costlier pick -> the cap's exact pair; otherwise the pick stands.
- Record the cap separately (`routing_decision.routing_cap`: cap, raw router pick,
  applied, final pick, costs). Never overwrite the baseline, Jev recommendation or
  raw router pick.
- It bounds release upgrades (`latest_release(max_cost=)`), the tester floor and
  strict-epoch escalation (`cap_stage_choice`, which runs last). Escalation stops
  at the cap and must stay terminating; exit 13 behavior is unchanged.
- Manual selections, started sessions and Dynamic Model Routing off are uncapped.
- A retired cap model repairs to its successor; an unpriced one is ignored with a
  log. History schema is unchanged: SCHEMA_VERSION is pinned by an adversarial test.
- The cap does not change the vector or scoring formula, so `SCORING_VERSION` stays
  "1.0" (a protected adversarial test pins `v1.0`). Update
  `docs/complexity-scoring.md` and `skills/model-router/SKILL.md` with any change.
