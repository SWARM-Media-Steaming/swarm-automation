//! Usage accounting, budgets and quotas for API-key billing.
//!
//! The *cost* of an invocation is not computed here. The worker already prices
//! every invocation (`issue_worker/token_usage.py` -> `UsageRecord`, aggregated
//! by `usage_report.py`); jobs hand those records to the backend and this module
//! sums them per tenant. A record counts as priced spend only under the same
//! rule as `usage_report.py` (`_PRICED_COST`): it reported some token counter
//! **and** carries an `estimated_cost`. Anything else is counted as *unpriced*:
//! its spend is unknown, never zero.
//!
//! `minimum_remaining_percent` keeps the worker's pause/resume meaning: a
//! provider whose remaining budget drops below it is "below minimum" (status 1)
//! and no new job is admitted for it; it resumes when the budget is raised or
//! the month rolls over. Remaining comes from the tenant's provider budget and,
//! when the provider reports a limit, from that too (the lower wins). With
//! neither, remaining is *unavailable* (status 2), which gates nothing.

use std::collections::BTreeMap;
use std::sync::Arc;

use serde::{Deserialize, Serialize};

use crate::clock::{period_of, Clock};
use crate::error::ApiError;
use crate::model::*;
use crate::store::Store;

/// A provider-reported limit older than this is treated as unavailable.
pub const REPORT_MAX_AGE_SECS: u64 = 3600;
pub const MAX_BATCH: usize = 500;
/// Same status codes as the worker's `ProviderUsage`.
pub const STATUS_USABLE: u8 = 0;
pub const STATUS_BELOW_MINIMUM: u8 = 1;
pub const STATUS_UNAVAILABLE: u8 = 2;

/// The subset of `token_usage.UsageRecord.to_dict()` accounting reads. Unknown
/// fields are ignored, so the worker may add columns freely.
/// `tests/fixtures/usage_record.json` pins the shape from both languages.
#[derive(Clone, Debug, Deserialize)]
pub struct UsageRecordIn {
    pub id: String,
    pub provider: String,
    #[serde(default)]
    pub estimated_cost: Option<f64>,
    /// Spend is accounted in dollars; a record in another currency is refused
    /// rather than summed as if it were USD. Absent means the worker default (USD).
    #[serde(default)]
    pub currency: Option<String>,
    #[serde(default)]
    pub input_tokens: Option<u64>,
    #[serde(default)]
    pub output_tokens: Option<u64>,
    #[serde(default)]
    pub reasoning_tokens: Option<u64>,
    #[serde(default)]
    pub cached_input_tokens: Option<u64>,
    #[serde(default)]
    pub total_tokens: Option<u64>,
    #[serde(default)]
    pub cache_read_tokens: Option<u64>,
    #[serde(default)]
    pub cache_write_tokens: Option<u64>,
}

#[derive(Debug, Deserialize)]
pub struct IngestBody {
    pub records: Vec<UsageRecordIn>,
    /// Provider-reported remaining limits, when a provider exposes them.
    #[serde(default)]
    pub provider_limits: BTreeMap<String, ProviderReport>,
}

#[derive(Debug, Default, PartialEq, Eq, Serialize)]
pub struct IngestResult {
    pub recorded: usize,
    pub duplicates: usize,
    pub rejected: usize,
}

impl UsageRecordIn {
    fn has_token_usage(&self) -> bool {
        [
            self.input_tokens,
            self.output_tokens,
            self.reasoning_tokens,
            self.cached_input_tokens,
            self.total_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
        ]
        .iter()
        .any(Option::is_some)
    }

    /// `Err` for a record that cannot be accounted for at all.
    pub fn to_entry(&self) -> Result<LedgerEntry, &'static str> {
        if self.id.is_empty() || self.id.len() > 200 {
            return Err("invalid id");
        }
        let provider = Provider::parse(&self.provider)
            .filter(|p| p.runs_jobs())
            .ok_or("unknown provider")?;
        if self
            .currency
            .as_deref()
            .is_some_and(|c| !c.is_empty() && !c.eq_ignore_ascii_case("USD"))
        {
            return Err("unsupported currency");
        }
        let cost_usd = match self.estimated_cost {
            Some(cost) if !cost.is_finite() || cost < 0.0 => return Err("invalid cost"),
            Some(cost) if self.has_token_usage() => Some(cost),
            _ => None,
        };
        Ok(LedgerEntry {
            id: self.id.clone(),
            provider,
            cost_usd,
        })
    }
}

/// `100 * (budget - spent) / budget`, clamped to 0..=100.
pub fn remaining_percent(budget_usd: f64, spent_usd: f64) -> f64 {
    if budget_usd <= 0.0 {
        return 0.0;
    }
    (100.0 * (budget_usd - spent_usd) / budget_usd).clamp(0.0, 100.0)
}

#[derive(Debug, Serialize)]
pub struct ProviderUsageView {
    pub provider: Provider,
    /// 0 usable, 1 below the minimum, 2 unavailable (as the worker's `ProviderUsage`).
    pub status: u8,
    /// `null` when there is no budget and no provider-reported limit.
    pub remaining_percent: Option<f64>,
    /// `budget`, `provider` or `unavailable`.
    pub source: &'static str,
    pub detail: Option<String>,
    pub spend_usd: f64,
    pub budget_usd: Option<f64>,
    pub priced_invocations: u64,
    pub unpriced_invocations: u64,
}

#[derive(Debug, Serialize)]
pub struct UsageView {
    pub period: String,
    pub total_spend_usd: f64,
    pub plan: PlanQuotas,
    pub budgets: Budgets,
    pub active_jobs: usize,
    pub providers: Vec<ProviderUsageView>,
}

#[derive(Debug, PartialEq)]
pub enum Denied {
    TenantInactive,
    KeyMissing(Provider),
    ConcurrentJobs {
        limit: u32,
    },
    MonthlySpendCap {
        cap_usd: f64,
    },
    ProviderBelowMinimum {
        provider: Provider,
        remaining_percent: f64,
        minimum: f64,
    },
}

impl Denied {
    pub fn into_error(self) -> ApiError {
        match self {
            Denied::TenantInactive => ApiError::forbidden("tenant_inactive", "This tenant's GitHub App installation is not active."),
            Denied::KeyMissing(provider) => {
                ApiError::Conflict(format!("No {} key is configured for this tenant.", provider.as_str()))
            }
            Denied::ConcurrentJobs { limit } => ApiError::TooManyRequests {
                code: "concurrent_job_limit",
                message: format!("This tenant is at its limit of {limit} concurrent jobs."),
            },
            Denied::MonthlySpendCap { cap_usd } => ApiError::TooManyRequests {
                code: "monthly_spend_cap",
                message: format!("This tenant reached its monthly spend cap of ${cap_usd:.2}."),
            },
            Denied::ProviderBelowMinimum { provider, remaining_percent, minimum } => ApiError::TooManyRequests {
                code: "provider_budget_low",
                message: format!(
                    "{} has {remaining_percent:.1}% of its budget left, below the {minimum}% minimum. It resumes when the budget is raised or the month rolls over.",
                    provider.as_str()
                ),
            },
        }
    }
}

pub fn validate_plan(plan: &PlanQuotas) -> Result<(), ApiError> {
    if plan.max_concurrent_jobs > 1000 {
        return Err(ApiError::BadRequest(
            "max_concurrent_jobs must be at most 1000.".into(),
        ));
    }
    if let Some(cap) = plan.monthly_spend_cap_usd {
        if !cap.is_finite() || cap <= 0.0 {
            return Err(ApiError::BadRequest(
                "monthly_spend_cap_usd must be a positive number or null.".into(),
            ));
        }
    }
    Ok(())
}

pub fn validate_budgets(budgets: &Budgets) -> Result<(), ApiError> {
    if !budgets.minimum_remaining_percent.is_finite()
        || !(0.0..=100.0).contains(&budgets.minimum_remaining_percent)
    {
        return Err(ApiError::BadRequest(
            "minimum_remaining_percent must be between 0 and 100.".into(),
        ));
    }
    for (provider, budget) in &budgets.provider_budgets_usd {
        if !provider.runs_jobs() {
            return Err(ApiError::BadRequest(
                "Only claude, codex and grok have budgets.".into(),
            ));
        }
        if !budget.is_finite() || *budget <= 0.0 {
            return Err(ApiError::BadRequest(
                "A provider budget must be a positive number of dollars.".into(),
            ));
        }
    }
    Ok(())
}

#[derive(Clone)]
pub struct Accounting {
    store: Arc<dyn Store>,
    clock: Arc<dyn Clock>,
    default_plan: PlanQuotas,
}

impl Accounting {
    pub fn new(store: Arc<dyn Store>, clock: Arc<dyn Clock>, default_plan: PlanQuotas) -> Self {
        Accounting {
            store,
            clock,
            default_plan,
        }
    }

    pub async fn plan(&self, tenant: &TenantId) -> Result<PlanQuotas, ApiError> {
        Ok(self
            .store
            .plan_quotas(tenant)
            .await?
            .unwrap_or(self.default_plan))
    }

    pub async fn budgets(&self, tenant: &TenantId) -> Result<Budgets, ApiError> {
        Ok(self.store.budgets(tenant).await?.unwrap_or_default())
    }

    pub async fn ingest(
        &self,
        tenant: &TenantId,
        body: IngestBody,
    ) -> Result<IngestResult, ApiError> {
        if body.records.len() > MAX_BATCH {
            return Err(ApiError::BadRequest(format!(
                "At most {MAX_BATCH} records per request."
            )));
        }
        let mut entries = Vec::new();
        let mut result = IngestResult::default();
        for record in &body.records {
            match record.to_entry() {
                Ok(entry) => entries.push(entry),
                Err(_) => result.rejected += 1,
            }
        }
        let now = self.clock.now_secs();
        let added = self
            .store
            .record_usage(tenant, &period_of(now), &entries)
            .await?;
        result.recorded = added;
        result.duplicates = entries.len() - added;
        for (name, mut report) in body.provider_limits {
            let provider = Provider::parse(&name).filter(|p| p.runs_jobs());
            let valid = report.remaining_percent.is_finite()
                && (0.0..=100.0).contains(&report.remaining_percent);
            match provider {
                Some(provider) if valid => {
                    report.reported_at = now;
                    report.detail = report.detail.map(|d| d.chars().take(200).collect());
                    self.store
                        .set_provider_report(tenant, provider, report)
                        .await?;
                }
                _ => result.rejected += 1,
            }
        }
        Ok(result)
    }

    async fn provider_view(
        &self,
        tenant: &TenantId,
        provider: Provider,
        spend: &Spend,
        budgets: &Budgets,
    ) -> Result<ProviderUsageView, ApiError> {
        let now = self.clock.now_secs();
        let provider_spend = spend.for_provider(provider);
        let budget = budgets.provider_budgets_usd.get(&provider).copied();
        let from_budget = budget.map(|b| remaining_percent(b, provider_spend.spend_usd));
        let report = self
            .store
            .provider_report(tenant, provider)
            .await?
            .filter(|r| {
                r.reported_at > 0 && now.saturating_sub(r.reported_at) <= REPORT_MAX_AGE_SECS
            });
        let (remaining, source, detail) = match (from_budget, &report) {
            (Some(b), Some(r)) if r.remaining_percent < b => {
                (Some(r.remaining_percent), "provider", r.detail.clone())
            }
            (Some(b), _) => (Some(b), "budget", None),
            (None, Some(r)) => (Some(r.remaining_percent), "provider", r.detail.clone()),
            (None, None) => (None, "unavailable", None),
        };
        let status = match remaining {
            None => STATUS_UNAVAILABLE,
            Some(r) if r < budgets.minimum_remaining_percent => STATUS_BELOW_MINIMUM,
            Some(_) => STATUS_USABLE,
        };
        Ok(ProviderUsageView {
            provider,
            status,
            remaining_percent: remaining,
            source,
            detail,
            spend_usd: provider_spend.spend_usd,
            budget_usd: budget,
            priced_invocations: provider_spend.priced_invocations,
            unpriced_invocations: provider_spend.unpriced_invocations,
        })
    }

    pub async fn usage(&self, tenant: &TenantId) -> Result<UsageView, ApiError> {
        let period = period_of(self.clock.now_secs());
        let spend = self.store.spend(tenant, &period).await?;
        let budgets = self.budgets(tenant).await?;
        let mut providers = Vec::new();
        for provider in Provider::ALL.into_iter().filter(|p| p.runs_jobs()) {
            providers.push(
                self.provider_view(tenant, provider, &spend, &budgets)
                    .await?,
            );
        }
        Ok(UsageView {
            period,
            total_spend_usd: spend.total_usd(),
            plan: self.plan(tenant).await?,
            budgets,
            active_jobs: self.store.active_jobs(tenant).await?,
            providers,
        })
    }

    /// Enforce the quotas before a job starts, and take its concurrency slot.
    /// The order is cheapest and most absolute first; the slot is taken last
    /// so a denied job never holds one.
    pub async fn admit(
        &self,
        tenant: &TenantId,
        job_id: &str,
        provider: Provider,
    ) -> Result<Result<(), Denied>, ApiError> {
        let Some(record) = self.store.tenant(tenant).await? else {
            return Err(ApiError::NotFound);
        };
        if record.status != TenantStatus::Active {
            return Ok(Err(Denied::TenantInactive));
        }
        if self.store.provider_key(tenant, provider).await?.is_none() {
            return Ok(Err(Denied::KeyMissing(provider)));
        }
        let plan = self.plan(tenant).await?;
        let period = period_of(self.clock.now_secs());
        let spend = self.store.spend(tenant, &period).await?;
        if let Some(cap) = plan.monthly_spend_cap_usd {
            if spend.total_usd() >= cap {
                return Ok(Err(Denied::MonthlySpendCap { cap_usd: cap }));
            }
        }
        let budgets = self.budgets(tenant).await?;
        let view = self
            .provider_view(tenant, provider, &spend, &budgets)
            .await?;
        if view.status == STATUS_BELOW_MINIMUM {
            return Ok(Err(Denied::ProviderBelowMinimum {
                provider,
                remaining_percent: view.remaining_percent.unwrap_or(0.0),
                minimum: budgets.minimum_remaining_percent,
            }));
        }
        if !self
            .store
            .reserve_job(tenant, job_id, provider, plan.max_concurrent_jobs)
            .await?
        {
            return Ok(Err(Denied::ConcurrentJobs {
                limit: plan.max_concurrent_jobs,
            }));
        }
        Ok(Ok(()))
    }

    pub async fn release(&self, tenant: &TenantId, job_id: &str) -> Result<bool, ApiError> {
        Ok(self.store.release_job(tenant, job_id).await?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record(id: &str, provider: &str, cost: Option<f64>, tokens: Option<u64>) -> UsageRecordIn {
        UsageRecordIn {
            id: id.into(),
            provider: provider.into(),
            estimated_cost: cost,
            currency: None,
            input_tokens: tokens,
            output_tokens: None,
            reasoning_tokens: None,
            cached_input_tokens: None,
            total_tokens: None,
            cache_read_tokens: None,
            cache_write_tokens: None,
        }
    }

    #[test]
    fn a_cost_without_token_usage_is_not_priced_spend() {
        // usage_report.py: a leftover 0.0 on a row with no tokens must not count.
        assert_eq!(
            record("a", "claude", Some(0.0), None)
                .to_entry()
                .unwrap()
                .cost_usd,
            None
        );
        assert_eq!(
            record("a", "claude", Some(1.5), Some(10))
                .to_entry()
                .unwrap()
                .cost_usd,
            Some(1.5)
        );
        assert_eq!(
            record("a", "claude", None, Some(10))
                .to_entry()
                .unwrap()
                .cost_usd,
            None
        );
        assert_eq!(
            record("a", "Codex", Some(0.0), Some(0))
                .to_entry()
                .unwrap()
                .cost_usd,
            Some(0.0)
        );
    }

    #[test]
    fn unusable_records_are_rejected() {
        assert!(record("", "claude", None, None).to_entry().is_err());
        assert!(record("a", "gemini", None, None).to_entry().is_err());
        assert!(record("a", "model-data", None, None).to_entry().is_err());
        assert!(record("a", "claude", Some(-1.0), Some(1))
            .to_entry()
            .is_err());
        let mut euro = record("a", "claude", Some(1.0), Some(1));
        euro.currency = Some("EUR".into());
        assert!(euro.to_entry().is_err());
        euro.currency = Some("usd".into());
        assert!(euro.to_entry().is_ok());
    }

    #[test]
    fn remaining_percent_is_clamped() {
        assert_eq!(remaining_percent(100.0, 0.0), 100.0);
        assert_eq!(remaining_percent(100.0, 25.0), 75.0);
        assert_eq!(remaining_percent(100.0, 100.0), 0.0);
        assert_eq!(remaining_percent(100.0, 250.0), 0.0);
        assert_eq!(remaining_percent(0.0, 1.0), 0.0);
    }

    #[test]
    fn plan_and_budget_validation() {
        assert!(validate_plan(&PlanQuotas {
            max_concurrent_jobs: 3,
            monthly_spend_cap_usd: None
        })
        .is_ok());
        assert!(validate_plan(&PlanQuotas {
            max_concurrent_jobs: 3,
            monthly_spend_cap_usd: Some(0.0)
        })
        .is_err());
        assert!(validate_plan(&PlanQuotas {
            max_concurrent_jobs: 5000,
            monthly_spend_cap_usd: None
        })
        .is_err());
        let mut budgets = Budgets::default();
        assert!(validate_budgets(&budgets).is_ok());
        budgets.minimum_remaining_percent = 101.0;
        assert!(validate_budgets(&budgets).is_err());
        budgets.minimum_remaining_percent = 10.0;
        budgets
            .provider_budgets_usd
            .insert(Provider::ModelData, 5.0);
        assert!(validate_budgets(&budgets).is_err());
    }
}
