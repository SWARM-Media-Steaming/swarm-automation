# SWARM Automation

SWARM Automation is a macOS-first native desktop control center for running
an AI issue worker against **any GitHub repository** — not just SWARM's own.
Test scheduling and execution belong in CI/CD, not this tool; per-issue
verification instead comes from **Adversarial UAT** (see below). You
give it `owner/name` and it clones the repo into a workspace it manages
itself (never a checkout you work in); a power-user override can point it at
an existing checkout instead. It bundles its own Python issue-worker
implementation, so the repository doesn't need to carry any automation
scripts of its own.

The application can:

- start, pause, resume, and stop the issue worker and its entire process tree;
- run issue pickup continuously, daily, on weekdays, on selected days, or only
  when **Run now** is clicked, working assigned issues highest-priority first
  (an `urgent`/`high`/`medium`/`low` label, with or without a `priority:`
  prefix; no label counts as low) and breaking ties by lowest issue number;
- drive up to three AI coding agents — **Claude Code**, **Codex CLI**, and
  **Grok Build** — giving each new issue to whichever included provider has the
  most usage remaining, and handing follow-up comments to a different agent than
  the one that did the previous pass; include/exclude each provider with a
  switch on its card. Choose **No preference** when you do not care who goes
  first, or pick one provider to win ties when remaining usage is equal;
- monitor multiple GitHub repositories in one scheduler, with an independent
  managed clone, assignee, branch policy, and bot identities for each
  repository;
- work the repositories one at a time with a single shared worker (the
  default), or turn on **One worker per repository** to run a worker for every
  repository at once — faster through a backlog, but AI credits are spent
  faster too;
- detect Git, GitHub CLI, Python, Node/npm, Claude Code, Codex, and Grok Build
  using the macOS login-shell path;
- install Claude Code or Codex with the vendor's npm package, run xAI's official
  installer for Grok Build in a Terminal window, and open Terminal for
  interactive provider sign-in;
- launch and verify a Claude Bot / Codex Bot / Grok Bot GitHub App setup for the
  configured repository;
- show the base, AI-integration, and active issue branches as a Git tree;
  optionally approve and squash issue pull requests into the AI-integration
  branch (a per-repository policy), delete their branches once the linked
  issue is closed, and expose the explicit human promotion gate;
- explain any control in place through a click-to-open help modal, and carry a
  **Help** tab with a "how to get started" walkthrough; and
- stream child output to the UI and a local log.

Configuration is stored as mode `0600` JSON in the application's macOS config
directory; managed clones live under
`~/Library/Application Support/app.swarm.automation/checkouts/` unless a
workspace folder is set.
Provider and GitHub credentials remain owned by their CLIs and are never copied
into the app configuration or automation log.

Repository Work Policy includes **Adversarial UAT** (off by default; CLI
`--adversarial-uat-enabled`, environment `SWARM_ADVERSARIAL_UAT_ENABLED`). It
replaces the same-session test instruction with a fresh independent coding
agent that derives tests from the issue specification. Tests live under
`tests/adversarial/` and are registered in `.swarm/tests.json` with an
`adversarial-` ID and `origin: "adversarial"`. Framework selection is
persisted in that manifest; the coding agent scaffolds it without a sign-off
gate. These suites run during the issue's own fix/re-test rounds only —
there is no scheduler in this app to re-run them afterward, so ongoing
regression coverage belongs in the repository's own CI/CD.

After the initial assessment, up to six implementer-fix/tester-retest rounds
run locally. Only a fresh tester can adjudicate disputed tests; the implementer
cannot modify them. Out-of-scope findings become separate labelled, assigned
issues. A clean pass follows normal PR delivery. A cap-hit still publishes the
branch and PR, bypasses automatic approval/merge/promotion, and marks the issue
**AI Needs Input** for a trusted-author adjudication. Quota pauses preserve the
phase and remaining rounds. Pilot this setting on one repository per stack
before enabling it broadly.

Optional AI execution history can be enabled under AI Configuration. It stores the
original issue, sanitized effective prompt, provider settings, lifecycle,
changes, delivery metadata, and failures in `swarm-automation.sqlite3` beneath
the configured worker state directory. Prompt-feedback upload is a separate
switch and is disabled by default; enabling local history never enables remote
upload. The database includes upload and reviewer-feedback state for a future
review-platform integration, but this release does not transmit records.

The **Feedback** tab reads that same local database for the selected
repository, split across three tabs:

- **Prompt grades** — the router's grade of each issue's original prompt, with
  the reason, the complexity score, and the AI platform, model, and reasoning
  effort that ran the pre-flight grading pass (usually not the platform that
  then worked the issue). Filter by grade, by search, or by grading platform.
- **Router activity** — one card per grading platform showing which router
  models it used and which AI tools it picked, with counts and percentages of
  that platform's graded issues. Select a platform or model to filter grades
  by what graded the issue rather than what worked it.
- **Execution history** — every AI execution grouped by issue and attempt,
  expandable to the original GitHub issue, the exact prompt submitted, the AI's
  summary of the requested and completed work, files/branch/commits/pull
  request, lifecycle notes and warnings, and any reviewer feedback once a
  review platform has provided it.

The execution view also shows a sortable UAT round column, per-round provider
pairings and disputes, and aggregate average rounds, clean-first-pass rate and
cap-hit rate. Counts are test files and failing suites. Quota consumption is an
approximate percentage-point drop from remaining-quota snapshots across used
providers, not metered token or dollar cost. Existing history databases migrate
automatically to schema 3 when history is enabled.

It is read-only and empty until "Store AI execution history" has recorded at
least one execution; grades and router activity also need Dynamic Model Routing
turned on.

## Branch safety model

The default AI integration branch is `ai-main` (the recommended name). Before
starting a fresh issue, the worker fetches `main`, merges it into `ai-main`, and
then creates exactly one issue branch named
`ai/<claude|codex|xai>/issue-<number>`. If a different provider continues the
issue later, it reuses that same branch. On GitHub remotes, the worker creates
the branch through the issue so it appears as a linked branch in the issue's
Development section. AI commit subjects start with the tool
identifier, such as `[codex]`, and the worker refuses to push an untagged commit.

On GitHub, the worker ensures a narrow, active repository ruleset blocks
deletion of the AI integration branch, including branches that already exist.
It does
not restrict normal pushes or pull requests. Deleting the branch remains
possible, but a repository administrator must first deliberately disable or
remove SWARM's named safeguard. The GitHub CLI identity that starts this first
run needs repository-administrator access; if it does not, SWARM stops before
continuing so the integration branch is never used without the safeguard.

Issue pull requests target `ai-main`; automatic approval and merging are both
off by default. An issue
branch cannot merge while its linked GitHub issue is open. After a person closes
the issue, an auto-merge profile squash-merges its PR and deletes the branch on
a later cycle. The
worker never commits or pushes to `main` directly. By default a person creates
or merges the `ai-main` → `main` promotion; the per-repository **Automatically
merge `ai-main` into `main`** toggle (off by default, next to the issue-PR
merging toggle on the Repository page) instead has the worker open, approve
(with another provider's bot), and merge that promotion PR itself after issue
PRs land, so finished work rolls up into `main` immediately. A promotion with
conflicts stays open for a person, and a failed promotion never affects the
issue's own delivery. A person can also create or merge the
`ai-main` → `main` promotion pull request from the Repository page's Promotion
queue. Its explicit **Merge to Main** action first reconciles `main` into
`ai-main`, obtains a configured bot approval, and completes the promotion
through GitHub's pull-request protections.

### Cleaning up branches no pull request will ever cover

An environment-only summary, a `Question` answer and a no-code `AI Needs Input` request
all finish without a commit, so no pull request is opened and the merged-PR
cleanup above never sees their issue branch. Those work-rounds now return the
checkout to `ai-main` and delete the empty branch locally and on the remote —
but only after proving the checkout is on that branch, the worktree is clean,
the branch carries nothing beyond the commit the attempt started from (locally
and on the remote), and GitHub confirms no open pull request uses it. Anything
unavailable or ambiguous keeps the branch and is logged; a cleanup problem is a
warning on an already-published result, never a failed run, and never hides a
branch that still exists.

Each worker cycle also reconciles historical `ai/*/issue-*` branches the same
way. A branch is only removed when it has never had a pull request, is not the
saved branch of an active, quota-paused, recovering or awaiting-input attempt,
carries no commits outside `ai-main`/`main`, and its issue has a terminal
no-code result in AI execution history or in the worker's own authenticated
issue comments. Having no pull request is never sufficient on its own — a live
attempt is legitimately between branch creation and PR publication.

### Monitoring GitHub Actions

The per-repository **Monitor GitHub Actions and fix failing pipelines** toggle
(off by default, on the Repository page) makes each worker run first look at the
newest Actions run of every workflow on `ai-main`. If one failed (failure,
timed out, or failed to start; in-progress and cancelled runs are ignored) and no
CI-failure issue is already tracking it, the worker files one issue — labelled
`bug` and `ci-failure`, assigned to the configured assignee, listing the failing
runs and the tail of their logs — and works it in that same run through the
normal start/complete/quota comments and delivery. The regular queue does not
also select that issue in that run, and nothing new is filed while a
`ci-failure` issue for the branch is open or was already filed for the same
head commit. Runs are read with the operator's own `gh` sign-in; if they cannot
be read, the worker logs it and carries on with the normal queue.

## Repository layout

```
src/            Rust backend (Tauri commands, process supervision, tool detection)
ui/             Frontend (plain HTML/CSS/JS, no build step)
issue_worker/   Vendored Python issue-worker implementation, bundled into every build
icons/          Application icons
capabilities/   Tauri v2 permission manifest
```

`issue_worker/` originated in, and stays in sync by hand with, the
[SWARM](https://github.com/DotNetRockStar/swarm) repository's
`scripts/issue_worker/` — SWARM's own automation still runs its own copy
independently; this is a vendored snapshot for this app to bundle, not a
shared/linked dependency. The desktop app always launches this bundled copy;
monitored repositories cannot override it with their own worker scripts.

## Run from source

Prerequisites are Rust, Node.js/npm, Xcode command-line tools, Git, GitHub CLI,
and Python 3. At least one of Claude Code, Codex CLI, or Grok Build must be
installed and signed in for the issue worker.

```bash
npm install
npm run dev
```

Or run `./scripts/run_now.sh`, which installs npm dependencies on a fresh
checkout if needed, starts the same `tauri dev` session, and on Ctrl+C also
kills the app binary that `tauri dev` otherwise leaves hidden in the menu bar.

Closing the window hides it to the menu bar and leaves active workers running.
Use **Quit and stop workers** in the menu-bar menu to terminate every supervised
process group and exit.

## Build the macOS application

```bash
npm install
npm run build
```

The packaged application includes the vendored Python issue-worker
implementation, so a selected repository does not need to contain any
automation scripts of its own.

## Releases and self-update

Every push to `main` runs `.github/workflows/release.yml`: it runs the Rust and
Python test suites and, when they pass, builds a signed `.app` and publishes it
to GitHub Releases as `v<version>`. Pushes to `ai-main` publish
`v<version>-beta.<run>` pre-releases.

The version is computed, not hand-edited. `VERSION` holds the version as of the
commit that last changed it, and every later commit on the branch adds one to
the patch (`0.1.1` → `0.1.2` → …; a promotion merge counts once). A minor bump
happens when a trusted author labels an issue `minor`; a major is a deliberate
human commit. See `.claude/rules/versioning.md`.

The app reads that feed and can update itself in place — the control is under
**AI Configuration → Software update** with three modes:

- **Don't check** — stay put until you update by hand.
- **Notify me** (default) — a banner appears; you choose when to install.
- **Install automatically on quit** — downloaded in the background, applied on
  the next quit.

**Check now** works in any mode. Updates install in place and restart the app;
configuration and running issue workers are untouched.

macOS builds are signed with a **self-signed** certificate (no Apple Developer
ID, not notarized). Its only job is a stable designated requirement so an
in-place update keeps the file-access grants you already gave. A *fresh DMG*
install still needs one right-click → Open the first time.

### One-time signing setup

```bash
scripts/generate-signing-material.sh
```

Generates the macOS signing certificate and the Tauri updater keypair, writes
the updater public key into `tauri.conf.json` (commit that), and offers to set
the repository secrets (`APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`,
`APPLE_SIGNING_IDENTITY`, `TAURI_SIGNING_PRIVATE_KEY`,
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD`). Re-running rotates both — see the script
header for the consequences.

## `.swarm/tests.json`

There is no in-app test scheduler, discovery UI, or suite runner — test
scheduling and execution belong in the repository's own CI/CD. The file
`.swarm/tests.json` still exists, but only as the registry **Adversarial
UAT** (above) writes to when it derives tests from an issue: suites there
carry an `adversarial-` ID and `origin: "adversarial"`, plus a persisted
`adversarialBootstrap` framework choice. Those suites run during that
issue's own fix/re-test rounds; nothing in this app re-runs them
afterward. A `.swarm/tests.json` left over from an older release of this
app (which did include a scheduler) is otherwise inert — it configures
nothing here.

The initial release targets macOS. Most backend supervision is Unix-compatible,
but interactive provider sign-in, Keychain integration, and packaging need
platform-specific work before Linux or Windows releases.

## Tests

```bash
cargo test
```
