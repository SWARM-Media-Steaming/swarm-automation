# Issue #205: version, input and rollback boundary UAT

These expectations come from the issue's refresh/review workflow and acceptance
criteria, not from the current implementation's diff fields. No earlier test or
suite was revised. Product code, VERSION and security-owned tests are untouched.

## Expected contracts

- Review and rollback version handles identify one calibration snapshot. History
  pruning may make an old snapshot unavailable, but may not give its ID to other
  data. This follows from manual review and AC 16's version rollback requirement.
- Refresh detects meaningful routing inputs, even when the representative
  simulation's example winners stay the same. Supported reasoning levels and
  task strengths/weaknesses matter to other requests and provider constraints
  (refresh steps 4–7; AC 13, 18).
- A displayed refresh summary must remain associated with the version it
  describes, including after rollback and after reopening AI Configuration
  without an in-memory result cache (AC 9, 13, 16).

## Reproduced in-scope failures

### 1. Pruned version IDs are reused for different data

Suite: `adversarial-205-model-calibration-immutable-versions`

Create an offline baseline, then make successive manual JSON-source refreshes
with different prices on one fixed date. Observation 31 receives the ID
`2027-01-15-002`, already used by observation 1. A fresh service activating that
old review handle installs an output price of 31 instead of the reviewed price
of 1. Both tests fail on those concrete assertions.

`_version_for()` chooses the first unused sequence from only the remaining
active/proposed/history files. `_prune_history()` removes the evidence that the
old sequence was already issued. This can turn approval of an old review page
into activation of unrelated data; retaining the current active version alone
does not protect version identity.

### 2. Routing inputs disappear when example winners are unchanged

Suite: `adversarial-205-model-calibration-routing-input-fidelity`

Bootstrap two equal routable models, `alpha` and `omega`; the example routes
choose `alpha`. Change only `omega`'s supported efforts to `[low]`, or its
strengths/weaknesses to `[documentation]` in the local authoritative catalog.
`documentation` is a supported task-fit keyword outside the five examples.
All three refreshes incorrectly return `no_change`, discard the refreshed
definition and leave no version to review/activate. Identical input correctly
returns `no_change` in the control test.

`diff_calibrations()` compares selected price, benchmark, performance, status
and example-route fields but omits these routing inputs. `_refresh_locked()`
then discards the correctly built model definition. Tests also require the
definition to round-trip through activation and rollback once it is retained.

### 3. Reopened configuration attributes rolled-back savings to the active version

Suite: `adversarial-205-model-calibration-rollback-summary-integration`

Start with input/output prices 2/8, refresh output price to 4, activate the new
version, then roll back to the baseline. A fresh status response passed through
the production JS helpers reports `-36.8%` impact together with
`Active calibration (version 2027-01-15-001)`, although that baseline still has
output price 8. The changed data belonged to `2027-01-15-002`.

`refreshResult(status, null)` substitutes the currently active version for the
version that actually produced `last_diff`. The existing cached-result path is
correct; the new tests verify both cached and uncached paths. Clearing a stale
summary after rollback is also accepted, so no particular new UI is required.

## Commands and observed results

All fixtures use temporary state directories, fixed observation times and
mocked source transport. No external source or installed app is needed.

```sh
python3 -m unittest discover -s tests/adversarial -p test_model_calibration_immutable_versions.py -v
python3 -m unittest discover -s tests/adversarial -p test_model_calibration_routing_input_fidelity.py -v
python3 -m unittest discover -s tests/adversarial -p test_model_calibration_rollback_summary_integration.py -v
```

The exact registered commands collect 2, 4 and 3 tests respectively and return
exit code 1 on the reproduced defects: six failing assertions, three passing
controls. All 21 preexisting issue #205 adversarial suites pass. Rust's 89 tests
and the frontend's 95 tests pass. Existing suite definitions, framework choice
and manifest metadata are preserved; the three new suites are enabled,
nondisruptive and each has a 120-second timeout.
