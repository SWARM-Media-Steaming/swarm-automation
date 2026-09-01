use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

pub const CONFIG_FILE: &str = "config.json";

/// Every AI provider the app knows how to drive. Order here is the default
/// rotation order and the order provider cards render in.
pub const KNOWN_PROVIDERS: [&str; 3] = ["claude", "codex", "grok"];

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
    /// Executable path override; empty means auto-detect on PATH.
    pub bin: String,
}

impl Default for ProviderSettings {
    fn default() -> Self {
        Self {
            id: String::new(),
            enabled: true,
            model: String::new(),
            effort: "high".into(),
            bin: String::new(),
        }
    }
}

impl ProviderSettings {
    fn preset(id: &str) -> Self {
        let (model, effort) = match id {
            "claude" => ("claude-sonnet-5", "high"),
            "codex" => ("gpt-5.6-sol", "high"),
            "grok" => ("grok-4.6", "high"),
            _ => ("", "high"),
        };
        Self {
            id: id.into(),
            enabled: true,
            model: model.into(),
            effort: effort.into(),
            bin: String::new(),
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
/// code reach `base_branch` without a human.
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
    /// First provider tried for a fresh issue in this repo. Empty = use the
    /// global `preferred_provider`.
    pub preferred_provider: String,
    pub auto_approve: bool,
    /// Compatibility mirror of `auto_approve`. Approval and squash-merging are
    /// one UI operation; `base_branch` is never touched automatically.
    pub auto_merge: bool,
    /// Ask the AI to add or update UAT and integration tests for each issue.
    pub require_issue_tests: bool,
    /// Let the AI return a summary without code when the issue is caused by
    /// local environment, credentials, services, or infrastructure state.
    pub allow_environment_only_summary: bool,
    /// Advanced: an existing local checkout to operate on as-is instead of a
    /// managed clone.
    pub repo_dir: String,
    /// Hour of the day (local, 0-23) the test scheduler runs its daily cycle
    /// once **Start** has been pressed. **Run now** ignores it.
    pub uat_hour: u8,
    /// Run the repository-defined `failureTriage` command after a real failure.
    pub uat_triage_enabled: bool,
    /// Repository-local selections used only by the deterministic test runner.
    /// Values are deliberately limited to non-secret discovery choices (for
    /// example an adb serial); credentials remain in the environment/files
    /// declared by the repository test definition.
    pub test_inputs: HashMap<String, String>,
    /// Disruptive suites are visible but blocked until explicitly enabled.
    pub allow_disruptive_tests: bool,
    pub run_dir: String,
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
            require_issue_tests: false,
            allow_environment_only_summary: false,
            repo_dir: String::new(),
            uat_hour: 3,
            uat_triage_enabled: true,
            test_inputs: HashMap::new(),
            allow_disruptive_tests: false,
            run_dir: String::new(),
        }
    }
}

impl RepoConfig {
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

    /// The provider tried first for this repo (`preferred_provider` when set,
    /// else the global default).
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

    /// Where the test runner keeps its state. Defaults to `<workspace>/.run`.
    pub fn effective_run_dir(&self, workspace: &Path) -> PathBuf {
        if self.run_dir.trim().is_empty() {
            workspace.join(".run")
        } else {
            PathBuf::from(&self.run_dir)
        }
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
        if self.uat_hour > 23 {
            return Err("the daily test hour must be between 0 and 23".into());
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

    /// Global default first provider for a repo that does not override it.
    pub preferred_provider: String,
    /// One entry per known provider.
    #[serde(default)]
    pub providers: Vec<ProviderSettings>,

    pub minimum_remaining_percent: u8,
    /// One issue worker per repository, running at the same time, instead of a
    /// single worker that visits each repository in turn. Faster when several
    /// repositories have ready issues, but AI credits are spent faster too.
    /// Off by default.
    pub parallel_repo_workers: bool,
    pub schedule_mode: String,
    pub schedule_time: String,
    pub schedule_days: Vec<String>,
    pub poll_interval_seconds: u64,
    pub email_enabled: bool,
    pub email_to: String,
    pub smtp_credentials_file: String,
    pub worker_state_dir: String,
    pub gh_bin: String,
    pub python_bin: String,

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
    pub allow_environment_only_summary: bool,
    #[serde(default, skip_serializing)]
    pub branch_prefix: String,
    #[serde(default, skip_serializing)]
    pub uat_hour: u8,
    #[serde(default, skip_serializing)]
    pub uat_triage_enabled: bool,
    #[serde(default, skip_serializing)]
    pub run_dir: String,
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
            minimum_remaining_percent: 10,
            parallel_repo_workers: false,
            schedule_mode: "continuous".into(),
            schedule_time: "09:00".into(),
            schedule_days: vec!["mon", "tue", "wed", "thu", "fri"]
                .into_iter()
                .map(str::to_string)
                .collect(),
            poll_interval_seconds: 600,
            email_enabled: false,
            email_to: String::new(),
            smtp_credentials_file: String::new(),
            worker_state_dir: home
                .join(".local/state/swarm-issue-worker")
                .to_string_lossy()
                .into_owned(),
            gh_bin: String::new(),
            python_bin: String::new(),
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
            allow_environment_only_summary: false,
            branch_prefix: String::new(),
            uat_hour: 0,
            uat_triage_enabled: false,
            run_dir: String::new(),
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
            if !self
                .enabled_providers()
                .any(|provider| provider.id == preferred)
            {
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
        if self.email_enabled {
            if self.email_to.trim().is_empty() {
                return Err("Email notifications need a recipient address.".into());
            }
            if self.smtp_credentials_file.trim().is_empty() {
                return Err("Email notifications need an SMTP credentials file.".into());
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
            if provider.enabled && provider.model.trim().is_empty() {
                return Err(format!(
                    "{} model cannot be empty while {} is enabled.",
                    provider_label(&provider.id),
                    provider_label(&provider.id),
                ));
            }
        }
        if self.enabled_providers().next().is_none() {
            return Err("Enable at least one AI provider.".into());
        }
        if !self
            .enabled_providers()
            .any(|provider| provider.id == self.preferred_provider)
        {
            return Err(
                "The global preferred provider must be one of the enabled providers.".into(),
            );
        }
        Ok(())
    }

    pub fn enabled_providers(&self) -> impl Iterator<Item = &ProviderSettings> {
        self.providers.iter().filter(|provider| provider.enabled)
    }

    pub fn provider(&self, id: &str) -> Option<&ProviderSettings> {
        self.providers.iter().find(|provider| provider.id == id)
    }

    #[cfg(test)]
    fn provider_mut(&mut self, id: &str) -> Option<&mut ProviderSettings> {
        self.providers.iter_mut().find(|provider| provider.id == id)
    }

    /// Fold a legacy single-repo / pre-`providers` config forward, guarantee a
    /// full provider set, dedupe + re-key repositories, and clear the
    /// transitional fields so they never round-trip.
    pub fn normalize(&mut self) {
        self.normalize_providers();
        self.normalize_repositories();
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
            let legacy_effort = |value: &str| {
                let value = value.trim();
                if value.is_empty() {
                    "high".to_string()
                } else {
                    value.to_string()
                }
            };
            self.providers = vec![
                ProviderSettings {
                    id: "claude".into(),
                    enabled: true,
                    model: legacy(&self.claude_model, "claude"),
                    effort: legacy_effort(&self.claude_effort),
                    bin: std::mem::take(&mut self.claude_bin),
                },
                ProviderSettings {
                    id: "codex".into(),
                    enabled: true,
                    model: legacy(&self.codex_model, "codex"),
                    effort: legacy_effort(&self.codex_effort),
                    bin: std::mem::take(&mut self.codex_bin),
                },
                ProviderSettings::preset("grok"),
            ];
        }
        for id in KNOWN_PROVIDERS {
            if self.provider(id).is_none() {
                self.providers.push(ProviderSettings::preset(id));
            }
        }
        self.claude_model.clear();
        self.claude_effort.clear();
        self.codex_model.clear();
        self.codex_effort.clear();
        self.claude_bin.clear();
        self.codex_bin.clear();
        if !KNOWN_PROVIDERS.contains(&self.preferred_provider.as_str()) {
            self.preferred_provider = "claude".into();
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
            repo.allow_environment_only_summary = self.allow_environment_only_summary;
            // A migrated config keeps whatever prefix it already used; a fresh
            // repo defaults to "ai".
            if !self.branch_prefix.trim().is_empty() {
                repo.branch_prefix = std::mem::take(&mut self.branch_prefix);
            }
            if self.uat_hour <= 23 {
                repo.uat_hour = self.uat_hour;
            }
            repo.uat_triage_enabled = self.uat_triage_enabled;
            repo.run_dir = std::mem::take(&mut self.run_dir);
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
                kept.push(repo);
            }
        }
        self.repositories = kept;

        // Clear transitional scalars regardless of migration path.
        self.profile_name.clear();
    }
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
        assert!(repo.auto_merge, "approval also enables issue PR merging");
        assert!(!repo.require_issue_tests);
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
        ] {
            assert!(
                !top.contains_key(legacy),
                "top-level legacy key {legacy} must not serialize"
            );
        }
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
        config.provider_mut("claude").unwrap().model.clear();
        assert!(config
            .validate_providers()
            .unwrap_err()
            .contains("model cannot be empty"));
    }
}
