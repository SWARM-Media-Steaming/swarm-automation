# Web backend rules

`web/` is the hosted, multi-tenant backend (Rust/axum, its own Cargo project;
`docs/web-architecture.md`, "Web backend", is the contract). It is additive: the
desktop (`src/`, root `Cargo.toml`) is not changed by it, and `issue_worker/`
stays the one shared worker.

- Keep `web/` a standalone package. Do not add `[workspace]` to either
  `Cargo.toml`, and do not make the desktop depend on it or the reverse. Run
  `cargo fmt --all -- --check`, `cargo clippy --all-targets -- -D warnings` and
  `cargo test --locked` in `web/` (CI does).
- **Tenant isolation is structural.** A handler that touches tenant data takes
  `TenantAccess` (it proves membership in the `{tenant}` path parameter and
  answers 404 otherwise); never read a tenant id from a body, header or query
  string. Every tenant-scoped `Store` method takes the tenant first, like
  `storage.py`; never add a tenant-less one. A new tenant-scoped route needs a
  row in `tenant_isolation.rs`'s endpoint list.
- **Secrets never leave.** Provider keys are write-only: no route, log line,
  error body or `Debug` output may carry one. Hold credentials in `Secret`, keep
  them out of `tracing` fields, and never echo a rejected request body. Plaintext
  leaves only through `Vault::job_environment`, one provider's key per job. New
  secret-bearing code gets a canary test in `secrets.rs`.
- Sessions and CSRF live in `auth.rs` extractors. State-changing routes must use
  `Authed`/`TenantAccess` (they enforce the token and `Origin`); the only
  cookie-less state-changing routes are the signed webhook and the bearer-token
  `/internal` API. No token goes in `localStorage`.
- Webhooks verify the signature over the raw body first and stay idempotent by
  delivery id and payload hash. Do not process before verifying.
- Usage is the worker's own record (`token_usage.UsageRecord`) priced by
  `usage_report.py`'s rule (tokens reported and a cost, else *unpriced*, never
  zero). Do not recompute prices here. A change to the fields `UsageRecordIn`
  reads updates `web/tests/fixtures/usage_record.json` and
  `issue_worker/test_web_usage_contract.py`. Remaining quota is `null`
  ("unavailable") when neither a budget nor a provider report exists; never
  invent a number.
- KMS is a `KeyWrapper` implementation, not a code path in handlers. Do not
  accept a KMS configuration this build cannot honor.
- The platform schema is `web/migrations/*.sql`, embedded as
  `swarm_web::schema::MIGRATIONS` and safe to apply twice (`psql -f`). It holds
  tenants, users (+ `user_identities`: GitHub is the only provider, never a
  password), sessions, memberships, the admin audit log, sealed provider-key metadata, quotas, budgets, the
  usage ledger, jobs, tenant documents and webhook deliveries. `postgres::PostgresStore` is the runtime store
  (`SWARM_WEB_STORE=postgres`, migrations applied at start) and `MemoryStore`
  serves tests; a `Store` change must pass `tests/store_contract.rs` on both; worker history and objects stay in `issue_worker/storage_remote.py`.
- `JobRunner` (`runner.rs`) is the only way a job container starts.
  `DockerJobRunner` and `EcsFargateJobRunner` share `JobSpec`, the image and
  `WORKER_ENTRYPOINT`. Docker is the local backend; Fargate is AWS. No
  Kubernetes and no Lambda for worker jobs. Contract tests use a fake docker
  CLI and a fake ECS endpoint. A live daemon is optional and must not be
  required for `cargo test`.
- The orchestrator (`orchestrator.rs`) is the web scheduler. It is
  webhook-driven (`issues`, comments, labels, pull requests) with `tick` as
  the poll and checkpoint resume. One container per repository. Exit 13 and
  quota pauses launch a fresh container from the stored checkpoint. Exit 14
  holds until Run now, Resume, a trusted follow-up, or a new image id. There
  is no default 15-minute job deadline. The desktop cron installer stays.
- A job's GitHub credential is one repository-scoped installation token from
  `github_app_auth.py mint-repository-token` (PEM on stdin, not argv, not
  cached). The container does not receive the app private key or cloud
  credentials. `job_launch.py` hydrates and publishes checkpoints around the
  unchanged worker.
- Member routes under `/tenants/{tenant}/work/...` belong in
  `tenant_isolation.rs`. `GET /events/jobs` is session-scoped because
  `ui/api.js` `listen()` does not substitute path parameters. Logs and SSE
  frames go through `redact_text`.
- **The desktop's commands are one table.** `web/src/catalog.rs` says what the web
  does with every `#[tauri::command]` and event: an endpoint, or `REMOVED` with a
  reason. The router mounts its routes from it (`api.rs`), `ui/api.js`'s `COMMANDS`
  / `EVENTS` mirror it and `docs/web-architecture.md` prints it; `api_catalog.rs`
  fails on drift. Adding a desktop command means a catalog row (or a removal), an
  adapter row, a docs row and a test; never hand-register a parallel route. Owner
  routes (settings, merge, promote, file an issue, activate) say `Access::Owner`;
  the tenant is only ever the path's, and `tenant_isolation.rs` already iterates
  the catalog.
- **Settings are `tenant_config` documents** written by `settings.rs` in the layout
  `desktop_import.py` uses, with the same credential and machine-path stripping.
  Do not let a credential-shaped key, a key block or a `*_bin`/`repo_dir` setting
  into a saved document, and keep provider keys on the write-only routes.
- **The worker computes what the desktop's Python helpers compute.** History, Jev
  feedback, usage, prompt grades, architecture docs and the routing calculator are
  operations of `issue_worker/web_bridge.py` reached through `bridge.rs` (stdin
  JSON, cleared environment, one tenant, repository ids resolved from that tenant's
  settings first). Add an operation there, not a second implementation in Rust. An
  operation the hosted deployment cannot serve yet is declared in
  `web_bridge.UNAVAILABLE` and answers 501 with its reason; never answer with
  made-up data.
- **SSE goes through `events.rs`.** Frames are redacted before they are stored,
  ids are process-wide and replayable from the bounded per-tenant ring, a stream
  is session-scoped with a heartbeat, and a slow client gets `resync`, never an
  unbounded buffer. Log lines pass through verbatim (the `Adversarial UAT for issue
  #...` / `Adversarial Cybersecurity for issue #...` formats the Overview replays).
- A behavior-changing web setting needs the `minor` label from a trusted author;
  the worker owns `VERSION`.
