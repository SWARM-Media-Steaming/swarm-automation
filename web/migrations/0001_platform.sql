-- Platform schema of the hosted backend (issue #417, part of #413).
--
-- One table per `Store` concern (web/src/store.rs). Everything tenant-scoped
-- carries `tenant_id` as the first column of its key and cascades from
-- `tenants`, so deleting a tenant removes its rows and no query can address a
-- row without naming one. A tenant's *worker* data (execution history, and the
-- checkpoints, logs, architecture docs and settings kept in object storage) is
-- not here: `issue_worker/storage_remote.py` keeps history in the tenant's own
-- schema (`t_<tenant>`) and everything else in S3. Settings (the desktop's
-- app-wide and per-repository configuration) are `tenant_config` documents in
-- that storage, so the worker job reads them where it reads its checkpoints.
--
-- Idempotent (IF NOT EXISTS throughout): safe to apply on every start. Provider
-- keys are stored sealed only (envelope encryption, web/src/crypto.rs); there is
-- no column that can hold a plaintext key.

CREATE TABLE IF NOT EXISTS platform_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS web_users (
    id          TEXT PRIMARY KEY,
    github_id   BIGINT NOT NULL UNIQUE,
    login       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The cookie value is never stored, only its SHA-256.
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES web_users (id) ON DELETE CASCADE,
    csrf_token  TEXT NOT NULL,
    expires_at  BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS web_sessions_expiry_idx ON web_sessions (expires_at);
CREATE INDEX IF NOT EXISTS web_sessions_user_idx ON web_sessions (user_id);

-- One tenant per GitHub App installation. The id grammar matches
-- issue_worker/storage.py (`[a-z0-9][a-z0-9_-]{0,62}`) because the same string
-- names the tenant's history schema and object-store prefix.
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id        TEXT PRIMARY KEY CHECK (tenant_id ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    installation_id  BIGINT NOT NULL UNIQUE,
    account_login    TEXT NOT NULL,
    account_type     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'deleted')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tenant_memberships (
    tenant_id  TEXT NOT NULL REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES web_users (id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('owner', 'member')),
    PRIMARY KEY (tenant_id, user_id)
);
CREATE INDEX IF NOT EXISTS tenant_memberships_user_idx ON tenant_memberships (user_id);

-- Secret metadata: which sealed key is stored for a provider, who set it and
-- when. `wrapper_key_id` tells a rotation which master key sealed the row.
CREATE TABLE IF NOT EXISTS tenant_provider_keys (
    tenant_id         TEXT NOT NULL REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    provider          TEXT NOT NULL CHECK (provider IN ('claude', 'codex', 'grok', 'model-data')),
    format_version    SMALLINT NOT NULL,
    wrapper_key_id    TEXT NOT NULL,
    wrapped_data_key  BYTEA NOT NULL,
    ciphertext        BYTEA NOT NULL,
    updated_at        BIGINT NOT NULL,
    updated_by        TEXT NOT NULL,
    PRIMARY KEY (tenant_id, provider)
);

-- Operator-set plan limits and tenant-set budgets.
CREATE TABLE IF NOT EXISTS tenant_plan_quotas (
    tenant_id              TEXT PRIMARY KEY REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    max_concurrent_jobs    INTEGER NOT NULL DEFAULT 2 CHECK (max_concurrent_jobs >= 0),
    monthly_spend_cap_usd  DOUBLE PRECISION CHECK (monthly_spend_cap_usd IS NULL OR monthly_spend_cap_usd >= 0)
);

CREATE TABLE IF NOT EXISTS tenant_budgets (
    tenant_id                  TEXT PRIMARY KEY REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    minimum_remaining_percent  DOUBLE PRECISION NOT NULL DEFAULT 10
                               CHECK (minimum_remaining_percent >= 0 AND minimum_remaining_percent <= 100),
    provider_budgets_usd       JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Usage accounting rows (the web view of `token_usage.UsageRecord`). A NULL
-- cost is an unpriced invocation: unknown, never zero. Idempotent per entry id
-- across the tenant.
CREATE TABLE IF NOT EXISTS tenant_usage_ledger (
    tenant_id    TEXT NOT NULL REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    entry_id     TEXT NOT NULL,
    period       TEXT NOT NULL CHECK (period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    provider     TEXT NOT NULL CHECK (provider IN ('claude', 'codex', 'grok', 'model-data')),
    cost_usd     DOUBLE PRECISION CHECK (cost_usd IS NULL OR cost_usd >= 0),
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, entry_id)
);
CREATE INDEX IF NOT EXISTS tenant_usage_ledger_period_idx ON tenant_usage_ledger (tenant_id, period, provider);

-- Provider-reported remaining headroom (valid for an hour; reported_at is epoch seconds).
CREATE TABLE IF NOT EXISTS tenant_provider_reports (
    tenant_id          TEXT NOT NULL REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    provider           TEXT NOT NULL CHECK (provider IN ('claude', 'codex', 'grok', 'model-data')),
    remaining_percent  DOUBLE PRECISION NOT NULL CHECK (remaining_percent >= 0 AND remaining_percent <= 100),
    detail             TEXT,
    reported_at        BIGINT NOT NULL,
    PRIMARY KEY (tenant_id, provider)
);

-- Worker jobs. A row holds a concurrent-job slot while `status = 'active'`
-- (reserve is idempotent per job id; release flips it to 'released').
CREATE TABLE IF NOT EXISTS tenant_jobs (
    tenant_id    TEXT NOT NULL REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    job_id       TEXT NOT NULL,
    provider     TEXT NOT NULL CHECK (provider IN ('claude', 'codex', 'grok', 'model-data')),
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at  TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, job_id)
);
CREATE INDEX IF NOT EXISTS tenant_jobs_active_idx ON tenant_jobs (tenant_id) WHERE status = 'active';

-- GitHub webhook deliveries arrive before the tenant is known, so they are not
-- tenant-scoped. A delivery id is claimed once; a payload hash is accepted once
-- (a replay under a new id is refused).
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id     TEXT PRIMARY KEY,
    payload_sha256  TEXT NOT NULL UNIQUE,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO platform_migrations (version) VALUES ('0001_platform') ON CONFLICT DO NOTHING;
