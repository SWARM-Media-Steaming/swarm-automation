# Issue 205 independent UAT findings

This assessment adds tests only. No earlier expectations were revised, no
dispute was adjudicated, and no security-owned tests or suite metadata changed.
The issue-derived oracles are in `issue205_independent_contract.md`.

## First external startup bypasses manual activation (high impact)

Suite: `adversarial-205-model-calibration-cold-start-contract`

Reproduction: start with no calibration state and a bundled model priced at
2 input / 8 output. Invoke the real `model_calibration.main` desktop CLI with
`refresh --initiated-by STARTUP --source json --activation-policy manual`.
The offline transport fixture returns prices 50 input / 200 output.

Observed: the external prices become active immediately. If status is read
before the same request, the bundled prices remain active and the external
prices become a proposal instead. Holding the first source fetch also yields
`healthy=false` and no active version when status is read during refresh.

Issue basis: Startup Refresh requires loading known-good calibration before
external work; Manual Refresh prohibits silent replacement unless safe
automatic activation is configured (AC 4–6, 15, 17). The existing bundled
catalog is the known-good routing baseline before this feature creates state.
This tests the first **external startup**, without changing existing local or
injected-fixture bootstrap expectations.

Evidence: `src/main.rs`'s startup function invokes refresh without bootstrapping;
`model_calibration.py:1556` activates when `previous is None` regardless of
policy. Status bootstrap cannot acquire the lock held by that first fetch.

## Damaged live catalog silently restores retired routing (high impact)

Suite: `adversarial-205-model-calibration-live-catalog-recovery`

Reproduction: activate a calibration retiring `gpt-5.6-luna` and retaining
`gpt-6-astra`. Keep the healthy `calibration_active.json` copy, but replace
`active_catalog.json` with truncated JSON or `{"models": null}`. Read status
through a fresh service instance and resolve an actual dynamic-router choice.

Observed: status reports the same healthy activated version, while the router
selects the retired `gpt-5.6-luna`. Removing the catalog file entirely is a
passing control: status repairs it and routing selects `gpt-6-astra`.

Issue basis: the last known-good calibration remains usable and determines
active routing (AC 6, 16–17). A valid recovery copy exists in both failing cases.
Evidence: `model_calibration.py:1321` treats mere file existence as a valid
publication; `dynamic_router.py:642` falls back to the bundled catalog when
loading that publication fails.

## An unchanged check hides pending proposal details (medium impact)

Suite: `adversarial-205-model-calibration-review-transitions`

Reproduction: create a manual proposal, repeat the identical feed as STARTUP,
then render AI Configuration using the actual persisted status response.

Observed: activation remains offered for the unchanged pending version, but
the detailed changes are hidden. The proposal still contains the pricing diff.
Evidence: `ui/app.js:3645` gates the changes panel on the latest refresh being
`changed`, ignoring an existing proposal after a `no_change` result.

Issue basis: users must be able to review detailed pricing and routing impact
before activating a proposed calibration (AC 12–13 / Refresh Change Summary).

## Activation leaves a stale manual-refresh summary (medium impact)

Suite: `adversarial-205-model-calibration-review-transitions`

Reproduction: render a manual refresh response, invoke the real UI activation
handler, and return the backend's actual successful activation/status data.

Observed: the activate button disappears and the success toast appears, but
the summary still says that exact version is available for review.
Evidence: `ui/app.js:3616` prefers the cached manual result when refresh time
has not advanced; activation at `ui/app.js:4001` does not reconcile that cache.

Issue basis: refresh/proposal status must accurately describe the current
calibration (AC 9 and the AI Configuration visibility requirements).

## Executed checks

- All 17 pre-existing issue 205 adversarial suites passed unchanged.
- `cargo test --locked`: 89 passed.
- `npm test`: 92 passed.
- `python3 -m unittest discover -s issue_worker -p 'test_*.py'`: 574 passed.
- All three new registered commands collected tests and exited 1 on real
  assertions: 10 tests total, 6 failing and 4 passing controls.
- Registry validation confirmed all earlier suites and top-level metadata
  were retained exactly; new suites are enabled, nondisruptive, and limited
  to 120 seconds with deterministic offline fixtures.

No out-of-scope findings were identified by these checks.
