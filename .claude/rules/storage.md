# Storage interface rules

`issue_worker/storage.py` is the one seam between the worker and where its
state lives; `docs/web-architecture.md` ("Storage") is the contract. The
hosted web version (#413) implements it in `storage_remote.py` (`RemoteStorage`:
S3-compatible objects via `object_store.py` for checkpoints, logs and documents,
a Postgres schema per tenant for execution history).

- Every `Storage` operation takes a `tenant` first. Never add a tenant-less
  method. The desktop and worker use `DEFAULT_TENANT`, and `LocalStorage` keeps
  that tenant at the exact legacy file names, bytes and SQLite path: a state
  directory must keep working across versions in both directions.
- A hosted job does not retarget the worker's checkpoint calls.
  `job_launch.py` and `job_checkpoint_sync.py` copy checkpoints and the
  delivery artifacts around the worker process. The container still uses
  `DEFAULT_TENANT` on local files, and the hosted copy is stored under the
  real tenant id. The desktop never sets `SWARM_JOB_STORAGE`.
- New persisted state is a new registered checkpoint kind, document collection
  or artifact name, not an ad hoc file under `state_dir`. The kind sets are
  closed on purpose; update `docs/web-architecture.md` with them.
- `storage.py` stays standard-library only and `issue_worker/` imports only the
  standard library and sibling modules, so the worker image never pulls in
  desktop code (`test_storage.WorkerImageTests` enforces it). Do not import
  `src/` or `ui/` assets, Tauri, or a third-party package from the worker.
- Execution history keeps `SCHEMA_VERSION` and migrations 3, 5 and 9 unchanged
  (a protected adversarial test pins them). A new backend wraps its errors in
  `StorageError`; `ExecutionHistoryService` absorbs it like `sqlite3.Error`.
- Every implementation must pass `storage_contract.StorageContract`: local,
  hosted-on-memory/SQLite, hosted-on-the-S3-test-server and (CI's `storage-live`
  workflow, `SWARM_TEST_POSTGRES_DSN`) hosted-on-Postgres. Extend the
  contract for new behavior instead of testing one backend only; the checkpoint
  resume tests open a second instance to model a freshly started process
  (exit 13 and 14).
- The hosted backend takes its database as an injected DB-API `connect`
  callable and its objects as an `ObjectStore`; it never imports psycopg, boto3
  or any SDK. `storage_factory` loads a driver only when the operator names one
  (`SWARM_STORAGE_POSTGRES_DRIVER`), and credentials come from the environment,
  never an argument, a log line, a `repr` or an error message.
- Hosted history reuses `ExecutionHistoryRepository`'s write paths (the
  sanitizer included) through `SqlExecutionHistory`; do not fork them. When
  `SCHEMA_VERSION` or a history column changes, `storage_schema.py` must follow
  (`SchemaParityTests` fails until it does). Keep the SQL the repository emits
  portable between SQLite and Postgres, or teach `storage_remote.translate` the
  difference, and wrap every driver failure in `StorageError`.
- Tenant isolation is structural: a Postgres schema per tenant (`search_path`
  is set on every connection) and a validated object prefix per tenant. Never
  build a key or an identifier from anything but `validate_tenant` /
  `check_*` output.
- `tenant_config` documents (`app`, `repo-<id>`) hold a tenant's settings. The
  desktop never writes them; the hosted backend and `desktop_import.py` do.
  The importer never reads provider credentials, key files or the keychain,
  strips credential-shaped keys, keeps what a tenant already has unless
  `--overwrite`, and must stay idempotent and dry-runnable (a dry run writes
  nothing, not even an empty tenant schema).
- Do not weaken `tests/adversarial/` to fit a storage change.
