# Web architecture

SWARM Automation gains a hosted, multi-tenant web version alongside the Tauri
desktop app (tracked in #413). The desktop stays fully working; `ui/` and
`issue_worker/` are shared. This document records the pieces as they land.
It covers the storage seam and its hosted Postgres/S3 implementation, the
desktop data importer, the web backend (API, auth, tenancy, provider keys,
usage and quotas), the job runner (one container per repository, Docker
locally and ECS Fargate on AWS), and the REST and Server-Sent Events API that
covers every desktop command.

## Storage

`issue_worker/storage.py` is the one seam between the worker and where its
state lives. It is standard-library only, so the worker image never imports
desktop code (`test_storage.WorkerImageTests` enforces this for every
non-test module in `issue_worker/`, and runs `worker_entrypoint.py` from an
isolated copy of the directory).

### Tenant scoping

Every operation takes a `tenant` as its first argument; there is no
tenant-less call. A tenant id matches `[a-z0-9][a-z0-9_-]{0,62}` and anything
else raises `StorageError`. The desktop and the worker use `DEFAULT_TENANT`
(`"default"`). `LocalStorage` keeps the default tenant at the exact on-disk
locations the app has always used and puts any other tenant under
`<state_dir>/tenants/<tenant>/` (its own history database included). A hosted
implementation maps the tenant to a database tenant and an object-store prefix.

### Operation groups

| Group | Methods | Holds |
| --- | --- | --- |
| Checkpoints | `read/write/delete/list_checkpoint(s)` | Resumable per-issue state, a JSON object per `(kind, key)` |
| Documents | `read/write/delete/list_document(s)` | Durable product data: `architecture_docs` (one snapshot per repository) and `tenant_config` (settings: key `app`, and `repo-<id>` per repository; written by the hosted backend and the desktop importer, never by the desktop itself) |
| Artifacts | `write/append/read/delete/list_artifact(s)`, `scratch_path`, `publish_artifact` | Text logs and CLI scratch (`last-ai-output.log`, `last-ai-diagnostic.log`, `completed-issues`) |
| Execution history | `execution_history(tenant)` | An `ExecutionHistoryStore`: `ai_executions`, adversarial rounds/epochs, Jev records and the per-prompt token-usage records (`ai_token_usage`) |

Checkpoint kinds are a closed set so a typo cannot create stray state. Singleton
kinds hold one document under key `CURRENT`: `in-progress`, `pending-delivery`,
`integration-recovery`, `promotion-blocked`. Keyed kinds hold one per key (an
issue number, or `<issue>-<timestamp>` for archives): `quota-paused`,
`closed-paused`, `abandoned`. Keys and artifact names are flat
(`[A-Za-z0-9_-][A-Za-z0-9._+-]{0,127}`): no separators and no leading dot, so
they cannot traverse out of their namespace.

Semantics every implementation must keep:

- A missing item reads as `None` / `False` / `[]`. Unreadable, corrupt or
  non-object data raises `StorageError`; backend exceptions are wrapped in it.
- Writes are atomic and all-or-nothing: a failed write leaves the previous
  value intact and readers never see a torn value.
- `list_*` results are sorted.
- `scratch_path` is a local file a subprocess can write (the AI CLIs'
  `--output-last-message`); `publish_artifact` makes it durable. The local
  implementation's scratch is the store, so publishing is a no-op.
- `ExecutionHistoryStore` preserves the history schema: `SCHEMA_VERSION` and
  the semantics of migrations 3 (adversarial summary and per-round records),
  5 (security columns and the `(execution_id, stage, round_number)` key) and
  9 (epochs, merge policy, delivery, promotion) are unchanged. The facade
  `ExecutionHistoryService` absorbs `sqlite3.Error` and `StorageError` alike:
  history is observability and never blocks delivery. Missing token counters
  stay `NULL` ("unavailable"), never zero; re-recording a batch is idempotent.

### Resuming exit 13 / exit 14

Exit 13 (strict-mode epoch yield) and exit 14 (automation hold) end the
process with the issue's progress held in checkpoints: the `in-progress` state
carries the adversarial phase, epoch and round, the token-usage events and the
hold record; a quota pause moves it to `quota-paused/<issue>`; delivery waits in
`pending-delivery`. A freshly started process must find exactly that state, so
the contract opens a *second* storage instance over the same backing store and
asserts it reads what the first wrote, including the shelve and restore round
trip.

### Local implementation

`LocalStorage(root, history_database=None, collection_dirs=None)`:

| Data | Default-tenant location |
| --- | --- |
| `in-progress`, `pending-delivery`, `integration-recovery`, `promotion-blocked` | `<root>/in-progress-issue.json`, `pending-delivery.json`, `integration-recovery.json`, `promotion-blocked.json` |
| `quota-paused`, `closed-paused`, `abandoned` | `<root>/quota-paused-issues/<key>.json`, `closed-paused-issues/`, `abandoned/` |
| Artifacts | `<root>/<name>` (the names above, plus `completed-issues`) |
| Documents | `<root>/architecture_docs/<key>.json`, or the directory in `collection_dirs` (the desktop keeps them beside the history database) |
| History | `history_database`, default `<root>/swarm-automation.sqlite3` |

The files are byte-compatible with what the worker wrote before the interface
existed, so a state directory carries over unchanged, in both directions.

### How the worker uses it

`Worker` builds a `LocalStorage` from its config and derives its legacy path
attributes (`in_progress_file`, `pending_file`, `paused_dir`, `ai_output_file`,
`completed_file`, ...) from `checkpoint_path` / `artifact_path`, so the layout
has one owner. Its history facade and `ArchitectureStore` read and write
through `Storage`. The worker's checkpoint reads and writes still operate on
those local paths (they are interleaved with Git operations on the same
checkout). A hosted job does not change that: `job_launch.py` hydrates the
tenant's hosted checkpoints into an empty state directory before the worker
and publishes them back after it exits, including after `SIGTERM`. Inside the
container the worker still uses `DEFAULT_TENANT`; the hosted copy is stored
under the real tenant id. The desktop never sets `SWARM_JOB_STORAGE`. Not
covered and still local: the single-worker process lock (`worker.lock`),
downloaded issue images, and the app-wide SQLite stores for complexity
profiles, diagnostics and engineering knowledge. The remote implementation is
`RemoteStorage` (below).

### Contract tests

`issue_worker/storage_contract.py` defines `StorageContract`, a test mixin.
An implementation's test module subclasses it with `unittest.TestCase` and
implements `new_storage()`, returning a **new instance over the same backing
store** on every call and an empty store for each test:

```python
class PostgresStorageContract(StorageContract, unittest.TestCase):
    def new_storage(self):
        return PostgresStorage(self.dsn)
```

The suite covers tenant isolation across all four groups, id/key/name
validation, atomic replace and failed-write safety, every checkpoint kind, the
exit-13 and exit-14 resume flows from a fresh instance, documents, artifacts
with scratch publishing, the history lifecycle and token-usage idempotency.
`issue_worker/test_storage.py` runs it against `LocalStorage` and adds the
local-only checks: legacy file names and bytes, the unchanged history schema
and migrations, worker path derivation, and the image-isolation tests.
`test_storage_remote.py` runs the same contract against `RemoteStorage` three
ways (below). The contract also pins `has_execution_history` (asking never
provisions) and `import_records` (the importer's idempotent row copy).

### Hosted storage: Postgres and S3

`issue_worker/storage_remote.py` is `RemoteStorage(objects, database, prefix="",
scratch_root=None)`. It is standard library only, like the rest of
`issue_worker/`; it does not import a database driver or an S3 SDK.

| Data | Where |
| --- | --- |
| Checkpoints | S3 object `<prefix>tenants/<tenant>/checkpoints/<kind>/<key>.json` (`current` for a singleton kind) |
| Documents | `<prefix>tenants/<tenant>/documents/<collection>/<key>.json` |
| Artifacts (logs, `completed-issues`) | `<prefix>tenants/<tenant>/artifacts/<name>` |
| Execution history | Postgres schema `t_<tenant>` (shortened with a digest past 63 bytes), registered in `swarm_storage.tenants` |

- **Objects.** JSON is written with the same indentation and key order as
  `LocalStorage`, so a checkpoint is byte-identical in both. A PUT is atomic, so
  readers never see a torn value. S3 has no append: `append_artifact` is a
  read-modify-write guarded by `If-Match` / `If-None-Match` and retried on
  contention (`StorageError` after eight lost races). The bucket must support
  conditional writes (AWS S3 and MinIO do). `object_store.S3ObjectStore` signs
  with Signature V4 over `urllib` (path-style addressing for MinIO, virtual-hosted
  optional), refuses redirects, never puts credentials in a `repr` or error, and
  refuses plain HTTP unless the host is loopback or `allow_insecure_http` is set.
  `MemoryObjectStore` backs tests and single-process use.
- **History.** `SqlExecutionHistory` subclasses the desktop's
  `ExecutionHistoryRepository`, so every write path, the sanitizer and the
  idempotency rules are the same code; only the connection, the creation script
  and the two usage queries ordered by `rowid` differ. `storage_schema.py`
  creates the final shape of migrations 1-10 directly and records versions
  1..`SCHEMA_VERSION` in `schema_migrations` (so migrations 3, 5 and 9 are
  visible as applied). Token counters are `BIGINT` and `ai_token_usage` has an
  identity `seq` column for insertion order (Postgres has no `rowid`); the
  schema-parity test fails if the desktop schema moves and this one does not.
  `ai_execution_history_enabled` still gates everything through
  `ExecutionHistoryService`.
- **Tenant isolation.** A schema per tenant plus an object prefix per tenant:
  every connection runs with `search_path` set to that tenant's schema, so none
  of the repository's SQL can name another tenant's rows. Provisioning is
  idempotent and serialised with an advisory lock; concurrent `create` calls are
  serialised per tenant so attempt numbers stay unique.
- **Errors.** Driver and socket failures are wrapped in `StorageError`, which
  `ExecutionHistoryService` absorbs like `sqlite3.Error`.
- **Driver injection.** `PostgresDatabase(connect)` takes a zero-argument callable
  returning a DB-API 2.0 connection (autocommit off), e.g. `lambda:
  psycopg.connect(dsn)`. `storage_factory.open_storage("hosted")` builds the whole
  thing from `SWARM_STORAGE_*` variables (listed in its docstring) and loads the
  driver named by `SWARM_STORAGE_POSTGRES_DRIVER` (`module:callable`) only when
  set; the worker image bundles none.
- **Local development.** `web/docker-compose.yml` starts Postgres 17 and MinIO
  and creates the bucket (`docker compose -f web/docker-compose.yml up -d`).
  Point `SWARM_STORAGE_POSTGRES_DSN` at `postgresql://swarm:swarm@localhost:5432/swarm`
  and `SWARM_STORAGE_S3_ENDPOINT` at `http://localhost:9000` (loopback HTTP is
  allowed). The always-on test suites need neither server: the S3 client is
  exercised against an in-process server that verifies every signature, and
  history runs on SQLite. Live Postgres is `SWARM_TEST_POSTGRES_DSN`.

Tests: `test_storage_remote.py` runs `StorageContract` against (1) in-memory
objects with SQLite history files, (2) `S3ObjectStore` against a local S3 test
server, and (3) a live Postgres when `SWARM_TEST_POSTGRES_DSN` is set and
`psycopg` is installed (`.github/workflows/storage-live.yml` runs it on every
push and pull request). It adds the signature test vector, conditional-write and
paged-listing tests, driver-error wrapping, schema parity with a freshly migrated
desktop database, per-tenant schemas, concurrent provisioning and attempt
numbers.

### Platform schema

`web/migrations/0001_platform.sql` (embedded as `swarm_web::schema::MIGRATIONS`)
is the Postgres schema for what the `Store` trait holds: `web_users`,
`web_sessions`, `tenants` (one per GitHub App installation), `tenant_memberships`,
`tenant_provider_keys` (sealed keys and their metadata, no plaintext column),
`tenant_plan_quotas`, `tenant_budgets`, `tenant_usage_ledger` (a `NULL` cost is
unpriced, never zero), `tenant_provider_reports`, `tenant_jobs` (a row holds a
concurrent-job slot while `active`) and `webhook_deliveries`. Tenant-scoped tables
lead their key with `tenant_id` and cascade from `tenants`. The file is idempotent;
apply it with `psql -f` or any runner. Per-tenant execution history is *not* here
(it is the `t_<tenant>` schema above) and neither are settings, which are
`tenant_config` documents in object storage so a worker job reads them beside its
checkpoints. The Postgres `Store` that queries these tables is the next step;
`memory::MemoryStore` remains the store until then.

### Desktop data import

`issue_worker/desktop_import.py` copies a desktop install into one tenant:
configuration, execution history and architecture documentation. It is re-runnable
and idempotent, has a `--dry-run`, and never imports credentials. See
[desktop-import.md](desktop-import.md).

### Worker image entrypoint

`web/worker/Dockerfile` is the image both runners start. It pins git, `gh`,
Node, and the Claude, Codex and Grok CLIs, runs as uid 1000, and its
entrypoint is `issue_worker/job_launch.py`. That wrapper clones the repository
into an empty workspace (a git object cache, if configured, is
`--reference-if-able --dissociate`), refuses a non-empty `HOME` or workspace,
hydrates and publishes checkpoints, then runs `worker_entrypoint.py` and
returns its exit code. `SWARM_JOB_WORKER` is unset in that image. The fixture
image (`Dockerfile.fixture`) sets it so acceptance tests can deliver without
a provider CLI. Build from the repository root:
`docker build -f web/worker/Dockerfile -t swarm-automation-worker .`

## Web backend (`web/`)

`web/` is a standalone Rust/axum Cargo project (`swarm-web`): its own
`Cargo.toml` and `Cargo.lock`, **not** a member of the desktop's package (the
root `Cargo.toml` has no `[workspace]`; keep it that way). It was chosen over
another language because the desktop's `src/config.rs` and `src/tools.rs` logic
is being ported and the backend shares its types and conventions. `src/` is
untouched. CI runs `cargo fmt`, `clippy -D warnings` and `cargo test --locked`
in `web/` beside the desktop's.

```
GET  /api/v1/health
GET  /api/v1/session                              who is signed in, tenants, CSRF token
GET  /api/v1/auth/github/login | /callback        GitHub App OAuth (state + PKCE)
POST /api/v1/auth/logout
GET  /api/v1/tenants[/{tenant}[/members|/provider-keys|/quotas|/usage]]
PUT  /api/v1/tenants/{tenant}/provider-keys/{provider}     write-only (owner)
DEL  /api/v1/tenants/{tenant}/provider-keys/{provider}     (owner)
PUT  /api/v1/tenants/{tenant}/budgets                      (owner)
GET  /api/v1/tenants/{tenant}/work/{owner}/{repo}/issues/{issue}[/logs]
POST /api/v1/tenants/{tenant}/work/{owner}/{repo}/issues/{issue}/{run,pause,resume,stop}
GET  /api/v1/events/jobs                          SSE `job-log` (session; no tenant in the path)
GET  /api/v1/events/automation-log                SSE `automation-log` (the desktop's log event)
GET  /api/v1/events/model-calibration             SSE `model-calibration-refreshed`
     /api/v1/tenants/{tenant}/...                 the desktop's commands (see the table below)
GET  /api/v1/version                              the build version
POST /api/v1/webhooks/github                      signed, idempotent
     /api/v1/internal/tenants/{tenant}/usage|quotas|jobs   operator bearer token
```

Everything else under `/api/v1` is a JSON 404; any other path is a static asset
from `ui/` (`SWARM_WEB_UI_DIR`, default `../ui`) with `*.test.js`, `*.md` and
dotfiles unserved. The same assets run on the desktop and the web: the
`web_*` rows in `ui/api.js`'s `COMMANDS` table map onto these routes. Every
response carries a strict CSP (`default-src 'none'; script-src 'self'; style-src
'self'; ...`, no `unsafe-inline`), `nosniff`, `frame-ancestors 'none'`,
`no-referrer`, and HSTS over HTTPS. Logs are JSON lines through a scrubbing
writer (`redact.rs`: provider and GitHub tokens, key blocks, bearer headers,
credentialed URLs, signatures, configured secrets); request logs carry the path
only, never the query string.

### Sign-in, sessions and CSRF

Sign-in is the GitHub App's user-to-server OAuth flow with a random `state`
bound to the browser by an `HttpOnly` cookie and a PKCE S256 challenge. The
user's GitHub token is used for the callback's three reads (`/user`,
`/user/installations`, and the organization role) and dropped: it is never
stored, logged or returned. The session is a 256-bit random id in an `HttpOnly`,
`SameSite=Lax` cookie (`Secure` and `__Host-` prefixed when `SWARM_WEB_PUBLIC_URL`
is `https`); the store keeps only its SHA-256. A new id is minted on every
sign-in, sessions expire (`SWARM_WEB_SESSION_TTL_SECS`, default 8 h) and logout
deletes the row. Nothing credential-like goes in `localStorage`.

CSRF: every state-changing request needs the session's token in `X-CSRF-Token`
(constant-time compare against the copy stored with the session) and, if the
browser sends an `Origin`, it must be the configured origin. The token is
delivered in a script-readable `swarm_csrf` cookie and in `GET /session`;
`ui/api.js` echoes it. The check is in the `Authed` extractor, so a handler
that needs a session cannot skip it.

### Tenants, roles and isolation

A tenant is a GitHub App installation, created on first sight at sign-in or by
the `installation` webhook. Tenant ids match the worker storage grammar
(`t` + 16 hex), so the same string names the database tenant and the
object-store prefix. A user belongs to the tenants GitHub says they can access;
every sign-in re-syncs memberships (access GitHub no longer grants is dropped,
role changes follow). Roles: **owner** is the installation's own user account or
an organization admin (needs the App's *Organization members: read*
permission; without it an organization's users are members, never owners) and
may write keys and budgets; **member** can read. Suspended or deleted
installations (webhook) refuse writes and new jobs.

Isolation is enforced in three places, each tested:

1. **HTTP**: every `/tenants/{tenant}/...` handler takes a `TenantAccess`
   extractor, which proves the signed-in user's membership in the path's tenant
   and answers `404` otherwise, so ids cannot be probed. Tenant ids in bodies or
   query strings are never read.
2. **Store**: every tenant-scoped `Store` method takes the tenant first (the
   same rule as `issue_worker/storage.py`) and can only address that tenant.
3. **Crypto**: sealed provider keys are bound to `(tenant, provider)`, so a row
   copied to another tenant or provider fails to decrypt.

### Provider keys

Per tenant: `claude`, `codex`, `grok` and `model-data` (the Artificial Analysis
key). **Write-only**: `PUT` stores, `GET` returns only
`{provider, configured, updated_at, updated_by}`, `DELETE` removes. A key is
never returned, never logged (it lives in `Secret`, whose `Debug`/`Display`
print a placeholder, and request errors never echo bodies) and is zeroized on
drop. Envelope encryption: a random data key per secret seals it with
AES-256-GCM; a `KeyWrapper` wraps the data key. `LocalKeyWrapper`
(`SWARM_WEB_LOCAL_KEY`, base64 32 bytes) is the development wrapper. **KMS on
AWS is the same trait with an AWS-backed implementation; it needs the AWS SDK
and lands with the deployment work, and `SWARM_WEB_KMS_KEY_ID` is refused at
startup until it exists rather than silently ignored.** `Vault::job_environment`
is the only place plaintext leaves: it returns the one provider's variable
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`), plus
`ARTIFICIAL_ANALYSIS_API_KEY` only for a job that fetches model data, never
another provider's key. No HTTP route reaches it.

### Usage, budgets and quotas

The worker prices every invocation (`token_usage.UsageRecord`,
`usage_report.py`); jobs post those rows to the operator-only
`/internal/tenants/{tenant}/usage` and the backend sums them per tenant and
month. The rule is `usage_report.py`'s: a row is priced spend only with some
token counter **and** an `estimated_cost`; anything else is counted as
*unpriced*, never as zero. Non-USD rows are refused. Re-posting a batch is
idempotent (record ids are scoped to the tenant). `web/tests/fixtures/
usage_record.json` is real `UsageRecord.to_dict()` output parsed from both
Python (`test_web_usage_contract.py`) and Rust (`usage_contract.rs`).

`minimum_remaining_percent` keeps its meaning. A provider's remaining headroom
is the tenant's provider budget (owner-set, USD per month) minus spend, and/or
a provider-reported limit when a job supplies one (`provider_limits`, valid for
an hour); the lower wins. Status mirrors the worker's `ProviderUsage`: `0`
usable, `1` below the minimum (no new job for that provider until the budget is
raised or the month rolls over), `2` **unavailable**: no budget and no reported
limit, `remaining_percent` is `null` and nothing is gated, never a fabricated
number.

Quotas are enforced by `POST /internal/tenants/{tenant}/jobs` before a job
starts, in this order: tenant active, the provider's key present, monthly spend
cap, provider budget, then an atomic concurrent-job slot (idempotent per job id;
`DELETE .../jobs/{id}` releases it). Plan quotas (`max_concurrent_jobs`, default
2; `monthly_spend_cap_usd`, default 100) are operator-set and only readable by a
tenant; budgets are the owner's. The operator API needs
`SWARM_WEB_INTERNAL_TOKEN` and is off (404) without it.

### Webhooks

`POST /api/v1/webhooks/github` verifies `X-Hub-Signature-256` (HMAC-SHA256 of
the raw body, constant time) **before anything is stored**; a bad signature is a
`401`. A delivery is then *claimed* by `X-GitHub-Delivery` and by payload hash:
the same id again is acknowledged as a `duplicate` (`200`) and not re-applied; a
captured signed body replayed under a new id is refused (`409 replay`). A
processing failure releases the claim and answers 5xx so GitHub's retry runs.
`installation` events create, suspend, unsuspend and delete tenants. Other
events for a known active installation are handed to the orchestrator when a
job runner is configured, and recorded (`detail: recorded`) when it is not.
The orchestrator starts one container for `issues`, human `issue_comment`,
label and pull-request events. The worker's own lifecycle-marker comments do
not start another container. A denial (missing key, quota, concurrency,
inactive tenant) is still HTTP 200 with a `detail`, so GitHub does not retry
it. Run now maps the same denial to HTTP 409.

### Configuration

`SWARM_WEB_PUBLIC_URL`, `SWARM_WEB_GITHUB_CLIENT_ID`,
`SWARM_WEB_GITHUB_CLIENT_SECRET`, `SWARM_WEB_GITHUB_WEBHOOK_SECRET` and
`SWARM_WEB_LOCAL_KEY` are required; `SWARM_WEB_BIND` (default `127.0.0.1:8080`),
`SWARM_WEB_UI_DIR`, `SWARM_WEB_GITHUB_APP_SLUG`, `SWARM_WEB_INTERNAL_TOKEN` and
`SWARM_WEB_SESSION_TTL_SECS` are optional. Run it with
`cargo run` in `web/` (tests: `cargo test --locked`).

`SWARM_WEB_BRIDGE=python` turns the worker bridge on (`SWARM_WEB_PYTHON`, default
`python3`, and `SWARM_WEB_WORKER_DIR`, default `../issue_worker`); the hosted
storage it reads is the `SWARM_STORAGE_*` set of `storage_factory.py`, forwarded
to the bridge child only. `SWARM_WEB_SSE_HEARTBEAT_SECS` (1-300, default 15) and
`SWARM_WEB_APP_VERSION` (what `app_version` answers; default the crate version)
are optional.

`SWARM_WEB_JOB_RUNNER` (`docker` or `fargate`) turns the orchestrator on. It
requires `SWARM_WEB_WORKER_IMAGE`, `SWARM_WEB_GITHUB_APP_ID` and
`SWARM_WEB_GITHUB_APP_PRIVATE_KEY`. CPU, memory, poll and quota-resume
intervals have defaults (1000 millis, 2048 MiB, 60 seconds, 60 seconds).
There is no default job deadline; `SWARM_WEB_JOB_MAX_RUNTIME_SECS` is the only
way to set one. Fargate also needs `SWARM_WEB_ECS_SUBNETS`. Storage variables
named `SWARM_STORAGE_*` are forwarded into the container; the app private key
and the ECS control-plane credentials are not. `web/docker-compose.jobs.yml`
builds the fixture image for a local acceptance run.

### Jobs

`JobRunner` (`web/src/runner.rs`) is start, status, cancel, pause, resume of
the same container, log snapshot, log stream, and resume-from-checkpoint
(always a new container). `DockerJobRunner` and `EcsFargateJobRunner` consume
the same `JobSpec` and entrypoint. Docker passes secrets through a mode-0600
env file that is removed after start, drops all capabilities, sets a read-only
root, a non-root user, pid and memory limits, and sinks the cloud metadata
address. Fargate sends no task role and disables public IP and execute
command. A `PauseTask` the backend does not implement stops the task; the
orchestrator keeps the slot and resumes from the checkpoint.

The orchestrator replaces `processes.rs` supervision and the cron installer
on the web path only. One repository has one active container. Exit 13
relaunches immediately from the `in-progress` checkpoint and keeps the
concurrency slot. Exit 11 waits `SWARM_WEB_QUOTA_RESUME_SECS` (default 60,
not 15 minutes) and relaunches from `quota-paused`. Exit 14 releases the slot
and holds until Run now, Resume, a trusted follow-up, or a new image id.
Exit 0/10 releases the slot; a webhook that arrived during the run starts one
follow-up, otherwise the next poll does. Stop cancels the container and does
not relaunch. The desktop scheduler is unchanged.

### REST and SSE for the desktop's commands

`web/src/catalog.rs` is the one table that says what the web does with each of the
desktop's `#[tauri::command]`s and events. The router (`web/src/api.rs`) registers
its routes from that table, `ui/api.js`'s `COMMANDS` / `EVENTS` rows mirror it, and
`web/tests/api_catalog.rs` fails when the desktop's command list, the catalog, the
adapter tables and this document disagree. A `*_background` command is the same
endpoint as the command it wraps. The tenant is a path segment that
`TenantAccess` re-checks for membership (404 otherwise); the desktop's call sites
do not pass one, so the adapter fills `{tenant}` from `SwarmApi.setTenant(id)` or
the first tenant of `GET /session`. Arguments are the camelCase names the UI
already sends: query parameters on `GET`/`DELETE` (a list or object is
JSON-encoded), a JSON object body otherwise. Path parameters win over any body
field and the tenant is never read from a body, header or query string.

Roles: **member** may read and run the actions that only spend the tenant's own
budget; **owner** is needed to change settings and for anything that merges,
promotes, files an issue, imports or activates. Every non-`GET` route needs the
CSRF token and an active tenant (a suspended installation can still be read).
A request body is at most 64 KiB and never echoed on an error.

| Desktop command | Endpoint | Role | Served by |
| --- | --- | --- | --- |
| `get_config` | `GET /api/v1/tenants/{tenant}/config` | member | backend (`GetConfig`) |
| `save_config` | `PUT /api/v1/tenants/{tenant}/config` | owner | backend (`SaveConfig`) |
| `save_feedback_repo_filter` | `PUT /api/v1/tenants/{tenant}/feedback-repo-filter` | member | backend (`FeedbackFilter`) |
| `web_list_repositories` | `GET /api/v1/tenants/{tenant}/repos` | member | backend (`Repositories`) |
| `web_get_repo_config` | `GET /api/v1/tenants/{tenant}/repos/{repoId}/config` | member | backend (`GetRepoConfig`) |
| `web_save_repo_config` | `PUT /api/v1/tenants/{tenant}/repos/{repoId}/config` | owner | backend (`SaveRepoConfig`) |
| `detect_tools`, `detect_tools_background` | `GET /api/v1/tenants/{tenant}/tools` | member | backend (`Tools`) |
| `check_provider_usage`, `check_provider_usage_background` | `GET /api/v1/tenants/{tenant}/provider-usage` | member | backend (`ProviderUsage`) |
| `get_model_data_key_status` | `GET /api/v1/tenants/{tenant}/model-data-key` | member | backend (`ModelDataKeyStatus`) |
| `save_model_data_key` | `PUT /api/v1/tenants/{tenant}/provider-keys/model-data` | owner | the write-only provider-key route |
| `clear_model_data_key` | `DELETE /api/v1/tenants/{tenant}/provider-keys/model-data` | owner | the write-only provider-key route |
| `verify_github_bots` | `GET /api/v1/tenants/{tenant}/repos/{repoId}/bots` | member | backend (`Readiness`) |
| `check_repo_bot_readiness` | `GET /api/v1/tenants/{tenant}/repos/{repoId}/readiness` | member | backend (`Readiness`) |
| `get_automation_status`, `get_automation_status_background` | `GET /api/v1/tenants/{tenant}/status` | member | backend (`Status`) |
| `start_issue_worker`, `request_issue_scan` | `POST /api/v1/tenants/{tenant}/scan` | member | backend (`Scan`) |
| `pause_process` | `POST /api/v1/tenants/{tenant}/processes/{process}/pause` | member | backend (`Pause`) |
| `resume_process` | `POST /api/v1/tenants/{tenant}/processes/{process}/resume` | member | backend (`Resume`) |
| `stop_process` | `POST /api/v1/tenants/{tenant}/processes/{process}/stop` | member | backend (`Stop`) |
| `get_recent_logs` | `GET /api/v1/tenants/{tenant}/logs` | member | backend (`RecentLogs`) |
| `get_execution_history`, `get_execution_history_background` | `GET /api/v1/tenants/{tenant}/history` | member | worker bridge `execution_history` |
| `get_jev_feedback`, `get_jev_feedback_background` | `GET /api/v1/tenants/{tenant}/jev-feedback` | member | worker bridge `jev_feedback` |
| `get_usage_report`, `get_usage_report_background` | `GET /api/v1/tenants/{tenant}/usage-report` | member | worker bridge `usage_report` |
| `get_prompt_grades`, `get_prompt_grades_background` | `GET /api/v1/tenants/{tenant}/prompt-grades` | member | worker bridge `prompt_grades` |
| `import_execution_history`, `import_execution_history_background` | `POST /api/v1/tenants/{tenant}/history/import` | owner | worker bridge `import_execution_history`: **501** until hosted |
| `get_knowledge_status`, `get_knowledge_status_background` | `GET /api/v1/tenants/{tenant}/knowledge` | member | worker bridge `knowledge_status`: **501** until hosted |
| `refresh_knowledge`, `refresh_knowledge_background` | `POST /api/v1/tenants/{tenant}/knowledge/refresh` | member | worker bridge `knowledge_refresh`: **501** until hosted |
| `ask_swarm`, `ask_swarm_background` | `POST /api/v1/tenants/{tenant}/knowledge/ask` | member | worker bridge `ask_swarm`: **501** until hosted |
| `get_architecture_docs` | `GET /api/v1/tenants/{tenant}/architecture-docs` | member | worker bridge `architecture_docs` |
| `get_model_calibration_status`, `get_model_calibration_status_background` | `GET /api/v1/tenants/{tenant}/calibration` | member | worker bridge `calibration_status`: **501** until hosted |
| `refresh_model_data`, `refresh_model_data_background` | `POST /api/v1/tenants/{tenant}/calibration/refresh` | member | worker bridge `calibration_refresh`: **501** until hosted |
| `analyze_model_calibration_update`, `analyze_model_calibration_update_background` | `POST /api/v1/tenants/{tenant}/calibration/analyze` | member | worker bridge `calibration_analyze`: **501** until hosted |
| `activate_model_calibration`, `activate_model_calibration_background` | `POST /api/v1/tenants/{tenant}/calibration/activate` | owner | worker bridge `calibration_activate`: **501** until hosted |
| `describe_routing_calculator` | `GET /api/v1/tenants/{tenant}/routing/calculator` | member | worker bridge `routing_describe` |
| `simulate_routing` | `POST /api/v1/tenants/{tenant}/routing/simulate` | member | worker bridge `routing_simulate` |
| `run_diagnostics`, `run_diagnostics_background` | `POST /api/v1/tenants/{tenant}/diagnostics/run` | member | worker bridge `diagnostics_run`: **501** until hosted |
| `file_diagnostic_issue`, `file_diagnostic_issue_background` | `POST /api/v1/tenants/{tenant}/diagnostics/issue` | owner | worker bridge `diagnostics_file_issue`: **501** until hosted |
| `git_overview`, `git_overview_background` | `GET /api/v1/tenants/{tenant}/repos/{repoId}/git` | member | worker bridge `git_overview`: **501** until hosted |
| `refresh_repo` | `POST /api/v1/tenants/{tenant}/repos/{repoId}/refresh` | member | worker bridge `git_overview`: **501** until hosted |
| `merge_issue_branch` | `POST /api/v1/tenants/{tenant}/repos/{repoId}/merge-issue` | owner | worker bridge `merge_issue_branch`: **501** until hosted |
| `merge_integration_branch` | `POST /api/v1/tenants/{tenant}/repos/{repoId}/merge-integration` | owner | worker bridge `merge_integration_branch`: **501** until hosted |
| `branch_push_access` | `GET /api/v1/tenants/{tenant}/repos/{repoId}/push-access` | member | worker bridge `branch_push_access`: **501** until hosted |
| `grant_bot_branch_push` | `POST /api/v1/tenants/{tenant}/repos/{repoId}/push-access` | owner | worker bridge `grant_bot_branch_push`: **501** until hosted |
| `promotion_overview`, `promotion_overview_background` | `GET /api/v1/tenants/{tenant}/promotions` | member | worker bridge `promotion_overview`: **501** until hosted |
| `open_integration_pr` | `POST /api/v1/tenants/{tenant}/repos/{repoId}/integration-pr` | owner | worker bridge `open_integration_pr`: **501** until hosted |
| `promote_integration_branch`, `promote_integration_branch_background` | `POST /api/v1/tenants/{tenant}/repos/{repoId}/promote` | owner | worker bridge `promote_integration_branch`: **501** until hosted |
| `app_version` | `GET /api/v1/version` | public | backend (`Version`) |

`GET` and the mutating routes share one error vocabulary (`error`, `code`):
`401 unauthorized`, `403 owner_required | csrf_token | csrf_origin |
tenant_inactive`, `404 not_found` (also a tenant or repository the caller cannot
reach), `400 bad_request`, `501 not_available_yet` (the endpoint exists, the
worker-side operation does not yet; the message says why), `503
bridge_unconfigured | jobs_unconfigured`.

**Settings** are the worker's `tenant_config` layout (`app`, and `repo-<id>` per
repository, written by `web/src/settings.rs`; `GET /config` reassembles the
desktop's `AppConfig` shape, `repositories` included). Credential-shaped keys and
key blocks, and the settings that only describe a desktop machine
(`workspace_root`, `*_bin`, `repo_dir`, ...), are dropped on save, with the same
rules as `desktop_import.py`; provider keys only go through the write-only
`provider-keys` routes. `PUT /config` replaces the whole configuration and
removes repositories that are no longer listed. `PUT /repos/{repoId}/config`
replaces one repository (the id is the path's). A member may only change which
repositories Feedback shows (`feedback-repo-filter`).

**The worker bridge.** History, Jev feedback, usage analytics, prompt grades,
architecture documentation and the routing calculator are computed by the shared
worker, not re-implemented: `web/src/bridge.rs` runs `issue_worker/web_bridge.py`
once per call (`SWARM_WEB_BRIDGE=python`; `SWARM_WEB_PYTHON`,
`SWARM_WEB_WORKER_DIR`). The request is JSON on stdin, the environment is cleared
down to `PATH` and the `SWARM_STORAGE_*` settings, and the one tenant comes from
`TenantAccess`; repository ids are resolved to `owner/name` from that tenant's own
settings before the call, so a caller can only ask about repositories the tenant
has configured. Reads never provision a tenant's history store: a tenant without
one reads like an empty desktop database. Without a bridge the endpoint answers
`503` rather than an empty list. The worker-side operations the hosted
deployment cannot serve yet (the knowledge index, model calibration snapshots and
diagnostics still live in the desktop's local state, and branch, merge and
promotion operations need a repository installation token on the bridge) answer
`501` with their reason (`web_bridge.UNAVAILABLE`); they are endpoints, tested for
tenancy, role and CSRF, that start answering when their hosted store lands.

**Process controls and status.** The hosted scheduler is always on and
webhook-driven, so there is no process to start: `start_issue_worker` and
`request_issue_scan` are `POST /scan` (a scheduler tick), and `pause_process`,
`resume_process` and `stop_process` take the process name `issue` and act on the
tenant's jobs through the orchestrator (the desktop's `uat:<repo>` slots do not
exist: UAT runs inside the job). `GET /status` is the desktop's
`AutomationStatus` shape built from the tenant's saved repositories and active
jobs. `GET /provider-usage` is the budget/quota status of `usage.rs`
(`remainingPercent` is `null` when there is no budget and no provider report,
never a made-up number); `GET /tools` reports each provider as ready when the
tenant has saved its key; readiness (`/bots`, `/readiness`) is per provider: key
saved and installation active.

**Intentionally removed on the web**

| Desktop command | Why |
| --- | --- |
| `choose_repository` | A browser has no native folder picker. Repositories are the GitHub App installation's repositories, saved with the tenant's settings (`GET /repos`). |
| `inspect_repository` | It inspects a path on the user's machine. A hosted job clones the repository fresh for each run. |
| `prepare_workspace` | A hosted job prepares its own clean workspace (`job_launch.py`); there is no managed checkout to prepare. |
| `open_workspace_folder` | There is no local folder to open; the work is on the issue branch and its pull request. |
| `open_automation_folder` | There is no local log file. Logs are `GET /logs` and the `automation-log` stream. |
| `open_external_url` | The browser opens links itself. |
| `hide_to_tray` | It hides the desktop window; the web page has no tray. |
| `launch_bot_setup` | The GitHub App is installed from GitHub (sign-in creates the tenant); there is no local bot setup terminal. |
| `install_ai_cli` | The provider CLIs are baked into the worker image; nothing is installed on the user's machine. |
| `open_provider_login` | Hosted jobs use the tenant's own provider API keys (`provider-keys`), not an interactive CLI login. |

**Events**

| Desktop event | Stream | Notes |
| --- | --- | --- |
| `automation-log` | `GET /api/v1/events/automation-log` | The desktop's `LogEvent` (`source`, `stream`, `line`, `timestamp`) plus `tenant`, `repository`, `issue`. |
| `job-log` | `GET /api/v1/events/jobs` | The per-issue projection of the same lines (`tenant`, `repository`, `issue`, `line`). |
| `model-calibration-refreshed` | `GET /api/v1/events/model-calibration` | Published after `POST /calibration/refresh`. |
| `system-permission-primed` | none (removed) | A macOS Automation permission prompt on the desktop; the web has no such permission. |

Every stream is session-scoped (the tenants the signed-in user belongs to; the
membership and the session are re-checked every 30 s and a logout ends the
stream), so `listen()` needs no path parameter. Frames are served from
`web/src/events.rs`:

- **Ids and resume.** Each frame has a process-wide increasing `id`. A reconnect
  sends `Last-Event-ID` (the browser's `EventSource` does it automatically) and
  gets what it missed from a bounded per-tenant ring (5000 frames), in id order,
  without gaps or duplicates. Asking for history older than the ring first sends
  `event: resync` with `{"reason": "history_truncated"}`; refetch `GET /logs`.
  Another tenant's frames are never replayed.
- **Heartbeat.** A `: ready` comment on connect, then `: heartbeat` every
  `SWARM_WEB_SSE_HEARTBEAT_SECS` (default 15).
- **Backpressure.** The live channel (512 frames) and each connection's queue (64)
  are bounded. A reader that falls behind gets `event: resync` with
  `{"reason": "lagged", "missed": n}` and is caught up from the ring; nothing grows
  without limit.
- **Redaction.** Every frame is scrubbed (provider and GitHub tokens, key blocks,
  credentialed URLs, `name=value` secrets) before it is stored or sent. Lines
  otherwise pass through verbatim, so the `Adversarial UAT for issue #...` and
  `Adversarial Cybersecurity for issue #...` boundary logs the Overview replays
  keep their format, issue number and round/max values. `GET /logs?limit=` returns
  the same ring in the desktop's log-file format (`[<unix seconds>]
  [<source>/<stream>] <line>`).

Tests: `web/tests/api_commands.rs` (every tenant route: no session 401, a foreign
tenant 404, a member on an owner route 403, no CSRF 403; settings; the bridge),
`web/tests/sse.rs` (scoping, resume, heartbeat, lag, redaction),
`web/tests/api_catalog.rs` (the drift checks above), `ui/api.test.js` (both
transports) and `issue_worker/test_web_bridge.py` (the worker operations).

### Web-only views in the shared `ui/` (#420)

The same `ui/` runs on the desktop and the web, so the hosted version's screens
are views of that app, not a second UI. The transport decides: `app.js` sets
`document.body.dataset.transport = "web"` only when `SwarmApi.transport()` is
`"web"`. `style.css` then shows every `[data-web-only]` element and hides every
`[data-desktop-only]` one; on the desktop the opposite holds and nothing changes.
Three more `body` markers drive the signed-in state: `data-session="signed-out"`
hides `[data-signed-in-only]` and shows `[data-signed-out-only]`. An expired
session keeps the signed-in layout and only raises the banner.

| View | Nav | What it does | Commands |
| --- | --- | --- | --- |
| Sign in (`view-signin`) | signed out only | the GitHub App sign-in link; the install link when the operator published one | `web_session` |
| Account (`view-account`) | signed in | the signed-in user, a tenant switcher (also in the top bar), the tenant's status and role, a read-only member list, Sign out | `web_session`, `web_list_members`, `web_logout` |
| API keys (`view-keys`) | signed in | one write-only row per provider (`claude`, `codex`, `grok`, `model-data`): set, replace, remove, and when it was last changed | `web_list_provider_keys`, `web_set_provider_key`, `web_delete_provider_key` |
| GitHub App (`view-github`) | signed in | sign-in, installation and active-installation checklist, the install link, Re-check | `web_session` |
| Quota & budget (`view-quota`) | signed in | month spend, active jobs against the plan, the monthly cap, one row per provider, and the budget form | `web_get_quotas`, `web_get_usage`, `web_set_budgets` |

Logic and state are `ui/web-account.js` (`SwarmWebAccount`), a DOM-free module
whose controller takes `SwarmApi.invoke`; `app.js` only renders what it holds, and
`app.js` never names a `web_*` command (`api.test.js` checks that). Rules the
module keeps:

- **Keys are write-only.** The input is `type="password"`, is cleared after every
  attempt whether or not it was saved, and the value is not kept in the controller
  state, a message or a log. A key is checked against the server's own shape rules
  (`validate_key`) before it is sent; the server stays the authority.
- **Owner-only writes.** Keys and budgets need an owner of an active tenant. A
  member or a suspended tenant sees the same view read-only and no request is made.
  The tenant is a path segment from the session, never typed by the user.
- **Unavailable is not zero.** A provider with neither a budget nor a provider
  report shows `—` and no meter; a failed `usage` or `quotas` request leaves its
  figures `—`, not `$0.00`. Each tenant section loads independently, so one failing
  endpoint names itself without blanking the rest.
- **Session expiry.** `SwarmApi.onSessionExpired` is called for a 401 from any
  command except `web_session`, and the page also re-reads the session every minute.
  Either raises the "Your session ended" banner with a sign-in link; unsaved edits
  stay on the page. No token is ever stored by the page (the CSRF value is read
  from its cookie by `api.js`).
- **Desktop-only affordances are hidden or replaced on the web:** hide to tray, the
  "Runs on this Mac" footer (replaced by "Hosted by SWARM"), the workspace folder row
  (prepare and reveal), the local bot setup button, and the provider CLI Install and
  Sign in buttons. External links open in a new tab with `noopener,noreferrer`
  instead of `open_external_url`.

Tests: `ui/web-account.test.js` (the logic, the controller over a mocked HTTP
transport, the adapter's expiry hook, and markup and design-system conformance) and
`ui/api.test.js` (both transports).

### Not yet built

Behind the seams above, by later issues of #413: the Postgres `Store` (the
schema exists, see "Platform schema", but the in-memory store used today still
loses state on restart and the binary says so at startup), the KMS
`KeyWrapper`, the worker-side operations listed as 501 above, the web screens for
repository settings and history beyond the views above, and per-process (`uat:<repo>`) controls. Scheduling
state lives in memory with that store. Checkpoints for a hosted job live in
the object store via `job_checkpoint_sync.py`.
