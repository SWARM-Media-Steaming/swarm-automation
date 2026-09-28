# Issue #205 independent acceptance tests

The issue's requirements are the oracle. No earlier expectations were revised.
These tests use temporary state directories, fixed model data, mocked HTTP
transport, and the real adapters, calibration service, persistence, and router.
The UI suite renders real Python service responses through the calibration
controller in `ui/app.js`, using local DOM and Tauri doubles.

Each suite has an explicit command in `.swarm/tests.json`, with a 120-second
timeout. The suite ID prefix below is `adversarial-205-model-calibration-`.

| Suite suffix | Requirement and reproduced failure |
| --- | --- |
| `external-input-integrity` | AC 3, 15, 18: empty feeds and entirely unusable price rows report success, advance the success clock, and auto-activate missing prices over valid active prices. Covers custom JSON, models.dev, and Artificial Analysis. |
| `external-discovery-idempotence` | AC 18 and Notifications: an identical feed containing an already-discovered model creates another version and another startup discovery notification. |
| `simulation-eligibility` | Safe automatic activation, discovery gate, AC 6/15/17: retiring the only enabled model while discovering an unapproved replacement produces successful simulated routes through the unapproved model. Auto-activation then writes an empty live catalog. |
| `effort-benchmark-retention` | AC 13/18: changes to Terminal Bench, measured task cost, or measured runtime are discarded as `no_change` when the chosen model/effort stays the same. The task-cost fixture changes from $2 to $7; the computed 250% cost increase is discarded with the calibration. |
| `review-ui` | Model Table/Detail and Startup Status: external discoveries in the proposal have no inspectable model row; historical prices/benchmarks stored by the backend do not appear in model detail; startup source errors are omitted despite the backend recording them. |
| `lifecycle-integration` | Passing controls: exact startup interval boundary, manual bypass, outage/backoff behavior, usable active routing during an event-controlled refresh, concurrent refresh exclusion, all four initiators, explicit activation, and byte-identical rollback. |

Reproduce the Python suites, including the unchanged earlier calibration tests:

```sh
python3 -m unittest discover -s tests/adversarial -p 'test_model_calibration_*.py' -v
```

Reproduce the UI integration suite:

```sh
node --test tests/adversarial/test_model_calibration_review_ui.js
```

Every failure above is within issue #205. Tests for unrelated behavior were not
added to these suites. Product code, earlier tests, and security-owned suites
remain untouched.

Validation: each registered command collected its tests (22 new cases total).
Five new suites fail; the lifecycle integration suite passes. The 17 earlier
calibration tests pass unchanged. The existing framework commands also pass:
`cargo test --locked` (89), `npm test` (90), and Python unittest discovery under
`issue_worker` (566). No out-of-scope failure was found in these runs.
