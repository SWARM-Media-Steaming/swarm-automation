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
separate Cargo project: run it in `web/`, see `.claude/rules/web-backend.md`). The web
job runner and orchestrator live in `web/` (`docs/web-architecture.md`); the desktop
cron installer is unchanged. Run checks
in the foreground. Independent suites under `tests/adversarial/` belong to the
adversarial tester; never weaken them to make a patch pass.

The hosted stack is `docker-compose.yml` (API, Postgres, MinIO, Docker job runner;
`docker compose up --build` after `cp web/.env.example .env`) and the AWS
infrastructure is Terraform in `web/infra/aws`, which nothing applies: do not add
a workflow that builds, pushes or applies. Validate deployment changes with
`terraform fmt -check -recursive && terraform init -backend=false && terraform
validate` in `web/infra/aws`, `docker compose config`, and
`python3 -m unittest test_web_deploy` in `issue_worker/` (the compose, Terraform,
CI and docs contract). See `.claude/rules/web-deployment.md`.
