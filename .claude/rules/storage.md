# Storage interface rules

`issue_worker/storage.py` is the one seam between the worker and where its
state lives; `docs/web-architecture.md` ("Storage") is the contract. The
hosted web version (#413) adds Postgres/S3 implementations behind it later.

- Every `Storage` operation takes a `tenant` first. Never add a tenant-less
  method. The desktop and worker use `DEFAULT_TENANT`, and `LocalStorage` keeps
  that tenant at the exact legacy file names, bytes and SQLite path: a state
  directory must keep working across versions in both directions.
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
- Every implementation must pass `storage_contract.StorageContract`. Extend the
  contract for new behavior instead of testing one backend only; the checkpoint
  resume tests open a second instance to model a freshly started process
  (exit 13 and 14).
- Do not weaken `tests/adversarial/` to fit a storage change.
