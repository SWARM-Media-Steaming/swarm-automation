use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

pub const CONFIG_FILE: &str = "config.json";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct AppConfig {
    pub profile_name: String,
    pub repo_dir: String,
    pub github_repository: String,
    pub assignee: String,
    pub trusted_followup_authors: Vec<String>,
    pub completion_authors: Vec<String>,
    pub ready_label: String,
    pub base_branch: String,
    pub remote_name: String,
    pub github_host: String,
    pub github_apps_config: String,
    pub require_bot_auth: bool,
    pub delivery_mode: String,
    pub auto_approve: bool,
    pub auto_merge: bool,
    pub merge_method: String,
    pub branch_prefix: String,
    pub preferred_provider: String,
    pub claude_model: String,
    pub claude_effort: String,
    pub codex_model: String,
    pub codex_effort: String,
    pub minimum_remaining_percent: u8,
    pub schedule_mode: String,
    pub schedule_time: String,
    pub schedule_days: Vec<String>,
    pub poll_interval_seconds: u64,
    pub email_enabled: bool,
    pub email_to: String,
    pub smtp_credentials_file: String,
    pub worker_state_dir: String,
    pub claude_bin: String,
    pub codex_bin: String,
    pub gh_bin: String,
    pub python_bin: String,
    pub uat_enabled: bool,
    pub uat_hour: u8,
    pub uat_issue_label: String,
    pub uat_batocera_host: String,
    pub uat_triage_enabled: bool,
    pub run_dir: String,
}

impl Default for AppConfig {
    fn default() -> Self {
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
        Self {
            profile_name: "My repository".into(),
            repo_dir: String::new(),
            github_repository: String::new(),
            assignee: String::new(),
            trusted_followup_authors: Vec::new(),
            completion_authors: Vec::new(),
            ready_label: "Ready For Testing".into(),
            base_branch: "main".into(),
            remote_name: "origin".into(),
            github_host: "github.com".into(),
            github_apps_config: home
                .join(".config/swarm/github-apps.json")
                .to_string_lossy()
                .into_owned(),
            require_bot_auth: true,
            delivery_mode: "pull-request".into(),
            auto_approve: true,
            auto_merge: true,
            merge_method: "merge".into(),
            branch_prefix: "swarm".into(),
            preferred_provider: "claude".into(),
            claude_model: "claude-sonnet-5".into(),
            claude_effort: "high".into(),
            codex_model: "gpt-5.6-sol".into(),
            codex_effort: "high".into(),
            minimum_remaining_percent: 10,
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
            claude_bin: String::new(),
            codex_bin: String::new(),
            gh_bin: String::new(),
            python_bin: String::new(),
            uat_enabled: true,
            uat_hour: 3,
            uat_issue_label: "Testing".into(),
            uat_batocera_host: "batocera.local".into(),
            uat_triage_enabled: true,
            run_dir: String::new(),
        }
    }
}

impl AppConfig {
    pub fn validate(&self) -> Result<(), String> {
        let repo = Path::new(self.repo_dir.trim());
        if self.repo_dir.trim().is_empty() || !repo.is_dir() {
            return Err("Choose an existing local repository folder.".into());
        }
        if !repo.join(".git").exists() {
            return Err("The selected folder is not a Git checkout (.git was not found).".into());
        }
        let repository = self.github_repository.trim();
        if repository.split('/').count() != 2
            || repository.starts_with('/')
            || repository.ends_with('/')
        {
            return Err("GitHub repository must use owner/name format.".into());
        }
        if self.assignee.trim().is_empty() {
            return Err("Set the GitHub assignee whose issues the worker may select.".into());
        }
        if !matches!(self.preferred_provider.as_str(), "claude" | "codex") {
            return Err("Preferred provider must be claude or codex.".into());
        }
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
        if self.uat_hour > 23 {
            return Err("UAT hour must be between 0 and 23.".into());
        }
        if !matches!(self.delivery_mode.as_str(), "pull-request" | "local-main") {
            return Err("Unknown delivery mode.".into());
        }
        if self.delivery_mode != "pull-request" && (self.auto_approve || self.auto_merge) {
            return Err("Automatic approval and merge require pull-request delivery.".into());
        }
        if !matches!(self.merge_method.as_str(), "merge" | "squash" | "rebase") {
            return Err("Unknown pull-request merge method.".into());
        }
        for (label, value) in [
            ("Claude model", &self.claude_model),
            ("Codex model", &self.codex_model),
            ("base branch", &self.base_branch),
            ("remote", &self.remote_name),
        ] {
            if value.trim().is_empty() {
                return Err(format!("{label} cannot be empty."));
            }
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

    pub fn effective_run_dir(&self) -> PathBuf {
        if self.run_dir.trim().is_empty() {
            Path::new(&self.repo_dir).join(".run")
        } else {
            PathBuf::from(&self.run_dir)
        }
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
    std::fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default()
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

    #[test]
    fn time_validation_is_strict() {
        assert!(validate_time("03:00").is_ok());
        assert!(validate_time("3:00").is_err());
        assert!(validate_time("24:00").is_err());
    }
}
