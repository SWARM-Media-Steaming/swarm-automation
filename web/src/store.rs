//! The persistence seam of the web backend.
//!
//! Production is Postgres behind this trait (with S3-compatible object storage
//! for job artifacts through the worker's `Storage` interface); development and
//! the test suite use [`crate::memory::MemoryStore`]. Mirroring
//! `issue_worker/storage.py`, **every tenant-scoped operation takes the tenant
//! first** and can only address that tenant's rows: there is no method that
//! reads or writes tenant data without one. Implementations wrap their backend
//! errors in [`StoreError`].

use std::fmt;

use async_trait::async_trait;

use serde_json::Value;

use crate::crypto::SealedSecret;
use crate::model::*;

#[derive(Debug)]
pub struct StoreError(pub String);

impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "storage error: {}", self.0)
    }
}

impl std::error::Error for StoreError {}

pub type StoreResult<T> = Result<T, StoreError>;

#[async_trait]
pub trait Store: Send + Sync {
    // ---- identity and sessions (not tenant data) -----------------------
    /// Sign in (or register) an identity, in one transaction. A known
    /// (provider, subject) refreshes login, display name, avatar and
    /// `last_login_at` and keeps its tenant id even when the login was renamed.
    /// An unknown one creates the user, the identity, a personal tenant whose id
    /// is the slugified login (deduplicated on collision) and the Owner
    /// membership, or creates none of them. Concurrent first sign-ins of one
    /// identity yield one user and one tenant.
    async fn register_identity(&self, profile: &IdentityProfile) -> StoreResult<Registration>;
    async fn user(&self, user_id: &str) -> StoreResult<Option<User>>;
    async fn create_session(&self, session: Session) -> StoreResult<()>;
    async fn session(&self, token_hash: &str) -> StoreResult<Option<Session>>;
    async fn delete_session(&self, token_hash: &str) -> StoreResult<()>;
    async fn delete_expired_sessions(&self, now: u64) -> StoreResult<usize>;

    // ---- platform administrators (platform-wide, not tenant data) -------
    /// The first-admin bootstrap: make `user_id` a platform admin when, and
    /// only when, no admin exists, in one atomic step that also writes the
    /// `admin.bootstrap` audit row. `false` (and no change) when an admin
    /// already exists (the user may be that admin), or the user is unknown.
    async fn bootstrap_platform_admin(&self, user_id: &str) -> StoreResult<bool>;
    /// How `user_id` signs in: provider and handle only (never the subject,
    /// which is the provider's internal id), ordered by provider then login.
    async fn identities_for_user(&self, user_id: &str) -> StoreResult<Vec<(String, String)>>;
    /// Every user, by login. Only the `Admin` extractor's routes call it.
    async fn platform_users(&self) -> StoreResult<Vec<User>>;
    /// Promote or demote `target_id` on behalf of `actor_id`, writing an audit
    /// row for a real change in the same step. Demoting the last admin is
    /// refused (`LastAdmin`), also when two demotions race. Whether the actor
    /// may do this is the caller's decision (`Admin`).
    async fn set_platform_admin(
        &self,
        actor_id: &str,
        target_id: &str,
        admin: bool,
    ) -> StoreResult<AdminChange>;
    /// The newest audit rows first, at most `limit`.
    async fn admin_audit_log(&self, limit: usize) -> StoreResult<Vec<AuditEntry>>;

    // ---- tenants and membership ---------------------------------------
    /// One tenant per GitHub App installation; creating is idempotent.
    async fn upsert_installation_tenant(
        &self,
        installation_id: u64,
        account_login: &str,
        account_type: &str,
    ) -> StoreResult<Tenant>;
    async fn tenant(&self, tenant: &TenantId) -> StoreResult<Option<Tenant>>;
    async fn tenant_by_installation(&self, installation_id: u64) -> StoreResult<Option<Tenant>>;
    async fn set_tenant_status(&self, tenant: &TenantId, status: TenantStatus) -> StoreResult<()>;
    /// Set the user's role on the tenant (GitHub is the source of truth).
    async fn set_membership(&self, tenant: &TenantId, user_id: &str, role: Role)
        -> StoreResult<()>;
    /// Drop the user's memberships in every tenant not listed in `keep`.
    async fn retain_memberships(&self, user_id: &str, keep: &[TenantId]) -> StoreResult<()>;
    async fn tenants_for_user(&self, user_id: &str) -> StoreResult<Vec<(Tenant, Role)>>;
    async fn role_in(&self, tenant: &TenantId, user_id: &str) -> StoreResult<Option<Role>>;
    async fn members(&self, tenant: &TenantId) -> StoreResult<Vec<Member>>;

    // ---- provider keys (sealed; the store never sees plaintext) ---------
    async fn put_provider_key(
        &self,
        tenant: &TenantId,
        provider: Provider,
        sealed: SealedSecret,
        updated_by: &str,
        updated_at: u64,
    ) -> StoreResult<()>;
    async fn provider_key(
        &self,
        tenant: &TenantId,
        provider: Provider,
    ) -> StoreResult<Option<StoredKey>>;
    async fn delete_provider_key(&self, tenant: &TenantId, provider: Provider)
        -> StoreResult<bool>;
    async fn provider_key_meta(&self, tenant: &TenantId) -> StoreResult<Vec<KeyMeta>>;

    // ---- quotas, budgets, usage and job slots ---------------------------
    async fn plan_quotas(&self, tenant: &TenantId) -> StoreResult<Option<PlanQuotas>>;
    async fn set_plan_quotas(&self, tenant: &TenantId, plan: PlanQuotas) -> StoreResult<()>;
    async fn budgets(&self, tenant: &TenantId) -> StoreResult<Option<Budgets>>;
    async fn set_budgets(&self, tenant: &TenantId, budgets: Budgets) -> StoreResult<()>;
    /// Record entries under a `YYYY-MM` period. Idempotent per entry id across
    /// the tenant (re-recording a batch changes nothing). Returns how many were new.
    async fn record_usage(
        &self,
        tenant: &TenantId,
        period: &str,
        entries: &[LedgerEntry],
    ) -> StoreResult<usize>;
    async fn spend(&self, tenant: &TenantId, period: &str) -> StoreResult<Spend>;
    async fn set_provider_report(
        &self,
        tenant: &TenantId,
        provider: Provider,
        report: ProviderReport,
    ) -> StoreResult<()>;
    async fn provider_report(
        &self,
        tenant: &TenantId,
        provider: Provider,
    ) -> StoreResult<Option<ProviderReport>>;
    /// Atomically take a concurrent-job slot. Reserving a job id that already
    /// holds one succeeds without taking another. `false` means at the limit.
    async fn reserve_job(
        &self,
        tenant: &TenantId,
        job_id: &str,
        provider: Provider,
        limit: u32,
    ) -> StoreResult<bool>;
    async fn release_job(&self, tenant: &TenantId, job_id: &str) -> StoreResult<bool>;
    async fn active_jobs(&self, tenant: &TenantId) -> StoreResult<usize>;

    // ---- tenant documents (settings and per-user preferences) -----------
    /// One JSON document of a tenant's collection (`tenant_config` in the
    /// worker's `Storage`: key `app`, and `repo-<id>` per repository).
    async fn document(
        &self,
        tenant: &TenantId,
        collection: &str,
        key: &str,
    ) -> StoreResult<Option<Value>>;
    /// Replace the document atomically.
    async fn put_document(
        &self,
        tenant: &TenantId,
        collection: &str,
        key: &str,
        value: Value,
    ) -> StoreResult<()>;
    async fn delete_document(
        &self,
        tenant: &TenantId,
        collection: &str,
        key: &str,
    ) -> StoreResult<bool>;
    /// Every document of the collection, sorted by key.
    async fn documents(
        &self,
        tenant: &TenantId,
        collection: &str,
    ) -> StoreResult<Vec<(String, Value)>>;

    // ---- webhook deliveries (arrive before a tenant is known) -----------
    /// Claim a delivery for processing. `Duplicate` for an id already claimed,
    /// `Replay` for an unseen id whose payload hash was already accepted.
    async fn claim_delivery(
        &self,
        delivery_id: &str,
        payload_sha256: &str,
    ) -> StoreResult<DeliveryClaim>;
    /// Give a claim back after a processing failure so GitHub's retry runs.
    async fn release_delivery(&self, delivery_id: &str) -> StoreResult<()>;
}
