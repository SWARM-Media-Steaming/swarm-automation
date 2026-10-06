//! Plain data types shared by the store, the services and the HTTP layer.

use std::collections::BTreeMap;
use std::fmt;

use serde::{Deserialize, Serialize};

use crate::crypto::SealedSecret;

/// A tenant id. Matches the worker storage's tenant id grammar
/// (`issue_worker/storage.py`: `[a-z0-9][a-z0-9_-]{0,62}`), so the same string
/// names the tenant in the database and in the object-store prefix.
#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(transparent)]
pub struct TenantId(String);

impl TenantId {
    pub fn parse(value: &str) -> Option<Self> {
        let mut chars = value.chars();
        let first = chars.next()?;
        let head_ok = first.is_ascii_lowercase() || first.is_ascii_digit();
        let tail_ok =
            chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-');
        (head_ok && tail_ok && value.len() <= 63).then(|| TenantId(value.to_string()))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for TenantId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Provider {
    Claude,
    Codex,
    Grok,
    ModelData,
}

impl Provider {
    pub const ALL: [Provider; 4] = [
        Provider::Claude,
        Provider::Codex,
        Provider::Grok,
        Provider::ModelData,
    ];

    pub fn parse(value: &str) -> Option<Provider> {
        match value.trim().to_ascii_lowercase().as_str() {
            "claude" => Some(Provider::Claude),
            "codex" => Some(Provider::Codex),
            "grok" => Some(Provider::Grok),
            "model-data" => Some(Provider::ModelData),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Provider::Claude => "claude",
            Provider::Codex => "codex",
            Provider::Grok => "grok",
            Provider::ModelData => "model-data",
        }
    }

    /// The environment variable the provider's CLI (or the model-data fetch)
    /// reads its key from. `src/secrets.rs` owns the model-data name.
    pub fn env_var(self) -> &'static str {
        match self {
            Provider::Claude => "ANTHROPIC_API_KEY",
            Provider::Codex => "OPENAI_API_KEY",
            Provider::Grok => "XAI_API_KEY",
            Provider::ModelData => "ARTIFICIAL_ANALYSIS_API_KEY",
        }
    }

    /// Whether the provider runs AI jobs (and so has usage and budgets). The
    /// model-data key only fetches benchmark data.
    pub fn runs_jobs(self) -> bool {
        self != Provider::ModelData
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    Owner,
    Member,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TenantStatus {
    Active,
    Suspended,
    Deleted,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct User {
    pub id: String,
    pub github_id: u64,
    pub login: String,
}

#[derive(Clone, Debug)]
pub struct Session {
    /// SHA-256 of the cookie value. The cookie value itself is never stored.
    pub token_hash: String,
    pub user_id: String,
    pub csrf_token: String,
    pub expires_at: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Tenant {
    pub id: TenantId,
    pub installation_id: u64,
    pub account_login: String,
    pub account_type: String,
    pub status: TenantStatus,
}

/// A GitHub App installation visible to a signed-in user, with the role GitHub
/// says the user holds on it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InstallationInfo {
    pub id: u64,
    pub account_login: String,
    pub account_type: String,
    pub viewer_role: Role,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Member {
    pub login: String,
    pub role: Role,
}

/// Metadata about a stored provider key. Never contains the key.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct KeyMeta {
    pub provider: Provider,
    pub configured: bool,
    pub updated_at: Option<u64>,
    pub updated_by: Option<String>,
}

#[derive(Clone, Debug)]
pub struct StoredKey {
    pub sealed: SealedSecret,
    pub updated_at: u64,
    pub updated_by: String,
}

/// Operator-set plan limits. The tenant can read them, not raise them.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct PlanQuotas {
    pub max_concurrent_jobs: u32,
    /// `None` means no monthly cap.
    pub monthly_spend_cap_usd: Option<f64>,
}

/// Tenant-set budgets for API-key billing. Spend is measured from the recorded
/// usage; a provider with no budget has no budget gate.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Budgets {
    /// The tenant-wide equivalent of the worker's `minimum_remaining_percent`.
    pub minimum_remaining_percent: f64,
    #[serde(default)]
    pub provider_budgets_usd: BTreeMap<Provider, f64>,
}

impl Default for Budgets {
    fn default() -> Self {
        Budgets {
            minimum_remaining_percent: 10.0,
            provider_budgets_usd: BTreeMap::new(),
        }
    }
}

/// One recorded AI invocation, reduced to what accounting needs.
#[derive(Clone, Debug, PartialEq)]
pub struct LedgerEntry {
    pub id: String,
    pub provider: Provider,
    /// Present only when the worker priced the invocation and it reported tokens.
    pub cost_usd: Option<f64>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize)]
pub struct ProviderSpend {
    pub spend_usd: f64,
    pub priced_invocations: u64,
    /// Invocations with no price: their spend is unknown, never counted as zero.
    pub unpriced_invocations: u64,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct Spend {
    pub providers: BTreeMap<Provider, ProviderSpend>,
}

impl Spend {
    pub fn total_usd(&self) -> f64 {
        self.providers.values().map(|p| p.spend_usd).sum()
    }

    pub fn for_provider(&self, provider: Provider) -> ProviderSpend {
        self.providers.get(&provider).copied().unwrap_or_default()
    }
}

/// A provider-reported usage limit (when the provider exposes one).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ProviderReport {
    pub remaining_percent: f64,
    #[serde(default)]
    pub detail: Option<String>,
    #[serde(default)]
    pub reported_at: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DeliveryClaim {
    New,
    /// The same delivery id was already accepted.
    Duplicate,
    /// A different delivery id carrying a payload that was already accepted.
    Replay,
}
