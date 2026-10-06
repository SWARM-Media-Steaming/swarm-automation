# Web architecture

SWARM Automation gains a hosted, multi-tenant web version alongside the Tauri
desktop app (tracked in #413). The desktop stays fully working; `ui/` and
`issue_worker/` are shared. This document records the pieces as they land.
Today it covers the storage seam; later sections (web backend, job runner,
auth) are added by their own issues.

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
| Documents | `read/write/delete/list_document(s)` | Durable product data (`architecture_docs`, one snapshot per repository) |
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
checkout); moving them onto the interface calls is part of adding a remote
implementation. Not covered yet and still local: the single-worker process lock
(`worker.lock`), downloaded issue images, and the app-wide SQLite stores for
complexity profiles, diagnostics and engineering knowledge. Postgres and
S3-compatible implementations are a later issue.

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

### Worker image entrypoint

`issue_worker/worker_entrypoint.py` runs the worker from a plain copy of
`issue_worker/` (no package install, no `src/` or `ui/`) and keeps its exit
codes. It is the command the future worker image runs.
