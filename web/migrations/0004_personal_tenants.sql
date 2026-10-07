-- Personal tenants (issue #440, part of #413).
--
-- A user's first sign-in registers a tenant of their own, named after their
-- login, with no GitHub App installation behind it. `tenants.installation_id`
-- therefore becomes nullable (UNIQUE still holds: it ignores NULLs), and a
-- partial unique index makes "one personal tenant per owner" a database fact,
-- so even two racing first sign-ins cannot leave a user two of them. Tenants
-- that an installation created keep their installation id.
--
-- Idempotent: the column change is guarded on the catalog and the index is
-- IF NOT EXISTS. Applying it twice, or on a database already in this shape,
-- changes nothing. The per-tenant worker schema is not touched.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = 'tenants'
                 AND column_name = 'installation_id' AND is_nullable = 'NO') THEN
        ALTER TABLE tenants ALTER COLUMN installation_id DROP NOT NULL;
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS tenants_personal_owner_idx
    ON tenants (owner_user_id) WHERE installation_id IS NULL;

INSERT INTO platform_migrations (version) VALUES ('0004_personal_tenants') ON CONFLICT DO NOTHING;
