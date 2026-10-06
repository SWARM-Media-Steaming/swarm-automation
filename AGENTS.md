# Repository instructions for Codex

This is the standalone CHOMP/SWARM Automation repository. `issue_worker/` is the
canonical Python worker; the desktop uses Tauri/Rust and `ui/` JavaScript.
Read `.claude/skills/swarm-automation-dev/SKILL.md` for architecture and test
conventions, and the relevant `.claude/rules/` files for the subsystem you change.

For native prompt caching/session work, follow `docs/prompt-caching.md` and
`.claude/rules/prompt-caching.md`. Preserve CLI authentication and compaction,
independent review sessions, existing routing priorities and nullable telemetry.
Do not add a UI toggle, direct inference API or application source-context cache.

Run Python tests with `python3 -m unittest discover -s issue_worker -p 'test_*.py'`
(the worker test module has a pytest hook-name conflict), frontend tests with
`npm test`, and Rust checks with `cargo test --locked` when relevant (the hosted backend is a
separate Cargo project: run it in `web/`, see `.claude/rules/web-backend.md`). Run checks
in the foreground. Independent suites under `tests/adversarial/` belong to the
adversarial tester; never weaken them to make a patch pass.
