# Issue #278 independent acceptance findings

These tests extend coverage without revising earlier tests, suite entries, or
the framework choice. No dispute was supplied. All findings below concern the
new cybersecurity stage and are in scope; no unrelated finding was identified.

## Acceptance oracle

The issue requires independent verification of security fixes (AC 6/7), accurate
structured findings and history (AC 11–13), explicit review execution failures
(AC 17), and logs without credentials. The repository's cybersecurity rules
also make low-confidence findings advisory only. Consequently:

- Two independently exploitable checks remain two vulnerabilities even in one
  module; finding identity cannot be just the affected filename.
- Expanded evidence does not fix unchanged code. A later regression invalidates
  an earlier remediation claim.
- A speculative scope claim cannot suppress executable security evidence.
- Invalid reports and provider exceptions must record failure and permit retry.
- Error logs must redact secrets just as persisted execution history does.

The local attack fixture permits reads only by the owner, and deletion only by
an administrator who owns the record. Independent child processes test both
denied attacks and allowed operations. Provider responses are deterministic
doubles, not evidence of a live model's detection quality. GitHub and delivery
are intercepted; real git checkpoints and SQLite history are exercised only in
temporary repositories.

## Reproduced failures

1. **Speculation bypasses the review gate.**
   `test_speculative_scope_claim_cannot_remove_a_failing_security_check` leaves
   the owner-bypass fixture vulnerable and registers a deterministic exploit
   test. A low-confidence out-of-scope report names that suite. The worker
   excludes it, runs no validation, and records `PASS`. Expected: advisory
   findings do not change the blocking suite set. In
   `adversarial_core.py:run_adversarial_stage`, suite exclusions use every raw
   out-of-scope finding before the security stage filters actionable confidence.

2. **Filename identity loses findings and invents remediations.**
   `test_two_vulnerabilities_in_one_module_are_both_counted_and_verified`
   reports and repairs independent read and delete vulnerabilities in
   `access.py`; history records one discovered vulnerability instead of two.
   `test_fixing_only_one_of_two_same_file_findings_receives_partial_credit`
   fixes reads while deletion remains exploitable; history records zero fixed
   instead of one. `test_more_precise_file_evidence_is_not_itself_a_security_fix`
   expands one unresolved finding's affected files from `access.py` to
   `access.py, read_api.py`, without changing the code; history reports one
   verified fix instead of zero. The relevant functions are
   `adversarial_security.py:finding_identity` and `SecurityStage.record_round`.

3. **Reintroduced exploits retain verified-fixed credit.**
   `test_a_later_fix_that_reintroduces_a_vulnerability_revokes_fixed_status`
   first fixes reads, then repairs deletion while reintroducing the read bypass.
   Fresh reviewers and real tests continue detecting the read exploit through
   the cap. History nevertheless reports both findings fixed (expected one),
   and the issue-summary data still describes the read vulnerability as
   remediated. `SecurityStage.record_round` only appends fixed findings.

4. **A provider exception is absent from security failure history.**
   `test_missing_coding_executable_records_failed_review` invokes the real
   coding-provider adapter with no available executable. It raises the normal
   `WorkerError`, but `security_review_status` remains empty instead of
   `FAILED`. `run_adversarial_stage` handles nonzero return codes but does not
   classify exceptions from `run_ai` as a failed security attempt.

5. **Unknown suite references persist an unrecoverable response.**
   `test_unknown_suite_reference_records_failure_without_delivery` submits an
   otherwise valid report with a nonexistent suite ID. Validation raises
   `WorkerError`, but no failed security status is recorded.
   `test_invalid_reference_is_replaced_by_a_fresh_corrected_review_on_retry`
   offers a corrected response on the next provider invocation; retry instead
   replays the cached rejected response and fails forever without invoking the
   reviewer again. The reference-validation branch is outside the shared
   report rejection/recovery handler.

6. **Security setup errors disclose credentials in logs.**
   `test_security_setup_failure_redacts_credentials_from_operator_logs` injects
   an authentication error containing a synthetic GitHub-shaped credential.
   SQLite correctly redacts it, while the operator log contains the raw token.
   `adversarial_core.py:record_stage_failure` logs the unsanitized reason.

## Executed checks

| Command / registered suite | Result |
| --- | --- |
| `adversarial-278-evidence-lifecycle` | 4 tests collected, 4 assertion failures |
| `adversarial-278-failure-recovery` | 6 tests collected, 5 assertion failures; nonzero-exit/clean-retry control passes |
| `python3 -m unittest discover -s tests/adversarial/issue278 -p test_review_contract.py` | 15 passed |
| `node --test tests/adversarial/issue278/test_review_surfaces.js` | 4 passed |
| `python3 -m unittest discover -s issue_worker -p 'test_adversarial*.py'` | 60 passed |
| `npm test` | 69 passed |
| `cargo test --locked adversarial` | 2 passed |

The two new suites use explicit unittest discovery argv, deterministic local
fixtures, `origin: adversarial`, enabled/non-disruptive settings, and 300-second
timeouts. Both return nonzero on their current assertion failures. All 53
pre-existing suites and all registry metadata were checked against HEAD and
preserved. No product files, existing tests, or VERSION were edited.
