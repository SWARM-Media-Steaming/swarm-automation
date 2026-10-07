//! The Postgres platform schema (issue #417).
//!
//! The SQL lives in `migrations/` so `psql -f` and any migration runner can apply
//! it; this module embeds it so the binary can too, and pins its shape in tests
//! that need no database. `issue_worker/test_storage_remote.py` applies the same
//! files to a live Postgres. Only the schema is here: the Postgres `Store` that
//! queries it is a later step, so `memory::MemoryStore` remains the store.

/// `(version, sql)` in the order they must be applied.
pub const MIGRATIONS: &[(&str, &str)] = &[
    (
        "0001_platform",
        include_str!("../migrations/0001_platform.sql"),
    ),
    (
        "0002_identity",
        include_str!("../migrations/0002_identity.sql"),
    ),
];

#[cfg(test)]
mod tests {
    use super::*;

    /// Splits on `;` outside `$$ ... $$` bodies (a DO block is one statement).
    fn statements(sql: &str) -> Vec<String> {
        let code: String = sql
            .lines()
            .filter(|line| !line.trim_start().starts_with("--"))
            .collect::<Vec<_>>()
            .join("\n");
        let mut out = Vec::new();
        let mut current = String::new();
        let mut in_body = false;
        let mut rest = code.as_str();
        while let Some(ch) = rest.chars().next() {
            if rest.starts_with("$$") {
                in_body = !in_body;
                current.push_str("$$");
                rest = &rest[2..];
                continue;
            }
            if ch == ';' && !in_body {
                out.push(std::mem::take(&mut current));
            } else {
                current.push(ch);
            }
            rest = &rest[ch.len_utf8()..];
        }
        out.push(current);
        out.into_iter()
            .map(|statement| statement.trim().to_string())
            .filter(|statement| !statement.is_empty())
            .collect()
    }

    #[test]
    fn migrations_are_ordered_and_name_their_own_version() {
        let versions: Vec<&str> = MIGRATIONS.iter().map(|(version, _)| *version).collect();
        let mut sorted = versions.clone();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(versions, sorted);
        for (version, sql) in MIGRATIONS {
            assert!(
                sql.contains(&format!("VALUES ('{version}')")),
                "{version} must record itself"
            );
        }
    }

    #[test]
    fn every_statement_is_safe_to_apply_twice() {
        for (version, sql) in MIGRATIONS {
            for statement in statements(sql) {
                let upper = statement.to_uppercase();
                let idempotent = upper.starts_with("CREATE TABLE IF NOT EXISTS")
                    || upper.starts_with("CREATE INDEX IF NOT EXISTS")
                    || (upper.starts_with("ALTER TABLE")
                        && upper.contains(" ADD COLUMN IF NOT EXISTS "))
                    || (upper.starts_with("DO $$") && upper.contains("IF EXISTS (SELECT")
                        || upper.starts_with("DO $$")
                            && upper.contains("IF to_regclass".to_uppercase().as_str()))
                    || (upper.starts_with("INSERT INTO") && upper.contains("ON CONFLICT"));
                assert!(idempotent, "{version}: not idempotent: {statement}");
            }
        }
    }

    #[test]
    fn tenant_scoped_tables_key_on_the_tenant_and_cascade_from_it() {
        let sql = MIGRATIONS[0].1;
        for table in [
            "tenant_memberships",
            "tenant_provider_keys",
            "tenant_plan_quotas",
            "tenant_budgets",
            "tenant_usage_ledger",
            "tenant_provider_reports",
            "tenant_jobs",
        ] {
            let statement = statements(sql)
                .into_iter()
                .find(|s| s.starts_with(&format!("CREATE TABLE IF NOT EXISTS {table} ")))
                .unwrap_or_else(|| panic!("{table} is missing"));
            let first_column = statement.lines().nth(1).unwrap_or_default().trim();
            assert!(
                first_column.starts_with("tenant_id"),
                "{table}: tenant_id must come first"
            );
            assert!(
                statement.contains("REFERENCES tenants (tenant_id) ON DELETE CASCADE"),
                "{table} must cascade from tenants"
            );
        }
    }

    #[test]
    fn it_covers_every_store_concern_and_holds_no_plaintext_secret() {
        let sql = MIGRATIONS[0].1;
        for table in [
            "web_users",
            "web_sessions",
            "tenants",
            "tenant_memberships",
            "tenant_provider_keys",
            "tenant_plan_quotas",
            "tenant_budgets",
            "tenant_usage_ledger",
            "tenant_provider_reports",
            "tenant_jobs",
            "webhook_deliveries",
        ] {
            assert!(
                sql.contains(&format!("CREATE TABLE IF NOT EXISTS {table} ")),
                "{table}"
            );
        }
        let keys = statements(sql)
            .into_iter()
            .find(|s| s.starts_with("CREATE TABLE IF NOT EXISTS tenant_provider_keys"))
            .unwrap();
        for column in ["wrapped_data_key  BYTEA", "ciphertext        BYTEA"] {
            assert!(keys.contains(column), "{column}");
        }
        for forbidden in ["plaintext", "api_key", "secret TEXT", "key TEXT"] {
            assert!(
                !keys.contains(forbidden),
                "provider keys must be stored sealed only: {forbidden}"
            );
        }
    }

    #[test]
    fn identity_migration_models_users_identities_ownership_and_audit() {
        let sql = MIGRATIONS[1].1;
        let all = statements(sql);
        let table = |name: &str| {
            all.iter()
                .find(|s| s.starts_with(&format!("CREATE TABLE IF NOT EXISTS {name} ")))
                .unwrap_or_else(|| panic!("{name} is missing"))
                .clone()
        };
        let identities = table("user_identities");
        assert!(identities.contains("REFERENCES users (id) ON DELETE CASCADE"));
        assert!(identities.contains("UNIQUE (provider, subject)"));
        let audit = table("admin_audit_log");
        assert_eq!(
            audit
                .matches("REFERENCES users (id) ON DELETE SET NULL")
                .count(),
            2
        );
        for column in [
            "display_name",
            "avatar_url",
            "email",
            "is_platform_admin BOOLEAN NOT NULL DEFAULT false",
            "last_login_at",
        ] {
            assert!(
                sql.contains(&format!("ADD COLUMN IF NOT EXISTS {column}")),
                "{column}"
            );
        }
        assert!(sql.contains("owner_user_id TEXT REFERENCES users (id)"));
        for index in [
            "user_identities_user_idx",
            "tenants_owner_idx",
            "admin_audit_log_actor_idx",
            "admin_audit_log_target_idx",
        ] {
            assert!(
                sql.contains(&format!("CREATE INDEX IF NOT EXISTS {index} ")),
                "{index}"
            );
        }
    }

    #[test]
    fn identity_migration_leaves_the_worker_schema_alone() {
        let sql = MIGRATIONS[1].1.to_lowercase();
        assert!(!sql.contains("schema_migrations"));
        assert!(!sql.contains("swarm_storage"));
    }
}
