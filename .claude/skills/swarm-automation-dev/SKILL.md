---
name: swarm-automation-dev
description: Use when working on this repository (SWARM Automation, the Tauri desktop control center for AI issue workers and UAT schedulers) — its test conventions, its standalone (non-workspace) Cargo setup, and the vendored issue_worker/ directory's relationship to the SWARM monorepo it originated from.
---

# Working in this repository

This app was extracted from `apps/automation/` inside the
[SWARM monorepo](https://github.com/DotNetRockStar/swarm) into its own
repository so it can be built and distributed independently of any one
target project — it's meant to run an issue worker and UAT scheduler
against **any** local Git checkout, not just SWARM's own.

## This is a standalone Cargo package, not a workspace member

`Cargo.toml` has concrete `version`/`edition`/`license` and concrete
dependency version numbers — it does **not** use `.workspace = true` or
`{ workspace = true }` anywhere, because there is no workspace here. If
you're comparing this file against the version that still lives in
SWARM's `apps/server/Cargo.toml` (a real workspace member) and are tempted
to "fix" this to match that pattern, don't — that would break the build,
since there's no `[workspace]` root above this directory to inherit from.

## `issue_worker/` is vendored, not shared

`issue_worker/*.py` is a hand-copied snapshot of SWARM's
`scripts/issue_worker/` at the time this repo was created. It is **not** a
git submodule, symlink, or otherwise live-linked to the SWARM repo.

**This copy has intentionally diverged from upstream.** The vendored worker
is now **N-provider** (Claude / Codex / Grok — an open set defined by
`KNOWN_PROVIDERS` and `ProviderSpec` in `swarm_issue_worker.py`), while
upstream `scripts/issue_worker/` is still a two-provider Claude↔Codex
rotation. Re-syncing is no longer a straight copy — port changes field by
field and keep the provider registry intact: `ProviderSpec`, the
`--enabled-provider` / `--<key>-model|effort|bin` flags, `choose_provider`'s
rotation, `review_provider`, `grok_capacity` / `_run_grok`, and the dynamic
`PREVIOUS_AI_RE` + branch-prune regex. Adding a fourth provider = one entry
in `KNOWN_PROVIDERS`, a `<key>_capacity` method, a `_run_<key>` branch in
`run_ai`, an entry in `PROVIDERS` (`setup_github_bots.py`) and the loader
tuple (`github_app_auth.py`), plus `config.rs::KNOWN_PROVIDERS` and
`PROVIDER_META` / `HELP_TOPICS` in `ui/app.js` on the app side.

Other ways the two copies relate:

- Editing a file under `issue_worker/` here only affects this app's bundled
  copy. It does not change SWARM's own live issue-worker automation, which
  runs its own independent copy of the same source.
- Conversely, a future improvement made in SWARM's `scripts/issue_worker/`
  (e.g. a new scheduling mode, a new CLI flag) will **not** automatically
  appear here. If `apps/automation`'s Rust code (in `src/main.rs`, notably
  `issue_worker_arguments()`) starts passing a flag this vendored copy
  doesn't understand yet, that's the signal to manually re-sync: diff
  SWARM's current `scripts/issue_worker/` against this directory and pull
  the relevant changes across by hand.
- `tauri.conf.json`'s `bundle.resources` entry (`"issue_worker/*.py":
  "issue_worker/"`) is what actually ships these files inside a packaged
  `.app` — see `worker_script_dir()` in `src/main.rs` for the runtime
  lookup order (a target repository's own `scripts/issue_worker/` first,
  falling back to this bundled copy).

## Test suite

`src/command_tests.rs` (registered from `src/main.rs` via `#[path]`)
invokes real `#[tauri::command]` handlers directly against a real, isolated
`AppState`/config-file/filesystem behind `tauri::test::mock_builder` — the
same shape SWARM's own `apps/server/src/gui_tests/` established (see that
project's `swarm-media-server-uat-tests` skill for the full rationale: no
reliable macOS UI-automation path exists today, and Tauri's simulated
IPC/ACL layer isn't usable under a bare `mock_context()`, so commands are
called as plain functions with a real `AppHandle<MockRuntime>` instead).

Two things every new command handler taking `tauri::AppHandle` needs to stay
testable this way:

1. Genericize it to `AppHandle<R: tauri::Runtime>` (mechanical — this repo's
   existing commands already show the pattern).
2. Route any `app.path().app_config_dir()`/`app_data_dir()` call through
   `AppState::test_data_dir` first (see `app_config_path`/
   `automation_log_path` in `src/main.rs`) — without this, parallel tests
   collide on the same real OS path, since `mock_context()`'s identifier
   defaults to empty.

Deliberately **not** covered by this suite: `start_issue_worker`,
`start_uat_scheduler`, `install_ai_cli`, and `launch_bot_setup` — these
spawn real child processes (`python3`, `bash`, `npm`) and are better
verified by an actual `npm run dev`/`npm run build` + launch than by tests
that would install real software or make real GitHub calls.

Run with `cargo test`.
