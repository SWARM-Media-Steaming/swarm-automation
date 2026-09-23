# Adversarial UAT rules

`adversarial_uat_enabled` is a repository setting, off by default. It replaces
`require_issue_tests`' same-session instruction. Question and no-code outcomes
never enter the loop. This behavior-changing setting requires the `minor`
label from a trusted author; the worker owns VERSION.

- Round zero is the independent assessment of the normal implementation.
  One counted round is one implementer fix plus one adversarial re-test.
  At most six counted rounds follow the assessment; clean-first-pass is zero.
- Each tester invocation has fresh context containing the issue/spec, current
  diff, trusted issue amendments and repository conventions, never the implementer's transcript or
  reasoning. Quota resume may continue the same unfinished phase. Provider
  capacity is checked again for each phase and the dynamic router is reused
  when enabled. Prefer another provider for testing; same-provider fallback
  still starts a fresh session. Dispute adjudication is always a new tester.
- Tests live under `tests/adversarial/`, registered with `adversarial-` IDs and
  `origin: "adversarial"` in `.swarm/tests.json`. Preserve non-adversarial suites.
  The existing scheduled runner and failure triage keep running these suites.
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
  the blocking suites. A finding may name out-of-scope `suite_ids`; these stay
  registered for scheduled runs but are excluded from this issue's blocking
  verdict after the separate issue is filed. A stable finding marker prevents
  duplicate auto-filing. Log both newly filed and deduplicated findings; keep
  each filed issue's title and URL in execution history so the app can surface
  the non-blocking follow-up to the user.
- All exchanges finish before delivery. Clean passes use normal delivery;
  cap-hit delivery explicitly disables automation and retains the PR and branch
  while the issue waits for trusted-author adjudication in AI Needs Input.
  Preserve the cap-hit PR body marker across scheduler runs: the PR reconciler
  must skip that marker even if automatic approval/promotion is enabled. Only
  a clean UAT follow-up removes the hold.
- State checkpoints retain the phase and cap across restarts and quota pauses.
  History is gated by `ai_execution_history_enabled`; migration 3 adds summary
  columns and per-round records to the existing database. Initial assessment
  is child round zero. Counts use test files changed and suites failing, since
  cross-language runners do not share a portable test-case count protocol.
  Capacity consumed is an approximate percentage-point drop across used
  providers' remaining-quota snapshots, never token/dollar cost.
- Emit the stable `Adversarial UAT for issue #...` boundary logs when an
  independent test, a fix/re-test round, a completed fix, or its re-test
  begins. The Overview panel replays these logs to show live progress, so
  preserve their issue number and round/max values when changing the loop.

Pilot on one repository before enabling across the fleet. Inspect the first
framework scaffold for each stack, particularly Rust/Tauri and Gradle/JUnit.
