//! In-memory [`Store`] for development and tests. State is lost on restart.

use std::collections::{BTreeMap, HashMap};
use std::sync::Mutex;

use async_trait::async_trait;

use crate::crypto::{random_hex, SealedSecret};
use crate::model::*;
use crate::store::{Store, StoreError, StoreResult};

#[derive(Debug, Default)]
struct TenantData {
    members: BTreeMap<String, Role>,
    keys: BTreeMap<Provider, StoredKey>,
    plan: Option<PlanQuotas>,
    budgets: Option<Budgets>,
    ledger: BTreeMap<String, (String, LedgerEntry)>,
    reports: BTreeMap<Provider, ProviderReport>,
    jobs: BTreeMap<String, Provider>,
    documents: BTreeMap<(String, String), serde_json::Value>,
}

#[derive(Debug, Default)]
struct Inner {
    users: HashMap<String, User>,
    /// (provider, subject) -> user id, the `user_identities` key.
    identities: HashMap<(String, String), String>,
    /// user id -> the tenant created for them at first sign-in.
    personal_tenants: HashMap<String, TenantId>,
    sessions: HashMap<String, Session>,
    tenants: HashMap<TenantId, Tenant>,
    tenant_by_installation: HashMap<u64, TenantId>,
    data: HashMap<TenantId, TenantData>,
    delivery_hash: HashMap<String, String>,
    hash_delivery: HashMap<String, String>,
}

#[derive(Default)]
pub struct MemoryStore {
    inner: Mutex<Inner>,
}

impl MemoryStore {
    pub fn new() -> Self {
        Self::default()
    }

    fn lock(&self) -> StoreResult<std::sync::MutexGuard<'_, Inner>> {
        self.inner
            .lock()
            .map_err(|_| StoreError("store lock poisoned".into()))
    }

    /// How many users the store holds (tests that prove a failed sign-in left
    /// nothing behind).
    pub fn user_count(&self) -> usize {
        self.inner.lock().expect("store lock").users.len()
    }

    pub fn tenant_count(&self) -> usize {
        self.inner.lock().expect("store lock").tenants.len()
    }

    /// The id of the user whose current login is `login`, for tests.
    pub fn user_of_login(&self, login: &str) -> String {
        let inner = self.inner.lock().expect("store lock");
        let mut found = inner.users.values().filter(|u| u.login == login);
        let user = found.next().expect("a user with that login");
        assert!(found.next().is_none(), "one user per login");
        user.id.clone()
    }

    /// Everything the store holds, as text, for tests that prove no plaintext
    /// secret is retained.
    pub fn debug_dump(&self) -> String {
        format!("{:?}", self.inner.lock().expect("store lock"))
    }
}

fn unix_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn tenant_data<'a>(inner: &'a mut Inner, tenant: &TenantId) -> StoreResult<&'a mut TenantData> {
    inner
        .data
        .get_mut(tenant)
        .ok_or_else(|| StoreError(format!("unknown tenant {tenant}")))
}

#[async_trait]
impl Store for MemoryStore {
    async fn register_identity(&self, profile: &IdentityProfile) -> StoreResult<Registration> {
        // The same refusals the `user_identities` CHECK constraints make.
        if profile.provider.is_empty() || profile.subject.is_empty() {
            return Err(StoreError("invalid identity".into()));
        }
        // The whole registration happens under one lock and mutates only after
        // every step that can fail, so it is atomic like the Postgres transaction.
        let mut inner = self.lock()?;
        let key = (profile.provider.clone(), profile.subject.clone());
        let now = unix_now();
        let existing = inner.identities.get(&key).cloned();
        let first_sign_in = existing.is_none();
        let user_id = existing.unwrap_or_else(|| format!("u{}", random_hex(8)));
        let tenant_id = match inner.personal_tenants.get(&user_id).cloned() {
            Some(id) => id,
            None => (1..)
                .map(|attempt| TenantId::personal(&profile.login, attempt))
                .find(|id| !inner.tenants.contains_key(id))
                .expect("an unbounded range has a free id"),
        };
        let user = User {
            id: user_id.clone(),
            login: profile.login.clone(),
            display_name: profile.display_name.clone(),
            avatar_url: profile.avatar_url.clone(),
            last_login_at: Some(now),
        };
        inner.identities.insert(key, user_id.clone());
        inner.users.insert(user_id.clone(), user.clone());
        let tenant = match inner.tenants.get_mut(&tenant_id) {
            Some(existing) => {
                existing.account_login = profile.login.clone();
                existing.clone()
            }
            None => {
                let tenant = Tenant {
                    id: tenant_id.clone(),
                    installation_id: None,
                    account_login: profile.login.clone(),
                    account_type: PERSONAL_ACCOUNT_TYPE.to_string(),
                    status: TenantStatus::Active,
                };
                inner.tenants.insert(tenant_id.clone(), tenant.clone());
                inner.data.insert(tenant_id.clone(), TenantData::default());
                inner
                    .personal_tenants
                    .insert(user_id.clone(), tenant_id.clone());
                tenant
            }
        };
        tenant_data(&mut inner, &tenant_id)?
            .members
            .insert(user_id, Role::Owner);
        Ok(Registration {
            user,
            tenant,
            first_sign_in,
        })
    }

    async fn user(&self, user_id: &str) -> StoreResult<Option<User>> {
        Ok(self.lock()?.users.get(user_id).cloned())
    }

    async fn create_session(&self, session: Session) -> StoreResult<()> {
        self.lock()?
            .sessions
            .insert(session.token_hash.clone(), session);
        Ok(())
    }

    async fn session(&self, token_hash: &str) -> StoreResult<Option<Session>> {
        Ok(self.lock()?.sessions.get(token_hash).cloned())
    }

    async fn delete_session(&self, token_hash: &str) -> StoreResult<()> {
        self.lock()?.sessions.remove(token_hash);
        Ok(())
    }

    async fn delete_expired_sessions(&self, now: u64) -> StoreResult<usize> {
        let mut inner = self.lock()?;
        let before = inner.sessions.len();
        inner.sessions.retain(|_, s| s.expires_at > now);
        Ok(before - inner.sessions.len())
    }

    async fn upsert_installation_tenant(
        &self,
        installation_id: u64,
        account_login: &str,
        account_type: &str,
    ) -> StoreResult<Tenant> {
        let mut inner = self.lock()?;
        if let Some(id) = inner.tenant_by_installation.get(&installation_id).cloned() {
            let tenant = inner
                .tenants
                .get_mut(&id)
                .ok_or_else(|| StoreError("dangling tenant".into()))?;
            tenant.account_login = account_login.to_string();
            tenant.account_type = account_type.to_string();
            return Ok(tenant.clone());
        }
        let id = TenantId::parse(&format!("t{}", random_hex(8)))
            .expect("generated tenant ids are valid");
        let tenant = Tenant {
            id: id.clone(),
            installation_id: Some(installation_id),
            account_login: account_login.to_string(),
            account_type: account_type.to_string(),
            status: TenantStatus::Active,
        };
        inner
            .tenant_by_installation
            .insert(installation_id, id.clone());
        inner.tenants.insert(id.clone(), tenant.clone());
        inner.data.insert(id, TenantData::default());
        Ok(tenant)
    }

    async fn tenant(&self, tenant: &TenantId) -> StoreResult<Option<Tenant>> {
        Ok(self.lock()?.tenants.get(tenant).cloned())
    }

    async fn tenant_by_installation(&self, installation_id: u64) -> StoreResult<Option<Tenant>> {
        let inner = self.lock()?;
        Ok(inner
            .tenant_by_installation
            .get(&installation_id)
            .and_then(|id| inner.tenants.get(id))
            .cloned())
    }

    async fn set_tenant_status(&self, tenant: &TenantId, status: TenantStatus) -> StoreResult<()> {
        let mut inner = self.lock()?;
        let entry = inner
            .tenants
            .get_mut(tenant)
            .ok_or_else(|| StoreError(format!("unknown tenant {tenant}")))?;
        entry.status = status;
        Ok(())
    }

    async fn set_membership(
        &self,
        tenant: &TenantId,
        user_id: &str,
        role: Role,
    ) -> StoreResult<()> {
        let mut inner = self.lock()?;
        tenant_data(&mut inner, tenant)?
            .members
            .insert(user_id.to_string(), role);
        Ok(())
    }

    async fn retain_memberships(&self, user_id: &str, keep: &[TenantId]) -> StoreResult<()> {
        let mut inner = self.lock()?;
        for (tenant, data) in inner.data.iter_mut() {
            if !keep.contains(tenant) {
                data.members.remove(user_id);
            }
        }
        Ok(())
    }

    async fn tenants_for_user(&self, user_id: &str) -> StoreResult<Vec<(Tenant, Role)>> {
        let inner = self.lock()?;
        let mut out: Vec<(Tenant, Role)> = inner
            .data
            .iter()
            .filter_map(|(id, data)| {
                let role = *data.members.get(user_id)?;
                Some((inner.tenants.get(id)?.clone(), role))
            })
            .collect();
        out.sort_by(|a, b| a.0.id.cmp(&b.0.id));
        Ok(out)
    }

    async fn role_in(&self, tenant: &TenantId, user_id: &str) -> StoreResult<Option<Role>> {
        Ok(self
            .lock()?
            .data
            .get(tenant)
            .and_then(|d| d.members.get(user_id).copied()))
    }

    async fn members(&self, tenant: &TenantId) -> StoreResult<Vec<Member>> {
        let inner = self.lock()?;
        let Some(data) = inner.data.get(tenant) else {
            return Ok(Vec::new());
        };
        let mut out: Vec<Member> = data
            .members
            .iter()
            .filter_map(|(id, role)| {
                Some(Member {
                    login: inner.users.get(id)?.login.clone(),
                    role: *role,
                })
            })
            .collect();
        out.sort_by(|a, b| a.login.cmp(&b.login));
        Ok(out)
    }

    async fn put_provider_key(
        &self,
        tenant: &TenantId,
        provider: Provider,
        sealed: SealedSecret,
        updated_by: &str,
        updated_at: u64,
    ) -> StoreResult<()> {
        let mut inner = self.lock()?;
        tenant_data(&mut inner, tenant)?.keys.insert(
            provider,
            StoredKey {
                sealed,
                updated_at,
                updated_by: updated_by.to_string(),
            },
        );
        Ok(())
    }

    async fn provider_key(
        &self,
        tenant: &TenantId,
        provider: Provider,
    ) -> StoreResult<Option<StoredKey>> {
        Ok(self
            .lock()?
            .data
            .get(tenant)
            .and_then(|d| d.keys.get(&provider).cloned()))
    }

    async fn delete_provider_key(
        &self,
        tenant: &TenantId,
        provider: Provider,
    ) -> StoreResult<bool> {
        let mut inner = self.lock()?;
        Ok(tenant_data(&mut inner, tenant)?
            .keys
            .remove(&provider)
            .is_some())
    }

    async fn provider_key_meta(&self, tenant: &TenantId) -> StoreResult<Vec<KeyMeta>> {
        let inner = self.lock()?;
        let data = inner.data.get(tenant);
        Ok(Provider::ALL
            .iter()
            .map(|provider| {
                let stored = data.and_then(|d| d.keys.get(provider));
                KeyMeta {
                    provider: *provider,
                    configured: stored.is_some(),
                    updated_at: stored.map(|k| k.updated_at),
                    updated_by: stored.map(|k| k.updated_by.clone()),
                }
            })
            .collect())
    }

    async fn plan_quotas(&self, tenant: &TenantId) -> StoreResult<Option<PlanQuotas>> {
        Ok(self.lock()?.data.get(tenant).and_then(|d| d.plan))
    }

    async fn set_plan_quotas(&self, tenant: &TenantId, plan: PlanQuotas) -> StoreResult<()> {
        let mut inner = self.lock()?;
        tenant_data(&mut inner, tenant)?.plan = Some(plan);
        Ok(())
    }

    async fn budgets(&self, tenant: &TenantId) -> StoreResult<Option<Budgets>> {
        Ok(self
            .lock()?
            .data
            .get(tenant)
            .and_then(|d| d.budgets.clone()))
    }

    async fn set_budgets(&self, tenant: &TenantId, budgets: Budgets) -> StoreResult<()> {
        let mut inner = self.lock()?;
        tenant_data(&mut inner, tenant)?.budgets = Some(budgets);
        Ok(())
    }

    async fn record_usage(
        &self,
        tenant: &TenantId,
        period: &str,
        entries: &[LedgerEntry],
    ) -> StoreResult<usize> {
        let mut inner = self.lock()?;
        let data = tenant_data(&mut inner, tenant)?;
        let mut added = 0;
        for entry in entries {
            if !data.ledger.contains_key(&entry.id) {
                data.ledger
                    .insert(entry.id.clone(), (period.to_string(), entry.clone()));
                added += 1;
            }
        }
        Ok(added)
    }

    async fn spend(&self, tenant: &TenantId, period: &str) -> StoreResult<Spend> {
        let inner = self.lock()?;
        let mut spend = Spend::default();
        let Some(data) = inner.data.get(tenant) else {
            return Ok(spend);
        };
        for (entry_period, entry) in data.ledger.values() {
            if entry_period != period {
                continue;
            }
            let slot = spend.providers.entry(entry.provider).or_default();
            match entry.cost_usd {
                Some(cost) => {
                    slot.spend_usd += cost;
                    slot.priced_invocations += 1;
                }
                None => slot.unpriced_invocations += 1,
            }
        }
        Ok(spend)
    }

    async fn set_provider_report(
        &self,
        tenant: &TenantId,
        provider: Provider,
        report: ProviderReport,
    ) -> StoreResult<()> {
        let mut inner = self.lock()?;
        tenant_data(&mut inner, tenant)?
            .reports
            .insert(provider, report);
        Ok(())
    }

    async fn provider_report(
        &self,
        tenant: &TenantId,
        provider: Provider,
    ) -> StoreResult<Option<ProviderReport>> {
        Ok(self
            .lock()?
            .data
            .get(tenant)
            .and_then(|d| d.reports.get(&provider).cloned()))
    }

    async fn reserve_job(
        &self,
        tenant: &TenantId,
        job_id: &str,
        provider: Provider,
        limit: u32,
    ) -> StoreResult<bool> {
        let mut inner = self.lock()?;
        let data = tenant_data(&mut inner, tenant)?;
        if data.jobs.contains_key(job_id) {
            return Ok(true);
        }
        if data.jobs.len() >= limit as usize {
            return Ok(false);
        }
        data.jobs.insert(job_id.to_string(), provider);
        Ok(true)
    }

    async fn release_job(&self, tenant: &TenantId, job_id: &str) -> StoreResult<bool> {
        let mut inner = self.lock()?;
        Ok(tenant_data(&mut inner, tenant)?
            .jobs
            .remove(job_id)
            .is_some())
    }

    async fn active_jobs(&self, tenant: &TenantId) -> StoreResult<usize> {
        Ok(self.lock()?.data.get(tenant).map_or(0, |d| d.jobs.len()))
    }

    async fn document(
        &self,
        tenant: &TenantId,
        collection: &str,
        key: &str,
    ) -> StoreResult<Option<serde_json::Value>> {
        let mut inner = self.lock()?;
        Ok(tenant_data(&mut inner, tenant)?
            .documents
            .get(&(collection.to_string(), key.to_string()))
            .cloned())
    }

    async fn put_document(
        &self,
        tenant: &TenantId,
        collection: &str,
        key: &str,
        value: serde_json::Value,
    ) -> StoreResult<()> {
        let mut inner = self.lock()?;
        tenant_data(&mut inner, tenant)?
            .documents
            .insert((collection.to_string(), key.to_string()), value);
        Ok(())
    }

    async fn delete_document(
        &self,
        tenant: &TenantId,
        collection: &str,
        key: &str,
    ) -> StoreResult<bool> {
        let mut inner = self.lock()?;
        Ok(tenant_data(&mut inner, tenant)?
            .documents
            .remove(&(collection.to_string(), key.to_string()))
            .is_some())
    }

    async fn documents(
        &self,
        tenant: &TenantId,
        collection: &str,
    ) -> StoreResult<Vec<(String, serde_json::Value)>> {
        let mut inner = self.lock()?;
        Ok(tenant_data(&mut inner, tenant)?
            .documents
            .iter()
            .filter(|((name, _), _)| name == collection)
            .map(|((_, key), value)| (key.clone(), value.clone()))
            .collect())
    }

    async fn claim_delivery(
        &self,
        delivery_id: &str,
        payload_sha256: &str,
    ) -> StoreResult<DeliveryClaim> {
        let mut inner = self.lock()?;
        if inner.delivery_hash.contains_key(delivery_id) {
            return Ok(DeliveryClaim::Duplicate);
        }
        if inner.hash_delivery.contains_key(payload_sha256) {
            return Ok(DeliveryClaim::Replay);
        }
        inner
            .delivery_hash
            .insert(delivery_id.to_string(), payload_sha256.to_string());
        inner
            .hash_delivery
            .insert(payload_sha256.to_string(), delivery_id.to_string());
        Ok(DeliveryClaim::New)
    }

    async fn release_delivery(&self, delivery_id: &str) -> StoreResult<()> {
        let mut inner = self.lock()?;
        if let Some(hash) = inner.delivery_hash.remove(delivery_id) {
            inner.hash_delivery.remove(&hash);
        }
        Ok(())
    }
}
