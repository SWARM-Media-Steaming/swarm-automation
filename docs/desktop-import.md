# Importing a desktop install into a tenant

`issue_worker/desktop_import.py` is a one-time, **re-runnable and idempotent**
import of a SWARM Automation desktop install into one tenant of the hosted
storage (see "Hosted storage" in [web-architecture.md](web-architecture.md)). It
generalizes the desktop's own history import (which backfills execution history
from GitHub) to the whole install.

## What is imported

| Source (desktop) | Destination (tenant) |
| --- | --- |
| `config.json` | `tenant_config` document `app` (app-wide settings) and one `repo-<id>` document per monitored repository |
| `<worker_state_dir>/architecture_docs/*.json` | `architecture_docs` documents, same keys, redacted again on the way in |
| `<worker_state_dir>/swarm-automation.sqlite3` | the tenant's execution history: `ai_executions` (including the adversarial, security, epoch, merge-policy and promotion columns of migrations 3, 5 and 9), `adversarial_rounds`, `adversarial_epochs`, `ai_token_usage`, `jev_decisions`, `jev_score_comparisons` |

Not imported: checkpoints and logs (they describe work in flight on one machine),
the knowledge/diagnostic/complexity stores, managed checkouts, and anything under
`worker.lock`. Re-add repositories' GitHub App installation on the web instead of
copying key files.

## Credentials are never imported

The tool never opens the macOS keychain, the GitHub App key files
(`github_apps_config` is a *path*, and it is left out), or a provider CLI's login.
Tenants re-enter provider API keys in the web app. As defence in depth the
configuration is also scrubbed: keys that look like credentials (`token`,
`*_token`, `secret`, `password`, `api_key`, `credential`, `private_key`,
`authorization`, ...) are dropped at any depth, PEM key blocks are removed, and
string values go through the history sanitizer. The report names what was
dropped (key paths only, never values). Settings that only mean something on the
desktop machine (`workspace_root`, `worker_state_dir`, `gh_bin`, `python_bin`,
`jev_bin`, each provider's `bin`, each repository's `repo_dir` and
`github_apps_config`) are listed as "left out as desktop-only".

Every imported history row passes through the same sanitizer as a live write
(secrets, key blocks); JSON columns keep their structure and only their string
values are redacted. The source is read from a **copy** of the database (and its
write-ahead log) in a temporary directory: the desktop install is never opened
for writing, so it is safe to run while the app is open, though closing the app
first guarantees the newest rows are on disk.

## Running it

```sh
# Hosted: storage comes from SWARM_STORAGE_* (see storage_factory.py); the
# Postgres driver is named, never bundled.
export SWARM_STORAGE_POSTGRES_DSN=postgresql://...
export SWARM_STORAGE_POSTGRES_DRIVER=psycopg:connect
export SWARM_STORAGE_S3_ENDPOINT=https://s3.us-east-1.amazonaws.com
export SWARM_STORAGE_S3_BUCKET=swarm-prod
export SWARM_STORAGE_S3_ACCESS_KEY_ID=... SWARM_STORAGE_S3_SECRET_ACCESS_KEY=...

python3 issue_worker/desktop_import.py --tenant acme \
    --config "$HOME/Library/Application Support/app.swarm.automation/config.json" \
    --dry-run
```

`--state-dir` defaults to the config's `worker_state_dir`; `--history-db` and
`--architecture-dir` override the two sources. `--only config,architecture,history`
picks sections. `--target local:<dir>` imports into a local state directory
(tenant under `<dir>/tenants/<id>/`) instead, which is also how to rehearse the
import without any server. `--json` prints the report as JSON. Importing into the
`default` tenant of hosted storage is refused. Exit status: `0` success, `1` a
problem to fix (unreadable config or database, unknown section, storage error;
nothing is half-imported: history is one transaction), `2` refused tenant.

The tenant id is the hosted tenant (the GitHub App installation's tenant id).

## Dry run

`--dry-run` does the work and writes nothing: documents are compared but not
written, and history is imported inside a transaction that is rolled back, so the
counts are exactly what the real run produces (including renumbering). A tenant
with no history yet is rehearsed on a scratch store, so a dry run never creates
the tenant's Postgres schema. Real output against a fixture install:

```text
SWARM Automation desktop import (dry run: nothing was written)
Tenant: acme

Configuration
  app                                          would import
  repo-acme__web                               would import
  repo-acme__api                               would import
  left out as desktop-only: gh_bin, providers.claude.bin, providers.codex.bin, python_bin, repositories.acme__web.github_apps_config, repositories.acme__web.repo_dir, worker_state_dir, workspace_root
  credential-like settings dropped: extras.api_key, extras.key_block, github_token

Architecture documentation
  acme_web-1a2b3c4d                            would import
  skipped (not a snapshot): broken.json, not-a-snapshot.json

Execution history
  ai_executions            3 in source: 3 would import, 0 already present
  adversarial_rounds       2 in source: 2 would import, 0 already present
  adversarial_epochs       1 in source: 1 would import, 0 already present
  ai_token_usage           2 in source: 2 would import, 0 already present
  jev_decisions            1 in source: 1 would import, 0 already present
  jev_score_comparisons    1 in source: 1 would import, 0 already present

Credentials: none imported. Provider API keys, the keychain and GitHub App key files are never read; enter provider keys in the web app.
```

The real run prints the same report with `imported` / `3 imported`; a second run
prints `unchanged` and `0 imported, 3 already present`.

Per-document statuses: `imported` (new), `unchanged` (already there, same
content), `kept` (the tenant already has a different version, left alone),
`overwritten` (`--overwrite`). Per-table history counts: `imported`, `already
present`, `renumbered` and `skipped (no parent execution)`.

## Idempotency and re-runs

- History rows keep their source ids, and an id already present is skipped, so a
  second run changes nothing. A run that failed part-way imported no history
  (single transaction): run it again.
- If the tenant already has a *different* execution for the same repository,
  issue and attempt number (the unique key), the imported execution is renumbered
  to the next free attempt and counted as `renumbered`. Re-running does not
  renumber again.
- Configuration and documents the tenant already has are **kept** by default, so a
  re-run never undoes edits made on the web. `--overwrite` replaces them with the
  desktop's versions.
- Execution history is only as complete as the source. A source whose schema is
  older (missing columns take their defaults) or newer (unknown columns are
  ignored, and the report says the source is newer) than this importer imports
  the columns both know.
- `ai_execution_history_enabled` still governs *live* recording on the web; it
  does not stop an explicit import. Use `--only config,architecture` to leave
  history out.

## Tests

`issue_worker/test_desktop_import.py` builds a fixture desktop install with the
real history repository (including planted secrets and an old-schema database) and
checks: everything lands, running twice changes nothing, the dry run writes
nothing and predicts the real run, no credential reaches the tenant or the
report, the source is byte-identical afterwards, other tenants are untouched,
attempt clashes renumber, the CLI output, and the hosted target on SQLite history
and on a live Postgres (`SWARM_TEST_POSTGRES_DSN`).
