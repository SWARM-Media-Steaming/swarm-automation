# Model Blacklist Rules

`skills/model-router/model-blacklist.json` is the one list of models that must
never be offered, chosen, or run. Every entry is an older release with a better
successor that the operator has confirmed; it is not a place for models that are
merely expensive, credit-billed (`USAGE_CREDIT_FAMILIES`), or unavailable to one
account.

## One list, two readers

- Python reads it through `issue_worker/available_models.py`
  (`blacklist`, `is_blacklisted`, `blacklist_successor`, `replace_blacklisted`).
- Rust embeds the same file at build time in `src/tools.rs`
  (`MODEL_BLACKLIST_JSON`, `is_blacklisted`, `without_blacklisted`).
- Never copy the list into code or a second file. Add or remove a model by
  editing the JSON only. `tauri.conf.json` bundles `skills/model-router/*.json`
  so a packaged app ships the same file.

## What a blacklisted model may and may not do

- It is dropped from every option list and from CLI discovery
  (`provider_models`, `available_models.configure`), so saved provider, router
  and tier selections are repaired into the same-family successor by
  `reconcile_config_models`.
- It stays in `models.yaml` and `_MODEL_CATALOG` as an inactive, deprecated
  peer so a newer discovered release can still infer its metadata from it.
  `model_router.load_model_catalog` forces `active=false`; `model_catalog`
  filters it out of the router prompt. Do not delete those rows.
- It can never be a routing, upgrade, Jev-recommended or tier target
  (`model_still_offered` is false for it and `ProviderSpec.from_args` swaps in
  the successor; reference tiers are derived from the catalog, so it never
  appears in them).
- A started session pinned to one finishes on it; the next fresh session, tester
  or fixer does not. `latest_release` moves a blacklisted model to its
  successor without a price comparison, but only when the successor has a price.

## When you add a model

- A release that supersedes another needs a `models.yaml` row and a
  `_MODEL_CATALOG` row (seed its capability rank from the measured Intelligence
  Index bands in `model_router.CAPABILITY_BANDS`), a price in
  `model_pricing.py` from the provider's own pricing page, then a blacklist
  entry for the release it replaces.
- The provider defaults (`swarm_issue_worker.py`, `src/config.rs`) must name a
  model that is not blacklisted; keep the Python and Rust copies in sync. There
  is no tier table to update: see `model-routing-no-static-tables.md`.
- Add or update tests: the blacklist tests in `test_available_models.py` and
  `src/tools.rs` should keep passing without edits when only the JSON changes.
