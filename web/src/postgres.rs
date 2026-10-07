//! The Postgres [`Store`] (issue #439, part of #413).
//!
//! Queries the platform schema in `migrations/` and replaces
//! [`crate::memory::MemoryStore`] as the runtime store when
//! `SWARM_WEB_STORE=postgres`. Every tenant-scoped operation takes the tenant
//! first and puts `tenant_id` in its `WHERE` or `INSERT`; the tenant is only a
//! bound parameter, never part of the SQL text. Every driver failure is wrapped
//! in [`StoreError`] without the connection string or any row value.
//!
//! Migrations run at start (`connect`): each version once, in order, inside one
//! transaction that holds an advisory lock, so several starting processes (or a
//! rolling deploy) do not race.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use deadpool_postgres::{ManagerConfig, Pool, RecyclingMethod, Runtime};
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{DigitallySignedStruct, SignatureScheme};
use serde_json::Value;
use tokio_postgres::config::SslMode;
use tokio_postgres::types::ToSql;
use tokio_postgres::{Config, NoTls, Row};

use crate::crypto::{random_hex, SealedSecret};
use crate::model::*;
use crate::schema::MIGRATIONS;
use crate::store::{Store, StoreError, StoreResult};

/// Held while migrations apply. Arbitrary but fixed.
const MIGRATION_LOCK: i64 = 0x5357_4d57_4542;
const POOL_SIZE: usize = 8;
const POOL_WAIT: Duration = Duration::from_secs(10);

pub struct PostgresStore {
    pool: Pool,
}

fn fail(error: tokio_postgres::Error) -> StoreError {
    match error.as_db_error() {
        // The code and message only: `detail` can quote row values.
        Some(db) => StoreError(format!("postgres {}: {}", db.code().code(), db.message())),
        None => StoreError("postgres connection error".into()),
    }
}

fn pool_failure(error: deadpool_postgres::PoolError) -> StoreError {
    match error {
        deadpool_postgres::PoolError::Backend(error) => fail(error),
        _ => StoreError("postgres pool unavailable".into()),
    }
}

fn int(value: u64) -> StoreResult<i64> {
    i64::try_from(value).map_err(|_| StoreError("value out of range".into()))
}

fn unint(value: i64) -> u64 {
    u64::try_from(value).unwrap_or(0)
}

fn provider_of(name: &str) -> StoreResult<Provider> {
    Provider::parse(name).ok_or_else(|| StoreError("unknown provider in store".into()))
}

fn tenant_of(name: String) -> StoreResult<TenantId> {
    TenantId::parse(&name).ok_or_else(|| StoreError("invalid tenant id in store".into()))
}

fn tenant_from(row: &Row) -> StoreResult<Tenant> {
    let status = match row.get::<_, String>("status").as_str() {
        "active" => TenantStatus::Active,
        "suspended" => TenantStatus::Suspended,
        "deleted" => TenantStatus::Deleted,
        _ => return Err(StoreError("unknown tenant status in store".into())),
    };
    Ok(Tenant {
        id: tenant_of(row.get("tenant_id"))?,
        installation_id: row.get::<_, Option<i64>>("installation_id").map(unint),
        account_login: row.get("account_login"),
        account_type: row.get("account_type"),
        status,
    })
}

fn status_name(status: TenantStatus) -> &'static str {
    match status {
        TenantStatus::Active => "active",
        TenantStatus::Suspended => "suspended",
        TenantStatus::Deleted => "deleted",
    }
}

fn role_name(role: Role) -> &'static str {
    match role {
        Role::Owner => "owner",
        Role::Member => "member",
    }
}

fn role_of(name: &str) -> StoreResult<Role> {
    match name {
        "owner" => Ok(Role::Owner),
        "member" => Ok(Role::Member),
        _ => Err(StoreError("unknown role in store".into())),
    }
}

/// `sslmode=require` means encrypted, not authenticated: the same as libpq,
/// which is what the DSN Terraform writes asks for (RDS certificates are not
/// in a public root store).
#[derive(Debug)]
struct EncryptOnly(Arc<rustls::crypto::CryptoProvider>);

impl ServerCertVerifier for EncryptOnly {
    fn verify_server_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.0.signature_verification_algorithms.supported_schemes()
    }
}

fn tls_connector() -> StoreResult<tokio_postgres_rustls::MakeRustlsConnect> {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = rustls::ClientConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .map_err(|_| StoreError("tls setup failed".into()))?
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(EncryptOnly(provider)))
        .with_no_client_auth();
    Ok(tokio_postgres_rustls::MakeRustlsConnect::new(config))
}

impl PostgresStore {
    /// Connect, then apply any migration not yet recorded. The DSN is never
    /// echoed: a malformed one is reported without its text.
    pub async fn connect(dsn: &str) -> StoreResult<Self> {
        let config: Config = dsn
            .parse()
            .map_err(|_| StoreError("the Postgres connection string is not valid".into()))?;
        let manager_config = ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        };
        let builder = match config.get_ssl_mode() {
            SslMode::Disable => {
                let manager =
                    deadpool_postgres::Manager::from_config(config, NoTls, manager_config);
                Pool::builder(manager)
            }
            _ => {
                let manager = deadpool_postgres::Manager::from_config(
                    config,
                    tls_connector()?,
                    manager_config,
                );
                Pool::builder(manager)
            }
        };
        let pool = builder
            .max_size(POOL_SIZE)
            .wait_timeout(Some(POOL_WAIT))
            .create_timeout(Some(POOL_WAIT))
            .runtime(Runtime::Tokio1)
            .build()
            .map_err(|_| StoreError("postgres pool could not be built".into()))?;
        let store = PostgresStore { pool };
        store.migrate().await?;
        Ok(store)
    }

    /// Apply every migration whose version is not in `platform_migrations`.
    pub async fn migrate(&self) -> StoreResult<()> {
        let mut client = self.pool.get().await.map_err(pool_failure)?;
        let tx = client.transaction().await.map_err(fail)?;
        tx.execute("SELECT pg_advisory_xact_lock($1)", &[&MIGRATION_LOCK])
            .await
            .map_err(fail)?;
        // The same definition 0001 creates, so the first run can read it.
        tx.batch_execute(
            "CREATE TABLE IF NOT EXISTS platform_migrations (
                 version TEXT PRIMARY KEY,
                 applied_at TIMESTAMPTZ NOT NULL DEFAULT now())",
        )
        .await
        .map_err(fail)?;
        for (version, sql) in MIGRATIONS {
            let applied = tx
                .query_opt(
                    "SELECT 1 FROM platform_migrations WHERE version = $1",
                    &[version],
                )
                .await
                .map_err(fail)?;
            if applied.is_none() {
                tx.batch_execute(sql).await.map_err(fail)?;
                tracing::info!(version, "applied platform migration");
            }
        }
        tx.commit().await.map_err(fail)
    }

    async fn client(&self) -> StoreResult<deadpool_postgres::Object> {
        self.pool.get().await.map_err(pool_failure)
    }

    async fn query(&self, sql: &str, params: &[&(dyn ToSql + Sync)]) -> StoreResult<Vec<Row>> {
        self.client().await?.query(sql, params).await.map_err(fail)
    }

    async fn execute(&self, sql: &str, params: &[&(dyn ToSql + Sync)]) -> StoreResult<u64> {
        self.client()
            .await?
            .execute(sql, params)
            .await
            .map_err(fail)
    }
}

/// Advisory-lock key serialising every change to who is a platform admin.
const ADMIN_LOCK: &str = "swarm-web-platform-admins";

/// The columns [`user_from`] reads. `login` is the first identity's.
const USER_SELECT: &str = "SELECT u.id, u.display_name, u.avatar_url, u.is_platform_admin,
        (extract(epoch FROM u.last_login_at))::bigint AS at,
        (SELECT i.login FROM user_identities i
          WHERE i.user_id = u.id ORDER BY i.id LIMIT 1) AS login
    FROM users u";

/// `None` for a user with no identity (it has no handle to show and cannot sign in).
fn user_from(row: &Row) -> Option<User> {
    Some(User {
        id: row.get("id"),
        login: row.get::<_, Option<String>>("login")?,
        display_name: row.get("display_name"),
        avatar_url: row.get("avatar_url"),
        last_login_at: row.get::<_, Option<i64>>("at").map(unint),
        is_platform_admin: row.get("is_platform_admin"),
    })
}

async fn insert_audit(
    tx: &deadpool_postgres::Transaction<'_>,
    actor_id: &str,
    target_id: &str,
    action: &str,
    detail: Value,
) -> StoreResult<()> {
    tx.execute(
        "INSERT INTO admin_audit_log (actor_user_id, target_user_id, action, detail)
         VALUES ($1, $2, $3, $4)",
        &[&actor_id, &target_id, &action, &detail],
    )
    .await
    .map_err(fail)?;
    Ok(())
}

/// An audit row with no target user (platform configuration changes).
async fn insert_config_audit(
    tx: &deadpool_postgres::Transaction<'_>,
    actor_id: &str,
    action: &str,
    detail: Value,
) -> StoreResult<()> {
    tx.execute(
        "INSERT INTO admin_audit_log (actor_user_id, target_user_id, action, detail)
         VALUES ($1, NULL, $2, $3)",
        &[&actor_id, &action, &detail],
    )
    .await
    .map_err(fail)?;
    Ok(())
}

const TENANT_COLUMNS: &str = "tenant_id, installation_id, account_login, account_type, status";

#[async_trait]
impl Store for PostgresStore {
    async fn register_identity(&self, profile: &IdentityProfile) -> StoreResult<Registration> {
        let mut client = self.client().await?;
        // Dropping the transaction on any early return rolls it back.
        let tx = client.transaction().await.map_err(fail)?;
        // Serialise sign-ins of one identity so a first sign-in happens once and
        // a concurrent twin waits, then finds the committed rows.
        tx.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))",
            &[&format!(
                "swarm-web-identity:{}:{}",
                profile.provider, profile.subject
            )],
        )
        .await
        .map_err(fail)?;
        let existing = tx
            .query_opt(
                "SELECT user_id FROM user_identities WHERE provider = $1 AND subject = $2",
                &[&profile.provider, &profile.subject],
            )
            .await
            .map_err(fail)?;
        let first_sign_in = existing.is_none();
        let (user_id, last_login, is_admin) = match existing {
            Some(row) => {
                let id: String = row.get("user_id");
                tx.execute(
                    "UPDATE user_identities SET login = $3 WHERE provider = $1 AND subject = $2",
                    &[&profile.provider, &profile.subject, &profile.login],
                )
                .await
                .map_err(fail)?;
                let row = tx
                    .query_one(
                        "UPDATE users SET display_name = $2, avatar_url = $3, last_login_at = now()
                         WHERE id = $1 RETURNING (extract(epoch FROM last_login_at))::bigint AS at,
                                                 is_platform_admin",
                        &[&id, &profile.display_name, &profile.avatar_url],
                    )
                    .await
                    .map_err(fail)?;
                (
                    id,
                    row.get::<_, i64>("at"),
                    row.get::<_, bool>("is_platform_admin"),
                )
            }
            None => {
                let id = format!("u{}", random_hex(8));
                let row = tx
                    .query_one(
                        "INSERT INTO users (id, display_name, avatar_url, last_login_at)
                         VALUES ($1, $2, $3, now())
                         RETURNING (extract(epoch FROM last_login_at))::bigint AS at",
                        &[&id, &profile.display_name, &profile.avatar_url],
                    )
                    .await
                    .map_err(fail)?;
                (id, row.get::<_, i64>("at"), false)
            }
        };

        let personal = tx
            .query_opt(
                &format!(
                    "SELECT {TENANT_COLUMNS} FROM tenants
                     WHERE owner_user_id = $1 AND installation_id IS NULL"
                ),
                &[&user_id],
            )
            .await
            .map_err(fail)?;
        let tenant = match personal {
            Some(row) => {
                // The id never follows a rename; the displayed account does.
                let mut tenant = tenant_from(&row)?;
                tx.execute(
                    "UPDATE tenants SET account_login = $2 WHERE tenant_id = $1",
                    &[&tenant.id.as_str(), &profile.login],
                )
                .await
                .map_err(fail)?;
                tenant.account_login = profile.login.clone();
                tenant
            }
            None => {
                let mut created = None;
                for attempt in 1.. {
                    let id = TenantId::personal(&profile.login, attempt);
                    // A taken id (maybe taken by a transaction still in flight,
                    // which this waits on) inserts nothing: try the next one.
                    let inserted = tx
                        .execute(
                            "INSERT INTO tenants
                                 (tenant_id, installation_id, account_login, account_type, owner_user_id)
                             VALUES ($1, NULL, $2, $3, $4)
                             ON CONFLICT (tenant_id) DO NOTHING",
                            &[&id.as_str(), &profile.login, &PERSONAL_ACCOUNT_TYPE, &user_id],
                        )
                        .await
                        .map_err(fail)?;
                    if inserted == 1 {
                        created = Some(id);
                        break;
                    }
                }
                Tenant {
                    id: created.expect("the loop only exits with an id"),
                    installation_id: None,
                    account_login: profile.login.clone(),
                    account_type: PERSONAL_ACCOUNT_TYPE.to_string(),
                    status: TenantStatus::Active,
                }
            }
        };
        tx.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES ($1, $2, 'owner')
             ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = 'owner'",
            &[&tenant.id.as_str(), &user_id],
        )
        .await
        .map_err(fail)?;
        if first_sign_in {
            // Last, so the identity's own constraints can still undo the rest.
            tx.execute(
                "INSERT INTO user_identities (user_id, provider, subject, login)
                 VALUES ($1, $2, $3, $4)",
                &[
                    &user_id,
                    &profile.provider,
                    &profile.subject,
                    &profile.login,
                ],
            )
            .await
            .map_err(fail)?;
        }
        tx.commit().await.map_err(fail)?;
        Ok(Registration {
            user: User {
                id: user_id,
                login: profile.login.clone(),
                display_name: profile.display_name.clone(),
                avatar_url: profile.avatar_url.clone(),
                last_login_at: Some(unint(last_login)),
                is_platform_admin: is_admin,
            },
            tenant,
            first_sign_in,
        })
    }

    async fn user(&self, user_id: &str) -> StoreResult<Option<User>> {
        let rows = self
            .query(&format!("{USER_SELECT} WHERE u.id = $1"), &[&user_id])
            .await?;
        Ok(rows.first().and_then(user_from))
    }

    async fn identities_for_user(&self, user_id: &str) -> StoreResult<Vec<(String, String)>> {
        let rows = self
            .query(
                "SELECT provider, login FROM user_identities WHERE user_id = $1
                 ORDER BY provider, login",
                &[&user_id],
            )
            .await?;
        Ok(rows.iter().map(|r| (r.get(0), r.get(1))).collect())
    }

    async fn bootstrap_platform_admin(&self, user_id: &str) -> StoreResult<bool> {
        let mut client = self.client().await?;
        let tx = client.transaction().await.map_err(fail)?;
        // The same lock as `set_platform_admin`, so "no admin exists" cannot
        // change between the check and the update.
        tx.execute("SELECT pg_advisory_xact_lock(hashtext($1))", &[&ADMIN_LOCK])
            .await
            .map_err(fail)?;
        let admins: i64 = tx
            .query_one("SELECT count(*) FROM users WHERE is_platform_admin", &[])
            .await
            .map_err(fail)?
            .get(0);
        if admins > 0 {
            return Ok(false);
        }
        let promoted = tx
            .query_opt(
                "UPDATE users SET is_platform_admin = true WHERE id = $1
                 RETURNING (SELECT i.login FROM user_identities i
                             WHERE i.user_id = users.id ORDER BY i.id LIMIT 1) AS login",
                &[&user_id],
            )
            .await
            .map_err(fail)?;
        let Some(row) = promoted else {
            return Ok(false);
        };
        let login: Option<String> = row.get("login");
        insert_audit(
            &tx,
            user_id,
            user_id,
            AUDIT_ADMIN_BOOTSTRAP,
            serde_json::json!({ "target_login": login }),
        )
        .await?;
        tx.commit().await.map_err(fail)?;
        Ok(true)
    }

    async fn platform_users(&self) -> StoreResult<Vec<User>> {
        let rows = self
            .query(
                // `login` is an output column, which ORDER BY can only use
                // bare, so the select is wrapped to sort case-insensitively.
                &format!("SELECT * FROM ({USER_SELECT}) AS listed ORDER BY lower(login), id"),
                &[],
            )
            .await?;
        Ok(rows.iter().filter_map(user_from).collect())
    }

    async fn set_platform_admin(
        &self,
        actor_id: &str,
        target_id: &str,
        admin: bool,
    ) -> StoreResult<AdminChange> {
        let mut client = self.client().await?;
        let tx = client.transaction().await.map_err(fail)?;
        // Serialises every admin change: two demotions cannot both see "two
        // admins" and leave none.
        tx.execute("SELECT pg_advisory_xact_lock(hashtext($1))", &[&ADMIN_LOCK])
            .await
            .map_err(fail)?;
        let rows = tx
            .query(&format!("{USER_SELECT} WHERE u.id = $1"), &[&target_id])
            .await
            .map_err(fail)?;
        let Some(current) = rows.first().and_then(user_from) else {
            return Ok(AdminChange::UnknownUser);
        };
        if current.is_platform_admin == admin {
            return Ok(AdminChange::Unchanged(current));
        }
        if !admin {
            let admins: i64 = tx
                .query_one("SELECT count(*) FROM users WHERE is_platform_admin", &[])
                .await
                .map_err(fail)?
                .get(0);
            if admins <= 1 {
                return Ok(AdminChange::LastAdmin);
            }
        }
        tx.execute(
            "UPDATE users SET is_platform_admin = $2 WHERE id = $1",
            &[&target_id, &admin],
        )
        .await
        .map_err(fail)?;
        let action = if admin {
            AUDIT_ADMIN_PROMOTE
        } else {
            AUDIT_ADMIN_DEMOTE
        };
        insert_audit(
            &tx,
            actor_id,
            target_id,
            action,
            serde_json::json!({ "target_login": current.login }),
        )
        .await?;
        tx.commit().await.map_err(fail)?;
        Ok(AdminChange::Changed(User {
            is_platform_admin: admin,
            ..current
        }))
    }

    async fn admin_audit_log(&self, limit: usize) -> StoreResult<Vec<AuditEntry>> {
        let limit = int(limit as u64)?;
        let rows = self
            .query(
                "SELECT id, actor_user_id, target_user_id, action, detail,
                        (extract(epoch FROM created_at))::bigint AS at
                 FROM admin_audit_log ORDER BY id DESC LIMIT $1",
                &[&limit],
            )
            .await?;
        Ok(rows
            .iter()
            .map(|row| AuditEntry {
                id: row.get("id"),
                actor_user_id: row.get("actor_user_id"),
                target_user_id: row.get("target_user_id"),
                action: row.get("action"),
                detail: row.get("detail"),
                created_at: unint(row.get("at")),
            })
            .collect())
    }

    async fn create_session(&self, session: Session) -> StoreResult<()> {
        let expires = int(session.expires_at)?;
        self.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at)
             VALUES ($1, $2, $3, $4)
             ON CONFLICT (token_hash) DO UPDATE SET
                 user_id = EXCLUDED.user_id,
                 csrf_token = EXCLUDED.csrf_token,
                 expires_at = EXCLUDED.expires_at",
            &[
                &session.token_hash,
                &session.user_id,
                &session.csrf_token,
                &expires,
            ],
        )
        .await?;
        Ok(())
    }

    async fn session(&self, token_hash: &str) -> StoreResult<Option<Session>> {
        let rows = self
            .query(
                "SELECT token_hash, user_id, csrf_token, expires_at FROM sessions
                 WHERE token_hash = $1",
                &[&token_hash],
            )
            .await?;
        Ok(rows.first().map(|row| Session {
            token_hash: row.get("token_hash"),
            user_id: row.get("user_id"),
            csrf_token: row.get("csrf_token"),
            expires_at: unint(row.get("expires_at")),
        }))
    }

    async fn delete_session(&self, token_hash: &str) -> StoreResult<()> {
        self.execute("DELETE FROM sessions WHERE token_hash = $1", &[&token_hash])
            .await?;
        Ok(())
    }

    async fn delete_expired_sessions(&self, now: u64) -> StoreResult<usize> {
        let now = int(now)?;
        let removed = self
            .execute("DELETE FROM sessions WHERE expires_at <= $1", &[&now])
            .await?;
        Ok(removed as usize)
    }

    async fn upsert_installation_tenant(
        &self,
        installation_id: u64,
        account_login: &str,
        account_type: &str,
    ) -> StoreResult<Tenant> {
        let installation = int(installation_id)?;
        let id = format!("t{}", random_hex(8));
        let rows = self
            .query(
                &format!(
                    "INSERT INTO tenants (tenant_id, installation_id, account_login, account_type)
                     VALUES ($1, $2, $3, $4)
                     ON CONFLICT (installation_id) DO UPDATE SET
                         account_login = EXCLUDED.account_login,
                         account_type = EXCLUDED.account_type
                     RETURNING {TENANT_COLUMNS}"
                ),
                &[&id, &installation, &account_login, &account_type],
            )
            .await?;
        tenant_from(
            rows.first()
                .ok_or_else(|| StoreError("no tenant row".into()))?,
        )
    }

    async fn tenant(&self, tenant: &TenantId) -> StoreResult<Option<Tenant>> {
        let rows = self
            .query(
                &format!("SELECT {TENANT_COLUMNS} FROM tenants WHERE tenant_id = $1"),
                &[&tenant.as_str()],
            )
            .await?;
        rows.first().map(tenant_from).transpose()
    }

    async fn tenant_by_installation(&self, installation_id: u64) -> StoreResult<Option<Tenant>> {
        let installation = int(installation_id)?;
        let rows = self
            .query(
                &format!("SELECT {TENANT_COLUMNS} FROM tenants WHERE installation_id = $1"),
                &[&installation],
            )
            .await?;
        rows.first().map(tenant_from).transpose()
    }

    async fn set_tenant_status(&self, tenant: &TenantId, status: TenantStatus) -> StoreResult<()> {
        let changed = self
            .execute(
                "UPDATE tenants SET status = $2 WHERE tenant_id = $1",
                &[&tenant.as_str(), &status_name(status)],
            )
            .await?;
        if changed == 0 {
            return Err(StoreError(format!("unknown tenant {tenant}")));
        }
        Ok(())
    }

    async fn set_membership(
        &self,
        tenant: &TenantId,
        user_id: &str,
        role: Role,
    ) -> StoreResult<()> {
        self.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES ($1, $2, $3)
             ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = EXCLUDED.role",
            &[&tenant.as_str(), &user_id, &role_name(role)],
        )
        .await?;
        Ok(())
    }

    async fn retain_memberships(&self, user_id: &str, keep: &[TenantId]) -> StoreResult<()> {
        let keep: Vec<&str> = keep.iter().map(TenantId::as_str).collect();
        self.execute(
            "DELETE FROM tenant_memberships WHERE user_id = $1 AND NOT (tenant_id = ANY($2))",
            &[&user_id, &keep],
        )
        .await?;
        Ok(())
    }

    async fn tenants_for_user(&self, user_id: &str) -> StoreResult<Vec<(Tenant, Role)>> {
        let rows = self
            .query(
                "SELECT t.tenant_id, t.installation_id, t.account_login, t.account_type,
                        t.status, m.role
                 FROM tenant_memberships m JOIN tenants t ON t.tenant_id = m.tenant_id
                 WHERE m.user_id = $1
                 ORDER BY t.tenant_id COLLATE \"C\"",
                &[&user_id],
            )
            .await?;
        rows.iter()
            .map(|row| Ok((tenant_from(row)?, role_of(&row.get::<_, String>("role"))?)))
            .collect()
    }

    async fn role_in(&self, tenant: &TenantId, user_id: &str) -> StoreResult<Option<Role>> {
        let rows = self
            .query(
                "SELECT role FROM tenant_memberships WHERE tenant_id = $1 AND user_id = $2",
                &[&tenant.as_str(), &user_id],
            )
            .await?;
        rows.first()
            .map(|row| role_of(&row.get::<_, String>("role")))
            .transpose()
    }

    async fn members(&self, tenant: &TenantId) -> StoreResult<Vec<Member>> {
        let rows = self
            .query(
                "SELECT i.login, m.role
                 FROM tenant_memberships m
                 JOIN LATERAL (SELECT login FROM user_identities
                                WHERE user_id = m.user_id ORDER BY id LIMIT 1) i ON true
                 WHERE m.tenant_id = $1
                 ORDER BY i.login COLLATE \"C\"",
                &[&tenant.as_str()],
            )
            .await?;
        rows.iter()
            .map(|row| {
                Ok(Member {
                    login: row.get("login"),
                    role: role_of(&row.get::<_, String>("role"))?,
                })
            })
            .collect()
    }

    async fn model_blacklist(&self) -> StoreResult<Vec<BlacklistEntry>> {
        let rows = self
            .query(
                "SELECT model, superseded_by, reason, updated_at, updated_by
                 FROM model_blacklist ORDER BY model",
                &[],
            )
            .await?;
        Ok(rows
            .iter()
            .map(|row| BlacklistEntry {
                model: row.get("model"),
                superseded_by: row.get("superseded_by"),
                reason: row.get("reason"),
                updated_at: unint(row.get("updated_at")),
                updated_by: row.get("updated_by"),
            })
            .collect())
    }

    async fn put_blacklist_entry(
        &self,
        actor_id: &str,
        entry: BlacklistEntry,
    ) -> StoreResult<bool> {
        let updated = int(entry.updated_at)?;
        let mut client = self.client().await?;
        let tx = client.transaction().await.map_err(fail)?;
        // `xmax = 0` is true only for a freshly inserted row.
        let rows = tx
            .query(
                "INSERT INTO model_blacklist (model, superseded_by, reason, updated_at, updated_by)
                 VALUES ($1, $2, $3, $4, $5)
                 ON CONFLICT (model) DO UPDATE SET
                     superseded_by = EXCLUDED.superseded_by,
                     reason = EXCLUDED.reason,
                     updated_at = EXCLUDED.updated_at,
                     updated_by = EXCLUDED.updated_by
                 RETURNING (xmax = 0) AS created",
                &[
                    &entry.model,
                    &entry.superseded_by,
                    &entry.reason,
                    &updated,
                    &entry.updated_by,
                ],
            )
            .await
            .map_err(fail)?;
        let created: bool = rows.first().map(|row| row.get("created")).unwrap_or(false);
        insert_config_audit(
            &tx,
            actor_id,
            AUDIT_BLACKLIST_SET,
            serde_json::json!({ "model": entry.model, "superseded_by": entry.superseded_by }),
        )
        .await?;
        tx.commit().await.map_err(fail)?;
        Ok(created)
    }

    async fn delete_blacklist_entry(&self, actor_id: &str, model: &str) -> StoreResult<bool> {
        let mut client = self.client().await?;
        let tx = client.transaction().await.map_err(fail)?;
        let removed = tx
            .execute("DELETE FROM model_blacklist WHERE model = $1", &[&model])
            .await
            .map_err(fail)?
            > 0;
        if removed {
            insert_config_audit(
                &tx,
                actor_id,
                AUDIT_BLACKLIST_REMOVE,
                serde_json::json!({ "model": model }),
            )
            .await?;
        }
        tx.commit().await.map_err(fail)?;
        Ok(removed)
    }

    async fn put_platform_key(
        &self,
        actor_id: &str,
        purpose: KeyPurpose,
        provider: Provider,
        sealed: SealedSecret,
        updated_by: &str,
        updated_at: u64,
    ) -> StoreResult<()> {
        let updated = int(updated_at)?;
        let version = i16::from(sealed.version);
        let mut client = self.client().await?;
        let tx = client.transaction().await.map_err(fail)?;
        tx.execute(
            "INSERT INTO platform_provider_keys
                 (purpose, provider, format_version, wrapper_key_id, wrapped_data_key,
                  ciphertext, updated_at, updated_by)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
             ON CONFLICT (purpose, provider) DO UPDATE SET
                 format_version = EXCLUDED.format_version,
                 wrapper_key_id = EXCLUDED.wrapper_key_id,
                 wrapped_data_key = EXCLUDED.wrapped_data_key,
                 ciphertext = EXCLUDED.ciphertext,
                 updated_at = EXCLUDED.updated_at,
                 updated_by = EXCLUDED.updated_by",
            &[
                &purpose.as_str(),
                &provider.as_str(),
                &version,
                &sealed.wrapper_key_id,
                &sealed.wrapped_data_key,
                &sealed.ciphertext,
                &updated,
                &updated_by,
            ],
        )
        .await
        .map_err(fail)?;
        insert_config_audit(
            &tx,
            actor_id,
            AUDIT_PLATFORM_KEY_SET,
            serde_json::json!({ "purpose": purpose.as_str(), "provider": provider.as_str() }),
        )
        .await?;
        tx.commit().await.map_err(fail)?;
        Ok(())
    }

    async fn platform_key(
        &self,
        purpose: KeyPurpose,
        provider: Provider,
    ) -> StoreResult<Option<StoredKey>> {
        let rows = self
            .query(
                "SELECT format_version, wrapper_key_id, wrapped_data_key, ciphertext,
                        updated_at, updated_by
                 FROM platform_provider_keys WHERE purpose = $1 AND provider = $2",
                &[&purpose.as_str(), &provider.as_str()],
            )
            .await?;
        Ok(rows.first().map(|row| StoredKey {
            sealed: SealedSecret {
                version: u8::try_from(row.get::<_, i16>("format_version")).unwrap_or(0),
                wrapper_key_id: row.get("wrapper_key_id"),
                wrapped_data_key: row.get("wrapped_data_key"),
                ciphertext: row.get("ciphertext"),
            },
            updated_at: unint(row.get("updated_at")),
            updated_by: row.get("updated_by"),
        }))
    }

    async fn delete_platform_key(
        &self,
        actor_id: &str,
        purpose: KeyPurpose,
        provider: Provider,
    ) -> StoreResult<bool> {
        let mut client = self.client().await?;
        let tx = client.transaction().await.map_err(fail)?;
        let removed = tx
            .execute(
                "DELETE FROM platform_provider_keys WHERE purpose = $1 AND provider = $2",
                &[&purpose.as_str(), &provider.as_str()],
            )
            .await
            .map_err(fail)?
            > 0;
        if removed {
            insert_config_audit(
                &tx,
                actor_id,
                AUDIT_PLATFORM_KEY_REMOVE,
                serde_json::json!({ "purpose": purpose.as_str(), "provider": provider.as_str() }),
            )
            .await?;
        }
        tx.commit().await.map_err(fail)?;
        Ok(removed)
    }

    async fn platform_key_meta(&self) -> StoreResult<Vec<PlatformKeyMeta>> {
        let rows = self
            .query(
                "SELECT purpose, provider, updated_at, updated_by FROM platform_provider_keys",
                &[],
            )
            .await?;
        let mut stored = std::collections::BTreeMap::new();
        for row in &rows {
            let purpose = KeyPurpose::parse(&row.get::<_, String>("purpose"))
                .ok_or_else(|| StoreError("unknown key purpose in store".into()))?;
            stored.insert(
                (purpose, provider_of(&row.get::<_, String>("provider"))?),
                (
                    unint(row.get("updated_at")),
                    row.get::<_, String>("updated_by"),
                ),
            );
        }
        Ok(KeyPurpose::ALL
            .iter()
            .flat_map(|purpose| {
                Provider::ALL
                    .iter()
                    .map(move |provider| (*purpose, *provider))
            })
            .map(|(purpose, provider)| {
                let entry = stored.get(&(purpose, provider));
                PlatformKeyMeta {
                    purpose,
                    provider,
                    configured: entry.is_some(),
                    updated_at: entry.map(|(at, _)| *at),
                    updated_by: entry.map(|(_, by)| by.clone()),
                }
            })
            .collect())
    }

    async fn put_provider_key(
        &self,
        tenant: &TenantId,
        provider: Provider,
        sealed: SealedSecret,
        updated_by: &str,
        updated_at: u64,
    ) -> StoreResult<()> {
        let updated = int(updated_at)?;
        let version = i16::from(sealed.version);
        self.execute(
            "INSERT INTO tenant_provider_keys
                 (tenant_id, provider, format_version, wrapper_key_id, wrapped_data_key,
                  ciphertext, updated_at, updated_by)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
             ON CONFLICT (tenant_id, provider) DO UPDATE SET
                 format_version = EXCLUDED.format_version,
                 wrapper_key_id = EXCLUDED.wrapper_key_id,
                 wrapped_data_key = EXCLUDED.wrapped_data_key,
                 ciphertext = EXCLUDED.ciphertext,
                 updated_at = EXCLUDED.updated_at,
                 updated_by = EXCLUDED.updated_by",
            &[
                &tenant.as_str(),
                &provider.as_str(),
                &version,
                &sealed.wrapper_key_id,
                &sealed.wrapped_data_key,
                &sealed.ciphertext,
                &updated,
                &updated_by,
            ],
        )
        .await?;
        Ok(())
    }

    async fn provider_key(
        &self,
        tenant: &TenantId,
        provider: Provider,
    ) -> StoreResult<Option<StoredKey>> {
        let rows = self
            .query(
                "SELECT format_version, wrapper_key_id, wrapped_data_key, ciphertext,
                        updated_at, updated_by
                 FROM tenant_provider_keys WHERE tenant_id = $1 AND provider = $2",
                &[&tenant.as_str(), &provider.as_str()],
            )
            .await?;
        Ok(rows.first().map(|row| StoredKey {
            sealed: SealedSecret {
                version: u8::try_from(row.get::<_, i16>("format_version")).unwrap_or(0),
                wrapper_key_id: row.get("wrapper_key_id"),
                wrapped_data_key: row.get("wrapped_data_key"),
                ciphertext: row.get("ciphertext"),
            },
            updated_at: unint(row.get("updated_at")),
            updated_by: row.get("updated_by"),
        }))
    }

    async fn delete_provider_key(
        &self,
        tenant: &TenantId,
        provider: Provider,
    ) -> StoreResult<bool> {
        Ok(self
            .execute(
                "DELETE FROM tenant_provider_keys WHERE tenant_id = $1 AND provider = $2",
                &[&tenant.as_str(), &provider.as_str()],
            )
            .await?
            > 0)
    }

    async fn provider_key_meta(&self, tenant: &TenantId) -> StoreResult<Vec<KeyMeta>> {
        let rows = self
            .query(
                "SELECT provider, updated_at, updated_by FROM tenant_provider_keys
                 WHERE tenant_id = $1",
                &[&tenant.as_str()],
            )
            .await?;
        let mut stored = std::collections::BTreeMap::new();
        for row in &rows {
            stored.insert(
                provider_of(&row.get::<_, String>("provider"))?,
                (
                    unint(row.get("updated_at")),
                    row.get::<_, String>("updated_by"),
                ),
            );
        }
        Ok(Provider::ALL
            .iter()
            .map(|provider| {
                let entry = stored.get(provider);
                KeyMeta {
                    provider: *provider,
                    configured: entry.is_some(),
                    updated_at: entry.map(|(at, _)| *at),
                    updated_by: entry.map(|(_, by)| by.clone()),
                }
            })
            .collect())
    }

    async fn plan_quotas(&self, tenant: &TenantId) -> StoreResult<Option<PlanQuotas>> {
        let rows = self
            .query(
                "SELECT max_concurrent_jobs, monthly_spend_cap_usd FROM tenant_plan_quotas
                 WHERE tenant_id = $1",
                &[&tenant.as_str()],
            )
            .await?;
        Ok(rows.first().map(|row| PlanQuotas {
            max_concurrent_jobs: u32::try_from(row.get::<_, i32>("max_concurrent_jobs"))
                .unwrap_or(0),
            monthly_spend_cap_usd: row.get("monthly_spend_cap_usd"),
        }))
    }

    async fn set_plan_quotas(&self, tenant: &TenantId, plan: PlanQuotas) -> StoreResult<()> {
        let jobs = i32::try_from(plan.max_concurrent_jobs).unwrap_or(i32::MAX);
        self.execute(
            "INSERT INTO tenant_plan_quotas (tenant_id, max_concurrent_jobs, monthly_spend_cap_usd)
             VALUES ($1, $2, $3)
             ON CONFLICT (tenant_id) DO UPDATE SET
                 max_concurrent_jobs = EXCLUDED.max_concurrent_jobs,
                 monthly_spend_cap_usd = EXCLUDED.monthly_spend_cap_usd",
            &[&tenant.as_str(), &jobs, &plan.monthly_spend_cap_usd],
        )
        .await?;
        Ok(())
    }

    async fn budgets(&self, tenant: &TenantId) -> StoreResult<Option<Budgets>> {
        let rows = self
            .query(
                "SELECT minimum_remaining_percent, provider_budgets_usd FROM tenant_budgets
                 WHERE tenant_id = $1",
                &[&tenant.as_str()],
            )
            .await?;
        rows.first()
            .map(|row| {
                let provider_budgets_usd =
                    serde_json::from_value(row.get::<_, Value>("provider_budgets_usd"))
                        .map_err(|_| StoreError("malformed provider budgets in store".into()))?;
                Ok(Budgets {
                    minimum_remaining_percent: row.get("minimum_remaining_percent"),
                    provider_budgets_usd,
                })
            })
            .transpose()
    }

    async fn set_budgets(&self, tenant: &TenantId, budgets: Budgets) -> StoreResult<()> {
        let per_provider = serde_json::to_value(&budgets.provider_budgets_usd)
            .map_err(|_| StoreError("budgets could not be encoded".into()))?;
        self.execute(
            "INSERT INTO tenant_budgets (tenant_id, minimum_remaining_percent, provider_budgets_usd)
             VALUES ($1, $2, $3)
             ON CONFLICT (tenant_id) DO UPDATE SET
                 minimum_remaining_percent = EXCLUDED.minimum_remaining_percent,
                 provider_budgets_usd = EXCLUDED.provider_budgets_usd",
            &[
                &tenant.as_str(),
                &budgets.minimum_remaining_percent,
                &per_provider,
            ],
        )
        .await?;
        Ok(())
    }

    async fn record_usage(
        &self,
        tenant: &TenantId,
        period: &str,
        entries: &[LedgerEntry],
    ) -> StoreResult<usize> {
        let mut client = self.client().await?;
        let tx = client.transaction().await.map_err(fail)?;
        let mut added = 0;
        for entry in entries {
            added += tx
                .execute(
                    "INSERT INTO tenant_usage_ledger
                         (tenant_id, entry_id, period, provider, cost_usd)
                     VALUES ($1, $2, $3, $4, $5)
                     ON CONFLICT (tenant_id, entry_id) DO NOTHING",
                    &[
                        &tenant.as_str(),
                        &entry.id,
                        &period,
                        &entry.provider.as_str(),
                        &entry.cost_usd,
                    ],
                )
                .await
                .map_err(fail)? as usize;
        }
        tx.commit().await.map_err(fail)?;
        Ok(added)
    }

    async fn spend(&self, tenant: &TenantId, period: &str) -> StoreResult<Spend> {
        let rows = self
            .query(
                "SELECT provider,
                        COALESCE(SUM(cost_usd), 0)::float8 AS spend_usd,
                        COUNT(cost_usd) AS priced,
                        COUNT(*) - COUNT(cost_usd) AS unpriced
                 FROM tenant_usage_ledger
                 WHERE tenant_id = $1 AND period = $2
                 GROUP BY provider",
                &[&tenant.as_str(), &period],
            )
            .await?;
        let mut spend = Spend::default();
        for row in &rows {
            spend.providers.insert(
                provider_of(&row.get::<_, String>("provider"))?,
                ProviderSpend {
                    spend_usd: row.get("spend_usd"),
                    priced_invocations: unint(row.get("priced")),
                    unpriced_invocations: unint(row.get("unpriced")),
                },
            );
        }
        Ok(spend)
    }

    async fn set_provider_report(
        &self,
        tenant: &TenantId,
        provider: Provider,
        report: ProviderReport,
    ) -> StoreResult<()> {
        let reported = int(report.reported_at)?;
        self.execute(
            "INSERT INTO tenant_provider_reports
                 (tenant_id, provider, remaining_percent, detail, reported_at)
             VALUES ($1, $2, $3, $4, $5)
             ON CONFLICT (tenant_id, provider) DO UPDATE SET
                 remaining_percent = EXCLUDED.remaining_percent,
                 detail = EXCLUDED.detail,
                 reported_at = EXCLUDED.reported_at",
            &[
                &tenant.as_str(),
                &provider.as_str(),
                &report.remaining_percent,
                &report.detail,
                &reported,
            ],
        )
        .await?;
        Ok(())
    }

    async fn provider_report(
        &self,
        tenant: &TenantId,
        provider: Provider,
    ) -> StoreResult<Option<ProviderReport>> {
        let rows = self
            .query(
                "SELECT remaining_percent, detail, reported_at FROM tenant_provider_reports
                 WHERE tenant_id = $1 AND provider = $2",
                &[&tenant.as_str(), &provider.as_str()],
            )
            .await?;
        Ok(rows.first().map(|row| ProviderReport {
            remaining_percent: row.get("remaining_percent"),
            detail: row.get("detail"),
            reported_at: unint(row.get("reported_at")),
        }))
    }

    async fn reserve_job(
        &self,
        tenant: &TenantId,
        job_id: &str,
        provider: Provider,
        limit: u32,
    ) -> StoreResult<bool> {
        let mut client = self.client().await?;
        let tx = client.transaction().await.map_err(fail)?;
        // Serialise reservations of one tenant so the limit cannot be raced past.
        let locked = tx
            .query_opt(
                "SELECT 1 FROM tenants WHERE tenant_id = $1 FOR UPDATE",
                &[&tenant.as_str()],
            )
            .await
            .map_err(fail)?;
        if locked.is_none() {
            return Err(StoreError(format!("unknown tenant {tenant}")));
        }
        let held = tx
            .query_opt(
                "SELECT 1 FROM tenant_jobs
                 WHERE tenant_id = $1 AND job_id = $2 AND status = 'active'",
                &[&tenant.as_str(), &job_id],
            )
            .await
            .map_err(fail)?;
        if held.is_some() {
            tx.commit().await.map_err(fail)?;
            return Ok(true);
        }
        let active: i64 = tx
            .query_one(
                "SELECT COUNT(*) FROM tenant_jobs WHERE tenant_id = $1 AND status = 'active'",
                &[&tenant.as_str()],
            )
            .await
            .map_err(fail)?
            .get(0);
        if active >= i64::from(limit) {
            tx.commit().await.map_err(fail)?;
            return Ok(false);
        }
        tx.execute(
            "INSERT INTO tenant_jobs (tenant_id, job_id, provider) VALUES ($1, $2, $3)
             ON CONFLICT (tenant_id, job_id) DO UPDATE SET
                 provider = EXCLUDED.provider, status = 'active', released_at = NULL",
            &[&tenant.as_str(), &job_id, &provider.as_str()],
        )
        .await
        .map_err(fail)?;
        tx.commit().await.map_err(fail)?;
        Ok(true)
    }

    async fn release_job(&self, tenant: &TenantId, job_id: &str) -> StoreResult<bool> {
        Ok(self
            .execute(
                "UPDATE tenant_jobs SET status = 'released', released_at = now()
                 WHERE tenant_id = $1 AND job_id = $2 AND status = 'active'",
                &[&tenant.as_str(), &job_id],
            )
            .await?
            > 0)
    }

    async fn active_jobs(&self, tenant: &TenantId) -> StoreResult<usize> {
        let rows = self
            .query(
                "SELECT COUNT(*) FROM tenant_jobs WHERE tenant_id = $1 AND status = 'active'",
                &[&tenant.as_str()],
            )
            .await?;
        Ok(rows.first().map_or(0, |row| row.get::<_, i64>(0) as usize))
    }

    async fn document(
        &self,
        tenant: &TenantId,
        collection: &str,
        key: &str,
    ) -> StoreResult<Option<Value>> {
        let rows = self
            .query(
                "SELECT value FROM tenant_documents
                 WHERE tenant_id = $1 AND collection = $2 AND doc_key = $3",
                &[&tenant.as_str(), &collection, &key],
            )
            .await?;
        Ok(rows.first().map(|row| row.get("value")))
    }

    async fn put_document(
        &self,
        tenant: &TenantId,
        collection: &str,
        key: &str,
        value: Value,
    ) -> StoreResult<()> {
        self.execute(
            "INSERT INTO tenant_documents (tenant_id, collection, doc_key, value)
             VALUES ($1, $2, $3, $4)
             ON CONFLICT (tenant_id, collection, doc_key) DO UPDATE SET
                 value = EXCLUDED.value, updated_at = now()",
            &[&tenant.as_str(), &collection, &key, &value],
        )
        .await?;
        Ok(())
    }

    async fn delete_document(
        &self,
        tenant: &TenantId,
        collection: &str,
        key: &str,
    ) -> StoreResult<bool> {
        Ok(self
            .execute(
                "DELETE FROM tenant_documents
                 WHERE tenant_id = $1 AND collection = $2 AND doc_key = $3",
                &[&tenant.as_str(), &collection, &key],
            )
            .await?
            > 0)
    }

    async fn documents(
        &self,
        tenant: &TenantId,
        collection: &str,
    ) -> StoreResult<Vec<(String, Value)>> {
        let rows = self
            .query(
                "SELECT doc_key, value FROM tenant_documents
                 WHERE tenant_id = $1 AND collection = $2
                 ORDER BY doc_key COLLATE \"C\"",
                &[&tenant.as_str(), &collection],
            )
            .await?;
        Ok(rows
            .iter()
            .map(|row| (row.get("doc_key"), row.get("value")))
            .collect())
    }

    async fn claim_delivery(
        &self,
        delivery_id: &str,
        payload_sha256: &str,
    ) -> StoreResult<DeliveryClaim> {
        let inserted = self
            .execute(
                "INSERT INTO webhook_deliveries (delivery_id, payload_sha256) VALUES ($1, $2)
                 ON CONFLICT DO NOTHING",
                &[&delivery_id, &payload_sha256],
            )
            .await?;
        if inserted > 0 {
            return Ok(DeliveryClaim::New);
        }
        let same_id = self
            .query(
                "SELECT 1 FROM webhook_deliveries WHERE delivery_id = $1",
                &[&delivery_id],
            )
            .await?;
        Ok(if same_id.is_empty() {
            DeliveryClaim::Replay
        } else {
            DeliveryClaim::Duplicate
        })
    }

    async fn release_delivery(&self, delivery_id: &str) -> StoreResult<()> {
        self.execute(
            "DELETE FROM webhook_deliveries WHERE delivery_id = $1",
            &[&delivery_id],
        )
        .await?;
        Ok(())
    }
}
