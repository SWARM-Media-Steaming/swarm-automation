# Adversarial cybersecurity rules

`adversarial_security_enabled` is a repository setting, off by default. It adds
a second adversarial agent to the same pre-delivery loop as UAT, with security
as its exclusive focus, so an issue runs

    implementation -> adversarial UAT -> adversarial cybersecurity -> delivery

Each stage is an independent switch and either may run alone. Question and
no-code outcomes never enter the loop. This behavior-changing setting requires
the `minor` label from a trusted author; the worker owns VERSION.

### One framework, two agents

- The durable loop lives once, in `adversarial_core.py`: `AdversarialStage`
  describes an agent, `AdversarialStageMixin` runs it. `adversarial_uat.py` and
  `adversarial_security.py` are stage definitions plus their named entry
  points; `Worker.adversarial_stages()` is the ordered registry.
- Add or change an agent by editing a stage, never by copying the loop. Round
  counting, quota pauses, provider choice, dynamic routing, framework
  bootstrap, finding dedup, edit validation and delivery are shared and must
  stay shared. Anything that reads `"adversarial"` as a literal state key,
  origin or log prefix is a bug waiting for the second agent.
- Round zero is the independent review of the normal implementation; at most
  six counted fix/re-test rounds follow. Each tester invocation has fresh
  context — issue, amendments, diff, changed files, repository conventions and
  earlier recorded findings — and never the implementer's transcript.

### What decides the verdict

- UAT's verdict is suite exit codes alone. Security's is suite exit codes **and**
  a fresh reviewer's structured in-scope findings, because a leaked credential
  or a permissive IAM policy has no natural failing unit test.
- Only `high` and `medium` confidence findings act. A `low` confidence finding
  is recorded as advisory: it never blocks, never gets fixed, and never becomes
  a GitHub issue. This is the main defence against security-agent noise, along
  with requiring real code evidence and refusing stylistic hardening.
- A finding is verified fixed when a *new* reviewer stops reporting it. The
  fixer's own claim is never the evidence.
- Statuses are `PASS`, `FIXED`, `FINDINGS_CREATED` and `FAILED`. A review that
  could not execute records `FAILED` with `security_review_error` before the
  error propagates — it must never be indistinguishable from a clean pass. A
  cap-hit is also `FAILED`, distinguished by `security_outcome = "cap_hit"`.

### Scope, issues and the label

- A vulnerability introduced by the change, affecting the functionality being
  changed, exposed by this implementation, or needed for the issue to be
  securely implemented, is fixed inside this issue.
- Anything legitimate but out of scope is filed as its own issue through the
  same labelled, assigned helper the CI monitor uses, carrying description,
  affected files, why it is a concern, attack scenario, impact, evidence,
  remediation, severity, confidence and the issue it was found during. The
  dedicated `adversarial-security` label is created when missing and must stay
  independently searchable from `adversarial-uat`.
- Dedup twice: the stable per-issue finding marker, and a reworded-title check
  against still-open `adversarial-security` issues. GitHub failures while
  deduplicating or filing are retryable and must never abort the round.

### Tests and ownership

- Security regression tests live under `tests/adversarial/security/`,
  registered with `adversarial-security-` IDs and `origin: "adversarial-security"`.
  The UAT agent does not own that subtree, and the security agent does not own
  the rest of `tests/adversarial/`. Neither may change the other's suites, and
  no fixer of either stage may change any of them — the worker restores
  attempted edits and turns them into a dispute a fresh reviewer adjudicates.
- The security stage's blocking suites are its own **plus** the UAT suites, so
  a hardening change that breaks behaviour fails its round. A clean review may
  legitimately register no suite and write no test, so neither may fail closed.
- Tests must be deterministic and safe: local fixtures only, never an attack on
  an external system, never a destructive or uncontrolled action.

### Persistence, routing and observability

- Migration 5 adds `security_outcome`, `security_review_status`,
  `security_review_error`, `security_round_count`, `security_findings` and
  `security_filed_findings`, and widens `adversarial_rounds` to
  `(execution_id, stage, round_number)` so a UAT round 0 and a security round 0
  of one execution are distinct rows. History is gated by
  `ai_execution_history_enabled`.
- Routing reuses the existing framework. The router context names this a
  "Security / adversarial code analysis task"; do not add a second
  model-selection path.
- Emit the stable `Adversarial Cybersecurity for issue #...` boundary logs when
  an independent review, a fix/re-test round, a completed fix or its re-test
  begins, plus the analyzed-file count, per-round finding counts, filed and
  suppressed findings, and the completing or failing status. The Overview panel
  replays these, so preserve their issue number and round/max values. Never log
  a secret, credential, token or other sensitive value — a security reviewer
  quotes real code, so everything persisted goes through the history
  sanitizer.

Pilot on one repository before enabling across the fleet.
