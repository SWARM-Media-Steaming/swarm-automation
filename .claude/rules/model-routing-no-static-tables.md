# No Static Routing Tables

Which model runs which complexity is computed, never stored. The scoring router
(`model_router.py`) reads the live catalog (refreshed Artificial Analysis
measurements, prices, the model blacklist, what each provider CLI offers) and
picks the cheapest capable model. Any table that names models per complexity band
goes stale the moment a new release lands, and a stored copy in `config.json`
silently overrides fresh data.

## What exists instead

- `dynamic_router.derived_routing_tiers(agent, ...)` asks the scoring router for
  every complexity 1-10 and merges neighbours that agree. `Config.tiers_for(key,
  excluded)` supplies the provider's configured model as the last resort for a
  score the router cannot decide. Use these for the router prompt's reference
  tiers, escalation ladders, diagnostics and the routing calculator.
- `dynamic_router.scored_floor(agent, complexity, ...)` is the one scored pick
  for a given score; the adversarial tester floor uses it.
- The worker ignores `--routing-tiers` / `--tiers`, and the desktop app neither
  stores nor sends `routing_tiers`; an old `config.json` that still has the key
  loads and drops it on the next save.

## Rules

- A model with no static price in `model_pricing.py` and no valid input/output
  price in its active feed calibration is never offered or routed to
  (`model_router.is_priced`, applied in `dynamic_router` scoring, the router
  prompt catalog, derived tiers, the tester floor and Jev's model list): its
  spend would be unrecorded and cost-first routing cannot compare it. Static catalog prices win when present; otherwise the feed rate records its
  spend automatically. Do not add a static row merely to onboard a feed model.
- Do not add a model name, per-band mapping or capability rank to code or
  `config.json` when the catalog, the measurements or the pricing catalog can
  supply it. New models arrive through discovery (`available_models`), the
  refreshed calibration and `models.yaml`; retirements are derived from release
  measurements or conditionally applied from `model-blacklist.json`.
- Starting worker and router models are derived too:
  `dynamic_router.suggested_defaults` (worker = the scoring router's pick for a
  simple task, router = its pick for a trivial one) via
  `routing_calculator.py defaults`. The desktop fills only empty settings
  (`AppConfig::apply_suggested_models`) and a saved choice is never replaced; an
  empty model reaches the worker as "auto". Do not put default model names in
  `config.rs`, `dynamic-routing-ui.js` or argparse defaults.
- The one remaining literal list is `fallback_models` in `src/tools.rs`, shown
  only when a provider CLI is not installed. Keep it free of blacklisted models
  (a test enforces this).
- Thresholds are policy, not data (`CAPABILITY_BANDS`, the expected-success
  floor, the frontier floor, upgrade tolerances). Keep them in one named
  constant or in `routing-rules.yaml`, not duplicated across files.
