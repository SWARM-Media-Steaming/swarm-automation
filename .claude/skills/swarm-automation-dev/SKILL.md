---
name: swarm-automation-dev
description: Use when working on this repository (SWARM Automation, the Tauri desktop control center for AI issue workers) — its test conventions, its standalone (non-workspace) Cargo setup, and the history/status of the bundled issue_worker/ directory.
---

# Working in this repository

The issue-worker automation used to live inside the
[SWARM monorepo](https://github.com/DotNetRockStar/swarm) as
`scripts/issue_worker/` (a Python script suite, originally added by "Migrate
issue automation to Python with provider bots" (#90)). It was pulled out
into this standalone repo/app so it could be built and distributed
independently of any one target project — it's meant to run an issue worker
against **any** local Git checkout, not just SWARM's own.
Once the standalone app existed, SWARM's own copy was deleted from the
monorepo as dead weight (issue #169, "Clean up old issue worker scripts":
*"The issue worker was converted into the swarm automation project so we
need to remove the old script files from here"*, closed 2026-09-01). There
is no `apps/automation/` and no `scripts/issue_worker/` left in the SWARM
monorepo today — this repo's `issue_worker/` is the only copy that exists
anywhere, not a fork or vendored snapshot of something still maintained
elsewhere.

## This is a standalone Cargo package, not a workspace member

`Cargo.toml` has concrete `version`/`edition`/`license` and concrete
dependency version numbers — it does **not** use `.workspace = true` or
`{ workspace = true }` anywhere, because there is no workspace here. If
you're comparing this file against the version that still lives in
SWARM's `apps/server/Cargo.toml` (a real workspace member) and are tempted
to "fix" this to match that pattern, don't — that would break the build,
since there's no `[workspace]` root above this directory to inherit from.

## `issue_worker/` is the canonical copy — there is nothing left to sync with

`issue_worker/*.py` is not a git submodule, symlink, or otherwise
live-linked to anything else, but it is also **not a fork of a still-
maintained upstream** — see above, SWARM's own `scripts/issue_worker/` was
deleted once this repo took over. A fix or feature made here is the only
place it can be made; there is no other copy to port it to, and no
"upstream" that could ever drift ahead of this one again. If you're
tempted to go "sync this into SWARM's own copy too" — there is no such
copy anymore; the request is already satisfied by fixing it here.

The worker is **N-provider** (Claude / Codex / Grok — an open set defined
by `KNOWN_PROVIDERS` and `ProviderSpec` in `swarm_issue_worker.py`), unlike
the old two-provider Claude↔Codex rotation the original SWARM script had —
that divergence is now just this file's own history, not an open gap to
close. Adding a fourth provider = one entry in `KNOWN_PROVIDERS`, a
`<key>_capacity` method, a `_run_<key>` branch in `run_ai`, an entry in
`PROVIDERS` (`setup_github_bots.py`) and the loader tuple
(`github_app_auth.py`), plus `config.rs::KNOWN_PROVIDERS` and
`PROVIDER_META` / `HELP_TOPICS` in `ui/app.js` on the app side. If it
should be reachable by dynamic model routing, see "Dynamic model routing"
below for the further entries it needs.

Fresh-work usage probes are deliberately isolated per provider through
`Worker.enabled_provider_usages`: an unexpected usage exception marks only
that provider unavailable for the current scheduling pass, so a healthy
enabled provider can still receive the issue. Keep that isolation when
adding provider-selection callers or changing quota probes.

Related, still-accurate mechanics:

- `tauri.conf.json`'s `bundle.resources` entry (`"issue_worker/*.py":
  "issue_worker/"`) is what actually ships these files inside a packaged
  `.app`. `worker_script_dir()` in `src/main.rs` resolves that bundled
  `resource_dir()/issue_worker` path at runtime — there is no fallback to
  a target repository's own script directory; every run, against whatever
  repository is configured, uses this bundled copy. (`inspect_repository_
  path`'s `worker_available` flag checking a target repo for a leftover
  `scripts/issue_worker/install_swarm_issue_cron.py` is a legacy detection
  heuristic for the UI, not something the worker's own execution path
  branches on.)

## Feedback is app-wide, not tied to the header repository

The Feedback view reads the one app-wide `swarm-automation.sqlite3` database.
Its repository chips are intentionally independent of the header's active
repository: `AppConfig.feedback_repo_filter` stores repo ids for that page
only, with an empty vector meaning all repositories. The Rust history/grade
commands resolve a non-empty id list to GitHub repository names and pass
repeated `--repository` arguments to `ai_execution_history.py`; an empty list
passes no repository argument so Python performs one global SQL query. Keep
the grade summary, router matrix, adversarial aggregate, paging, and sorting
server-side over that filtered union rather than merging per-repo responses in
JavaScript. GitHub backlog import is the exception: it runs once per selected
configured repository and returns a success/failure result for each one.

## Dynamic model routing

Optional per-repo setting (`dynamic_model_routing`) that, when on, has one
of the enabled providers grade a new issue and pick which provider handles
it, instead of the operator always choosing one provider up front. Lives
mostly in `issue_worker/dynamic_router.py`; `swarm_issue_worker.py` calls
it from `apply_dynamic_routing`. It is a two-step decision, and both steps are
now genuine AI discretion bounded by operator settings:

1. **Which provider.** The router (an ephemeral, tool-free call to one
   provider's own CLI — see `run_provider_router`) grades the issue and picks
   `selected_provider` from the enabled candidates, weighing each one's
   `strengths` text (`_DEFAULT_PROVIDER_STRENGTHS` /
   `provider_strengths_preset` in `config.rs` — real input to the prompt,
   genuinely read by the router, not decorative; its UI textbox was removed
   2026-09-22 because an *editable* field next to model/effort looked like a
   rule the app enforced, when it is only advice the router can overrule).
   This is real AI discretion: `_select_candidate` can be overridden by the
   operator's `preferred_provider` tie-break or by the rework-favors-a-
   different-tool rule, but nothing here is mechanical.
2. **Which model, at which effort.** Also real AI discretion, since issue #176
   ("Dynamic Model Routing: add a cost-vs-best-model routing preference").
   `resolve_routing_decision` now honours the router's own `selected_model` /
   `reasoning_effort`, validated against the canonical cross-provider catalog
   (`_MODEL_CATALOG`) rather than re-derived from the complexity band. Before
   that issue the suggestion was deliberately discarded in favor of
   `tier_for_complexity(chosen.tiers, complexity)`; that lookup is still in the
   code and still matters, but only as the **fallback** path — do not read an
   older comment or docstring as saying the tier table is the sole source of
   the worker model.

   What governs the choice is the global `routing_optimization` setting
   ("Optimize routing for cost" on the AI Configuration page, `"cost"` or
   `"best"`, defaulting to `"best"`): `"cost"` tells the router to start from
   the least expensive capable catalog model and treat a frontier model as a
   last resort at complexity 9 or 10 (high risk may justify a capable mid-tier
   model); `"best"` tells it to ignore cost and fit the model to the task
   (explicitly *not* "always pick the top tier"). The prompt is the control —
   `resolve_routing_decision` still accepts a valid catalog name at any
   complexity.

   The complexity/risk/context scores are still graded and recorded — they are
   now inputs the router reasons over rather than a lookup key.

Three things keep that discretion from going wrong, and all three must survive
any later change here:

- **The prompt is grounded in the whole valid set.** `build_router_prompt`
  lists every model of every offered tool from `_MODEL_CATALOG`, filtered by
  `allow_usage_credit_models` (forwarded from the desktop the same way
  `routing_tiers` is) and by any model the provider just rejected in this run
  (`RouterCandidate.excluded_models`, from the credit-model re-route path).
- **One corrective retry, never a silent substitution.** A model outside that
  catalog raises `InvalidRouterModel`; `SwarmIssueWorker.resolve_router_
  response` spends exactly one follow-up call (`build_model_correction_prompt`,
  the original prompt plus the rejected name and the catalog restated) and
  takes that second answer.
- **Failure degrades, never blocks.** A second invalid answer — or a failed
  corrective call — resolves the first response through `tier_for_complexity`
  (`model_source: "tier"`), so the worst case is exactly the pre-#176
  behavior. The same tier path covers a decision whose *tool* pick was
  overruled, since the router's model then belongs to a different tool.

Since 2026-09-22, each model also carries a short built-in description of what
it tends to be good for (`_MODEL_DESCRIPTIONS` / `model_description`, now the
by-slug view of `_MODEL_CATALOG`) — Haiku-tier "fast, cheap, well-scoped"
through Opus-tier "large, ambiguous, high-risk" — plus a relative `cost` rank
comparable across providers, which is what makes "optimize for cost" a
cross-provider judgement rather than a per-tier one. These are shown in the
router prompt and folded into the stored `tier_explanation`, so they show up in
AI execution history and the routing notice posted on the issue. Unlike the
tier tables and provider strengths, this table has no `config.rs` counterpart
and nothing to keep in sync: it is never sent to the app or saved in
`config.json`, only built into `dynamic_router.py`. A model missing from it
cannot be routed to, so adding a model means adding it here.

Adding a provider's tiers or strengths without a matching edit on the other
side (Python vs. `config.rs`) is a real way to introduce drift — the router
prompt and the saved config would disagree about that provider's defaults. The
model catalog is the one exception: Python-only by design, see above.

## Issue outcomes and no-code flows

Every assigned issue, including one labelled `Question`, goes through the
normal provider capacity check and optional dynamic pre-flight grading/router
before its work prompt is built. Do not short-circuit routing based on labels.
After routing, the issue type constrains the selected worker's allowed outcome:

- Normal issues are unattended/autonomous. The AI resolves ordinary ambiguity,
  chooses the best maintainable approach, changes code, verifies it, and uses
  the usual commit/PR/`Ready For Testing` delivery flow. It must not ask the
  user about preferences or implementation choices.
- An issue labelled `Question` is a strict no-code task. The AI may inspect the
  repository with read-only commands, but must post a grounded answer using
  `SWARM_QUESTION_ANSWER`; it may not edit files or create a commit, and the
  worker must not apply `Ready For Testing`. A trusted follow-up comment may
  request clarification even though the answer has no commit.
- `SWARM_NEEDS_INPUT` is the narrow escape hatch for any issue type when work is
  genuinely impossible without credentials, authority, unavailable external
  information, or an external action only the user can perform. The issue is
  labelled `AI Needs Input` (and has `Ready For Testing` removed), receives one
  explicit action/question plus separate Summary, Recommendations, and
  Step-by-step guide sections, and remains dormant until a configured trusted
  follow-up author comments. Never request secrets in an issue; tell the user
  where to configure them and ask for a non-sensitive confirmation such as
  `done`.

Both no-code outcomes are durable GitHub lifecycle states with authenticated,
idempotent HTML markers. Selection and follow-up parsing must support them
without requiring a previous commit SHA. See
`.claude/rules/issue-lifecycle-comments.md` before changing their comment,
label, cursor, or resumption behavior.

## Multi-repository scheduling

`install_swarm_issue_cron.py` has two deliberately different scheduling
models. With the default sequential setting, the outer cycle visits each
repository once. With `--parallel-repos`, a long-running scheduler gives each
repository a persistent thread supervised by `run_parallel_repos`: after a
worker reports progress in continuous mode, that repository checks for its
next issue immediately and never waits for another repository's worker.
Polling intervals, scheduled wakeups, Run now requests, transcode deferrals,
and live `repos.json` reloads are supervisor concerns and must not reintroduce
a shared completion barrier. `--once` remains finite and uses `run_cycle` so
the caller can receive one aggregate exit status.

## Adversarial agents share one loop

Pre-delivery verification is an open set of adversarial agents, not a single
UAT feature. The durable loop — round counting, provider choice and dynamic
routing, quota pause/resume, framework bootstrap, edit validation, out-of-scope
finding dedup and filing, and hand-off to delivery — lives **once** in
`issue_worker/adversarial_core.py`. `AdversarialStage` describes one agent;
`AdversarialStageMixin` runs any of them.

Today there are two stages, each its own repository setting, run in this order
when both are on:

| | `adversarial_uat.py` | `adversarial_security.py` |
| --- | --- | --- |
| Setting | `adversarial_uat_enabled` | `adversarial_security_enabled` |
| State key | `adversarial` | `adversarial_security` |
| Test root | `tests/adversarial/` | `tests/adversarial/security/` |
| Suite origin | `adversarial` | `adversarial-security` |
| Result marker | `SWARM_ADVERSARIAL_RESULT:` | `SWARM_SECURITY_RESULT:` |
| Finding label | `adversarial-uat` | `adversarial-security` |
| Verdict | suite exit codes | suite exit codes **and** structured in-scope findings |

Adding a third agent should be a new `AdversarialStage` subclass, an entry in
`ADVERSARIAL_STAGES` and `Worker.adversarial_stages()`, a `RepoConfig` flag
plus its `--…-enabled` argument, and a UI toggle — not a second copy of the
loop. Anything that treats `"adversarial"` as a literal state key, suite
origin, or log prefix is a latent bug; use the stage. The rules files
`.claude/rules/adversarial-uat-testing.md` and
`.claude/rules/adversarial-security-testing.md` are the behavioural contract.

`adversarial_uat.py` re-exports the core's shared names (`MAX_ROUNDS`,
`DEFINITION`, `RESULT_MARKER`, `read_definition`, `run_suites`, …) because
registered adversarial suites under `tests/adversarial/` import them from
there; keep those aliases when moving code around.

## Per-prompt AI token usage is centralized, not per-agent

Every Claude/Codex/Grok invocation's token usage (issue #280) is captured at
the two chokepoints every agent already goes through, not inside each agent:
`Worker.run_ai` (primary implementation, and — via
`AdversarialStageMixin.run_adversarial_stage` — both adversarial stages) and
`Worker.run_router` (dynamic routing / pre-flight grading, including its one
corrective model-name retry). A new agent that calls through either of those
gets usage tracking for free; adding tracking inside a new agent directly is
the bug this design exists to prevent.

`issue_worker/token_usage.py` owns provider-agnostic normalization
(`normalize_claude_usage`/`normalize_codex_usage`/`normalize_grok_usage`,
dispatched by `normalize_usage`), cost estimation (`estimate_cost`, which
reuses `dynamic_router.model_cost`'s 1–5 relative rank — there is no other
per-model dollar pricing table in this app, so a rank change is the only
thing that ever needs to change a cost estimate), the `AgentType`/`PromptType`
enums, and the GitHub `### AI Usage` table renderer (`render_ai_usage_markdown`).
`Worker._record_usage_event` is the one place that normalizes, costs, logs
(`AI_USAGE_RECORDED`), and stashes a usage event; `record_ai_usage` and
`record_router_usage` are its two thin, context-specific callers.

`Worker.infer_ai_agent_context` reads the *existing* adversarial loop state
(`stage.key`/`phase`/`round` in the in-progress state file) to attribute a
`run_ai` call to `primary`/`adversarial_uat`/`adversarial_cybersecurity`
automatically — it does not add a parameter to `run_ai` for this, since that
would be exactly the kind of per-call-site wiring a future agent could forget.

Usage events accumulate in `Worker.token_usage_events` (mirrored into the
in-progress state under `token_usage_events` once that file exists, since a
pre-flight routing call can happen before it does — see
`save_new_state`/`_append_token_usage_event`) for the whole work-round, are
rendered into the existing single completion comment (`pending["ai_usage_
report"]` inside `render_pending_comment`, not a second GitHub comment), and
are persisted in one batch to the `ai_token_usage` table (migration 6 in
`ai_execution_history.py`) by `flush_token_usage_to_history` once
`finish_execution_history` confirms the work-round's `ai_executions` row
exists. That table is gated by `ai_execution_history_enabled` like every
other execution-history table (`adversarial_rounds` included); the GitHub
report itself is not — it renders from the in-memory/state event list
regardless of that setting, the same way `render_usage_report`'s quota lines
already do.

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
`install_ai_cli`, and `launch_bot_setup` — these spawn real child processes
(`python3`, `bash`, `npm`) and are better verified by an actual `npm run
dev`/`npm run build` + launch than by tests that would install real software
or make real GitHub calls.

Run with `cargo test`. The `test` job in `.github/workflows/ci.yml`
runs `cargo fmt --all -- --check` before clippy, tests, Python, and frontend
suites; a long line that rustfmt would wrap (common in `src/main.rs` unit
tests) fails CI even when `cargo test` is green. Format with `cargo fmt
--all` before committing Rust changes.

`issue_worker/test_swarm_issue_worker.py` is the Python-side counterpart —
`unittest.TestCase`-based, with a real local git remote/repo fixture per
test (`WorkerTestCase.setUp`: a bare `remote.git` plus a working checkout
with `main`/`ai-main`, so tests exercise real `git push`/`fetch`/branch
operations rather than mocking git itself; only `gh` calls need mocking,
via `mock.patch.object(worker.github, "gh", ...)`). **Run it with
`python3 -m unittest test_swarm_issue_worker` from inside `issue_worker/`,
not `pytest`** — pytest's module-level `setup_module` auto-detection
collides with this file importing `setup_github_bots` under a name pytest
mistakes for that hook, failing every test at collection with
`AttributeError: module 'setup_github_bots' has no attribute '__code__'`.

When testing or changing integration-branch creation, preserve its GitHub
deletion safeguard: before the worker first pushes a new integration branch,
it adds SWARM's named active repository ruleset with the `deletion` rule for
that exact ref. It deliberately uses the operator's administrator-authorized
GitHub CLI identity, not a worker GitHub App token, and must fail before the
first push if the safeguard cannot be verified or created. Do not replace this
with a broad branch-protection update, which could overwrite an existing
repository policy.
