use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::path::{Path, PathBuf};

pub const CONFIG_FILE: &str = "config.json";

/// Every AI provider the app knows how to drive. Order here is the default
/// rotation order and the order provider cards render in.
pub const KNOWN_PROVIDERS: [&str; 3] = ["claude", "codex", "grok"];

/// `preferred_provider` value meaning the user has no favorite. A new issue
/// goes to the enabled provider with the most usage remaining. Exact ties
/// follow [`KNOWN_PROVIDERS`] order instead of a named provider.
pub const PREFERRED_PROVIDER_AUTO: &str = "auto";

fn default_true() -> bool {
    true
}

/// Human label for a provider id (`"claude"` -> `"Claude"`).
pub fn provider_label(id: &str) -> &'static str {
    match id {
        "claude" => "Claude",
        "codex" => "Codex",
        "grok" => "Grok",
        _ => "Unknown",
    }
}

/// `owner/name` as a filesystem-safe directory / slot key (`owner/name` ->
/// `owner__name`). Also the stable `id` of a [`RepoConfig`].
pub fn repo_slug(github_repository: &str) -> String {
    github_repository
        .trim()
        .replace('/', "__")
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.') {
                c
            } else {
                '-'
            }
        })
        .collect()
}

/// Starting worker and router settings for one provider, derived by the worker
/// from the live model catalog (`routing_calculator.py defaults`). Nothing here
/// names a model: the app fills only settings that are still empty, so a
/// fresh install follows the current measurements, prices and blacklist and a
/// saved choice is never overwritten.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct SuggestedModels {
    #[serde(default)]
    pub model: String,
    #[serde(default)]
    pub effort: String,
    #[serde(default)]
    pub router_model: String,
    #[serde(default)]
    pub router_effort: String,
}

/// What each AI tool tends to be good at. The router weighs these when it
/// chooses which enabled tool receives an issue, so the "Codex is better at
/// this, Grok at that" judgement is an editable setting rather than something
/// hardcoded in the worker. Keep in sync with `_DEFAULT_PROVIDER_STRENGTHS` in
/// `issue_worker/dynamic_router.py`.
pub fn provider_strengths_preset(id: &str) -> &'static str {
    match id {
        "claude" => {
            "Multi-file refactors, following an existing codebase's conventions, careful \
             review of someone else's work, and writing documentation or tests in the \
             surrounding style."
        }
        "codex" => {
            "Precise bug fixes, test-driven changes, and long autonomous edit-run-verify \
             loops where the work is checked by running it."
        }
        "grok" => {
            "Fast turnarounds on well-scoped changes, scripting and configuration work, \
             and quick orientation in unfamiliar code."
        }
        _ => "",
    }
}

/// Built-in complexity bands. An empty saved table is filled from this on
/// normalize so the mapping stays in config instead of being scattered
/// through the worker.
/// Automatic routing is always cost-first after capability gates.
fn default_routing_optimization() -> String {
    "cost".into()
}

fn default_jev_model() -> String {
    "jev-latest".into()
}

fn default_jev_timeout_seconds() -> f64 {
    8.0
}

fn default_jev_max_retries() -> u8 {
    2
}

fn default_jev_confidence_automation() -> f64 {
    0.90
}

fn default_jev_confidence_fallback() -> f64 {
    0.70
}

fn default_jev_confidence_security() -> f64 {
    0.95
}

fn default_jev_fallback() -> String {
    "rules".into()
}

fn default_model_data_min_refresh_interval_hours() -> f64 {
    6.0
}

fn default_knowledge_context_token_limit() -> u32 {
    2500
}

fn default_owner_scope_id() -> String {
    "local".into()
}

/// Per-provider settings. One entry per id in [`KNOWN_PROVIDERS`]. `enabled`
/// is the "include this provider in the flow" switch; a disabled provider is
/// never selected by the worker and drops out of the readiness checks and the
/// preferred-provider choice.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct ProviderSettings {
    pub id: String,
    pub enabled: bool,
    pub model: String,
    pub effort: String,
    /// Model that grades an issue and picks a worker model when dynamic
    /// routing is on. Ignored while routing is off.
    pub router_model: String,
    /// Reasoning effort for [`Self::router_model`].
    pub router_effort: String,
    /// What this tool is best at. The router weighs it when it picks which
    /// enabled tool an issue goes to. Ignored while routing is off. The UI no
    /// longer edits this (the router treats it as advice, which a text box
    /// implied was a rule): the desktop omits it, so a save always resets it to
    /// [`provider_strengths_preset`]. It stays in the file for hand-editing.
    pub strengths: String,
    /// Executable path override; empty means auto-detect on PATH.
    pub bin: String,
    /// Per-provider usage reserve, as a percent of the most constrained
    /// remaining window. `None` means inherit the legacy shared
    /// [`AppConfig::minimum_remaining_percent`] until [`AppConfig::normalize`]
    /// copies that value in. `0` is valid: the provider may be selected until
    /// that window is empty.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub minimum_remaining_percent: Option<u8>,
}

impl Default for ProviderSettings {
    fn default() -> Self {
        Self {
            id: String::new(),
            enabled: true,
            model: String::new(),
            effort: "high".into(),
            router_model: String::new(),
            router_effort: "low".into(),
            strengths: String::new(),
            bin: String::new(),
            minimum_remaining_percent: None,
        }
    }
}

impl ProviderSettings {
    fn preset(id: &str) -> Self {
        // Models start empty and are filled from the live catalog; see
        // [`SuggestedModels`]. An empty model reaches the worker as "auto".
        Self {
            id: id.into(),
            enabled: true,
            model: String::new(),
            effort: "low".into(),
            router_model: String::new(),
            router_effort: "low".into(),
            strengths: provider_strengths_preset(id).into(),
            bin: String::new(),
            minimum_remaining_percent: None,
        }
    }
}

fn default_providers() -> Vec<ProviderSettings> {
    KNOWN_PROVIDERS
        .iter()
        .map(|id| ProviderSettings::preset(id))
        .collect()
}

/// One monitored GitHub repository. The AI worker clones it into a managed
/// workspace, cuts issue branches from `integration_branch`, and never lets
/// code reach `base_branch` without a human — unless `auto_promote` is on.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct RepoConfig {
    /// Stable key = `repo_slug(github_repository)`. Used for the process slot
    /// and the workspace directory name.
    pub id: String,
    /// Include this repository in the monitoring rotation.
    pub enabled: bool,
    pub github_repository: String,
    pub assignee: String,
    /// The branch the integration branch mirrors and PRs eventually land on
    /// (via a human). Default `"main"`.
    pub base_branch: String,
    /// The shared AI-work branch. Issue branches are cut from it; issue PRs
    /// target it. Default `"ai-main"`.
    pub integration_branch: String,
    /// Issue-branch namespace: `<prefix>/<ai>/issue-<n>`. Default `"ai"`.
    pub branch_prefix: String,
    pub remote_name: String,
    pub github_host: String,
    /// Path to this repo's GitHub App keys. Empty = the per-repo default
    /// (`~/.config/swarm/github-apps-<id>.json`).
    pub github_apps_config: String,
    pub require_bot_auth: bool,
    pub ready_label: String,
    pub trusted_followup_authors: Vec<String>,
    pub completion_authors: Vec<String>,
    /// Tie-break provider for this repo when remaining usage is equal.
    /// Empty inherits the global `preferred_provider`. [`PREFERRED_PROVIDER_AUTO`]
    /// means no favorite: the provider with the most usage remaining is chosen.
    pub preferred_provider: String,
    pub auto_approve: bool,
    /// Compatibility mirror of `auto_approve`. Approval and squash-merging are
    /// one UI operation; `base_branch` is only touched by `auto_promote`.
    pub auto_merge: bool,
    /// Automatically roll `integration_branch` up into `base_branch` (approve
    /// and merge the promotion PR) after issue PRs land. Off by default — the
    /// human-owned branch is otherwise only ever changed by a person. Needs
    /// `auto_approve` (the worker ignores it otherwise).
    pub auto_promote: bool,
    /// Watch the latest GitHub Actions runs on `integration_branch`. When a
    /// pipeline is failing and nothing tracks it yet, the worker files a
    /// labelled, assigned issue and works it in that same run. Off by default.
    pub monitor_actions: bool,
    /// Ask the AI to add or update UAT and integration tests for each issue.
    pub require_issue_tests: bool,
    #[serde(default)]
    pub adversarial_uat_enabled: bool,
    /// Run an independent adversarial security review after the
    /// implementation (and after adversarial UAT when that is also on): find,
    /// fix and re-verify vulnerabilities the change introduces, and file
    /// anything legitimate but out of scope as its own `adversarial-security`
    /// issue.
    #[serde(default)]
    pub adversarial_security_enabled: bool,
    /// After three unresolved adversarial fix/re-test rounds, merge the latest
    /// commit even when blocking tests or security findings remain. Off by
    /// default: strict mode starts another escalated epoch instead.
    #[serde(default)]
    pub adversarial_best_effort_merge: bool,
    /// Ask the AI to update any Claude skill, agent, rule, workflow, or
    /// `CLAUDE.md` file in the repository that is relevant to the issue.
    #[serde(default)]
    pub update_claude_assets_enabled: bool,
    /// Keep the interactive architecture documentation for this repository up
    /// to date: after completed issue work the worker runs a bounded
    /// documentation-impact review (which may use AI/provider capacity).
    /// Repository-scoped and off by default.
    #[serde(default)]
    pub architecture_docs_enabled: bool,
    /// Dynamic-routing ceiling per provider (issue #401): the most expensive
    /// model and effort routing may select. An empty model means uncapped.
    /// No default model name is stored; the worker compares by estimated cost.
    #[serde(default)]
    pub routing_cap_claude_model: String,
    #[serde(default)]
    pub routing_cap_claude_effort: String,
    #[serde(default)]
    pub routing_cap_codex_model: String,
    #[serde(default)]
    pub routing_cap_codex_effort: String,
    #[serde(default)]
    pub routing_cap_grok_model: String,
    #[serde(default)]
    pub routing_cap_grok_effort: String,
    /// Let the AI return a summary without code when the issue is caused by
    /// local environment, credentials, services, or infrastructure state.
    pub allow_environment_only_summary: bool,
    /// Advanced: an existing local checkout to operate on as-is instead of a
    /// managed clone.
    pub repo_dir: String,
}

impl Default for RepoConfig {
    fn default() -> Self {
        Self {
            id: String::new(),
            enabled: true,
            github_repository: String::new(),
            assignee: String::new(),
            base_branch: "main".into(),
            integration_branch: "ai-main".into(),
            branch_prefix: "ai".into(),
            remote_name: "origin".into(),
            github_host: "github.com".into(),
            github_apps_config: String::new(),
            require_bot_auth: true,
            ready_label: "Ready For Testing".into(),
            trusted_followup_authors: Vec::new(),
            completion_authors: Vec::new(),
            preferred_provider: String::new(),
            auto_approve: true,
            auto_merge: true,
            auto_promote: false,
            monitor_actions: false,
            require_issue_tests: false,
            adversarial_uat_enabled: false,
            adversarial_security_enabled: false,
            adversarial_best_effort_merge: false,
            update_claude_assets_enabled: false,
            architecture_docs_enabled: false,
            routing_cap_claude_model: String::new(),
            routing_cap_claude_effort: String::new(),
            routing_cap_codex_model: String::new(),
            routing_cap_codex_effort: String::new(),
            routing_cap_grok_model: String::new(),
            routing_cap_grok_effort: String::new(),
            allow_environment_only_summary: false,
            repo_dir: String::new(),
        }
    }
}

impl RepoConfig {
    /// The worker's `--routing-caps` value: `{provider: {model, effort}}` for
    /// each provider with a cap. A cap needs both a model and an effort; a half
    /// set cap means uncapped, so a stale effort never caps by itself.
    pub fn routing_caps_json(&self) -> String {
        let mut caps = serde_json::Map::new();
        for (provider, model, effort) in [
            (
                "claude",
                &self.routing_cap_claude_model,
                &self.routing_cap_claude_effort,
            ),
            (
                "codex",
                &self.routing_cap_codex_model,
                &self.routing_cap_codex_effort,
            ),
            (
                "grok",
                &self.routing_cap_grok_model,
                &self.routing_cap_grok_effort,
            ),
        ] {
            let (model, effort) = (model.trim(), effort.trim());
            if !model.is_empty() && !effort.is_empty() {
                caps.insert(
                    provider.into(),
                    serde_json::json!({ "model": model, "effort": effort }),
                );
            }
        }
        serde_json::Value::Object(caps).to_string()
    }

    fn with_repository(github_repository: &str) -> Self {
        let github_repository = github_repository.trim().to_string();
        Self {
            id: repo_slug(&github_repository),
            github_repository,
            ..Self::default()
        }
    }

    /// A short human label — `owner/name` if set, else the id.
    pub fn label(&self) -> String {
        let repo = self.github_repository.trim();
        if repo.is_empty() {
            self.id.clone()
        } else {
            repo.to_string()
        }
    }

    /// Tie-break provider for this repo (`preferred_provider` when set,
    /// else the global default). [`PREFERRED_PROVIDER_AUTO`] is a real
    /// selection, not "unset".
    pub fn effective_preferred_provider<'a>(&'a self, global: &'a str) -> &'a str {
        if self.preferred_provider.trim().is_empty() {
            global
        } else {
            self.preferred_provider.trim()
        }
    }

    /// Path to this repo's GitHub App keys (`github_apps_config` when set,
    /// else the per-repo default under `~/.config/swarm`).
    pub fn effective_apps_config(&self) -> String {
        let configured = self.github_apps_config.trim();
        if !configured.is_empty() {
            return configured.to_string();
        }
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
        self.effective_apps_config_at(&home)
    }

    fn effective_apps_config_at(&self, home: &Path) -> String {
        // Before multi-repository profiles, every installation used this one
        // path. Keep using it when present so an upgrade does not appear to
        // lose already-created GitHub Apps and their non-recoverable PEM keys.
        let legacy = home.join(".config/swarm/github-apps.json");
        if legacy.is_file() {
            return legacy.to_string_lossy().into_owned();
        }
        home.join(format!(".config/swarm/github-apps-{}.json", self.id))
            .to_string_lossy()
            .into_owned()
    }

    fn validate(&self) -> Result<(), String> {
        let repository = self.github_repository.trim();
        if repository.is_empty() {
            return Err("set the GitHub repository (owner/name)".into());
        }
        if repository.split('/').count() != 2
            || repository.starts_with('/')
            || repository.ends_with('/')
        {
            return Err("GitHub repository must use owner/name format".into());
        }
        if self.assignee.trim().is_empty() {
            return Err("set the GitHub assignee whose issues the worker may select".into());
        }
        let base = self.base_branch.trim();
        let integration = self.integration_branch.trim();
        if base.is_empty() {
            return Err("base branch cannot be empty".into());
        }
        if integration.is_empty() {
            return Err("integration branch cannot be empty".into());
        }
        if base == integration {
            return Err("the integration branch must differ from the base branch".into());
        }
        let prefix = self.branch_prefix.trim();
        if prefix.is_empty() || prefix.contains(char::is_whitespace) || prefix.contains('/') {
            return Err(
                "branch prefix must be one non-empty path segment with no spaces or slashes".into(),
            );
        }
        if self.remote_name.trim().is_empty() {
            return Err("git remote cannot be empty".into());
        }
        let override_path = self.repo_dir.trim();
        if !override_path.is_empty() {
            let repo = Path::new(override_path);
            if !repo.is_dir() {
                return Err("the working-copy override path does not exist".into());
            }
            if !repo.join(".git").exists() {
                return Err("the working-copy override is not a Git checkout".into());
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct AppConfig {
    /// One entry per monitored repository. Empty only in an old config file
    /// written before this field existed; `normalize()` folds the legacy
    /// single-repo fields into `repositories[0]` on load.
    #[serde(default)]
    pub repositories: Vec<RepoConfig>,

    /// Parent directory for managed clones. Empty means
    /// `<app-data-dir>/checkouts`; each repo lives in `<root>/<repo id>`.
    pub workspace_root: String,

    /// Global tie-break provider for a repo that does not override it.
    /// [`PREFERRED_PROVIDER_AUTO`] means no favorite: new work goes to the
    /// enabled provider with the most usage remaining.
    pub preferred_provider: String,
    /// One entry per known provider.
    #[serde(default)]
    pub providers: Vec<ProviderSettings>,

    /// When on, the issue worker asks the configured router model to grade
    /// each new issue and then picks the worker model and effort from the live
    /// model catalog. Off preserves the manually selected worker model and effort.
    /// No per-band model table is stored: the worker computes it on demand.
    pub dynamic_model_routing: bool,
    /// Automatic routing is always cost-first after capability, expected-success,
    /// safety, and context-fit gates. Legacy `"best"` values migrate to `"cost"`
    /// on normalize. Manual operator model selections are unchanged.
    #[serde(default = "default_routing_optimization")]
    pub routing_optimization: String,
    /// Off by default: a model that draws on a separate usage-credit balance
    /// (e.g. Claude's `fable` alias) is left out of every catalog offered to
    /// the worker model, router model, and routing tiers — for manual
    /// selection and for [`crate::tools::reconcile_config_models`]'s
    /// automatic repair alike — so nothing can silently start spending
    /// credits the account may not have. Turning this on makes those models
    /// selectable again.
    #[serde(default)]
    pub allow_usage_credit_models: bool,

    /// Automatically refresh Dynamic Model Routing's model/pricing/benchmark
    /// data shortly after the app starts, subject to
    /// [`Self::model_data_min_refresh_interval_hours`]. Always runs
    /// asynchronously after the app is already usable, and never blocks
    /// startup on the external source: the last known-good calibration is
    /// loaded first regardless of this setting. See
    /// `issue_worker/model_calibration.py`.
    #[serde(default = "default_true")]
    pub model_data_refresh_on_startup: bool,
    /// Minimum hours between two successful startup/scheduled refreshes.
    /// The manual "Refresh Model Data" action always bypasses this. Clamped
    /// to a non-negative, sub-two-week range on normalize.
    #[serde(default = "default_model_data_min_refresh_interval_hours")]
    pub model_data_min_refresh_interval_hours: f64,
    // Model data comes from Artificial Analysis when an API key is configured.
    // Every validated refresh activates, including recorded regressions. The
    // former `model_data_source`, `model_data_source_url`,
    // `model_calibration_auto_activate` and `model_calibration_apply_to_routing`
    // settings are gone; older config files that still carry them load fine
    // and the values are ignored.
    /// Legacy shared usage reserve. Copied onto each provider that has not set
    /// its own value. Kept in memory as a fallback; no longer written once
    /// per-provider floors exist.
    #[serde(skip_serializing)]
    pub minimum_remaining_percent: u8,
    /// One issue worker per repository, running at the same time, instead of a
    /// single worker that visits each repository in turn. Faster when several
    /// repositories have ready issues, but AI credits are spent faster too.
    /// Off by default.
    pub parallel_repo_workers: bool,
    // AI execution history is always recorded (sanitized, local) and prompt
    // feedback upload no longer exists; both former settings are ignored when
    // an older config still carries them.
    /// Repository ids selected in the Feedback page. Empty means all
    /// repositories, which keeps old configs global by default.
    #[serde(default)]
    pub feedback_repo_filter: Vec<String>,

    /// Engineering Knowledge Platform (issue #291). On by default so Ask SWARM
    /// and automatic agent context can use the existing execution-history
    /// database. Retrieval is deterministic; generated summaries are a
    /// separate switch.
    #[serde(default = "default_true")]
    pub engineering_knowledge_enabled: bool,
    /// Spend extra AI tokens to write repository/architecture/decision
    /// summaries. Off by default.
    #[serde(default)]
    pub automatic_knowledge_generation: bool,
    #[serde(default = "default_true")]
    pub generate_repository_summaries: bool,
    #[serde(default = "default_true")]
    pub generate_architecture_summaries: bool,
    #[serde(default = "default_true")]
    pub generate_engineering_decisions: bool,
    #[serde(default)]
    pub generate_component_documentation: bool,
    #[serde(default = "default_true")]
    pub generate_risk_summaries: bool,
    #[serde(default)]
    pub generate_issue_clustering: bool,
    /// Approximate token budget for the Knowledge Context Pack injected into
    /// an implementing agent. Clamped on normalize.
    #[serde(default = "default_knowledge_context_token_limit")]
    pub knowledge_context_token_limit: u32,
    /// Ownership/scope identifier so a future permission layer can restrict
    /// retrieval. Defaults to `"local"` until login/multi-tenancy exists.
    #[serde(default = "default_owner_scope_id")]
    pub knowledge_owner_scope_id: String,

    /// Jev decision engine (issue #299). Off by default: Swarm behavior is
    /// unchanged and Jev adds no call, cost, or latency.
    #[serde(default)]
    pub jev_enabled: bool,
    #[serde(default)]
    pub jev_bin: String,
    #[serde(default = "default_jev_model")]
    pub jev_model: String,
    #[serde(default = "default_jev_timeout_seconds")]
    pub jev_timeout_seconds: f64,
    #[serde(default = "default_jev_max_retries")]
    pub jev_max_retries: u8,
    #[serde(default = "default_jev_confidence_automation")]
    pub jev_confidence_automation: f64,
    #[serde(default = "default_jev_confidence_fallback")]
    pub jev_confidence_fallback: f64,
    #[serde(default = "default_jev_confidence_security")]
    pub jev_confidence_security: f64,
    #[serde(default = "default_jev_fallback")]
    pub jev_fallback: String,
    // Every Jev decision use (pre-flight, workflow, UAT and cyber findings,
    // RAG scope, triage, completion) is always on when Jev is enabled; the
    // former per-use `jev_use_*` settings were removed and old configs that
    // carry them still load, ignored.
    pub schedule_mode: String,
    pub schedule_time: String,
    pub schedule_days: Vec<String>,
    pub poll_interval_seconds: u64,
    pub worker_state_dir: String,
    pub gh_bin: String,
    pub python_bin: String,

    /// Set once the app has attempted to establish macOS's one-time
    /// Automation permission for controlling Terminal (used to run provider
    /// and `gh` sign-in commands). Guards `spawn_permission_priming` so it
    /// runs at most once per install, on startup, instead of leaving the
    /// permission prompt to surprise the user later mid sign-in.
    #[serde(default)]
    pub terminal_automation_permission_primed: bool,

    // --- Legacy fields (pre-`repositories` / pre-`providers`). Read once by
    //     `normalize()` to carry a v1 config forward, then never written. ---
    #[serde(default, skip_serializing)]
    pub profile_name: String,
    #[serde(default, skip_serializing)]
    pub repo_dir: String,
    #[serde(default, skip_serializing)]
    pub github_repository: String,
    #[serde(default, skip_serializing)]
    pub assignee: String,
    #[serde(default, skip_serializing)]
    pub trusted_followup_authors: Vec<String>,
    #[serde(default, skip_serializing)]
    pub completion_authors: Vec<String>,
    #[serde(default, skip_serializing)]
    pub ready_label: String,
    #[serde(default, skip_serializing)]
    pub base_branch: String,
    #[serde(default, skip_serializing)]
    pub remote_name: String,
    #[serde(default, skip_serializing)]
    pub github_host: String,
    #[serde(default, skip_serializing)]
    pub github_apps_config: String,
    #[serde(default = "default_true", skip_serializing)]
    pub require_bot_auth: bool,
    #[serde(default = "default_true", skip_serializing)]
    pub auto_approve: bool,
    #[serde(default, skip_serializing)]
    pub auto_merge: bool,
    #[serde(default, skip_serializing)]
    pub require_issue_tests: bool,
    #[serde(default, skip_serializing)]
    pub adversarial_uat_enabled: bool,
    #[serde(default, skip_serializing)]
    pub adversarial_security_enabled: bool,
    #[serde(default, skip_serializing)]
    pub adversarial_best_effort_merge: bool,
    #[serde(default, skip_serializing)]
    pub update_claude_assets_enabled: bool,
    #[serde(default, skip_serializing)]
    pub architecture_docs_enabled: bool,
    #[serde(default, skip_serializing)]
    pub allow_environment_only_summary: bool,
    #[serde(default, skip_serializing)]
    pub branch_prefix: String,
    #[serde(default, skip_serializing)]
    pub claude_model: String,
    #[serde(default, skip_serializing)]
    pub claude_effort: String,
    #[serde(default, skip_serializing)]
    pub codex_model: String,
    #[serde(default, skip_serializing)]
    pub codex_effort: String,
    #[serde(default, skip_serializing)]
    pub claude_bin: String,
    #[serde(default, skip_serializing)]
    pub codex_bin: String,
}

impl Default for AppConfig {
    fn default() -> Self {
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
        Self {
            repositories: Vec::new(),
            workspace_root: String::new(),
            preferred_provider: "claude".into(),
            providers: default_providers(),
            dynamic_model_routing: false,
            routing_optimization: default_routing_optimization(),
            allow_usage_credit_models: false,
            model_data_refresh_on_startup: true,
            model_data_min_refresh_interval_hours: default_model_data_min_refresh_interval_hours(),
            minimum_remaining_percent: 10,
            parallel_repo_workers: false,
            feedback_repo_filter: Vec::new(),
            engineering_knowledge_enabled: true,
            automatic_knowledge_generation: false,
            generate_repository_summaries: true,
            generate_architecture_summaries: true,
            generate_engineering_decisions: true,
            generate_component_documentation: false,
            generate_risk_summaries: true,
            generate_issue_clustering: false,
            knowledge_context_token_limit: default_knowledge_context_token_limit(),
            knowledge_owner_scope_id: default_owner_scope_id(),
            jev_enabled: false,
            jev_bin: String::new(),
            jev_model: default_jev_model(),
            jev_timeout_seconds: default_jev_timeout_seconds(),
            jev_max_retries: default_jev_max_retries(),
            jev_confidence_automation: default_jev_confidence_automation(),
            jev_confidence_fallback: default_jev_confidence_fallback(),
            jev_confidence_security: default_jev_confidence_security(),
            jev_fallback: default_jev_fallback(),
            schedule_mode: "continuous".into(),
            schedule_time: "09:00".into(),
            schedule_days: vec!["mon", "tue", "wed", "thu", "fri"]
                .into_iter()
                .map(str::to_string)
                .collect(),
            poll_interval_seconds: 600,
            worker_state_dir: home
                .join(".local/state/swarm-issue-worker")
                .to_string_lossy()
                .into_owned(),
            gh_bin: String::new(),
            python_bin: String::new(),
            terminal_automation_permission_primed: false,
            profile_name: String::new(),
            repo_dir: String::new(),
            github_repository: String::new(),
            assignee: String::new(),
            trusted_followup_authors: Vec::new(),
            completion_authors: Vec::new(),
            ready_label: String::new(),
            base_branch: String::new(),
            remote_name: String::new(),
            github_host: String::new(),
            github_apps_config: String::new(),
            require_bot_auth: true,
            auto_approve: true,
            auto_merge: false,
            require_issue_tests: false,
            adversarial_uat_enabled: false,
            adversarial_security_enabled: false,
            adversarial_best_effort_merge: false,
            update_claude_assets_enabled: false,
            architecture_docs_enabled: false,
            allow_environment_only_summary: false,
            branch_prefix: String::new(),
            claude_model: String::new(),
            claude_effort: String::new(),
            codex_model: String::new(),
            codex_effort: String::new(),
            claude_bin: String::new(),
            codex_bin: String::new(),
        }
    }
}

impl AppConfig {
    pub fn validate(&self) -> Result<(), String> {
        if self.repositories.is_empty() {
            return Err("Add at least one GitHub repository to monitor.".into());
        }
        let mut seen = HashSet::new();
        for repo in &self.repositories {
            if !seen.insert(repo.id.as_str()) {
                return Err(format!("Repository {} is listed twice.", repo.label()));
            }
            repo.validate()
                .map_err(|error| format!("Repository {}: {error}.", repo.label()))?;
            let preferred = repo.effective_preferred_provider(&self.preferred_provider);
            if !self.preference_targets_enabled_provider(preferred) {
                return Err(format!(
                    "Repository {}: preferred provider '{preferred}' is not an enabled provider.",
                    repo.label()
                ));
            }
        }
        if self.enabled_repos().next().is_none() {
            return Err("Enable at least one repository.".into());
        }

        self.validate_providers()?;
        if !matches!(
            self.schedule_mode.as_str(),
            "continuous" | "daily" | "weekdays" | "custom" | "manual"
        ) {
            return Err("Unknown issue-worker schedule mode.".into());
        }
        validate_time(&self.schedule_time)?;
        if self.schedule_mode == "custom" && self.schedule_days.is_empty() {
            return Err("Choose at least one day for a custom schedule.".into());
        }
        if self.schedule_days.iter().any(|day| {
            !matches!(
                day.as_str(),
                "mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun"
            )
        }) {
            return Err("Schedule days contain an unsupported value.".into());
        }
        if self.poll_interval_seconds == 0 {
            return Err("Polling interval must be at least one second.".into());
        }
        if self.minimum_remaining_percent > 100 {
            return Err("Minimum remaining quota must be between 0 and 100 percent.".into());
        }
        for provider in &self.providers {
            if self.provider_minimum_remaining(&provider.id) > 100 {
                return Err(format!(
                    "{} minimum remaining quota must be between 0 and 100 percent.",
                    provider_label(&provider.id)
                ));
            }
        }
        Ok(())
    }

    pub fn repositories(&self) -> &[RepoConfig] {
        &self.repositories
    }

    pub fn repo(&self, id: &str) -> Option<&RepoConfig> {
        self.repositories.iter().find(|repo| repo.id == id)
    }

    pub fn enabled_repos(&self) -> impl Iterator<Item = &RepoConfig> {
        self.repositories.iter().filter(|repo| repo.enabled)
    }

    fn validate_providers(&self) -> Result<(), String> {
        let mut seen = HashSet::new();
        for provider in &self.providers {
            if !KNOWN_PROVIDERS.contains(&provider.id.as_str()) {
                return Err(format!("Unknown AI provider id: {}", provider.id));
            }
            if !seen.insert(provider.id.as_str()) {
                return Err(format!("Duplicate AI provider entry: {}", provider.id));
            }
            // An empty worker model means "auto": the worker picks it from the
            // live catalog, so it is not an error.
        }
        if self.enabled_providers().next().is_none() {
            return Err("Enable at least one AI provider.".into());
        }
        if !self.preference_targets_enabled_provider(&self.preferred_provider) {
            return Err(
                "The global preferred provider must be one of the enabled providers.".into(),
            );
        }
        Ok(())
    }

    /// `auto` is always allowed. A named provider must be one of the enabled
    /// providers so the worker is not pinned to something the user turned off.
    fn preference_targets_enabled_provider(&self, preferred: &str) -> bool {
        let preferred = preferred.trim();
        preferred == PREFERRED_PROVIDER_AUTO
            || self
                .enabled_providers()
                .any(|provider| provider.id == preferred)
    }

    pub fn enabled_providers(&self) -> impl Iterator<Item = &ProviderSettings> {
        self.providers.iter().filter(|provider| provider.enabled)
    }

    pub fn provider(&self, id: &str) -> Option<&ProviderSettings> {
        self.providers.iter().find(|provider| provider.id == id)
    }

    /// The usage reserve for `id`: the provider's own floor when set, otherwise
    /// the legacy shared [`Self::minimum_remaining_percent`].
    pub fn provider_minimum_remaining(&self, id: &str) -> u8 {
        self.provider(id)
            .and_then(|provider| provider.minimum_remaining_percent)
            .unwrap_or(self.minimum_remaining_percent)
    }

    /// True while any provider still has no worker or router model, which the
    /// worker fills from the live catalog (see [`SuggestedModels`]).
    pub fn has_unset_models(&self) -> bool {
        self.providers.iter().any(|provider| {
            provider.model.trim().is_empty() || provider.router_model.trim().is_empty()
        })
    }

    /// Fill only the settings that are still empty from `suggested`. A model the
    /// user (or an earlier fill) chose is never replaced. Returns one line per
    /// change for the log.
    pub fn apply_suggested_models(
        &mut self,
        suggested: &std::collections::HashMap<String, SuggestedModels>,
    ) -> Vec<String> {
        let mut changes = Vec::new();
        for provider in &mut self.providers {
            let Some(found) = suggested.get(&provider.id) else {
                continue;
            };
            if provider.model.trim().is_empty() && !found.model.trim().is_empty() {
                provider.model = found.model.clone();
                if !found.effort.trim().is_empty() {
                    provider.effort = found.effort.clone();
                }
                changes.push(format!(
                    "{} worker model set to '{}' from the live catalog.",
                    provider_label(&provider.id),
                    provider.model
                ));
            }
            if provider.router_model.trim().is_empty() && !found.router_model.trim().is_empty() {
                provider.router_model = found.router_model.clone();
                if !found.router_effort.trim().is_empty() {
                    provider.router_effort = found.router_effort.clone();
                }
                changes.push(format!(
                    "{} router model set to '{}' from the live catalog.",
                    provider_label(&provider.id),
                    provider.router_model
                ));
            }
        }
        changes
    }

    #[cfg(test)]
    pub fn provider_mut(&mut self, id: &str) -> Option<&mut ProviderSettings> {
        self.providers.iter_mut().find(|provider| provider.id == id)
    }

    /// Fold a legacy single-repo / pre-`providers` config forward, guarantee a
    /// full provider set, dedupe + re-key repositories, and clear the
    /// transitional fields so they never round-trip.
    pub fn normalize(&mut self) {
        self.normalize_providers();
        self.normalize_routing();
        self.normalize_model_calibration();
        self.normalize_knowledge();
        self.normalize_repositories();
        let repository_ids: HashSet<_> = self
            .repositories
            .iter()
            .map(|repo| repo.id.clone())
            .collect();
        let mut seen_feedback_ids = HashSet::new();
        self.feedback_repo_filter
            .retain(|id| repository_ids.contains(id) && seen_feedback_ids.insert(id.clone()));
    }

    fn normalize_providers(&mut self) {
        if self.providers.is_empty() {
            let legacy = |value: &str, id: &str| {
                let value = value.trim();
                if value.is_empty() {
                    ProviderSettings::preset(id).model
                } else {
                    value.to_string()
                }
            };
            let legacy_effort = |value: &str, id: &str| {
                let value = value.trim();
                if value.is_empty() {
                    ProviderSettings::preset(id).effort
                } else {
                    value.to_string()
                }
            };
            self.providers = vec![
                ProviderSettings {
                    id: "claude".into(),
                    model: legacy(&self.claude_model, "claude"),
                    effort: legacy_effort(&self.claude_effort, "claude"),
                    bin: std::mem::take(&mut self.claude_bin),
                    ..ProviderSettings::preset("claude")
                },
                ProviderSettings {
                    id: "codex".into(),
                    model: legacy(&self.codex_model, "codex"),
                    effort: legacy_effort(&self.codex_effort, "codex"),
                    bin: std::mem::take(&mut self.codex_bin),
                    ..ProviderSettings::preset("codex")
                },
                ProviderSettings::preset("grok"),
            ];
        }
        for id in KNOWN_PROVIDERS {
            if self.provider(id).is_none() {
                self.providers.push(ProviderSettings::preset(id));
            }
        }
        let inherited_minimum = self.minimum_remaining_percent;
        for provider in &mut self.providers {
            if provider.router_effort.trim().is_empty() {
                provider.router_effort = "low".into();
            }
            if provider.strengths.trim().is_empty() {
                provider.strengths = provider_strengths_preset(&provider.id).into();
            }
            if provider.minimum_remaining_percent.is_none() {
                provider.minimum_remaining_percent = Some(inherited_minimum);
            }
        }
        self.claude_model.clear();
        self.claude_effort.clear();
        self.codex_model.clear();
        self.codex_effort.clear();
        self.claude_bin.clear();
        self.codex_bin.clear();
        match canonicalize_preferred_provider(&self.preferred_provider) {
            Some(value) if !value.is_empty() => self.preferred_provider = value,
            _ => self.preferred_provider = "claude".into(),
        }
    }

    fn normalize_routing(&mut self) {
        // Cost-first is the only automatic mode. Legacy "best" (and anything
        // else) migrates here so older config files keep routing.
        if self.routing_optimization.trim() != "cost" {
            self.routing_optimization = default_routing_optimization();
        }
        self.normalize_jev();
    }

    /// A malformed source name or interval (e.g. from an older config file
    /// or a hand-edited one) self-heals to a safe default rather than
    /// failing `validate()`, matching [`Self::normalize_routing`] above --
    /// this setting only ever changes when a refresh runs, never something
    /// that should block "Save configuration".
    fn normalize_model_calibration(&mut self) {
        if !self.model_data_min_refresh_interval_hours.is_finite()
            || self.model_data_min_refresh_interval_hours < 0.0
        {
            self.model_data_min_refresh_interval_hours =
                default_model_data_min_refresh_interval_hours();
        } else if self.model_data_min_refresh_interval_hours > 336.0 {
            self.model_data_min_refresh_interval_hours = 336.0;
        }
    }

    fn normalize_jev(&mut self) {
        if self.jev_model.trim().is_empty() {
            self.jev_model = default_jev_model();
        }
        if !self.jev_timeout_seconds.is_finite() || self.jev_timeout_seconds < 1.0 {
            self.jev_timeout_seconds = default_jev_timeout_seconds();
        } else if self.jev_timeout_seconds > 60.0 {
            self.jev_timeout_seconds = 60.0;
        }
        if self.jev_max_retries > 5 {
            self.jev_max_retries = 5;
        }
        let clamp = |value: f64, default: f64| {
            if !value.is_finite() {
                return default;
            }
            let number = if value > 1.0 && value <= 100.0 {
                value / 100.0
            } else {
                value
            };
            if !(0.0..=1.0).contains(&number) {
                default
            } else {
                number
            }
        };
        self.jev_confidence_automation = clamp(
            self.jev_confidence_automation,
            default_jev_confidence_automation(),
        );
        self.jev_confidence_fallback = clamp(
            self.jev_confidence_fallback,
            default_jev_confidence_fallback(),
        );
        self.jev_confidence_security = clamp(
            self.jev_confidence_security,
            default_jev_confidence_security(),
        );
        if self.jev_confidence_security < self.jev_confidence_automation {
            self.jev_confidence_security = self.jev_confidence_automation;
        }
        if !matches!(self.jev_fallback.trim(), "rules" | "llm" | "rules_then_llm") {
            self.jev_fallback = default_jev_fallback();
        }
    }

    fn normalize_knowledge(&mut self) {
        self.knowledge_context_token_limit = self.knowledge_context_token_limit.clamp(200, 20_000);
        if self.knowledge_owner_scope_id.trim().is_empty() {
            self.knowledge_owner_scope_id = default_owner_scope_id();
        }
    }

    fn normalize_repositories(&mut self) {
        if self.repositories.is_empty() && !self.github_repository.trim().is_empty() {
            let mut repo = RepoConfig::with_repository(&self.github_repository);
            repo.assignee = std::mem::take(&mut self.assignee);
            repo.repo_dir = std::mem::take(&mut self.repo_dir);
            repo.trusted_followup_authors = std::mem::take(&mut self.trusted_followup_authors);
            repo.completion_authors = std::mem::take(&mut self.completion_authors);
            if !self.ready_label.trim().is_empty() {
                repo.ready_label = std::mem::take(&mut self.ready_label);
            }
            if !self.base_branch.trim().is_empty() {
                repo.base_branch = std::mem::take(&mut self.base_branch);
            }
            if !self.remote_name.trim().is_empty() {
                repo.remote_name = std::mem::take(&mut self.remote_name);
            }
            if !self.github_host.trim().is_empty() {
                repo.github_host = std::mem::take(&mut self.github_host);
            }
            repo.github_apps_config = std::mem::take(&mut self.github_apps_config);
            repo.require_bot_auth = self.require_bot_auth;
            repo.auto_approve = self.auto_approve;
            repo.auto_merge = self.auto_merge;
            repo.require_issue_tests = self.require_issue_tests;
            repo.adversarial_uat_enabled = self.adversarial_uat_enabled;
            repo.adversarial_security_enabled = self.adversarial_security_enabled;
            repo.adversarial_best_effort_merge = self.adversarial_best_effort_merge;
            repo.update_claude_assets_enabled = self.update_claude_assets_enabled;
            repo.architecture_docs_enabled = self.architecture_docs_enabled;
            repo.allow_environment_only_summary = self.allow_environment_only_summary;
            // A migrated config keeps whatever prefix it already used; a fresh
            // repo defaults to "ai".
            if !self.branch_prefix.trim().is_empty() {
                repo.branch_prefix = std::mem::take(&mut self.branch_prefix);
            }
            self.repositories.push(repo);
        }

        // Re-key every repo from its current `github_repository`, drop
        // unparseable entries, dedupe by id (first wins).
        let mut seen = HashSet::new();
        let mut kept = Vec::new();
        for mut repo in std::mem::take(&mut self.repositories) {
            let slug = repo_slug(&repo.github_repository);
            if slug.is_empty() || !repo.github_repository.trim().contains('/') {
                continue;
            }
            repo.id = slug;
            if seen.insert(repo.id.clone()) {
                if repo.integration_branch.trim().is_empty() {
                    repo.integration_branch = "ai-main".into();
                }
                if repo.branch_prefix.trim().is_empty() {
                    repo.branch_prefix = "ai".into();
                }
                if repo.base_branch.trim().is_empty() {
                    repo.base_branch = "main".into();
                }
                if repo.remote_name.trim().is_empty() {
                    repo.remote_name = "origin".into();
                }
                // Enterprise hosts are intentionally out of scope for now.
                // Normalize older profiles to the one supported GitHub host.
                repo.github_host = "github.com".into();
                // Approval and issue-PR merging are one user-facing operation.
                repo.auto_merge = repo.auto_approve;
                if let Some(preferred) = canonicalize_preferred_provider(&repo.preferred_provider) {
                    repo.preferred_provider = preferred;
                }
                // A routing cap is a model plus an effort; half of one is no cap.
                for (model, effort) in [
                    (
                        &mut repo.routing_cap_claude_model,
                        &mut repo.routing_cap_claude_effort,
                    ),
                    (
                        &mut repo.routing_cap_codex_model,
                        &mut repo.routing_cap_codex_effort,
                    ),
                    (
                        &mut repo.routing_cap_grok_model,
                        &mut repo.routing_cap_grok_effort,
                    ),
                ] {
                    *model = model.trim().to_string();
                    *effort = effort.trim().to_lowercase();
                    if model.is_empty() || effort.is_empty() {
                        model.clear();
                        effort.clear();
                    }
                }
                kept.push(repo);
            }
        }
        self.repositories = kept;

        // Clear transitional scalars regardless of migration path.
        self.profile_name.clear();
    }
}

/// Normalize a preferred-provider setting.
///
/// `Some("")` is "unset" (a repository inherits the global value).
/// `Some("auto")` and `Some` of a known provider id are canonical selections.
/// `None` is an unrecognized value, left for validation to reject on a
/// repository override and replaced with the default on the global setting.
fn canonicalize_preferred_provider(value: &str) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Some(String::new());
    }
    if trimmed.eq_ignore_ascii_case(PREFERRED_PROVIDER_AUTO) {
        return Some(PREFERRED_PROVIDER_AUTO.into());
    }
    KNOWN_PROVIDERS
        .iter()
        .find(|id| id.eq_ignore_ascii_case(trimmed))
        .map(|id| (*id).to_string())
}

fn validate_time(value: &str) -> Result<(), String> {
    let (hour, minute) = value
        .split_once(':')
        .ok_or_else(|| "Schedule time must use 24-hour HH:MM format.".to_string())?;
    let hour: u8 = hour
        .parse()
        .map_err(|_| "Schedule time must use 24-hour HH:MM format.".to_string())?;
    let minute: u8 = minute
        .parse()
        .map_err(|_| "Schedule time must use 24-hour HH:MM format.".to_string())?;
    if hour > 23 || minute > 59 || value.len() != 5 {
        return Err("Schedule time must use 24-hour HH:MM format.".into());
    }
    Ok(())
}

pub fn load(path: &Path) -> AppConfig {
    let mut config: AppConfig = std::fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default();
    config.normalize();
    config
}

pub fn save(path: &Path, config: &AppConfig) -> Result<(), String> {
    config.validate()?;
    save_unchecked(path, config)
}

/// Writes the config file as-is, skipping the full-settings `validate()` gate
/// `save` applies. Reserved for internal bookkeeping flags (e.g. one-time
/// permission priming, see `terminal_automation_permission_primed`) that must
/// persist even before the user has finished initial setup — an
/// unconfigured app (no repositories yet) is a normal state for those, even
/// though it is not a valid state to run the worker against.
pub fn save_unchecked(path: &Path, config: &AppConfig) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    let payload = serde_json::to_vec_pretty(config).map_err(|error| error.to_string())?;
    let temporary = path.with_extension("json.tmp");
    std::fs::write(&temporary, payload).map_err(|error| error.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&temporary, std::fs::Permissions::from_mode(0o600))
            .map_err(|error| error.to_string())?;
    }
    std::fs::rename(temporary, path).map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config_with_one_repo() -> AppConfig {
        let mut config = AppConfig::default();
        config.repositories.push(RepoConfig {
            assignee: "octocat".into(),
            ..RepoConfig::with_repository("octocat/example")
        });
        config
    }

    #[test]
    fn time_validation_is_strict() {
        assert!(validate_time("03:00").is_ok());
        assert!(validate_time("3:00").is_err());
        assert!(validate_time("24:00").is_err());
    }

    #[test]
    fn normalize_folds_a_legacy_single_repo_config_forward() {
        let legacy = r#"{
            "github_repository": "octocat/Hello-World",
            "assignee": "octocat",
            "base_branch": "trunk",
            "branch_prefix": "swarm",
            "auto_merge": false,
            "claude_model": "claude-opus-5",
            "preferred_provider": "codex"
        }"#;
        let mut config: AppConfig = serde_json::from_str(legacy).expect("legacy config parses");
        assert!(config.repositories.is_empty());
        config.normalize();

        assert_eq!(config.repositories.len(), 1);
        let repo = &config.repositories[0];
        assert_eq!(repo.id, "octocat__Hello-World");
        assert_eq!(repo.github_repository, "octocat/Hello-World");
        assert_eq!(repo.assignee, "octocat");
        assert_eq!(repo.base_branch, "trunk");
        assert_eq!(repo.integration_branch, "ai-main", "new default");
        assert_eq!(repo.branch_prefix, "swarm", "migrated prefix preserved");
        assert_eq!(repo.github_host, "github.com", "only supported host");
        assert!(repo.require_bot_auth, "bot authentication defaults on");
        assert!(repo.auto_approve, "automatic PR approval defaults on");
        assert!(
            !repo.auto_promote,
            "promotion into the base branch defaults off"
        );
        assert!(!repo.monitor_actions, "Actions monitoring defaults off");
        assert!(repo.auto_merge, "approval also enables issue PR merging");
        assert!(!repo.require_issue_tests);
        assert!(!repo.adversarial_uat_enabled);
        assert!(
            !repo.adversarial_security_enabled,
            "the adversarial cybersecurity review defaults off"
        );
        assert!(
            !repo.adversarial_best_effort_merge,
            "best-effort adversarial merge stays off unless explicitly enabled"
        );
        assert!(!repo.update_claude_assets_enabled);
        assert!(
            !repo.architecture_docs_enabled,
            "architecture documentation defaults off for migrated configs"
        );
        assert!(!repo.allow_environment_only_summary);
        assert_eq!(config.provider("claude").unwrap().model, "claude-opus-5");
        assert_eq!(config.preferred_provider, "codex");

        let value: serde_json::Value = serde_json::to_value(&config).unwrap();
        let top = value.as_object().unwrap();
        assert!(top.contains_key("repositories"));
        for legacy in [
            "github_repository",
            "assignee",
            "base_branch",
            "claude_model",
            "profile_name",
            "minimum_remaining_percent",
        ] {
            assert!(
                !top.contains_key(legacy),
                "top-level legacy key {legacy} must not serialize"
            );
        }
    }

    #[test]
    fn normalize_copies_the_shared_quota_floor_onto_providers_that_have_none() {
        let mut config: AppConfig = serde_json::from_str(
            r#"{
                "minimum_remaining_percent": 0,
                "providers": [
                    {"id": "claude", "enabled": true, "model": "claude-sonnet-5"},
                    {"id": "codex", "enabled": true, "model": "gpt-5.6-luna", "minimum_remaining_percent": 25},
                    {"id": "grok", "enabled": true, "model": "grok-4.6"}
                ]
            }"#,
        )
        .expect("config with mixed quota floors parses");
        config.normalize();

        assert_eq!(config.provider_minimum_remaining("claude"), 0);
        assert_eq!(config.provider_minimum_remaining("codex"), 25);
        assert_eq!(config.provider_minimum_remaining("grok"), 0);

        let value: serde_json::Value = serde_json::to_value(&config).unwrap();
        assert!(value.get("minimum_remaining_percent").is_none());
        let providers = value["providers"].as_array().unwrap();
        let floor = |id: &str| {
            providers
                .iter()
                .find(|provider| provider["id"] == id)
                .and_then(|provider| provider["minimum_remaining_percent"].as_u64())
        };
        assert_eq!(floor("claude"), Some(0));
        assert_eq!(floor("codex"), Some(25));
        assert_eq!(floor("grok"), Some(0));
    }

    #[test]
    fn repository_bot_config_prefers_an_existing_legacy_file() {
        let home = tempfile::tempdir().unwrap();
        let legacy = home.path().join(".config/swarm/github-apps.json");
        std::fs::create_dir_all(legacy.parent().unwrap()).unwrap();
        std::fs::write(&legacy, "{}\n").unwrap();
        let repo = RepoConfig::with_repository("octocat/example");
        assert_eq!(
            repo.effective_apps_config_at(home.path()),
            legacy.to_string_lossy()
        );

        std::fs::remove_file(&legacy).unwrap();
        assert!(repo
            .effective_apps_config_at(home.path())
            .ends_with("github-apps-octocat__example.json"));
    }

    #[test]
    fn validate_rejects_a_broken_repo_set() {
        let mut config = config_with_one_repo();
        assert!(config.validate().is_ok());

        // no repositories
        let empty = AppConfig::default();
        assert!(empty
            .validate()
            .unwrap_err()
            .contains("at least one GitHub repository"));

        // base == integration
        config.repositories[0].integration_branch = "main".into();
        assert!(config
            .validate()
            .unwrap_err()
            .contains("must differ from the base branch"));
        config.repositories[0].integration_branch = "ai-main".into();

        // duplicate repo
        config.repositories.push(RepoConfig {
            assignee: "octocat".into(),
            ..RepoConfig::with_repository("octocat/example")
        });
        assert!(config.validate().unwrap_err().contains("listed twice"));
        config.repositories.pop();

        // per-repo preferred provider not enabled
        config.repositories[0].preferred_provider = "grok".into();
        for provider in &mut config.providers {
            provider.enabled = provider.id == "claude";
        }
        config.preferred_provider = "claude".into();
        assert!(config
            .validate()
            .unwrap_err()
            .contains("not an enabled provider"));
    }

    #[test]
    fn removed_settings_in_older_configs_are_ignored() {
        // Written before these settings were made fixed behavior or removed.
        let older: AppConfig = serde_json::from_str(
            r#"{"auto_update":"auto","ai_execution_history_enabled":false,
                "prompt_feedback_upload_enabled":true,"jev_use_uat":false,
                "jev_use_completion":false}"#,
        )
        .unwrap();
        assert!(serde_json::to_string(&older)
            .unwrap()
            .find("auto_update")
            .is_none());
        assert!(serde_json::to_string(&older)
            .unwrap()
            .find("jev_use_uat")
            .is_none());
    }

    #[test]
    fn feedback_repository_filter_defaults_global_and_normalizes_saved_ids() {
        let older: AppConfig = serde_json::from_str("{}").unwrap();
        assert!(older.feedback_repo_filter.is_empty());

        let mut config = AppConfig {
            repositories: vec![
                RepoConfig::with_repository("octocat/one"),
                RepoConfig::with_repository("octocat/two"),
            ],
            feedback_repo_filter: vec![
                "octocat__two".into(),
                "missing".into(),
                "octocat__two".into(),
            ],
            ..AppConfig::default()
        };
        config.normalize();
        assert_eq!(config.feedback_repo_filter, vec!["octocat__two"]);

        let encoded = serde_json::to_string(&config).unwrap();
        let decoded: AppConfig = serde_json::from_str(&encoded).unwrap();
        assert_eq!(decoded.feedback_repo_filter, vec!["octocat__two"]);
    }

    #[test]
    fn validate_providers_rejects_a_broken_provider_set() {
        let mut config = config_with_one_repo();
        for provider in &mut config.providers {
            provider.enabled = provider.id == "codex";
        }
        config.preferred_provider = "claude".into();
        assert!(config
            .validate_providers()
            .unwrap_err()
            .contains("preferred provider"));

        for provider in &mut config.providers {
            provider.enabled = false;
        }
        assert!(config
            .validate_providers()
            .unwrap_err()
            .contains("at least one"));

        config.providers = default_providers();
        config.preferred_provider = "claude".into();
        // An empty model is "auto", not an error: the worker fills it from the catalog.
        config.provider_mut("claude").unwrap().model.clear();
        assert!(config.validate_providers().is_ok());
    }

    #[test]
    fn empty_models_are_filled_from_suggestions_and_chosen_ones_are_kept() {
        let mut config = config_with_one_repo();
        config.normalize();
        // Nothing in the app names a model: a fresh config starts empty.
        assert!(config.has_unset_models());
        assert!(config
            .providers
            .iter()
            .all(|p| p.model.is_empty() && p.router_model.is_empty()));

        config.provider_mut("codex").unwrap().model = "my-chosen-model".into();
        config.provider_mut("codex").unwrap().effort = "high".into();
        let suggested: std::collections::HashMap<String, SuggestedModels> = serde_json::from_value(
            serde_json::json!({
                "claude": {"model": "w", "effort": "medium", "router_model": "r", "router_effort": "low"},
                "codex": {"model": "w2", "effort": "low", "router_model": "r2", "router_effort": "low"},
            }),
        )
        .unwrap();
        let changes = config.apply_suggested_models(&suggested);

        let claude = config.provider("claude").unwrap();
        assert_eq!(
            (claude.model.as_str(), claude.effort.as_str()),
            ("w", "medium")
        );
        assert_eq!(claude.router_model, "r");
        let codex = config.provider("codex").unwrap();
        assert_eq!(
            (codex.model.as_str(), codex.effort.as_str()),
            ("my-chosen-model", "high")
        );
        assert_eq!(codex.router_model, "r2");
        assert_eq!(changes.len(), 3);
        // Grok had no suggestion, so it stays unset for the worker's auto.
        assert!(config.has_unset_models());
        assert!(config.apply_suggested_models(&suggested).is_empty());
    }

    #[test]
    fn routing_caps_default_uncapped_and_half_a_cap_is_cleared_on_normalize() {
        let mut config = AppConfig::default();
        let mut repo = RepoConfig::with_repository("octocat/example");
        assert_eq!(repo.routing_caps_json(), "{}");
        repo.routing_cap_grok_model = " grok-4 ".into();
        repo.routing_cap_claude_model = "m".into();
        repo.routing_cap_claude_effort = " HIGH ".into();
        config.repositories = vec![repo];
        config.normalize();
        let repo = &config.repositories[0];
        assert!(repo.routing_cap_grok_model.is_empty());
        assert_eq!(repo.routing_cap_claude_effort, "high");
        let older: RepoConfig = serde_json::from_str("{\"github_repository\":\"a/b\"}").unwrap();
        assert!(older.routing_cap_claude_model.is_empty());
    }

    #[test]
    fn routing_optimization_defaults_to_cost_and_migrates_best() {
        let mut config = config_with_one_repo();
        config.normalize();
        assert_eq!(config.routing_optimization, "cost");

        config.routing_optimization = "best".into();
        config.normalize();
        assert_eq!(config.routing_optimization, "cost");

        let encoded = serde_json::to_string(&config).unwrap();
        let mut decoded: AppConfig = serde_json::from_str(&encoded).unwrap();
        decoded.normalize();
        assert_eq!(decoded.routing_optimization, "cost");
        assert!(decoded.validate().is_ok());

        decoded.routing_optimization = "cheapest".into();
        decoded.normalize();
        assert_eq!(decoded.routing_optimization, "cost");
        let mut older: AppConfig =
            serde_json::from_str(r#"{"dynamic_model_routing": true}"#).unwrap();
        older.normalize();
        assert_eq!(older.routing_optimization, "cost");
        assert!(!older.jev_enabled);
        assert_eq!(older.jev_model, "jev-latest");
        assert!((older.jev_confidence_automation - 0.90).abs() < f64::EPSILON);
    }

    #[test]
    fn model_calibration_settings_default_safely_and_self_heal() {
        let mut config = config_with_one_repo();
        config.normalize();
        assert!(config.model_data_refresh_on_startup);
        assert_eq!(config.model_data_min_refresh_interval_hours, 6.0);

        config.model_data_min_refresh_interval_hours = 1.0;
        let encoded = serde_json::to_string(&config).unwrap();
        let mut decoded: AppConfig = serde_json::from_str(&encoded).unwrap();
        decoded.normalize();
        assert_eq!(decoded.model_data_min_refresh_interval_hours, 1.0);
        assert!(decoded.validate().is_ok());

        // An out-of-range interval (hand-edited config, or a config written
        // before this setting existed) self-heals rather than blocking
        // "Save configuration".
        decoded.model_data_min_refresh_interval_hours = -5.0;
        decoded.normalize();
        assert_eq!(decoded.model_data_min_refresh_interval_hours, 6.0);

        decoded.model_data_min_refresh_interval_hours = 10_000.0;
        decoded.normalize();
        assert_eq!(decoded.model_data_min_refresh_interval_hours, 336.0);

        let mut older: AppConfig =
            serde_json::from_str(r#"{"dynamic_model_routing": true}"#).unwrap();
        older.normalize();
        assert!(older.model_data_refresh_on_startup);

        // Settings removed in favor of fixed behavior are ignored, not errors.
        let legacy: AppConfig = serde_json::from_str(
            r#"{"model_data_source":"local","model_data_source_url":"https://x.example",
                "model_calibration_auto_activate":false,"model_calibration_apply_to_routing":false}"#,
        )
        .unwrap();
        assert!(legacy.model_data_refresh_on_startup);
    }

    #[test]
    fn dynamic_routing_defaults_off_and_persists_router_settings() {
        let mut config = config_with_one_repo();
        config.normalize();
        assert!(!config.dynamic_model_routing);
        // Router models are filled from the live catalog, not named here.
        assert_eq!(config.provider("claude").unwrap().router_model, "");
        assert_eq!(config.provider("codex").unwrap().router_effort, "low");
        assert_eq!(config.provider("grok").unwrap().router_model, "");
        assert_eq!(
            config.provider("codex").unwrap().strengths,
            provider_strengths_preset("codex")
        );
        // No per-band model table is stored: it is not part of the saved config.
        let saved = serde_json::to_value(&config).unwrap();
        assert!(saved.get("routing_tiers").is_none());
        assert!(config.validate().is_ok());

        config.dynamic_model_routing = true;
        config.provider_mut("grok").unwrap().router_model = "grok-4.6".into();
        config.provider_mut("grok").unwrap().router_effort = "low".into();
        config.provider_mut("grok").unwrap().strengths = "Grok is best at scripting".into();
        let encoded = serde_json::to_string(&config).unwrap();
        let mut decoded: AppConfig = serde_json::from_str(&encoded).unwrap();
        decoded.normalize();
        assert!(decoded.dynamic_model_routing);
        assert_eq!(decoded.provider("grok").unwrap().router_model, "grok-4.6");
        assert_eq!(
            decoded.provider("grok").unwrap().strengths,
            "Grok is best at scripting"
        );
        assert!(decoded.validate().is_ok());

        // Cleared strengths fall back to the built-in description so the
        // router always has something to distinguish the tools by.
        decoded.provider_mut("grok").unwrap().strengths.clear();
        decoded.normalize();
        assert_eq!(
            decoded.provider("grok").unwrap().strengths,
            provider_strengths_preset("grok")
        );

        // The desktop UI no longer sends `strengths` at all; a provider saved
        // without it must come back with the built-in description.
        let mut from_ui: AppConfig = serde_json::from_str(
            r#"{"providers":[{"id":"claude","enabled":true,"model":"claude-sonnet-5",
                "effort":"low","router_model":"claude-haiku-4-5","router_effort":"low","bin":""}]}"#,
        )
        .unwrap();
        from_ui.normalize();
        assert_eq!(
            from_ui.provider("claude").unwrap().strengths,
            provider_strengths_preset("claude")
        );

        // A config saved before tiers were derived still loads; the stored table
        // is ignored and dropped on the next save.
        let legacy = serde_json::json!({
            "dynamic_model_routing": true,
            "routing_tiers": {"claude": [{"min_complexity": 1, "max_complexity": 10,
                "model": "claude-opus-5", "effort": "max"}]}
        });
        let mut legacy: AppConfig = serde_json::from_value(legacy).unwrap();
        legacy.normalize();
        assert!(serde_json::to_value(&legacy)
            .unwrap()
            .get("routing_tiers")
            .is_none());

        let mut older: AppConfig =
            serde_json::from_str(r#"{"dynamic_model_routing": false}"#).unwrap();
        assert!(!older.dynamic_model_routing);
        assert!(older.providers.is_empty());
        older.normalize();
        assert_eq!(older.provider("claude").unwrap().router_model, "");
    }

    #[test]
    fn auto_preference_is_valid_even_when_only_one_provider_is_enabled() {
        let mut config = config_with_one_repo();
        for provider in &mut config.providers {
            provider.enabled = provider.id == "grok";
        }
        config.preferred_provider = "AUTO".into();
        config.repositories[0].preferred_provider = "Auto".into();
        config.normalize();
        assert_eq!(config.preferred_provider, "auto");
        assert_eq!(config.repositories[0].preferred_provider, "auto");
        assert!(config.validate().is_ok());
        assert_eq!(
            config.repositories[0].effective_preferred_provider("claude"),
            "auto"
        );

        config.repositories[0].preferred_provider.clear();
        assert_eq!(
            config.repositories[0].effective_preferred_provider(&config.preferred_provider),
            "auto"
        );
        assert!(config.validate().is_ok());

        config.preferred_provider = "not-a-provider".into();
        config.normalize();
        assert_eq!(config.preferred_provider, "claude");
    }
}
