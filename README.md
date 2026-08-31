# SWARM Automation

SWARM Automation is a macOS-first native desktop control center for running
an AI issue worker and (when the target repository has one) a closed-loop
UAT scheduler against **any GitHub repository** — not just SWARM's own. You
give it `owner/name` and it clones the repo into a workspace it manages
itself (never a checkout you work in); a power-user override can point it at
an existing checkout instead. It bundles its own Python issue-worker
implementation, so the repository doesn't need to carry any automation
scripts of its own.

The application can:

- start, pause, resume, and stop the issue worker and its entire process tree;
- run issue pickup continuously, daily, on weekdays, on selected days, or only
  when **Run now** is clicked;
- drive up to three AI coding agents — **Claude Code**, **Codex CLI**, and
  **Grok Build** — rotating over whichever ones you include in the flow;
  include/exclude each provider with a switch on its card, and pick which one
  is tried first;
- monitor multiple GitHub repositories in one scheduler, with an independent
  managed clone, assignee, branch policy, bot identities, and UAT process for
  each repository;
- detect Git, GitHub CLI, Python, Node/npm, Claude Code, Codex, and Grok Build
  using the macOS login-shell path;
- install Claude Code or Codex with the vendor's npm package, run xAI's official
  installer for Grok Build in a Terminal window, and open Terminal for
  interactive provider sign-in;
- launch and verify a Claude Bot / Codex Bot / Grok Bot GitHub App setup for the
  configured repository;
- show the base, AI-integration, and active issue branches as a Git tree;
  optionally approve and squash issue pull requests into the AI-integration
  branch, delete their branches, and expose the explicit human promotion gate;
- supervise a target repository's own `scripts/tests/full_uat_cron.sh` when
  that frozen runner exists in the selected checkout;
- explain any control in place through a click-to-open help modal, and carry a
  **Help** tab with a "how to get started" walkthrough; and
- stream child output to the UI and a local log.

Configuration is stored as mode `0600` JSON in the application's macOS config
directory; managed clones live under
`~/Library/Application Support/app.swarm.automation/checkouts/` unless a
workspace folder is set. The optional SMTP password is stored separately in
macOS Keychain.
Provider and GitHub credentials remain owned by their CLIs and are never copied
into the app configuration or automation log.

## Branch safety model

The default AI integration branch is `ai-main` (the recommended name). Before
starting a fresh issue, the worker fetches `main`, merges it into `ai-main`, and
then creates exactly one issue branch named
`ai/<claude|codex|xai>/issue-<number>`. If a different provider continues the
issue later, it reuses that same branch. AI commit subjects start with the tool
identifier, such as `[codex]`, and the worker refuses to push an untagged commit.

Issue pull requests target `ai-main`; automatic approval and merging are both
off by default. An issue
branch cannot merge while its linked GitHub issue is open. After a person closes
the issue, the Repository view can squash-merge its PR and delete the branch; an
auto-merge profile performs the same reconciliation on a later cycle. Nothing
in the worker commits or pushes to `main`. A person can create, review, and
explicitly merge the `ai-main` → `main` promotion pull request from the Repository
view.

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
automation scripts of its own. Full UAT controls are intentionally
repository-specific: they are enabled only when the selected checkout
contains `scripts/tests/full_uat_cron.sh`, and the desktop app invokes that
runner without modifying its test logic.

The initial release targets macOS. Most backend supervision is Unix-compatible,
but interactive provider sign-in, Keychain integration, and packaging need
platform-specific work before Linux or Windows releases.

## Tests

```bash
cargo test
```
