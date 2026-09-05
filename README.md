# SWARM Automation

SWARM Automation is a macOS-first native desktop control center for running
an AI issue worker and (when the target repository has one) a deterministic
test scheduler against **any GitHub repository** — not just SWARM's own. You
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
  switch on its card, and pick which one wins ties;
- monitor multiple GitHub repositories in one scheduler, with an independent
  managed clone, assignee, branch policy, bot identities, and test scheduler for
  each repository;
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
- discover and run every suite a target repository declares in
  `.swarm/tests.json` — not just UAT — resolving tools, files, services, mounts,
  credentials, and Fire TV devices independently for each suite without using
  AI credits, and keeping a per-run history with per-suite pass / fail /
  skipped outcomes;
- explain any control in place through a click-to-open help modal, and carry a
  **Help** tab with a "how to get started" walkthrough; and
- stream child output to the UI and a local log.

Configuration is stored as mode `0600` JSON in the application's macOS config
directory; managed clones live under
`~/Library/Application Support/app.swarm.automation/checkouts/` unless a
workspace folder is set.
Provider and GitHub credentials remain owned by their CLIs and are never copied
into the app configuration or automation log.

## Branch safety model

The default AI integration branch is `ai-main` (the recommended name). Before
starting a fresh issue, the worker fetches `main`, merges it into `ai-main`, and
then creates exactly one issue branch named
`ai/<claude|codex|xai>/issue-<number>`. If a different provider continues the
issue later, it reuses that same branch. On GitHub remotes, the worker creates
the branch through the issue so it appears as a linked branch in the issue's
Development section. AI commit subjects start with the tool
identifier, such as `[codex]`, and the worker refuses to push an untagged commit.

Issue pull requests target `ai-main`; automatic approval and merging are both
off by default. An issue
branch cannot merge while its linked GitHub issue is open. After a person closes
the issue, the Repository view can squash-merge its PR and delete the branch; an
auto-merge profile performs the same reconciliation on a later cycle. Nothing
in the worker commits or pushes to `main`. A person can create or merge the
`ai-main` → `main` promotion pull request from the Overview or Repository view.
The Overview's explicit **Merge to Main** action first reconciles `main` into
`ai-main`, obtains a configured bot approval, and completes the promotion
through GitHub's pull-request protections.

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
automation scripts of its own. Test controls are repository-specific and driven
entirely by the repository's `.swarm/tests.json`; environment gates belong to
each suite's own `requirements`, not to this tool.

## Releases and self-update

Every push to `main` runs `.github/workflows/release.yml`: it runs the Rust and
Python test suites and, when they pass, builds a signed `.app` and publishes it
to GitHub Releases as `automation-v0.1.<n>` (the patch number is the commit
count). The app reads that feed and can update itself in place — **AI
Configuration is unrelated; the control is under Advanced → Software update**
with three modes:

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

## Repository test definitions

Add `.swarm/tests.json` to a repository to make its test inventory explicit and
portable. The checked-in definition in this repository is a working example.
When the selected repository does not have one yet, open **Test Scheduler** and
choose **Detect tests & create draft**. The app recognizes conventional Rust,
JavaScript, Python, Go, Gradle/Android, and `scripts/tests` entry points, then
shows an editable JSON preview. Detection never executes a discovered command.
After validation, **Save definition** creates `.swarm/tests.json` in the
repository and enables the scheduler immediately; commit the new file to keep
it with the project.
The current schema version is `1`:

```json
{
  "version": 1,
  "suites": [
    {
      "id": "backend",
      "name": "Backend tests",
      "command": ["cargo", "test"],
      "timeoutSeconds": 1800,
      "disruptive": false,
      "requirements": {
        "executables": ["cargo"],
        "files": ["Cargo.toml"],
        "servers": [
          { "name": "API", "host": "127.0.0.1", "port": 8080, "timeoutSeconds": 3 }
        ],
        "mounts": [
          { "name": "ROM share", "path": "/Volumes/roms", "kind": "smb" }
        ],
        "credentials": [
          { "name": "Test token", "environment": "TEST_TOKEN" },
          { "name": "Device key", "file": "~/.config/example/device-key.json" }
        ],
        "devices": [
          { "type": "fireTv", "input": "fireTvSerial" }
        ]
      }
    }
  ],
  "reporting": {
    "command": ["scripts/tests/report_results.sh"],
    "timeoutSeconds": 300
  },
  "failureTriage": {
    "command": ["scripts/tests/triage_failure_read_only.sh"],
    "timeoutSeconds": 300
  }
}
```

Suite IDs must be unique and use letters, digits, `-`, or `_`. Commands are
argument arrays and run directly from the repository root with closed stdin;
they are never placed in an interactive shell. `timeoutSeconds` defaults to
1800. Disabled suites and disruptive suites that have not been allowed in the
repository profile are skipped.

Every requirement is evaluated per suite. Missing equipment or configuration
marks only that suite as blocked/Skipped; it does not create a test failure or
stop eligible suites. When exactly one authorized device is returned by
`adb devices -l`, it is selected automatically. A saved repository choice is
reused when present, while multiple eligible devices produce Waiting for input
until a device is selected in Test Scheduler. The selected serial is exposed to
commands as `SWARM_FIRE_TV_SERIAL`.

Results are updated atomically after each state change in
`<run-dir>/test-results.json`. States are Ready, Running, Passed, Failed,
Skipped, and Waiting for input. Suite logs are retained beside the result file.
Each completed cycle is also archived under `<run-dir>/test-runs/` (newest 50)
and shown in the **Test runs** history in Test Scheduler, where opening a run
lists every suite it executed with its pass / fail / skipped outcome.
An optional deterministic reporting command runs after the cycle and receives
the result path in `SWARM_TEST_RESULTS`, allowing an existing GitHub issue
reporter to remain in place. Reporting and triage commands default to a
300-second timeout and also receive closed stdin. AI is never used for discovery, preflight,
execution, or structured reporting. `failureTriage.command` is invoked only
after a real failure and only when **Read-only AI triage on failure** is enabled;
it receives the same results environment variable and must not change the
checkout. Disable triage for an entirely zero-credit workflow.

The initial release targets macOS. Most backend supervision is Unix-compatible,
but interactive provider sign-in, Keychain integration, and packaging need
platform-specific work before Linux or Windows releases.

## Tests

```bash
cargo test
```
