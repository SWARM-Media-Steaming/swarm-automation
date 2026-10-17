# Issue #465 independent completion contract

The issue requires automatically closing an adversarial-created issue when its
work is completed, without marking it ready for human testing. Ordinary code
deliveries continue to leave issues open with the configured ready label.

Repository evidence used to turn that requirement into executable expectations:

- `issue_worker/adversarial_uat.py` and `adversarial_security.py` define the two
  origin labels carried by filed findings. `adversarial-protected` also routes
  human-created issues to adjudication, so that label alone is not an origin.
- `.claude/rules/issue-branch-delivery.md` requires a successful push and PR
  before announcing completed code work. Push or PR errors must leave the
  issue open and recoverable.
- `.claude/rules/issue-lifecycle-comments.md` requires an idempotent public
  completion receipt. Retrying GitHub delivery must neither lose the intended
  disposition nor post duplicate receipts.
- The development skill permits accepted environment-only/no-longer-reproduces
  resolutions. `run_selected_issue` calls `finalize_environment_only` and
  returns `ISSUE_COMPLETED_EXIT_CODE` for that accepted outcome. This is
  completed work with nothing remaining for a human to test, so #465 applies
  even though there is no new commit. A request for external input is instead
  paused work and must remain open.

The new `adversarial-issue465-completion-boundaries` suite exercises real local
Git pushes, durable checkpoints, completion rendering, fresh worker recovery,
both finding origins, all three worker providers, follow-up work, origin-label
lookalikes, delivery failures, dry run, paused work, and no-code resolutions.
Only GitHub responses and quota probes are simulated. Fixtures never contact
GitHub or invoke an AI CLI.

Initial verification: 12 test methods collected, nine passed and three failed.
Both accepted no-code origin cases remained OPEN; a configured close error was
never reached because no close was attempted. The no-code finalizer nevertheless
recorded local completion and removed the in-progress checkpoint. Those three
assertions describe one in-scope gap: accepted no-code adversarial resolutions
bypass automatic closure and its retry boundary.

The earlier `adversarial-issue465-finding-completion` suite collected and passed
all three tests. No earlier expectation, test, registration, security-owned
file, product source, or VERSION was revised.

The six pre-existing failing suites were also reproduced and retained. Their
calibration routing/catalog, UI fixture, and Jev dependency-fixture failures are
reported separately as out of scope; they do not become #465 blocking tests.
