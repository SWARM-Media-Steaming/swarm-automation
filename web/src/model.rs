//! Plain data types shared by the store, the services and the HTTP layer.

use std::collections::BTreeMap;
use std::fmt;

use serde::{Deserialize, Serialize};

use crate::crypto::{random_hex, SealedSecret};

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

    /// The id of a personal tenant: the login slugified to the grammar, and for
    /// `attempt` 1 that is all. A taken id is retried with `attempt` 2, 3, ... as
    /// a `-<attempt>` suffix (after [`PERSONAL_ID_ATTEMPTS`], a random one, so a
    /// crowded name still terminates). Only lowercase letters, digits, `_` and
    /// `-` survive; the id starts with a letter or digit and fits 63 characters.
    pub fn personal(login: &str, attempt: u32) -> TenantId {
        let mut slug = String::new();
        for c in login.chars() {
            let c = c.to_ascii_lowercase();
            if c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' {
                slug.push(c);
            } else if !slug.ends_with('-') {
                slug.push('-');
            }
        }
        let slug = slug.trim_matches(['-', '_']);
        let slug = if slug.is_empty() { "user" } else { slug };
        let suffix = match attempt {
            0 | 1 => String::new(),
            n if n <= PERSONAL_ID_ATTEMPTS => format!("-{n}"),
            _ => format!("-{}", random_hex(4)),
        };
        // The slug is ASCII, so cutting at a byte offset is safe.
        let room = 63 - suffix.len();
        let cut = slug[..slug.len().min(room)].trim_end_matches(['-', '_']);
        TenantId::parse(&format!("{cut}{suffix}")).expect("a slugified id is valid")
    }
}

/// How many numbered ids ([`TenantId::personal`]) are tried before random ones.
pub const PERSONAL_ID_ATTEMPTS: u32 = 50;

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
    /// The handle at the user's last sign-in (the provider's `login`).
    pub login: String,
    pub display_name: Option<String>,
    pub avatar_url: Option<String>,
    /// Unix seconds of the last sign-in.
    pub last_login_at: Option<u64>,
    /// A platform administrator (`users.is_platform_admin`): someone who runs
    /// the hosted service. Not a tenant [`Role::Owner`], which is per tenant.
    pub is_platform_admin: bool,
}

/// What an identity provider says about a person after a successful sign-in:
/// the provider's stable `subject` (the numeric GitHub id as text) and the
/// profile fields that are refreshed on every sign-in. It never carries a token.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IdentityProfile {
    pub provider: String,
    pub subject: String,
    pub login: String,
    pub display_name: Option<String>,
    pub avatar_url: Option<String>,
}

/// The outcome of [`crate::store::Store::register_identity`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Registration {
    pub user: User,
    /// The user's personal tenant (their Owner membership is already stored).
    pub tenant: Tenant,
    /// `true` when this call created the user, so it was their first sign-in.
    pub first_sign_in: bool,
}

/// `Tenant::account_type` of the tenant every user gets at first sign-in. It has
/// no GitHub App installation.
pub const PERSONAL_ACCOUNT_TYPE: &str = "Personal";

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
    /// `None` for a personal tenant, which no GitHub App installation backs.
    pub installation_id: Option<u64>,
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

/// What [`crate::store::Store::set_platform_admin`] did.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AdminChange {
    /// The flag changed and one audit row was written. Carries the user as stored.
    Changed(User),
    /// The user already was in the requested state; nothing was written.
    Unchanged(User),
    UnknownUser,
    /// The change would leave the platform with no administrator.
    LastAdmin,
}

/// One row of `admin_audit_log`. The actor and the target are `None` once the
/// user they name has been deleted (the trail outlives the people in it).
#[derive(Clone, Debug, PartialEq)]
pub struct AuditEntry {
    pub id: i64,
    pub actor_user_id: Option<String>,
    pub target_user_id: Option<String>,
    pub action: String,
    pub detail: serde_json::Value,
    /// Unix seconds.
    pub created_at: u64,
}

/// `AuditEntry::action` of the first administrator, made at sign-in by
/// `SWARM_WEB_BOOTSTRAP_ADMINS`.
pub const AUDIT_ADMIN_BOOTSTRAP: &str = "admin.bootstrap";
pub const AUDIT_ADMIN_PROMOTE: &str = "admin.promote";
pub const AUDIT_ADMIN_DEMOTE: &str = "admin.demote";
pub const AUDIT_BLACKLIST_SET: &str = "admin.blacklist.set";
pub const AUDIT_BLACKLIST_REMOVE: &str = "admin.blacklist.remove";
pub const AUDIT_PLATFORM_KEY_SET: &str = "admin.platform_key.set";
pub const AUDIT_PLATFORM_KEY_REMOVE: &str = "admin.platform_key.remove";

/// One retired model (`model_blacklist`). `superseded_by` is empty when none is
/// named; the entry then applies outright, as in `model-blacklist.json`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct BlacklistEntry {
    pub model: String,
    pub superseded_by: String,
    pub reason: String,
    pub updated_at: u64,
    pub updated_by: String,
}

/// What a platform provider key is used for.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum KeyPurpose {
    /// Cross-cutting AI concerns: routing, complexity analysis, Jev.
    Platform,
    /// Swarm automation concerns.
    Automation,
}

impl KeyPurpose {
    pub const ALL: [KeyPurpose; 2] = [KeyPurpose::Platform, KeyPurpose::Automation];

    pub fn parse(value: &str) -> Option<KeyPurpose> {
        match value.trim().to_ascii_lowercase().as_str() {
            "platform" => Some(KeyPurpose::Platform),
            "automation" => Some(KeyPurpose::Automation),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            KeyPurpose::Platform => "platform",
            KeyPurpose::Automation => "automation",
        }
    }

    /// The job environment variable carrying this purpose's key for `provider`.
    /// Distinct from the tenant's own `Provider::env_var` so a platform key can
    /// never stand in for (or shadow) a tenant's key.
    pub fn env_var(self, provider: Provider) -> &'static str {
        match (self, provider) {
            (KeyPurpose::Platform, Provider::Claude) => "SWARM_PLATFORM_ANTHROPIC_API_KEY",
            (KeyPurpose::Platform, Provider::Codex) => "SWARM_PLATFORM_OPENAI_API_KEY",
            (KeyPurpose::Platform, Provider::Grok) => "SWARM_PLATFORM_XAI_API_KEY",
            (KeyPurpose::Platform, Provider::ModelData) => {
                "SWARM_PLATFORM_ARTIFICIAL_ANALYSIS_API_KEY"
            }
            (KeyPurpose::Automation, Provider::Claude) => "SWARM_AUTOMATION_ANTHROPIC_API_KEY",
            (KeyPurpose::Automation, Provider::Codex) => "SWARM_AUTOMATION_OPENAI_API_KEY",
            (KeyPurpose::Automation, Provider::Grok) => "SWARM_AUTOMATION_XAI_API_KEY",
            (KeyPurpose::Automation, Provider::ModelData) => {
                "SWARM_AUTOMATION_ARTIFICIAL_ANALYSIS_API_KEY"
            }
        }
    }
}

/// Metadata about a platform provider key. Never contains the key.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct PlatformKeyMeta {
    pub purpose: KeyPurpose,
    pub provider: Provider,
    pub configured: bool,
    pub updated_at: Option<u64>,
    pub updated_by: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DeliveryClaim {
    New,
    /// The same delivery id was already accepted.
    Duplicate,
    /// A different delivery id carrying a payload that was already accepted.
    Replay,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn id(login: &str, attempt: u32) -> String {
        TenantId::personal(login, attempt).to_string()
    }

    #[test]
    fn a_personal_id_is_the_slugified_login() {
        assert_eq!(id("octocat", 1), "octocat");
        assert_eq!(id("Octo-Cat", 1), "octo-cat");
        assert_eq!(id("octo-cat[bot]", 1), "octo-cat-bot");
        assert_eq!(id("-weird__name-", 1), "weird__name");
        assert_eq!(id("a  b..c", 1), "a-b-c");
        assert_eq!(id("ünï", 1), "n");
        assert_eq!(id("日本語", 1), "user");
        assert_eq!(id("", 1), "user");
        assert_eq!(id("___", 1), "user");
    }

    #[test]
    fn a_taken_id_gets_a_numbered_suffix_and_then_a_random_one() {
        assert_eq!(id("alice", 0), "alice");
        assert_eq!(id("alice", 2), "alice-2");
        assert_eq!(id("alice", 50), "alice-50");
        let random = id("alice", 51);
        assert!(random.starts_with("alice-") && random.len() == "alice-".len() + 8);
        assert_ne!(
            random,
            id("alice", 51),
            "past the numbered ids the suffix is random"
        );
    }

    #[test]
    fn every_personal_id_fits_the_tenant_grammar() {
        let long = "x".repeat(200);
        for login in [
            long.as_str(),
            "a",
            "0",
            "-",
            "A-B_C",
            "xx-".repeat(40).as_str(),
        ] {
            for attempt in [1, 2, 9, 50, 51] {
                let id = TenantId::personal(login, attempt);
                assert!(id.as_str().len() <= 63, "{id}");
                assert_eq!(TenantId::parse(id.as_str()).as_ref(), Some(&id));
            }
        }
        assert_eq!(id(&long, 1).len(), 63);
        assert_eq!(id(&long, 2).len(), 63);
        assert!(id(&long, 2).ends_with("-2"));
    }
}
