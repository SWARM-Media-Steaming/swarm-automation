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
    async fn upsert_user(&self, github_id: u64, login: &str) -> StoreResult<User>;
    async fn user(&self, user_id: &str) -> StoreResult<Option<User>>;
    async fn create_session(&self, session: Session) -> StoreResult<()>;
    async fn session(&self, token_hash: &str) -> StoreResult<Option<Session>>;
    async fn delete_session(&self, token_hash: &str) -> StoreResult<()>;
    async fn delete_expired_sessions(&self, now: u64) -> StoreResult<usize>;

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
