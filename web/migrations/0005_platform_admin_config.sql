-- Platform-level configuration an admin manages (see docs/web-architecture.md,
-- "Platform configuration").
--
-- model_blacklist: the database form of skills/model-router/model-blacklist.json.
-- A row names a retired model, its successor ('' = none; the entry then applies
-- outright) and why. Seeded once from the bundled file's entries, guarded on this
-- migration's own version row so a row an admin deleted never comes back when the
-- file is applied again (`psql -f`).
--
-- platform_provider_keys: provider API keys the platform itself uses, sealed like
-- tenant keys (envelope encryption, write-only). `purpose` is 'platform' for
-- cross-cutting AI concerns (routing, complexity analysis, Jev) and 'automation'
-- for Swarm automation concerns.
--
-- Idempotent: tables are IF NOT EXISTS and the seed runs once.

CREATE TABLE IF NOT EXISTS model_blacklist (
    model          TEXT PRIMARY KEY CHECK (model <> ''),
    superseded_by  TEXT NOT NULL DEFAULT '',
    reason         TEXT NOT NULL DEFAULT '',
    updated_at     BIGINT NOT NULL,
    updated_by     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_provider_keys (
    purpose           TEXT NOT NULL CHECK (purpose IN ('platform', 'automation')),
    provider          TEXT NOT NULL CHECK (provider IN ('claude', 'codex', 'grok', 'model-data')),
    format_version    SMALLINT NOT NULL,
    wrapper_key_id    TEXT NOT NULL,
    wrapped_data_key  BYTEA NOT NULL,
    ciphertext        BYTEA NOT NULL,
    updated_at        BIGINT NOT NULL,
    updated_by        TEXT NOT NULL,
    PRIMARY KEY (purpose, provider)
);

INSERT INTO model_blacklist (model, superseded_by, reason, updated_at, updated_by)
SELECT v.model, v.superseded_by, v.reason, 0, 'seed'
FROM (VALUES
    ('claude-sonnet-4-6', 'claude-sonnet-5-5', 'Older Sonnet release, Sonnet 5.5 is stronger at the same or lower price.'),
    ('claude-sonnet-5', 'claude-sonnet-5-5', 'Sonnet 5.5 measures higher at every effort and is faster at the same price.'),
    ('claude-opus-4-6', 'claude-opus-5-5', 'Older Opus release, Opus 5.5 is stronger and cheaper.'),
    ('claude-opus-4-7', 'claude-opus-5-5', 'Older Opus release, Opus 5.5 is stronger and cheaper.'),
    ('claude-opus-4-8', 'claude-opus-5-5', 'Older Opus release, Opus 5.5 is stronger and cheaper.'),
    ('claude-opus-5', 'claude-opus-5-5', 'Opus 5.5 measures higher and costs less.'),
    ('claude-fable-5', 'claude-fable-5-1', 'Fable 5.1 supersedes Fable 5.'),
    ('gpt-6-sol', 'gpt-6-1-sol', 'GPT-6.1 Sol supersedes GPT-6 Sol.'),
    ('grok-4.7-build-fast', 'grok-4.7', 'Faster than Grok 4.7 but not smarter, and costs about twice as much.'),
    ('claude-haiku-4-5', 'claude-haiku-5-5', 'Older Haiku release, Haiku 5.5 supersedes it once the CLI offers it and it is priced.')
) AS v (model, superseded_by, reason)
WHERE NOT EXISTS (SELECT 1 FROM platform_migrations WHERE version = '0005_platform_admin_config')
ON CONFLICT (model) DO NOTHING;

INSERT INTO platform_migrations (version) VALUES ('0005_platform_admin_config') ON CONFLICT DO NOTHING;
