# Adversarial UAT rules

`adversarial_uat_enabled` is a repository setting, off by default. It replaces
`require_issue_tests`' same-session instruction. Question and no-code outcomes
never enter the loop. This behavior-changing setting requires the `minor`
label from a trusted author; the worker owns VERSION.

- The loop itself is shared with the adversarial cybersecurity agent and lives
  in `adversarial_core.py`; `adversarial_uat.py` is only the UAT stage
  definition plus its named entry points. Change the loop there, for both
  agents, rather than adding a UAT-only branch. See
  `adversarial-security-testing.md`. When both settings are on, UAT runs first
  and cybersecurity reviews the result.
- Round zero is the independent assessment of the normal implementation.
  One counted round is one implementer fix plus one adversarial re-test.
  At most three counted rounds follow the assessment inside one epoch.
  Three counted rounds are one epoch; clean-first-pass is zero counted rounds.
  `adversarial_best_effort_merge` (default off, never inferred) chooses what
  an exhausted epoch does. Off, persist the epoch and immediately start another
  escalated epoch: re-run dynamic routing, raise reasoning effort, and change
  model/provider or strategy when progress stalls. Merge only after the
  acceptance policy passes. On, stop after the first epoch and deliver the
  latest commit as **Best-effort merge with unresolved adversarial results**.
  That delivery merges into the integration branch and promotes to the base
  branch only when both automatic promotion and automatic issue-PR merging are
  on. It must not become `AI Needs Input` solely because findings remain.
- Each tester invocation has fresh context containing the issue/spec, current
  diff, trusted issue amendments and repository conventions, never the implementer's transcript or
  reasoning. Quota resume may continue the same unfinished phase. Provider
  capacity is checked again for each phase and the dynamic router is reused
  when enabled. Prefer another provider for testing; same-provider fallback
  still starts a fresh session. A tester never runs below a floor: the model the
  scoring router picks for a STANDARD task (complexity 4, `scored_floor`, never
  the operator's saved tier table) at medium effort or better, and, on the same
  provider, no weaker than the first assessment's tester (`apply_tester_floor`,
  `tester_reference`).
  The stage router grades the pass against the change under test, so a small
  change can otherwise draw a model too light to follow the edit rules. A
  rejected tester result restores the edits and tells the next attempt which
  rule it broke and what to do instead (`rejection_remedy`). Listing one more file in an existing adversarial suite's
  `requirements.files` is not a revision (`_only_gains_required_files`) only when
  each added entry is an existing regular file inside the repo (a missing path
  could switch the suite off in the repository's own tooling); any other change
  to an existing suite entry, or to an existing test file (even a pure append,
  which can neutralize earlier tests without deleting a line), still is. A tester
  whose result was rejected steps up rather than repeating: higher effort after
  the first rejection, then one capability level per further rejection
  (`apply_tester_floor`). It gets no extra context or permission. Dispute adjudication is always a new tester.
- Tests live under `tests/adversarial/`, registered with `adversarial-` IDs and
  `origin: "adversarial"` in `.swarm/tests.json`. Preserve non-adversarial suites.
  `tests/adversarial/security/` and `origin: "adversarial-security"` belong to
  the cybersecurity agent; UAT neither owns nor may edit them.
  This app has no test scheduler: these suites run only during this issue's own
  fix/re-test rounds. Nothing here re-runs them after delivery — ongoing
  regression coverage belongs to the repository's own CI/CD.
- The fixer cannot change, disable or retire adversarial tests. The worker
  restores attempted edits. A fresh tester may revise earlier expectations
  only in response to a dispute, with a recorded resolution grounded in the
  specification. The tester may scaffold test framework manifests but cannot
  repair product code. Executable suite exit codes determine the outcome.
- `ai_test_assist bootstrap` returns a plan without writes or command execution.
  Manifest detection comes first; language-native frameworks cover otherwise
  empty stacks; AI inference handles unrecognized stacks when available. The
  full coding CLI applies the scaffold autonomously. Persist the chosen plan
  as `adversarialBootstrap` metadata and reuse it on subsequent issues.
- Out-of-scope findings include reproduction evidence and go through the same
  labelled, assigned issue-creation helper as the CI monitor. They do not enter
  the blocking suites. GitHub failures while deduplicating or filing them,
  including an exit-zero malformed `gh issue list --json` response, are
  retryable and must not abort the original issue's round. A finding may name
  out-of-scope `suite_ids`; these stay registered in `.swarm/tests.json` but
  are excluded from this issue's blocking verdict after the separate issue is
  filed. A stable finding marker prevents duplicate auto-filing. Log both newly
  filed and deduplicated findings; keep each filed issue's title and URL in
  execution history so the app can surface the non-blocking follow-up to the
  user.
- All exchanges in the current epoch finish before delivery. A clean pass uses
  normal delivery. With best effort explicitly enabled, exhaustion of the first
  epoch files the unresolved notes in a separate labelled follow-up issue, then
  delivers the latest commit. The stage remains `FAILED` with outcome
  `cap_hit`, labelled **Best-effort merge with unresolved adversarial results**,
  and the original issue does not wait for trusted-author input. Strict mode
  does not deliver on exhaustion. Older cap-hit PR markers still block
  automatic release until every stage outcome is `clean_first_pass` or
  `resolved_after_n`; a best-effort delivery opts in with its own PR notice.
- State checkpoints retain the phase, epoch, and round across restarts and
  quota pauses. After three strict epochs in one process the worker exits 13
  and the scheduler resumes the same checkpoint; that exit is progress, not a
  failure and not `AI Needs Input`. History is gated by
  `ai_execution_history_enabled`. Migration 3 adds summary columns and
  per-round records; migration 9 adds epoch rows, fingerprints, merge policy,
  delivery, unresolved-at-merge state, and promotion result. Initial assessment
  is child round zero. The first epoch's boundary logs stay `N of 3`; a later
  epoch appends ` in epoch N`. Counts use test files changed and suites failing,
  since cross-language runners do not share a portable test-case count protocol.
  Capacity consumed is an approximate percentage-point drop across used
  providers' remaining-quota snapshots, never token/dollar cost.
- Emit the stable `Adversarial UAT for issue #...` boundary logs when an
  independent test, a fix/re-test round, a completed fix, its re-test, a
  strict-mode epoch, or a best-effort delivery begins. The Overview panel
  replays these logs, so preserve their issue number and round/max values.
  Each phase also logs `Adversarial UAT for issue #...: fixer|tester <Provider> model
  <model> with effort <effort>.` (`AdversarialStage.attribution_log`, shared by both
  agents) so the Overview names the agent; history rounds show fixer/tester effort,
  `Not recorded` for legacy rows. Never log reasoning text.

Pilot on one repository before enabling across the fleet. Inspect the first
framework scaffold for each stack, particularly Rust/Tauri and Gradle/JUnit.
