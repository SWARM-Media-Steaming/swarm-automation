# Issue #278 independent adversarial acceptance tests

The test oracle comes from the issue: a review must run before it can pass,
material in-scope findings require verified remediation, unrelated findings
remain separate, failures stay observable, and enabled reviews participate in
the existing lifecycle and router. No earlier expectations have been revised.

These are UAT tests of the cybersecurity feature, registered with
`origin: adversarial`; they do not belong to the security agent's separately
owned `tests/adversarial/security/` subtree. Their directory is intentionally
not a Python package, so the older broadly named rustfmt discovery suite does
not recursively collect this issue's new blocking assertions.

Run from the repository root:

```sh
python3 -m unittest discover -s tests/adversarial/issue278 -p test_review_contract.py
node --test tests/adversarial/issue278/test_review_surfaces.js
```

Both commands collect real tests and return nonzero on assertion failure.
The registered IDs are `adversarial-278-review-contract` and
`adversarial-278-review-surfaces`, each with a 300-second timeout.
The registered commands were also executed through the production `run_suites`
runner: 15 Python tests collected (6 pass, 9 fail), and 4 JavaScript tests
collected (1 pass, 3 fail). Both suites correctly exit 1 on the current product
code. The failures below are intentional regression assertions, not setup
errors or expected-failure annotations.

## Fixtures and boundaries

`harness.py` reuses only the established worker test setup: each test gets a
temporary git checkout, bare local remote and SQLite database. Coding providers
and GitHub are doubles. The default delivery boundary is intercepted, and the
cap test intercepts PR delivery before any GitHub operations. No real credentials
or external attack targets are involved. The token fixture is a recognizably
fake deterministic string already understood by the repository's sanitizer.

The representative attack is unauthorized access to someone else's record.
The vulnerable fixture returns true for `can_read('alice', 'bob')`; the repair
compares user and owner. Child-process unittest suites assert the access boundary,
and a separate functional suite asserts that owners can still read. The provider
double only simulates the agent's reports and edits; these tests establish the
orchestration contract, not a live model's detection quality.

## In-scope reproduction findings

All entries below concern the new cybersecurity stage or its integration.
They must not be excluded as out-of-scope suites.

| Test suffix / surface | Reproduced behavior | Requirement and code path |
| --- | --- | --- |
| `empty_json_is_not_evidence_that_security_analysis_completed` | `SWARM_SECURITY_RESULT: {}` reaches PASS and delivery with no analysis or finding arrays. | AC 5/17: `SecurityStage.parse_report` defaults missing findings and summary to empty values. A complete clean review without a suite is separately tested and allowed. |
| `successful_retry_replaces_the_failed_current_verdict` | A malformed first report followed by a valid clean review remains FAILED. | Retry/history contract: `record_stage_failure` saves `review_error`; successful rounds never clear the current error, and `SecurityStage.review_status` gives it precedence forever. |
| `failed_review_is_included_in_history_failure_rate` | A reviewer failure is stored as FAILED but the history aggregate reports zero reviews. | AC 13/17 and failure observability: `ExecutionHistoryRepository.security_summary` excludes attempts with empty `security_outcome`, exactly what `failure_history_fields` leaves. |
| `framework_setup_error_is_recorded_as_a_security_failure` | Framework setup raises `WorkerError` while `security_review_status` stays empty. | AC 17: setup/auth/provider errors outside the narrow report/session checks in `run_adversarial_stage` bypass failure recording. |
| `renaming_an_unfixed_vulnerability_does_not_verify_its_remediation` | Alternating two titles for the same unchanged access bypass produces two verified fixes, despite the final FAILED verdict. | AC 7/13: `finding_identity` uses title equality and `record_round` treats title disappearance as verification. Evidence, file and exploit remain unchanged throughout. |
| `a_failing_reproduction_overrides_a_reviewers_claim_of_remediation` | When the reviewer stops listing a finding but its explicitly associated exploit test still fails, metadata records one verified fix. | AC 7: `record_round` credits disappearance without requiring the finding's executable validation to pass. |
| `security_keeps_uat_scope_exclusions_without_refiling_unrelated_bugs` | UAT files and excludes a pre-existing unrelated functional failure; the security stage reruns it and exhausts six fix rounds. | Scope rules/AC 15: `initialize_stage` resets exclusions and security runs both origins without inheriting the completed UAT scope decision. The unrelated suite must remain registered, while the in-scope owner-access suite must still run. |
| `known_token_in_duplicate_finding_title_is_scrubbed_from_logs` | The second report of an already-filed security finding prints the fake token from its title verbatim. | Explicit no-secret logging requirement: `file_stage_findings`' already-filed path logs the raw title even though the existing history sanitizer recognizes the fixture. |
| `unresolved_security_cap_does_not_enter_successful_automatic_delivery` | After six rounds with the same unresolved access bypass, the pipeline calls PR delivery with `allow_automation=True`. | In-scope fix/verify requirement and repository cap-hold convention: `run_adversarial_pipeline` uses ordinary `finalize_issue` for cap hits, whose `deliver_pull_request` call enables normal automatic delivery. The test checks the delivery control rather than requiring any particular finalizer architecture. |
| History `reviewStatus` | The actual early-failure payload (`securityOutcome: ''`, status FAILED, error present) displays an em dash. | AC 13/17: `ui/adversarial-security.js` checks outcome before explicit failure status. |
| Live failed row | The worker's security failure log leaves the Overview security row running. | Failure observability: `ui/now-working.js` recognizes start/fix/retest logs but not failure boundaries. |
| Live completed row | The worker's security PASS completion log also leaves the Overview row running. | Execution status controls: the replay parser does not end the active security row at review completion. |

Passing controls cover implementation-triggered review, disabled bypass, security
task context on all dynamic-routing phases, malformed finding rejection, a
complete clean report with no security suite, and the full local
attack/fix/retest/separate-labelled-assigned-issue path. The frontend controls
preserve distinct rendering for legacy, disabled and completed clean reviews.

## Existing coverage

The registry's original suites and all non-suite metadata are retained exactly.
All 48 previously registered adversarial suites passed during this review, as
did 88 Rust tests, 69 frontend tests and the existing 60 focused UAT/security
worker tests. No unrelated failing suite was found in those checks.

The complete existing `issue-worker` suite collected 507 tests: 505 passed,
and two errored. Those two errors are outside #278 and must not block delivery:

- `WorkerTestCase.test_followup_recreated_github_branch_is_linked_to_the_issue`
- `WorkerTestCase.test_fresh_github_branch_is_created_from_the_issue`

Both fixtures call `prepare_repository`, which synchronizes a missing local
integration branch and invokes `protect_new_integration_branch`. The ruleset
lookup is not mocked, so their intentionally disabled GitHub binary executes
`/usr/bin/false api --method GET --paginate --slurp repos/DotNetRockStar/swarm/rulesets`
and raises `WorkerError`. This occurs before any adversarial stage. The two test
methods and all three implicated worker methods are unchanged from the parent
commit. An isolated rerun of those two tests reproduces both errors.

Retain suite ID `issue-worker` and file a separate test-fixture issue for the
worker to label and assign. Mock the integration-deletion safeguard API with a
valid fixture while preserving the branch-to-issue association assertions.
Neither the existing tests nor their registration is changed here.
