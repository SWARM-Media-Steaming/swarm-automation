# Web architecture

SWARM Automation gains a hosted, multi-tenant web version alongside the Tauri
desktop app (tracked in #413). The desktop stays fully working; `ui/` and
`issue_worker/` are shared. This document is the contract for the web path.

| Section | What it answers |
| --- | --- |
| [Storage](#storage) | where the worker's state lives (local files or Postgres and S3) |
| [Web backend](#web-backend-web) | the API, sign-in, tenants, provider keys, usage, webhooks, jobs |
| [Local stack](#local-stack) | `docker compose up`, the acceptance flow, building images |
| [API contract](#api-contract), [Server-Sent Events](#server-sent-events) | what a client may rely on |
| [Tenancy model](#tenancy-model), [Runner abstraction](#runner-abstraction) | isolation and the one-container-per-job runtime |
| [AWS mapping](#aws-mapping) | the Terraform in `web/infra/aws`, IAM and secrets |
| [Threat model](#threat-model), [Decisions](#decisions), [Known gaps](#known-gaps) | what is defended, why it is built this way, what is not built yet |

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
- **Local development.** The repository-root `docker-compose.yml` runs the whole
  hosted stack ("Local stack" below). `web/docker-compose.yml` starts only
  Postgres 17 and MinIO and creates the bucket
  (`docker compose -f web/docker-compose.yml up -d`).
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

`web/migrations/*.sql` (embedded as `swarm_web::schema::MIGRATIONS`, applied in
order and recorded in `platform_migrations`) is the Postgres schema for what the
`Store` trait holds. `0001_platform` creates tenants (one per GitHub App
installation), `tenant_provider_keys` (sealed keys and their metadata, no
plaintext column), `tenant_plan_quotas`, `tenant_budgets`, `tenant_usage_ledger`
(a `NULL` cost is unpriced, never zero), `tenant_provider_reports`, `tenant_jobs`
(a row holds a concurrent-job slot while `active`) and `webhook_deliveries`.
Tenant-scoped tables lead their key with `tenant_id` and cascade from `tenants`.

`0002_identity` is the identity model (#438). GitHub is the only sign-in method
today; there is no username, email or password login, and more OIDC providers
join as extra `user_identities` rows:

| Table | Holds | Keys |
|-------|-------|------|
| `users` (was `web_users`) | a person: `display_name`, `avatar_url`, nullable `email`, `is_platform_admin` (default false), `created_at`, `last_login_at` | `id` |
| `user_identities` | how they sign in: `provider`, `subject` (the provider's stable id, the GitHub numeric id as text), `login`, `created_at` | `user_id` -> `users` ON DELETE CASCADE; UNIQUE (`provider`, `subject`) |
| `sessions` (was `web_sessions`) | hashed cookie, CSRF token, expiry | `user_id` -> `users` ON DELETE CASCADE |
| `tenant_memberships` | `role` CHECK (`owner`, `member`) | (`tenant_id`, `user_id`) unique; both FK, cascade |
| `tenants.owner_user_id` | the owning user, nullable | -> `users` ON DELETE SET NULL |
| `admin_audit_log` | `action`, JSON `detail`, `created_at` | `actor_user_id`, `target_user_id` -> `users` ON DELETE SET NULL (the trail outlives the people in it) |

Every foreign key column is indexed. `users.github_id` and `users.login` moved
into `user_identities` (backfilled as provider `github`). Every file is
idempotent and the whole sequence may be applied twice (`psql -f` over each file
in order): `0002` guards each step on the catalog, and drops the empty
`web_users`/`web_sessions` that a re-run of `0001` recreates. The per-tenant
worker schema (`t_<tenant>`, `SCHEMA_VERSION`) is untouched. Per-tenant
execution history is *not* here (it is the `t_<tenant>` schema above) and neither
are settings, which are `tenant_config` documents in object storage so a worker
job reads them beside its checkpoints. `0003_tenant_documents` adds
`tenant_documents` (the `Store`'s tenant documents: tenant first in the key,
cascading from `tenants`). `0004_personal_tenants` (#440) makes
`tenants.installation_id` nullable (a personal tenant has no installation; the
UNIQUE constraint ignores NULLs) and adds the partial unique index
`tenants_personal_owner_idx` on `tenants (owner_user_id) WHERE installation_id IS
NULL`, so one owner can never hold two personal tenants even under racing
sign-ins.

### Postgres `Store`

`postgres::PostgresStore` implements the `Store` trait over these tables
(`tokio-postgres` behind a `deadpool` pool; the worker's Python side keeps its
own driver). `SWARM_WEB_STORE=postgres` selects it and takes the connection
string from `SWARM_STORAGE_POSTGRES_DSN` (one database serves the platform
tables and the worker's `t_<tenant>` history schemas); unset or `memory` keeps
`MemoryStore`, which tests use. At start the API applies every migration not
recorded in `platform_migrations`, in order, in one transaction under an
advisory lock, so concurrent starts do not race and a restart changes nothing.
Sessions, sign-ins, memberships and sealed keys therefore survive an API
restart. `sslmode=disable` connects in clear; any other mode encrypts but, like
libpq `require`, does not verify the server certificate (the DSN Terraform
writes asks for exactly that). Errors become `StoreError` with the SQLSTATE and
message only: never the DSN or a row value. `tests/store_contract.rs` runs one
contract against `MemoryStore` and, when `SWARM_TEST_POSTGRES_DSN` is set (CI's
`storage-live` workflow), against Postgres, plus a restart test.

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
GET  /api/v1/auth/{provider}/login | /callback    identity-provider OAuth (state + PKCE); only `github` exists
POST /api/v1/auth/logout
GET  /api/v1/admin/users                          platform admin only (404 for anyone else)
POST /api/v1/admin/users/{userId}/promote|demote  platform admin only, audited
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

Sign-in is an OIDC-style flow behind the `IdentityProvider` trait
(`web/src/identity.rs`): the authorize URL, the code exchange and the person's
profile (a stable `subject`, `login`, display name, avatar). GitHub (the App's
user-to-server OAuth, `GitHubIdentity` in `github.rs`) is the only
implementation; another provider is one more implementation in
`IdentityProviders` and a `user_identities.provider` value, and the callback, the
store and the personal tenant do not change. There is no username, email or
password login and no registration route: an unknown `{provider}` in
`/api/v1/auth/{provider}/...` is a 404. The flow uses a random `state`
bound to the browser by an `HttpOnly` cookie and a PKCE S256 challenge. The
user's GitHub token is used for the callback's three reads (`/user`,
`/user/installations`, and the organization role) and dropped: it is never
stored, logged or returned, and an error names what failed, never the request.
The session is a 256-bit random id in an `HttpOnly`,
`SameSite=Lax` cookie (`Secure` and `__Host-` prefixed when `SWARM_WEB_PUBLIC_URL`
is `https`); the store keeps only its SHA-256. A new id is minted on every
sign-in, sessions expire (`SWARM_WEB_SESSION_TTL_SECS`, default 8 h) and logout
deletes the row. Nothing credential-like goes in `localStorage`.

**Registration (#440).** The callback hands the cleaned profile to
`Store::register_identity`, one transaction. A (provider, subject) it has not
seen creates the `users` row, the `user_identities` row, a personal tenant and
the user's Owner membership (and sets `tenants.owner_user_id`), or none of them.
The tenant id is the login slugified to the tenant grammar
(`[a-z0-9][a-z0-9_-]{0,62}`: lowercase, other characters become `-`, edges
trimmed, `user` if nothing is left) and deduplicated when taken by suffixing
`-2`, `-3`, ... (then a random suffix after 50). Racing first sign-ins of one
identity are serialised (a Postgres advisory lock on provider and subject; the
partial unique index backs it), so exactly one user and one tenant result;
people whose logins collide each get their own id because the insert is `ON
CONFLICT DO NOTHING` and the loop moves on. A later sign-in matches on (provider,
subject), refreshes `login`, display name, avatar and `last_login_at`, and
**never changes the tenant id** when the GitHub login was renamed (the tenant's
`account_login` follows for display). A user whose personal tenant is missing
(a row from before `0004`) gets one at their next sign-in.

CSRF: every state-changing request needs the session's token in `X-CSRF-Token`
(constant-time compare against the copy stored with the session) and, if the
browser sends an `Origin`, it must be the configured origin. The token is
delivered in a script-readable `swarm_csrf` cookie and in `GET /session`;
`ui/api.js` echoes it. The check is in the `Authed` extractor, so a handler
that needs a session cannot skip it.

### Platform admins and the first admin (#441)

A **platform admin** (`users.is_platform_admin`) runs the hosted service. It is
not a tenant **owner**: an owner administers their own tenant (keys, budgets,
settings) and is a per-tenant role synced from GitHub; an admin acts on the
platform and holds no access to any tenant's data by being one. The flag is
only ever changed through `Store::set_platform_admin` and
`Store::bootstrap_platform_admin`; sign-in never touches it.

`Admin` (`auth.rs`) is the only door to the `/admin` routes. It is built on
`Authed`, so it has the same session, `Origin` and CSRF rules, and then reads the
flag from the store on every request (a demotion applies to a live session at
once). Anonymous is `401`, a missing or wrong CSRF token is `403 csrf_token`,
and a signed-in user who is not an admin gets `404 not_found`, the answer of a
route that does not exist, so the admin API is not discoverable.

| Command (`ui/api.js`) | Endpoint | Access | Does |
| --- | --- | --- | --- |
| `web_admin_list_users` | `GET /api/v1/admin/users` | platform admin | every user: `id`, `login`, `display_name`, `avatar_url`, `last_login_at`, `is_platform_admin`, by login |
| `web_admin_promote_user` | `POST /api/v1/admin/users/{userId}/promote` | platform admin | make the user an admin; `{"user": ..., "changed": bool}` |
| `web_admin_demote_user` | `POST /api/v1/admin/users/{userId}/demote` | platform admin | remove the flag; `409` for the last admin |

These routes are `catalog::ADMIN_ROUTES` (not tenant routes and not desktop
commands, so they are not in the table above); `api_catalog.rs` checks them
against `ui/api.js` and this page, and `tests/admin.rs` runs every one as an
anonymous caller, a non-admin and an admin. Promoting an admin or demoting a
non-admin is `200` with `changed: false` and writes nothing. An unknown user is
`404`.

**The last admin cannot be demoted** (`409 conflict`, by anyone, including that
admin). The check and the update are one step under a Postgres advisory lock
(`MemoryStore`: one mutex), so two admins demoting each other at the same moment
leave one. Every real change writes an `admin_audit_log` row in the same
transaction: `admin.bootstrap`, `admin.promote` or `admin.demote`, with the actor
and the target user ids and `{"target_login": ...}`; a refusal or a no-op writes
none. `Store::admin_audit_log` reads it (newest first); there is no HTTP route
for it yet.

**Bootstrap.** `SWARM_WEB_BOOTSTRAP_ADMINS` lists GitHub accounts, comma or
whitespace separated: a numeric GitHub id (stable, the safer spelling) or a
login (case-insensitive, `@` optional; a login that is renamed can later be
claimed by someone else, so prefer ids). An all-digit entry is an id. When a
listed account signs in and **no admin exists**, the callback calls
`Store::bootstrap_platform_admin`, which makes it admin atomically and writes
`admin.bootstrap` (actor and target both that user). It is once-only and
idempotent: while any admin exists the list does nothing, so a second listed
account is an ordinary user until an admin promotes them, repeated sign-ins add
no row, and demoting a listed admin later (possible only while another admin exists)
does not bring the bootstrap back. Only a `github` identity matches. An invalid entry stops the
start (`Config::from_lookup`) instead of leaving the platform without its admin.
The value is not a secret (it is a plain Terraform variable,
`bootstrap_admins`, and a plain Compose variable), and unset means no bootstrap:
nobody becomes admin and `/admin` answers `404` to everyone. Once the first
admin has promoted the others, the variable can be emptied.

### Tenants, roles and isolation

A tenant is either the user's **personal tenant** (`account_type` `Personal`, no
installation, created at first sign-in, named after the login: see
"Registration" above) or a GitHub App installation, created on first sight at
sign-in or by the `installation` webhook (`t` + 16 hex). Both match the worker
storage grammar, so the same string names the database tenant and the
object-store prefix. A personal tenant has no installation to mint a repository
token from, so a job for it is refused until an installation backs the work. A user belongs to the tenants GitHub says they can access;
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
and is listed under "Known gaps", and `SWARM_WEB_KMS_KEY_ID` is refused at
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
`SWARM_WEB_UI_DIR`, `SWARM_WEB_GITHUB_APP_SLUG`, `SWARM_WEB_INTERNAL_TOKEN`,
`SWARM_WEB_SESSION_TTL_SECS` and `SWARM_WEB_BOOTSTRAP_ADMINS` (GitHub logins or
numeric ids who may become the first platform admin; see "Platform admins")
are optional. Run it with
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
named `SWARM_STORAGE_*` (the names `storage_factory.py` reads, with the database
DSN and the S3 key pair as secrets) are forwarded into the container; the app
private key and the ECS control-plane credentials are not. The Fargate runner
signs its ECS calls with the API task's own role when the ECS agent provides one
(`AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` / `_FULL_URI`, optional
`AWS_CONTAINER_AUTHORIZATION_TOKEN`, refreshed every minute) and otherwise with
`SWARM_WEB_ECS_ACCESS_KEY_ID` / `SWARM_WEB_ECS_SECRET_ACCESS_KEY`, which is for a
non-AWS endpoint. On Fargate `SWARM_WEB_JOB_CPU_MILLIS` is in CPU units (1024 is
one vCPU), not millicores. A PEM private key may be given on one line with a
literal `\n` for each line break. `docker-compose.yml` runs all of this locally
(see "Local stack"); `web/docker-compose.jobs.yml` builds the fixture image for a
storage-only run.

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

## Local stack

`docker-compose.yml` at the repository root runs the whole hosted stack on one
machine:

```bash
cp web/.env.example .env     # GitHub App values and SWARM_WEB_LOCAL_KEY
docker compose up --build
python3 scripts/web_smoke.py # in another terminal; no sign-in needed
```

| Service | Image | Role |
| --- | --- | --- |
| `postgres` | `postgres:17` | execution history (one schema per tenant) and the platform schema (`web/migrations`, applied on the first start) |
| `minio`, `minio-bucket` | `minio/minio`, `minio/mc` | the S3-compatible object store and its `swarm-dev` bucket (checkpoints, logs, documents) |
| `worker-image` | `web/worker/Dockerfile` | builds the image the runner starts for every job, then exits. `SWARM_WORKER_DOCKERFILE=web/worker/Dockerfile.fixture` swaps in the fixture worker that needs no provider CLI |
| `api` | `web/Dockerfile` | the backend and the shared `ui/`, on `http://localhost:8080` |

Every published port is bound to `127.0.0.1`. The API runs read-only with all
capabilities dropped. It reaches Postgres and MinIO by service name, and so do the
job containers it starts: the runner attaches each job to the `swarm-stack`
network (`SWARM_WEB_DOCKER_NETWORK`) and gives it the same `SWARM_STORAGE_*`
values, under the names `storage_factory.py` reads. Credentials in the file
(`dev`/`devpassword`, `swarm`/`swarm`) are throwaway values for this machine.

**The Docker socket.** `DockerJobRunner` starts job containers through the host's
Docker, so the API container mounts `/var/run/docker.sock`. Whoever controls the
API process can therefore start any container on that host, which is
root-equivalent. That is acceptable for a developer's own machine and is why the
stack only listens on loopback. Do not expose this stack, and do not use this
runner for a shared or production host: use the Fargate runner (AWS mapping
below), a remote or rootless Docker daemon, or both. On Linux set `DOCKER_GID` to
the socket's group; the default (`0`) is what Docker Desktop and Rancher Desktop
present.

`web/docker-compose.yml` (Postgres and MinIO only, for the storage tests) and
`web/docker-compose.jobs.yml` (the fixture image with the same isolation flags
the runner applies) are still there for those narrower uses.

### Acceptance flow

The flow from #413, as a person runs it against the local stack. Steps 1-3 need a
GitHub App you registered and, for step 5, a public URL for its webhook (a tunnel
to `localhost:8080`); sign-in itself works without one. The right-hand column is
what covers each step without a GitHub App.

| # | Step | Expected | Automated coverage |
| --- | --- | --- | --- |
| 0 | `docker compose up --build`, `python3 scripts/web_smoke.py` | healthy, 8/8 checks | `test_web_deploy.py` (compose and smoke logic), CI `web-deploy` (smoke against the built binary) |
| 1 | open `http://localhost:8080`, **Sign in with GitHub** | signed in, a tenant per installation, CSRF cookie set | `web/tests/auth_flow.rs` |
| 2 | **GitHub App** page: install the App on a repository, **Re-check** | installation active | `web/tests/webhooks.rs`, `ui/web-account.test.js` |
| 3 | **API keys**: save the provider key; set a budget on **Quota & budget** | key shows *configured* (never the value); budget shown | `web/tests/secrets.rs`, `web/tests/quotas.rs` |
| 4 | save the repository's settings | `tenant_config` documents written, credential-shaped keys dropped | `web/tests/api_commands.rs`, `issue_worker/test_desktop_import.py` |
| 5 | label an issue (webhook), or **Run now** | one container for the repository, key for that provider only | `web/tests/orchestrator.rs`, `web/tests/webhooks.rs` |
| 6 | watch **Overview** | live job log lines over SSE, tokens redacted | `web/tests/sse.rs`, `web/tests/orchestrator.rs` |
| 7 | the job delivers | branch pushed, PR opened, lifecycle comments on the issue | `web/worker/fixture_worker.py`, `issue_worker/test_job_launch.py` |
| 8 | exit 13 / quota pause / exit 14 | relaunch from the stored checkpoint / wait / hold | `web/tests/orchestrator.rs`, `issue_worker/test_job_launch.py` |
| 9 | open another tenant's URL | `404`, never `403` | `web/tests/tenant_isolation.rs` |
| 10 | **Sign out** | session gone everywhere | `web/tests/auth_flow.rs` |

With the fixture worker (`SWARM_WORKER_DOCKERFILE=web/worker/Dockerfile.fixture`,
`SWARM_WORKER_IMAGE=swarm-automation-worker:fixture` in `.env`) step 7 delivers a
commit without a provider CLI, which is how the acceptance run avoids spending
provider quota.

### Building and publishing images

Building and publishing are **documented, not enabled**: no workflow here builds,
pushes or deploys an image (GitHub Actions no longer publishes releases, see
`.claude/rules/versioning.md`). A person with registry access does:

```bash
# from the repository root; the Fargate task definitions are X86_64
docker build --platform linux/amd64 -f web/Dockerfile        -t swarm-automation-api:$TAG .
docker build --platform linux/amd64 -f web/worker/Dockerfile -t swarm-automation-worker:$TAG .

aws ecr get-login-password | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
docker tag swarm-automation-api:$TAG    "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/swarm/api:$TAG"
docker tag swarm-automation-worker:$TAG "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/swarm/worker:$TAG"
docker push "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/swarm/api:$TAG"
docker push "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/swarm/worker:$TAG"
```

Use the `VERSION` string (or a commit SHA) as `$TAG`: the ECR repositories are
immutable-tag and scan on push, and `api_image` / `worker_image` take a full URI
with that tag. Both Dockerfiles pin every tool they install (the worker image pins
`gh`, Node and the three provider CLIs; both pin `psycopg`); bump the pins
together and rebuild. Turning this into a workflow later needs an OIDC role, not
stored AWS keys.

## API contract

Everything is under `/api/v1`; any other path is a static asset of `ui/`. Requests
and responses are JSON. A change to a route is a change to this contract: the
desktop's command table (`web/src/catalog.rs`) is the one source, `ui/api.js`
mirrors it and `web/tests/api_catalog.rs` fails on drift, so the full route table
is the one under "REST and SSE for the desktop's commands" above and is not
repeated.

- **Authentication.** A browser session: an `HttpOnly`, `SameSite=Lax` cookie
  (`Secure` and `__Host-` over HTTPS). No bearer token in `localStorage`. The one
  other credential is the operator's bearer token on `/api/v1/internal/...`, off
  unless `SWARM_WEB_INTERNAL_TOKEN` is set, and the webhook's HMAC signature.
- **CSRF.** Every non-`GET` route needs `X-CSRF-Token` (the value in `GET
  /session` and the `swarm_csrf` cookie) and, when the browser sends an `Origin`,
  it must be the configured origin. The signed webhook and the bearer-token
  operator API are the only cookie-less writes.
- **Tenancy.** The tenant is always the `{tenant}` path segment, re-checked for
  membership on every request; it is never read from a body, header or query
  string. A tenant the caller cannot reach is `404`.
- **Roles.** `member` reads and runs jobs against the tenant's own budget;
  `owner` changes settings, keys and budgets and does anything that merges,
  promotes, files an issue, imports or activates (`403 owner_required`).
- **Errors.** `{"error": "<message>", "code": "<code>"}`: `400 bad_request`, `401
  unauthorized`, `403 owner_required | csrf_token | csrf_origin | tenant_inactive`,
  `404 not_found` (also what a non-admin gets from `/admin`), `409` (a quota or
  concurrency denial from Run now, a webhook `replay`, demoting the last
  platform admin), `501 not_available_yet`, `503 bridge_unconfigured | jobs_unconfigured`.
  A body is at most 64 KiB and is never echoed in an error.
- **Unavailable is not zero.** A figure the backend cannot know (remaining quota
  with no budget and no provider report, history without a bridge) is `null` or a
  `503`/`501` with the reason, never a made-up number.
- **Public routes.** `GET /health`, `GET /session` (anonymous answer is
  `{"authenticated": false, "login_url": ..., "install_url": ...}`), `GET /version`
  and the OAuth redirect/callback. Everything else needs a session.
- **Security headers** on every response: a strict CSP (no `unsafe-inline`),
  `nosniff`, `frame-ancestors 'none'`, `no-referrer`, and HSTS over HTTPS.

Test: `web/tests/api_commands.rs`, `api_catalog.rs`, `auth_flow.rs`,
`tenant_isolation.rs`, `ui/api.test.js` (both transports) and the deployed-stack
checks of `scripts/web_smoke.py`.

## Server-Sent Events

Live updates are Server-Sent Events, not WebSockets: one-way, over plain HTTP,
reconnecting by themselves and carrying `Last-Event-ID`. `ui/api.js`'s `listen()`
is an `EventSource` on the web and Tauri's `listen` on the desktop; both deliver
the same payloads to the same handlers.

| Stream | Event name | `data` (JSON) |
| --- | --- | --- |
| `GET /api/v1/events/automation-log` | `automation-log` | `{"source", "stream", "line", "timestamp", "tenant", "repository", "issue"}` |
| `GET /api/v1/events/jobs` | `job-log` | `{"tenant", "repository", "issue", "line"}` |
| `GET /api/v1/events/model-calibration` | `model-calibration-refreshed` | the calibration status document |
| any | `resync` | `{"reason": "history_truncated"}` or `{"reason": "lagged", "missed": n}` |

A frame is `id: <n>`, `event: <name>`, `data: <one JSON line>`, then a blank line;
`: ready` opens the stream and `: heartbeat` follows every
`SWARM_WEB_SSE_HEARTBEAT_SECS` (default 15). Ids are process-wide and increasing;
a reconnect with `Last-Event-ID` is replayed from a bounded per-tenant ring (5000
frames) without gaps or duplicates, and `resync` tells the client to refetch
`GET /logs`. A slow reader gets `resync`, never an unbounded buffer. Streams are
session-scoped (the signed-in user's tenants, re-checked every 30 s, ended by
logout), so `listen()` needs no tenant parameter. Frames are scrubbed before they
are stored or sent; log lines are otherwise verbatim, so the `Adversarial UAT for
issue #...` and `Adversarial Cybersecurity for issue #...` boundary logs keep their
format. Behind a load balancer the idle timeout must exceed the heartbeat (the ALB
here is 120 s). Details and tests: "REST and SSE" above, `web/tests/sse.rs`.

## Tenancy model

A tenant is one GitHub App installation (a user account or an organization). Its
id (`t` + 16 hex) is the same string in the platform database, the worker's
storage grammar and every object key, so there is no mapping to get wrong. Isolation
is structural and enforced in every layer a request touches:

| Layer | How a tenant is kept apart |
| --- | --- |
| HTTP | `TenantAccess` proves membership in the path's tenant, `404` otherwise |
| Platform store | every tenant-scoped `Store` method takes the tenant first; tables lead their key with `tenant_id` |
| Provider keys | sealed with AES-256-GCM, bound to `(tenant, provider)`; one provider's variable per job |
| Execution history | a Postgres schema per tenant (`t_<tenant>`), `search_path` set on every connection |
| Objects | a validated prefix per tenant (`tenants/<tenant>/...`); keys come only from `validate_tenant` / `check_*` output |
| Jobs | one container per repository; one repository-scoped GitHub installation token; one provider key; its own tmpfs workspace and `HOME` |
| Events | per-tenant replay ring; a stream only carries the signed-in user's tenants |

Roles are per tenant: **owner** (the installation's user, or an organization admin)
and **member**. Memberships are re-synced from GitHub at every sign-in, and a
suspended or deleted installation refuses writes and new jobs. Settings are
`tenant_config` documents, written only by the hosted backend and
`desktop_import.py`. The one exception to per-tenant credentials is the shared
storage key a job holds on AWS (see the threat model and known gaps).

## Runner abstraction

`JobRunner` (`web/src/runner.rs`) is the only way a worker container starts. The
orchestrator speaks to the trait: `start`, `status`, `cancel`, `pause`,
`resume_running`, `resume_from_checkpoint` (always a new container), `logs` and
`stream_logs`. `DockerJobRunner` and `EcsFargateJobRunner` consume one `JobSpec`
(image, entrypoint, command, plain and secret environment, CPU, memory, optional
deadline) and the same entrypoint, `issue_worker/job_launch.py`, so the worker
cannot tell them apart.

| | Docker (local) | ECS Fargate (AWS) |
| --- | --- | --- |
| Start | `docker run` with an env file (mode 0600, removed after start) | `RunTask` against the `<prefix>-worker` task definition, container `worker` overridden |
| Secrets in transit | env file, never argv | container environment override; the repository token is minted per job |
| Isolation | read-only root, uid 1000, `--cap-drop ALL`, `no-new-privileges`, pid and memory limits, tmpfs `/tmp`, `/workspace`, `HOME`, metadata address sunk | read-only root, uid 1000, all capabilities dropped, ephemeral volumes, no public IP, no execute-command, no inbound |
| Cloud identity | none | an empty task role (denies the AWS control plane); no inherited credentials |
| Pause | `docker pause` | `PauseTask` is sent; where unsupported the task is stopped and resumed from the checkpoint |
| Control-plane auth | the host's Docker socket | the API task role (`AWS_CONTAINER_CREDENTIALS_*`), or a static key pair for a non-AWS endpoint |
| CPU unit | millicores (`--cpus`) | Fargate CPU units (1024 = 1 vCPU): set `SWARM_WEB_JOB_CPU_MILLIS` accordingly |

The orchestrator (`web/src/orchestrator.rs`) is webhook-driven with `tick` as the
poll and the checkpoint resume. One repository has one active container; exit 13
relaunches immediately from the `in-progress` checkpoint, exit 11 waits
`SWARM_WEB_QUOTA_RESUME_SECS`, exit 14 holds until Run now, Resume, a trusted
follow-up or a new image id. There is no default job deadline.

## AWS mapping

`web/infra/aws` is a Terraform root module (provider `hashicorp/aws`, plus
`random`). **Nothing applies it**: CI runs `terraform fmt -check`, `init
-backend=false` and `validate` with no credentials, and a person runs `plan` and
`apply` (README there). Terraform over CDK because the repository has no Node build
step, the resource set is fixed and modest, the diff is plain text, and format and
validation need no AWS account.

| Concern | AWS resource | File |
| --- | --- | --- |
| API | ECS cluster and Fargate service `<prefix>-api` behind an ALB (HTTPS, TLS 1.3 policy, `/api/v1/health`, 120 s idle timeout for SSE) | `ecs.tf`, `alb.tf` |
| Job runner | Fargate task definition `<prefix>-worker`, started by `EcsFargateJobRunner` | `ecs.tf` |
| Postgres | RDS PostgreSQL 17, private subnets, KMS, TLS enforced, backups, Multi-AZ | `rds.tf` |
| Object storage | one S3 bucket: versioned, KMS, TLS-only, public access blocked | `s3.tf` |
| Encryption | one KMS key (rotated) for S3, RDS, secrets, logs and ECR | `kms.tf` |
| Secrets | Secrets Manager: the generated database DSN, and the GitHub App secrets, the sealing key and the storage key pair, filled by the operator | `secrets.tf` |
| Images | two immutable, scanned ECR repositories | `ecr.tf` |
| Network | VPC, public subnets for the ALB only, private subnets for everything else, one NAT, an S3 endpoint, one security group per tier | `network.tf` |
| IAM | execution roles (API, jobs), the API task role, a permissionless job task role, a bucket-scoped storage user | `iam.tf` |

| Role | May | May not |
| --- | --- | --- |
| API execution | pull the API image, write its logs, read the API's own secrets and decrypt them | anything else |
| Job execution | pull the worker image, write job logs | read any secret |
| API task | `RunTask` for the worker definition on this cluster, tag, describe and stop those tasks, pass exactly the two job roles | touch S3, secrets or KMS |
| Job task | nothing; an explicit deny covers ECS, IAM, Secrets Manager, KMS, SSM, STS and ECR | everything |
| Storage user | get/put/delete objects in the one bucket, use the KMS key through S3 only | any other service |

How the API's environment is built from this: `SWARM_WEB_JOB_RUNNER=fargate`,
`SWARM_WEB_ECS_CLUSTER`, `SWARM_WEB_ECS_TASK_DEFINITION` (the worker family),
`SWARM_WEB_ECS_SUBNETS` and `SWARM_WEB_ECS_SECURITY_GROUPS` (the job group: no
inbound, 443 and the database out), `SWARM_WEB_WORKER_IMAGE`, and the
`SWARM_STORAGE_*` set (S3 over HTTPS with virtual-hosted addressing, the DSN with
`sslmode=require`). Secrets arrive through ECS `secrets`, never as plain
environment. `SWARM_WEB_KMS_KEY_ID` is deliberately not set: the build has no KMS
key wrapper and refuses that setting, so tenants' provider keys are sealed with
`SWARM_WEB_LOCAL_KEY` from Secrets Manager until the wrapper lands. The service
starts at zero tasks and is limited to one: the SSE replay rings and the job
scheduler are per process (identity, tenants, sessions, keys, usage and job
slots are in Postgres, `SWARM_WEB_STORE=postgres`).

Cost: a NAT gateway, Multi-AZ RDS, an ALB and Fargate tasks are not free; a
throwaway environment sets `protect_data = false` and `db_multi_az = false`.

## Threat model

Assets: tenants' provider API keys, their GitHub installation access, their source
code and issue content, other tenants' data, and the operator's cloud account.
Trust boundaries: browser to API, GitHub to API (webhooks), API to job container,
job container to the repository's code, provider APIs and storage.

| Threat | Mitigation | Residual risk |
| --- | --- | --- |
| A user reads or changes another tenant's data | `TenantAccess` (404), tenant-first store methods, per-tenant schema and prefix, bound key sealing, per-tenant event ring; `tenant_isolation.rs` iterates every catalog route | a job holds the shared storage key (below) |
| Session theft, CSRF | `HttpOnly` `SameSite=Lax` cookie, hashed at rest, rotation on sign-in, CSRF token plus `Origin` check, strict CSP with no inline code, nothing in `localStorage` | an XSS-free `ui/` is a property to keep testing (`ui/web-account.test.js`) |
| A user gives themselves platform admin | `Admin` (signed in, CSRF, flag read per request, `404` otherwise); sign-in never sets the flag; the only other path is `SWARM_WEB_BOOTSTRAP_ADMINS`, which works while no admin exists and only for a listed GitHub account; every change is audited; the last admin cannot be demoted | the bootstrap list is operator-controlled input: a listed login that GitHub later reassigns is the first account to sign in once no admin exists, so list ids and empty the variable once admins exist |
| Provider key disclosure | write-only API, envelope encryption, `Secret` redaction, zeroize, canary tests; plaintext leaves only through `Vault::job_environment`, one provider | the sealing key is a Secrets Manager value until the KMS wrapper lands |
| Forged or replayed webhook | HMAC verified over the raw body before any work, idempotent by delivery id and payload hash | none beyond the App secret |
| Hostile repository or issue content steering a job | one container per repository, read-only root, uid 1000, no capabilities, no inbound, per-job tmpfs, fresh clone, a repository-scoped, short-lived installation token, no app private key and no AWS identity in the container, metadata address blocked, trusted-author gate for follow-ups | outbound 443 is open: a job can exfiltrate what it can read (its repository, its own provider key, its storage key) |
| A job reads other tenants' objects or schemas | prefix and schema isolation in the worker's own code | the storage user's key and the database login are not per tenant. **Planned**: per-job STS session policies for `tenants/<id>/*` and a role per tenant schema |
| Secret leakage through logs and SSE | log writer and event frames scrub tokens, key blocks, credentialed URLs, configured secrets; request logs carry no query string | a secret in an unusual shape is not matched; scrubbing is defence in depth, not a licence to log secrets |
| Operator or CI credential in source | Terraform never holds the GitHub App key, the sealing key or the storage key (put by hand); `.env`, state and tfvars are git- and docker-ignored; a test scans the deployment files | the Terraform state holds the generated database password: use an encrypted, access-controlled backend |
| Docker socket on the local stack | loopback-only ports, read-only API container, documented as dev only | root-equivalent on that host: never expose or share it |
| Runaway cost or abuse | per-tenant concurrency, monthly spend cap and provider budgets checked before a job starts | no default job deadline (`SWARM_WEB_JOB_MAX_RUNTIME_SECS` opts in) |
| Image supply chain | tools and drivers pinned, immutable ECR tags, scan on push | pins are bumped by hand; the base images float on their tag until pinned by digest |
| Operator API misuse | disabled (404) without `SWARM_WEB_INTERNAL_TOKEN`; 24-character minimum; compared in constant time | the token is a bearer secret: keep it off the internet and rotate it |

## Decisions

- **Rust/axum in `web/`, a standalone Cargo project.** It ports the desktop's
  `config.rs` and `tools.rs` logic and shares types and conventions. The root
  `Cargo.toml` has no `[workspace]` and the desktop does not depend on `web/`.
- **One shared `ui/` and one shared `issue_worker/`.** The transport adapter
  (`ui/api.js`) is the only thing that differs; markup is `data-web-only` and
  `data-desktop-only` attributes, not forks. Worker behaviour is unchanged: only
  storage, transport and runtime plumbing moved.
- **Postgres and S3-compatible storage behind `storage.py`.** The worker's seam is
  the contract; local files stay byte-compatible in both directions. History is a
  schema per tenant so the repository's own SQL cannot name another tenant's rows.
- **Server-Sent Events, not WebSockets.** Traffic is server to client, it works
  through the ALB and `EventSource` reconnects with `Last-Event-ID`.
- **One container per repository job behind `JobRunner`.** Docker locally, Fargate
  on AWS. No Kubernetes and no Lambda: a job runs for hours, needs a real
  filesystem and `git`, and must be torn down afterwards.
- **GitHub App for sign-in, installation tokens and webhooks.** One identity
  provider behind an `IdentityProvider` trait, repository-scoped job tokens, no
  passwords.
- **Bring your own provider keys, sealed per tenant.** Spend is the tenant's own;
  the platform enforces budgets but never holds a shared provider key.
- **Terraform, not CDK**, for the reasons above; and applied only by a human.
- **A KMS key for data at rest, a local wrapper for provider keys, for now.** The
  API refuses a KMS setting it cannot honour rather than silently ignoring it.
- **A scoped storage user, not a job role.** The worker's S3 client signs with a
  key and a job must not hold an AWS identity, so jobs get the storage user's key
  through the API and the job's task role is empty. STS-scoped per-job credentials
  are the follow-up.
- **The compose stack mounts the Docker socket.** It is the only way the Docker
  runner can run on a developer's machine; the threat model records what that
  means.
- **Images are built by a person, not CI.** GitHub Actions no longer publishes;
  publishing moves to the web path later (OIDC, not stored keys).
- **`VERSION` is untouched.** The worker owns it; this change is a patch-level
  deployment addition behind existing settings.

## Known gaps

What is not built yet, by design of the phased work in #413:

- **Per-process runtime state.** The Postgres `Store` holds identity, tenants,
  sessions, keys, usage and job slots, but the SSE replay rings (`events.rs`)
  and the scheduler's in-flight bookkeeping are still per process, so the AWS
  service stays limited to one task until those move too.
- **KMS key wrapper.** `SWARM_WEB_KMS_KEY_ID` is refused until a `KeyWrapper` for
  KMS exists.
- **Per-tenant storage credentials for jobs** (STS session policy and a database
  role per tenant), and a DNS-aware egress allowlist for job tasks
  (`web/worker/egress-allowlist.txt`).
- **Worker operations answering 501** (`web_bridge.UNAVAILABLE`): the knowledge
  index, model calibration snapshots, diagnostics, and branch, merge and promotion
  operations.
- **Fargate log snapshots.** The runner can read CloudWatch Logs but the API does
  not wire a log group, so the Overview reads the worker's own log artifacts and
  the event stream.
- **Web screens** for repository settings and history beyond the views listed
  above, and per-process (`uat:<repo>`) controls.
- **Images are not built in CI**, and the AWS module has been validated
  (`terraform validate`) but never applied.

The desktop stays the fallback until a human decides to retire it.
