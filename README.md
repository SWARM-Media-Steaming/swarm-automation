# SWARM Automation

SWARM Automation is a macOS-first native desktop control center for running
an AI issue worker and (when the target repository has one) a closed-loop
UAT scheduler against **any local Git checkout on GitHub** — not just
SWARM's own repositories. It bundles its own Python issue-worker
implementation, so the repository you point it at doesn't need to carry any
automation scripts of its own.

The application can:

- start, pause, resume, and stop the issue worker and its entire process tree;
- run issue pickup continuously, daily, on weekdays, on selected days, or only
  when **Run now** is clicked;
- point the bundled issue worker at any local GitHub checkout and configure its
  assignee, provider, model, delivery, bot, quota, and notification settings;
- detect Git, GitHub CLI, Python, Node/npm, Claude Code, and Codex using the
  macOS login-shell path;
- install Claude Code or Codex with the vendor's npm package, and open Terminal
  for interactive provider sign-in;
- launch and verify a Claude Bot/Codex Bot GitHub App setup for the configured
  repository;
- supervise a target repository's own `scripts/tests/full_uat_cron.sh` when
  that frozen runner exists in the selected checkout; and
- stream child output to the UI and a local log.

Configuration is stored as mode `0600` JSON in the application's macOS config
directory. The optional SMTP password is stored separately in macOS Keychain.
Provider and GitHub credentials remain owned by their CLIs and are never copied
into the app configuration or automation log.

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
shared/linked dependency.

## Run from source

Prerequisites are Rust, Node.js/npm, Xcode command-line tools, Git, GitHub CLI,
and Python 3. At least one of Claude Code or Codex CLI must be installed for the
issue worker.

```bash
npm install
npm run dev
```

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
