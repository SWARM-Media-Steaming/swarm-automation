# Web architecture

SWARM Automation gains a hosted, multi-tenant web version alongside the Tauri
desktop app (tracked in #413). The desktop stays fully working; `ui/` and
`issue_worker/` are shared. This document records the pieces as they land.
It covers the storage seam and its hosted Postgres/S3 implementation, the
desktop data importer, and the web backend (API, auth, tenancy, provider keys,
usage and quotas), and the job runner (one container per repository, Docker
locally and ECS Fargate on AWS).

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

### Not yet built

Behind the seams above, by later issues of #413: the Postgres `Store` (the
schema exists, see "Platform schema", but the in-memory store used today still
loses state on restart and the binary says so at startup), the KMS
`KeyWrapper`, and the UI screens that call the `web_*` commands. Scheduling
state lives in memory with that store. Checkpoints for a hosted job live in
the object store via `job_checkpoint_sync.py`.
