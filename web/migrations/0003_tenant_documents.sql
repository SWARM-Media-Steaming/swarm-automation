-- Tenant documents of the hosted backend (issue #439, part of #413).
--
-- The `Store` trait's `document` / `put_document` / `documents` collection
-- (web/src/store.rs): small JSON documents a tenant owns, namely the
-- per-user preferences and the settings the web backend serves. Like every
-- tenant-scoped table the tenant id is the first column of the key and the
-- rows cascade from `tenants`. Worker-visible settings stay `tenant_config`
-- documents in object storage (issue_worker/storage.py); this table is the
-- web backend's own copy and holds no credential.
--
-- Idempotent (IF NOT EXISTS, ON CONFLICT): safe to apply on every start.

CREATE TABLE IF NOT EXISTS tenant_documents (
    tenant_id   TEXT NOT NULL REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    collection  TEXT NOT NULL CHECK (collection <> ''),
    doc_key     TEXT NOT NULL CHECK (doc_key <> ''),
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, collection, doc_key)
);

INSERT INTO platform_migrations (version) VALUES ('0003_tenant_documents') ON CONFLICT DO NOTHING;
