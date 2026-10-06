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
- A behavior-changing web setting needs the `minor` label from a trusted author;
  the worker owns `VERSION`.
