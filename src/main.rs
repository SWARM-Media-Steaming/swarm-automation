mod config;
mod processes;
mod tools;

use config::{AppConfig, RepoConfig, CONFIG_FILE};
use processes::{ProcessManager, ProcessStatus};
use serde::Serialize;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Mutex;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Manager, State};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;

const MAIN_WINDOW: &str = "main";
const SMTP_KEYRING_SERVICE: &str = "app.swarm.automation";
const SMTP_KEYRING_ACCOUNT: &str = "smtp-password";
const REQUIRED_WORKER_RESOURCES: [&str; 6] = [
    "install_swarm_issue_cron.py",
    "swarm_issue_worker.py",
    "github_app_auth.py",
    "setup_github_bots.py",
    "codex_rate_limits.py",
    "send_issue_notification.py",
];

struct AppState {
    config: Mutex<AppConfig>,
    processes: ProcessManager,
    /// Cached presence of the optional SMTP password. Checking Keychain can
    /// trigger a macOS access prompt, so status polling must not probe it every
    /// two seconds.
    smtp_password_configured: Mutex<Option<bool>>,
    /// Test-only override for `app_config_path`/`automation_log_path`.
    /// `mock_context()`'s identifier defaults to empty, so every test would
    /// otherwise resolve to the same shared OS path; this gives each test's
    /// own `AppState` instance a genuinely unique temp directory instead.
    /// Always `None` in production — see apps/server/src/gui.rs's
    /// `test_data_dir` for the same pattern.
    test_data_dir: Option<PathBuf>,
}

impl Default for AppState {
    fn default() -> Self {
        Self {
            config: Mutex::new(AppConfig::default()),
            processes: ProcessManager::default(),
            smtp_password_configured: Mutex::new(None),
            test_data_dir: None,
        }
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct RepositoryInspection {
    path: String,
    valid: bool,
    branch: String,
    github_repository: String,
    dirty: bool,
    worker_available: bool,
    uat_available: bool,
    error: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct RepoStatus {
    id: String,
    label: String,
    github_repository: String,
    enabled: bool,
    /// Per-repo UAT scheduler process (slot `uat:<id>`).
    uat: ProcessStatus,
    /// Absolute path of the working copy for this repo (a managed clone or the
    /// advanced override).
    workspace_path: String,
    /// True once that path is a real Git checkout on disk.
    workspace_ready: bool,
    /// True when the app manages the clone (no `repo_dir` override).
    workspace_managed: bool,
    worker_available: bool,
    uat_available: bool,
    bot_config_exists: bool,
    /// Per-repo validation error, if any.
    repo_config_error: String,
    repository: RepositoryInspection,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct AutomationStatus {
    /// The single rotating issue-worker scheduler (services every enabled repo).
    issue: ProcessStatus,
    /// Shared one-off task slot (installs, bot setup).
    task: ProcessStatus,
    scheduler_repo_count: usize,
    repos: Vec<RepoStatus>,
    bot_config_exists: bool,
    smtp_password_configured: bool,
    /// Global (non-repo) validation error, if any.
    config_error: String,
    log_path: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct BotVerification {
    provider: String,
    configured: bool,
    valid: bool,
    message: String,
}

fn app_config_path<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<PathBuf, String> {
    if let Some(state) = app.try_state::<AppState>() {
        if let Some(dir) = &state.test_data_dir {
            return Ok(dir.join(CONFIG_FILE));
        }
    }
    app.path()
        .app_config_dir()
        .map(|directory| directory.join(CONFIG_FILE))
        .map_err(|error| error.to_string())
}

fn automation_log_path<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<PathBuf, String> {
    if let Some(state) = app.try_state::<AppState>() {
        if let Some(dir) = &state.test_data_dir {
            return Ok(dir.join("logs/automation.log"));
        }
    }
    app.path()
        .app_data_dir()
        .map(|directory| directory.join("logs/automation.log"))
        .map_err(|error| error.to_string())
}

fn current_config(state: &State<'_, AppState>) -> Result<AppConfig, String> {
    state
        .config
        .lock()
        .map(|config| config.clone())
        .map_err(|_| "Configuration state lock was poisoned".into())
}

fn cached_smtp_password_configured(
    state: &AppState,
    probe: impl FnOnce() -> bool,
) -> Result<bool, String> {
    if let Some(configured) = *state
        .smtp_password_configured
        .lock()
        .map_err(|_| "SMTP password state lock was poisoned".to_string())?
    {
        return Ok(configured);
    }
    let configured = probe();
    *state
        .smtp_password_configured
        .lock()
        .map_err(|_| "SMTP password state lock was poisoned".to_string())? = Some(configured);
    Ok(configured)
}

fn set_cached_smtp_password_configured(state: &AppState, configured: bool) -> Result<(), String> {
    *state
        .smtp_password_configured
        .lock()
        .map_err(|_| "SMTP password state lock was poisoned".to_string())? = Some(configured);
    Ok(())
}

#[tauri::command]
fn get_config(state: State<'_, AppState>) -> Result<AppConfig, String> {
    current_config(&state)
}

#[tauri::command]
fn save_config<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    state: State<'_, AppState>,
    mut config: AppConfig,
) -> Result<AppConfig, String> {
    // Fold any legacy shape and guarantee a full provider set before persisting.
    config.normalize();
    config::save(&app_config_path(&app)?, &config)?;
    *state
        .config
        .lock()
        .map_err(|_| "Configuration state lock was poisoned".to_string())? = config.clone();
    Ok(config)
}

#[tauri::command]
async fn choose_repository(app: tauri::AppHandle) -> Result<Option<RepositoryInspection>, String> {
    let (sender, receiver) = tokio_oneshot();
    app.dialog().file().pick_folder(move |folder| {
        let _ = sender.send(folder);
    });
    let selected = receiver.recv().map_err(|error| error.to_string())?;
    Ok(selected.map(|folder| {
        let path = folder.to_string();
        inspect_repository_path(Path::new(&path))
    }))
}

// tauri-plugin-dialog callbacks are synchronous from this application's
// perspective. A std channel avoids adding an async runtime solely for a
// native picker while still keeping the command's JS contract asynchronous.
fn tokio_oneshot<T>() -> (std::sync::mpsc::Sender<T>, std::sync::mpsc::Receiver<T>) {
    std::sync::mpsc::channel()
}

#[tauri::command]
fn inspect_repository(path: String) -> RepositoryInspection {
    inspect_repository_path(Path::new(&path))
}

fn inspect_repository_path(path: &Path) -> RepositoryInspection {
    let canonical = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());
    let mut inspection = RepositoryInspection {
        path: canonical.to_string_lossy().into_owned(),
        valid: false,
        branch: String::new(),
        github_repository: String::new(),
        dirty: false,
        worker_available: canonical
            .join("scripts/issue_worker/install_swarm_issue_cron.py")
            .is_file(),
        uat_available: canonical.join("scripts/tests/full_uat_cron.sh").is_file(),
        error: String::new(),
    };
    if !canonical.is_dir() {
        inspection.error = "Folder does not exist.".into();
        return inspection;
    }
    let git = match tools::find_executable("git", "") {
        Some(git) => git,
        None => {
            inspection.error = "Git is not installed.".into();
            return inspection;
        }
    };
    let inside = run_capture(
        &git,
        &["-C", &inspection.path, "rev-parse", "--is-inside-work-tree"],
    );
    if !inside.0 {
        inspection.error = "Folder is not a Git checkout.".into();
        return inspection;
    }
    inspection.valid = true;
    inspection.branch = run_capture(&git, &["-C", &inspection.path, "branch", "--show-current"])
        .1
        .trim()
        .to_string();
    inspection.dirty = !run_capture(&git, &["-C", &inspection.path, "status", "--porcelain"])
        .1
        .trim()
        .is_empty();
    let remote = run_capture(
        &git,
        &["-C", &inspection.path, "remote", "get-url", "origin"],
    )
    .1;
    inspection.github_repository = github_slug(&remote).unwrap_or_default();
    inspection
}

fn github_slug(remote: &str) -> Option<String> {
    let trimmed = remote.trim().trim_end_matches(".git");
    let path = if let Some((_, path)) = trimmed.rsplit_once("github.com:") {
        path
    } else if let Some((_, path)) = trimmed.rsplit_once("github.com/") {
        path
    } else {
        return None;
    };
    (path.split('/').count() == 2).then(|| path.to_string())
}

#[tauri::command]
fn detect_tools(state: State<'_, AppState>) -> Result<Vec<tools::ToolInfo>, String> {
    let config = current_config(&state)?;
    let host = config
        .repositories()
        .first()
        .map(repo_host)
        .unwrap_or_else(|| "github.com".into());
    Ok(tools::detect(&config, &host))
}

#[tauri::command]
async fn detect_tools_background(app: tauri::AppHandle) -> Result<Vec<tools::ToolInfo>, String> {
    tauri::async_runtime::spawn_blocking(move || detect_tools(app.state()))
        .await
        .map_err(|error| format!("Tool detection background task failed: {error}"))?
}

#[tauri::command]
fn get_automation_status<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    state: State<'_, AppState>,
) -> Result<AutomationStatus, String> {
    let config = current_config(&state)?;
    let log_path = automation_log_path(&app)?;
    let worker_available = worker_script_dir(&app).is_ok();
    let mut repos = Vec::new();
    for repo in config.repositories() {
        let managed = repo.repo_dir.trim().is_empty();
        let workspace = resolve_workspace(&app, &config, repo).unwrap_or_default();
        let mut repository = inspect_repository_path(&workspace);
        let workspace_ready = repository.valid;
        if !workspace_ready && managed {
            repository.error = "Not cloned yet — press Clone / update in Repository.".into();
        }
        repos.push(RepoStatus {
            id: repo.id.clone(),
            label: repo.label(),
            github_repository: repo.github_repository.clone(),
            enabled: repo.enabled,
            uat: state.processes.status(
                &app,
                &format!("uat:{}", repo.id),
                &format!("UAT scheduler · {}", repo.label()),
                &log_path,
            )?,
            workspace_path: workspace.to_string_lossy().into_owned(),
            workspace_ready,
            workspace_managed: managed,
            worker_available,
            uat_available: repository.uat_available,
            bot_config_exists: Path::new(&repo.effective_apps_config()).is_file(),
            repo_config_error: repo_error(&config, repo),
            repository,
        });
    }
    Ok(AutomationStatus {
        issue: state
            .processes
            .status(&app, "issue", "Issue worker scheduler", &log_path)?,
        task: state
            .processes
            .status(&app, "task", "Setup task", &log_path)?,
        scheduler_repo_count: config.enabled_repos().count(),
        bot_config_exists: config
            .repositories()
            .iter()
            .all(|repo| Path::new(&repo.effective_apps_config()).is_file()),
        repos,
        smtp_password_configured: cached_smtp_password_configured(state.inner(), || {
            smtp_password().is_ok()
        })?,
        config_error: config.validate().err().unwrap_or_default(),
        log_path: log_path.to_string_lossy().into_owned(),
    })
}

#[tauri::command]
async fn get_automation_status_background(
    app: tauri::AppHandle,
) -> Result<AutomationStatus, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        get_automation_status(app.clone(), state)
    })
    .await
    .map_err(|error| format!("Status refresh background task failed: {error}"))?
}

/// The validation message for a single repo (empty when it is fine), so the UI
/// can flag exactly which repo card needs attention.
fn repo_error(config: &AppConfig, repo: &RepoConfig) -> String {
    match config.validate() {
        Ok(()) => String::new(),
        Err(message) => {
            let needle = format!("Repository {}:", repo.label());
            if message.starts_with(&needle) {
                message[needle.len()..]
                    .trim()
                    .trim_end_matches('.')
                    .to_string()
            } else {
                String::new()
            }
        }
    }
}

#[tauri::command]
fn start_issue_worker(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    run_once: bool,
) -> Result<ProcessStatus, String> {
    let config = current_config(&state)?;
    config.validate()?;
    if config.schedule_mode == "manual" && !run_once {
        return Err(
            "The schedule is set to Manual only. Use Run now or choose a recurring schedule."
                .into(),
        );
    }
    let providers = resolve_providers(&config);
    if providers
        .iter()
        .filter(|provider| provider.enabled)
        .all(|provider| provider.bin.as_os_str().is_empty())
    {
        return Err(
            "Install and sign in to at least one enabled AI provider (Claude, Codex, or Grok) \
             before starting the worker."
                .into(),
        );
    }

    let python = tools::configured_or_detected(&config.python_bin, "python3")?;
    let git = tools::configured_or_detected("", "git")?;
    let gh = tools::configured_or_detected(&config.gh_bin, "gh")?;
    let state_root = PathBuf::from(&config.worker_state_dir);

    // Ensure every enabled repo's workspace, then build one spec entry each.
    let mut spec = Vec::new();
    // The desktop app and its Python worker form one versioned unit. Never
    // borrow automation scripts from a monitored repository: that copy may
    // implement a different command-line interface.
    let script_dir = worker_script_dir(&app)?;
    for repo in config.enabled_repos() {
        let workspace = prepared_workspace(&app, &config, repo)?;
        let repo_state = state_root.join(&repo.id);
        std::fs::create_dir_all(&repo_state).map_err(|error| error.to_string())?;
        spec.push(serde_json::json!({
            "label": repo.label(),
            "workspace_dir": workspace.to_string_lossy(),
            "state_dir": repo_state.to_string_lossy(),
            "base_branch": repo.base_branch,
            "remote_name": repo.remote_name,
            "integration_branch": repo.integration_branch,
            "worker_args": repo_worker_args(&config, repo, &workspace, &git, &gh),
        }));
    }
    if spec.is_empty() {
        return Err("Enable at least one repository before starting the worker.".into());
    }
    let runner = script_dir.join("install_swarm_issue_cron.py");

    std::fs::create_dir_all(&state_root).map_err(|error| error.to_string())?;
    let repos_file = state_root.join("repos.json");
    std::fs::write(
        &repos_file,
        serde_json::to_vec_pretty(&spec).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;

    let mut arguments = scheduler_arguments(&config, &runner, &python, &git, &repos_file, run_once);
    for provider in &providers {
        arguments.extend([format!("--{}-model", provider.id), provider.model.clone()]);
        arguments.extend([format!("--{}-effort", provider.id), provider.effort.clone()]);
        arguments.extend([
            format!("--{}-bin", provider.id),
            provider.bin.to_string_lossy().into_owned(),
        ]);
        if provider.enabled {
            arguments.extend(["--enabled-provider".into(), provider.id.clone()]);
        }
    }
    arguments.extend([
        "--minimum-remaining-percent".into(),
        config.minimum_remaining_percent.to_string(),
        "--preferred-provider".into(),
        config.preferred_provider.clone(),
        "--gh-bin".into(),
        gh.to_string_lossy().into_owned(),
    ]);
    let mut environment = vec![
        ("PATH".into(), tools::enhanced_path()),
        (
            "SWARM_ISSUE_WORKER_SCRIPT_DIR".into(),
            script_dir.to_string_lossy().into_owned(),
        ),
    ];
    if config.email_enabled {
        let password = smtp_password()?;
        set_cached_smtp_password_configured(state.inner(), true)?;
        environment.push(("SWARM_SMTP_PASSWORD".into(), password));
        arguments.extend([
            "--smtp-credentials-file".into(),
            config.smtp_credentials_file.clone(),
            "--email-to".into(),
            config.email_to.clone(),
        ]);
    } else {
        arguments.push("--no-email".into());
    }

    state.processes.spawn(
        &app,
        "issue",
        "Issue worker scheduler",
        &python,
        &arguments,
        &environment,
        &state_root,
        automation_log_path(&app)?,
    )
}

/// A provider with its executable resolved to a concrete path (empty when the
/// CLI is not installed / not on PATH). `enabled` mirrors the config switch —
/// the worker is handed every known provider's details so it can still resume
/// an issue paused on a provider the user has since excluded, but only selects
/// from the enabled set for new work.
struct ResolvedProvider {
    id: String,
    model: String,
    effort: String,
    bin: PathBuf,
    enabled: bool,
}

fn resolve_providers(config: &AppConfig) -> Vec<ResolvedProvider> {
    config
        .providers
        .iter()
        .map(|provider| ResolvedProvider {
            id: provider.id.clone(),
            model: provider.model.clone(),
            effort: provider.effort.clone(),
            bin: tools::find_executable(&provider.id, &provider.bin).unwrap_or_default(),
            enabled: provider.enabled,
        })
        .collect()
}

/// Global scheduler flags for `install_swarm_issue_cron.py`. Per-repo detail
/// lives in the `--repos-file`; unknown provider/email flags added by the
/// caller are forwarded to every repo's worker invocation.
fn scheduler_arguments(
    config: &AppConfig,
    runner: &Path,
    python: &Path,
    git: &Path,
    repos_file: &Path,
    run_once: bool,
) -> Vec<String> {
    let mut arguments = vec![
        runner.to_string_lossy().into_owned(),
        "--repos-file".into(),
        repos_file.to_string_lossy().into_owned(),
        "--state-dir".into(),
        config.worker_state_dir.clone(),
        "--python-bin".into(),
        python.to_string_lossy().into_owned(),
        "--git-bin".into(),
        git.to_string_lossy().into_owned(),
        "--interval-seconds".into(),
        config.poll_interval_seconds.to_string(),
        "--schedule-mode".into(),
        if config.schedule_mode == "manual" {
            "continuous".into()
        } else {
            config.schedule_mode.clone()
        },
        "--schedule-time".into(),
        config.schedule_time.clone(),
        "--schedule-days".into(),
        config.schedule_days.join(","),
    ];
    if run_once {
        arguments.push("--once".into());
    }
    arguments
}

/// `swarm_issue_worker.py` flag list for one repo, embedded in `repos.json`.
fn repo_worker_args(
    config: &AppConfig,
    repo: &RepoConfig,
    workspace: &Path,
    git: &Path,
    gh: &Path,
) -> Vec<String> {
    let state_dir = PathBuf::from(&config.worker_state_dir).join(&repo.id);
    let mut arguments = vec![
        "--repo-dir".into(),
        workspace.to_string_lossy().into_owned(),
        "--state-dir".into(),
        state_dir.to_string_lossy().into_owned(),
        "--git-bin".into(),
        git.to_string_lossy().into_owned(),
        "--gh-bin".into(),
        gh.to_string_lossy().into_owned(),
        "--base-branch".into(),
        repo.base_branch.clone(),
        "--integration-branch".into(),
        repo.integration_branch.clone(),
        "--remote-name".into(),
        repo.remote_name.clone(),
        "--branch-prefix".into(),
        repo.branch_prefix.clone(),
        "--github-repository".into(),
        repo.github_repository.clone(),
        "--assignee".into(),
        repo.assignee.clone(),
        "--ready-label".into(),
        repo.ready_label.clone(),
        "--github-host".into(),
        repo_host(repo),
        "--github-apps-config".into(),
        repo.effective_apps_config(),
        "--preferred-provider".into(),
        repo.effective_preferred_provider(&config.preferred_provider)
            .to_string(),
        if repo.require_bot_auth {
            "--require-bot-auth"
        } else {
            "--no-require-bot-auth"
        }
        .into(),
        if repo.auto_approve {
            "--auto-approve"
        } else {
            "--no-auto-approve"
        }
        .into(),
        if repo.auto_merge {
            "--auto-merge"
        } else {
            "--no-auto-merge"
        }
        .into(),
        if repo.require_issue_tests {
            "--require-issue-tests"
        } else {
            "--no-require-issue-tests"
        }
        .into(),
        if repo.allow_environment_only_summary {
            "--allow-environment-only-summary"
        } else {
            "--no-allow-environment-only-summary"
        }
        .into(),
    ];
    // A blank list is a genuine "trust no one" — fall back to the assignee so
    // a first run works without filling in two more fields.
    let trusted = if repo.trusted_followup_authors.is_empty() {
        std::slice::from_ref(&repo.assignee)
    } else {
        repo.trusted_followup_authors.as_slice()
    };
    for author in trusted {
        arguments.extend(["--trusted-followup-author".into(), author.clone()]);
    }
    let completion = if repo.completion_authors.is_empty() {
        std::slice::from_ref(&repo.assignee)
    } else {
        repo.completion_authors.as_slice()
    };
    for author in completion {
        arguments.extend(["--completion-author".into(), author.clone()]);
    }
    arguments
}

#[tauri::command]
fn start_uat_scheduler(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    repo_id: String,
    run_once: bool,
) -> Result<ProcessStatus, String> {
    let config = current_config(&state)?;
    config.validate()?;
    let repo = resolve_repo(&config, &repo_id)?;
    if !repo.uat_enabled {
        return Err(format!("UAT scheduling is disabled for {}.", repo.label()));
    }
    let workspace = prepared_workspace(&app, &config, repo)?;
    let script = workspace.join("scripts/tests/full_uat_cron.sh");
    if !script.is_file() {
        return Err("This repository does not contain scripts/tests/full_uat_cron.sh.".into());
    }
    let mut arguments = vec![script.to_string_lossy().into_owned()];
    if run_once {
        arguments.push("--once".into());
    }
    let environment = uat_environment(&config, repo, &workspace);
    state.processes.spawn(
        &app,
        &format!("uat:{}", repo.id),
        &format!("UAT scheduler · {}", repo.label()),
        Path::new("/bin/bash"),
        &arguments,
        &environment,
        &workspace,
        automation_log_path(&app)?,
    )
}

fn uat_environment(
    config: &AppConfig,
    repo: &RepoConfig,
    workspace: &Path,
) -> Vec<(String, String)> {
    let mut environment = vec![
        ("PATH".into(), tools::enhanced_path()),
        ("SWARM_FULL_UAT_CRON_HOUR".into(), repo.uat_hour.to_string()),
        (
            "SWARM_GITHUB_REPOSITORY".into(),
            repo.github_repository.clone(),
        ),
        ("SWARM_E2E_ISSUE_LABEL".into(), repo.uat_issue_label.clone()),
        (
            "SWARM_RUN_DIR".into(),
            repo.effective_run_dir(workspace)
                .to_string_lossy()
                .into_owned(),
        ),
        (
            "SWARM_UAT_BATOCERA_HOST".into(),
            repo.uat_batocera_host.clone(),
        ),
        (
            "SWARM_UAT_TRIAGE_ENABLED".into(),
            if repo.uat_triage_enabled { "1" } else { "0" }.into(),
        ),
        (
            "SWARM_MIN_REMAINING_PERCENT".into(),
            config.minimum_remaining_percent.to_string(),
        ),
    ];
    // Per-enabled-provider model / effort / executable for the frozen UAT
    // runner (it reads SWARM_<ID>_MODEL / SWARM_<ID>_EFFORT / <ID>_BIN).
    for provider in config.enabled_providers() {
        let upper = provider.id.to_uppercase();
        environment.push((format!("SWARM_{upper}_MODEL"), provider.model.clone()));
        environment.push((format!("SWARM_{upper}_EFFORT"), provider.effort.clone()));
        if let Some(path) = tools::find_executable(&provider.id, &provider.bin) {
            environment.push((format!("{upper}_BIN"), path.to_string_lossy().into_owned()));
        }
    }
    environment
}

#[tauri::command]
fn pause_process(state: State<'_, AppState>, process: String) -> Result<ProcessStatus, String> {
    state.processes.pause(&process)
}

#[tauri::command]
fn resume_process(state: State<'_, AppState>, process: String) -> Result<ProcessStatus, String> {
    state.processes.resume(&process)
}

#[tauri::command]
fn stop_process(state: State<'_, AppState>, process: String) -> Result<ProcessStatus, String> {
    state.processes.stop(&process)
}

#[tauri::command]
fn install_ai_cli(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    provider: String,
) -> Result<ProcessStatus, String> {
    let config = current_config(&state)?;
    // Grok Build has no npm package — run its official installer in a visible
    // Terminal window (same mechanism as provider sign-in) so the user sees
    // exactly what executes.
    if provider == "grok" {
        open_terminal_command("curl -fsSL https://x.ai/cli/install.sh | bash")?;
        return state
            .processes
            .status(&app, "task", "Install grok", &automation_log_path(&app)?);
    }
    let (program, arguments) = tools::install_spec(&provider)?;
    let workspace = config
        .repositories()
        .first()
        .and_then(|repo| resolve_workspace(&app, &config, repo).ok())
        .unwrap_or_default();
    state.processes.spawn(
        &app,
        "task",
        &format!("Install {provider}"),
        &program,
        &arguments,
        &[("PATH".into(), tools::enhanced_path())],
        repo_or_home(&workspace),
        automation_log_path(&app)?,
    )
}

#[tauri::command]
fn launch_bot_setup(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    repo_id: String,
) -> Result<ProcessStatus, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?;
    let workspace = resolve_workspace(&app, &config, repo).unwrap_or_default();
    let script = worker_script_dir(&app)?.join("setup_github_bots.py");
    let python = tools::configured_or_detected(&config.python_bin, "python3")?;
    let log_path = automation_log_path(&app)?;
    let running = state
        .processes
        .status(&app, "task", "Setup task", &log_path)?;
    if running.state != "stopped" {
        if running.detail.contains("setup_github_bots.py") {
            // Treat another click as "reopen/restart setup". The old loopback
            // page becomes invalid, and the newly spawned assistant opens a
            // fresh page with the configuration already saved so far.
            state.processes.stop("task")?;
        } else {
            return Err("Another setup or installation task is already running.".into());
        }
    }
    let mut arguments = vec![
        script.to_string_lossy().into_owned(),
        "--repository".into(),
        repo.github_repository.clone(),
        "--config".into(),
        repo.effective_apps_config(),
    ];
    for provider in config.enabled_providers() {
        arguments.extend(["--provider".into(), provider.id.clone()]);
    }
    state.processes.spawn(
        &app,
        "task",
        &format!("GitHub bot setup · {}", repo.label()),
        &python,
        &arguments,
        &[("PATH".into(), tools::enhanced_path())],
        repo_or_home(&workspace),
        log_path,
    )
}

#[tauri::command]
fn verify_github_bots(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    repo_id: String,
) -> Result<Vec<BotVerification>, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?;
    let script = worker_script_dir(&app)?.join("github_app_auth.py");
    let python = tools::configured_or_detected(&config.python_bin, "python3")?;
    let apps_config = repo.effective_apps_config();
    let providers: Vec<String> = config
        .enabled_providers()
        .map(|provider| provider.id.clone())
        .collect();
    Ok(providers
        .into_iter()
        .map(|provider| {
            if !Path::new(&apps_config).is_file() {
                return BotVerification {
                    provider: provider.clone(),
                    configured: false,
                    valid: false,
                    message: format!(
                        "Local bot credentials were not found at {apps_config}. Existing GitHub Apps still need their app ID, installation ID, and private PEM key linked here."
                    ),
                };
            }
            let (valid, message) = run_capture_owned(
                &python,
                &[
                    script.to_string_lossy().into_owned(),
                    "--config".into(),
                    apps_config.clone(),
                    "check".into(),
                    "--provider".into(),
                    provider.clone(),
                ],
            );
            BotVerification {
                provider,
                configured: true,
                valid,
                message: message.trim().to_string(),
            }
        })
        .collect())
}

// ----- Branch tree + manual merge -----------------------------------------

#[derive(Serialize, Default)]
#[serde(rename_all = "camelCase")]
struct CommitTip {
    sha: String,
    subject: String,
    author: String,
    committed_at: String,
}

#[derive(Serialize, Default)]
#[serde(rename_all = "camelCase")]
struct BranchAheadBehind {
    ahead: u32,
    behind: u32,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct IssueBranchInfo {
    name: String,
    ai_tool: String,
    issue_number: u64,
    issue_state: String,
    ahead_of_integration: u32,
    behind_integration: u32,
    last_commit: CommitTip,
    pr_number: Option<u64>,
    pr_url: String,
    pr_state: String,
    mergeable: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct RepoGitOverview {
    repo_id: String,
    github_repository: String,
    base_branch: String,
    integration_branch: String,
    workspace_ready: bool,
    /// `origin/<base>` tip.
    base_tip: CommitTip,
    /// Does `origin/<integration>` exist yet?
    integration_exists: bool,
    /// `origin/<integration>` relative to `origin/<base>`.
    integration_vs_base: BranchAheadBehind,
    integration_tip: CommitTip,
    /// Open `ai-main -> main` PR, if any.
    integration_pr_url: String,
    integration_pr_number: Option<u64>,
    issue_branches: Vec<IssueBranchInfo>,
    /// `git log --graph` text for the tree view's raw toggle.
    graph: String,
    error: String,
}

fn git_c(git: &Path, workspace: &str, args: &[&str]) -> (bool, String) {
    let mut full = vec!["-C", workspace];
    full.extend_from_slice(args);
    run_capture(git, &full)
}

fn commit_tip(git: &Path, workspace: &str, refname: &str) -> CommitTip {
    let (ok, out) = git_c(
        git,
        workspace,
        &["log", "-1", "--format=%H%x1f%s%x1f%an%x1f%cI", refname],
    );
    if !ok {
        return CommitTip::default();
    }
    let mut parts = out.splitn(4, '\u{1f}');
    CommitTip {
        sha: parts.next().unwrap_or_default().to_string(),
        subject: parts.next().unwrap_or_default().to_string(),
        author: parts.next().unwrap_or_default().to_string(),
        committed_at: parts.next().unwrap_or_default().to_string(),
    }
}

fn ahead_behind(git: &Path, workspace: &str, left: &str, right: &str) -> BranchAheadBehind {
    // `git rev-list --left-right --count L...R` -> "behind\tahead" for R vs L.
    let (ok, out) = git_c(
        git,
        workspace,
        &[
            "rev-list",
            "--left-right",
            "--count",
            &format!("{left}...{right}"),
        ],
    );
    if !ok {
        return BranchAheadBehind::default();
    }
    let mut nums = out.split_whitespace();
    let behind = nums.next().and_then(|n| n.parse().ok()).unwrap_or(0);
    let ahead = nums.next().and_then(|n| n.parse().ok()).unwrap_or(0);
    BranchAheadBehind { ahead, behind }
}

fn require_closed_issue(
    issue_number: u64,
    issue_state: &str,
    integration_branch: &str,
) -> Result<(), String> {
    if issue_state.trim().eq_ignore_ascii_case("closed") {
        Ok(())
    } else {
        Err(format!(
            "Issue #{issue_number} must be closed before its branch can be merged into {integration_branch}."
        ))
    }
}

fn issue_branch_is_complete(issue_state: &str, pull_request_state: &str) -> bool {
    issue_state.trim().eq_ignore_ascii_case("closed")
        && pull_request_state.trim().eq_ignore_ascii_case("merged")
}

#[tauri::command]
fn git_overview(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    repo_id: String,
) -> Result<RepoGitOverview, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?.clone();
    let git = tools::configured_or_detected("", "git")?;
    let gh = tools::configured_or_detected(&config.gh_bin, "gh").ok();
    let workspace = resolve_workspace(&app, &config, &repo)?;
    let ws = workspace.to_string_lossy().into_owned();

    let mut overview = RepoGitOverview {
        repo_id: repo.id.clone(),
        github_repository: repo.github_repository.clone(),
        base_branch: repo.base_branch.clone(),
        integration_branch: repo.integration_branch.clone(),
        workspace_ready: workspace.join(".git").is_dir(),
        base_tip: CommitTip::default(),
        integration_exists: false,
        integration_vs_base: BranchAheadBehind::default(),
        integration_tip: CommitTip::default(),
        integration_pr_url: String::new(),
        integration_pr_number: None,
        issue_branches: Vec::new(),
        graph: String::new(),
        error: String::new(),
    };
    if !overview.workspace_ready {
        overview.error = "Not cloned yet — clone the repository in Repository first.".into();
        return Ok(overview);
    }

    let (fetch_ok, fetch_message) = git_c(&git, &ws, &["fetch", "--prune", &repo.remote_name]);
    if !fetch_ok {
        overview.error = format!(
            "Could not refresh {}: {}. Showing cached branch data.",
            repo.remote_name, fetch_message
        );
    }
    let base_ref = format!("{}/{}", repo.remote_name, repo.base_branch);
    let integ_ref = format!("{}/{}", repo.remote_name, repo.integration_branch);
    overview.base_tip = commit_tip(&git, &ws, &base_ref);
    overview.integration_exists =
        git_c(&git, &ws, &["rev-parse", "--verify", "--quiet", &integ_ref]).0;
    if overview.integration_exists {
        overview.integration_tip = commit_tip(&git, &ws, &integ_ref);
        overview.integration_vs_base = ahead_behind(&git, &ws, &base_ref, &integ_ref);
    }

    // Issue branches: refs/remotes/<remote>/<prefix>/<ai>/issue-<n>
    let prefix = format!("{}/{}/", repo.remote_name, repo.branch_prefix);
    let (_, refs) = git_c(
        &git,
        &ws,
        &["for-each-ref", "--format=%(refname:short)", "refs/remotes"],
    );
    let mut branches: Vec<(String, String, u64)> = Vec::new();
    for line in refs.lines() {
        let line = line.trim();
        let Some(rest) = line.strip_prefix(&prefix) else {
            continue;
        };
        let Some((ai, tail)) = rest.split_once('/') else {
            continue;
        };
        let Some(num) = tail.strip_prefix("issue-") else {
            continue;
        };
        if let Ok(number) = num.parse::<u64>() {
            branches.push((line.to_string(), ai.to_string(), number));
        }
    }
    branches.sort_by_key(|(_, _, n)| *n);

    // One `gh pr list` call, joined by head ref.
    let mut pr_by_head: std::collections::HashMap<String, (u64, String, String, String)> =
        std::collections::HashMap::new();
    let mut issue_states: std::collections::HashMap<u64, String> = std::collections::HashMap::new();
    if let Some(gh) = &gh {
        let (ok, out) = run_capture_owned(
            gh,
            &[
                "pr".into(),
                "list".into(),
                "--repo".into(),
                repo.github_repository.clone(),
                "--state".into(),
                "all".into(),
                "--limit".into(),
                "1000".into(),
                "--json".into(),
                "number,url,headRefName,state,mergeable,baseRefName".into(),
            ],
        );
        if ok {
            if let Ok(list) = serde_json::from_str::<serde_json::Value>(&out) {
                for pr in list.as_array().cloned().unwrap_or_default() {
                    let head = pr["headRefName"].as_str().unwrap_or_default().to_string();
                    let base = pr["baseRefName"].as_str().unwrap_or_default();
                    let entry = (
                        pr["number"].as_u64().unwrap_or(0),
                        pr["url"].as_str().unwrap_or_default().to_string(),
                        pr["state"].as_str().unwrap_or_default().to_string(),
                        pr["mergeable"].as_str().unwrap_or_default().to_string(),
                    );
                    if head == repo.integration_branch
                        && base == repo.base_branch
                        && entry.2 == "OPEN"
                    {
                        overview.integration_pr_url = entry.1.clone();
                        overview.integration_pr_number = Some(entry.0);
                    }
                    // Only a PR targeting the configured AI integration
                    // branch can make an issue branch complete. GitHub
                    // returns newest first, so keep the newest matching PR
                    // when historical and current PRs share a head.
                    if base == repo.integration_branch {
                        pr_by_head.entry(head).or_insert(entry);
                    }
                }
            }
        }
        let (ok, out) = run_capture_owned(
            gh,
            &[
                "issue".into(),
                "list".into(),
                "--repo".into(),
                repo.github_repository.clone(),
                "--state".into(),
                "all".into(),
                "--limit".into(),
                "1000".into(),
                "--json".into(),
                "number,state".into(),
            ],
        );
        if ok {
            if let Ok(list) = serde_json::from_str::<serde_json::Value>(&out) {
                for issue in list.as_array().cloned().unwrap_or_default() {
                    if let Some(number) = issue["number"].as_u64() {
                        issue_states.insert(
                            number,
                            issue["state"].as_str().unwrap_or_default().to_string(),
                        );
                    }
                }
            }
        }
    }

    // A closed issue whose PR is already merged has no active promotion work.
    // Hide a stale remote ref immediately; the worker and manual merge command
    // also remove that ref explicitly.
    branches.retain(|(name, _, number)| {
        let head_ref = name
            .strip_prefix(&format!("{}/", repo.remote_name))
            .unwrap_or(name);
        let pull_request_state = pr_by_head
            .get(head_ref)
            .map(|pr| pr.2.as_str())
            .unwrap_or_default();
        let issue_state = issue_states
            .get(number)
            .map(String::as_str)
            .unwrap_or_default();
        !issue_branch_is_complete(issue_state, pull_request_state)
    });

    for (name, ai, number) in &branches {
        let ab = if overview.integration_exists {
            ahead_behind(&git, &ws, &integ_ref, name)
        } else {
            BranchAheadBehind::default()
        };
        let head_ref = name
            .strip_prefix(&format!("{}/", repo.remote_name))
            .unwrap_or(name)
            .to_string();
        let pr = pr_by_head.get(&head_ref);
        overview.issue_branches.push(IssueBranchInfo {
            name: head_ref,
            ai_tool: ai.clone(),
            issue_number: *number,
            issue_state: issue_states.get(number).cloned().unwrap_or_default(),
            ahead_of_integration: ab.ahead,
            behind_integration: ab.behind,
            last_commit: commit_tip(&git, &ws, name),
            pr_number: pr.map(|p| p.0),
            pr_url: pr.map(|p| p.1.clone()).unwrap_or_default(),
            pr_state: pr.map(|p| p.2.clone()).unwrap_or_default(),
            mergeable: pr.map(|p| p.3.clone()).unwrap_or_default(),
        });
    }

    // Raw graph for the toggle.
    let mut graph_args = vec![
        "log".to_string(),
        "--graph".into(),
        "--oneline".into(),
        "--decorate".into(),
        "--color=never".into(),
        "-40".into(),
    ];
    if overview.integration_exists {
        graph_args.push(integ_ref.clone());
    } else {
        graph_args.push(base_ref.clone());
    }
    graph_args.extend(branches.iter().map(|(name, _, _)| name.clone()));
    let (_, graph) = run_capture_owned(
        &git,
        &std::iter::once("-C".to_string())
            .chain(std::iter::once(ws.clone()))
            .chain(graph_args)
            .collect::<Vec<_>>(),
    );
    overview.graph = graph;
    Ok(overview)
}

#[tauri::command]
async fn git_overview_background(
    app: tauri::AppHandle,
    repo_id: String,
) -> Result<RepoGitOverview, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        git_overview(app.clone(), state, repo_id)
    })
    .await
    .map_err(|error| format!("Repository refresh background task failed: {error}"))?
}

#[tauri::command]
fn refresh_repo(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    repo_id: String,
) -> Result<RepoGitOverview, String> {
    git_overview(app, state, repo_id)
}

#[tauri::command]
fn merge_issue_branch(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    repo_id: String,
    pr_number: u64,
    issue_number: u64,
) -> Result<RepoGitOverview, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?.clone();
    let worker = state.processes.status(
        &app,
        "issue",
        "Issue worker scheduler",
        &automation_log_path(&app)?,
    )?;
    if worker.state != "stopped" {
        return Err("Stop the issue worker before merging an issue branch.".into());
    }
    let gh = tools::configured_or_detected(&config.gh_bin, "gh")?;
    let git = tools::configured_or_detected("", "git")?;
    // Establish the cleanup path before the irreversible GitHub merge. This
    // guarantees that a successful merge can be followed by branch removal.
    let workspace = prepared_workspace(&app, &config, &repo)?;
    let ws = workspace.to_string_lossy().into_owned();

    let (issue_ok, issue_state) = run_capture_owned(
        &gh,
        &[
            "issue".into(),
            "view".into(),
            issue_number.to_string(),
            "--repo".into(),
            repo.github_repository.clone(),
            "--json".into(),
            "state".into(),
            "--jq".into(),
            ".state".into(),
        ],
    );
    if !issue_ok {
        return Err(format!(
            "Could not inspect issue #{issue_number}: {issue_state}"
        ));
    }
    require_closed_issue(issue_number, &issue_state, &repo.integration_branch)?;

    let (view_ok, view_out) = run_capture_owned(
        &gh,
        &[
            "pr".into(),
            "view".into(),
            pr_number.to_string(),
            "--repo".into(),
            repo.github_repository.clone(),
            "--json".into(),
            "state,headRefName,baseRefName,headRefOid".into(),
        ],
    );
    if !view_ok {
        return Err(format!("Could not inspect PR #{pr_number}: {view_out}"));
    }
    let pr: serde_json::Value = serde_json::from_str(&view_out)
        .map_err(|error| format!("GitHub returned invalid PR details: {error}"))?;
    let head = pr["headRefName"].as_str().unwrap_or_default();
    let base = pr["baseRefName"].as_str().unwrap_or_default();
    let state_name = pr["state"].as_str().unwrap_or_default();
    let parts: Vec<_> = head.split('/').collect();
    let valid_tool = parts.get(1).is_some_and(|tool| {
        config::KNOWN_PROVIDERS.contains(tool) || matches!(*tool, "xai" | "grok")
    });
    if state_name != "OPEN"
        || base != repo.integration_branch
        || parts.len() != 3
        || parts[0] != repo.branch_prefix
        || !valid_tool
        || parts[2] != format!("issue-{issue_number}")
    {
        return Err(format!(
            "PR #{pr_number} is not the open issue #{issue_number} branch targeting {}.",
            repo.integration_branch
        ));
    }
    let head_sha = pr["headRefOid"].as_str().unwrap_or_default();

    let (ok, message) = run_capture_owned(
        &gh,
        &[
            "pr".into(),
            "merge".into(),
            pr_number.to_string(),
            "--repo".into(),
            repo.github_repository.clone(),
            "--squash".into(),
            "--delete-branch".into(),
            "--match-head-commit".into(),
            head_sha.into(),
        ],
    );
    if !ok {
        return Err(format!("Could not merge PR #{pr_number}: {message}"));
    }

    // The issue is already closed; record where its branch landed.
    let (merge_ok, merge_sha) = run_capture_owned(
        &gh,
        &[
            "pr".into(),
            "view".into(),
            pr_number.to_string(),
            "--repo".into(),
            repo.github_repository.clone(),
            "--json".into(),
            "mergeCommit".into(),
            "--jq".into(),
            ".mergeCommit.oid".into(),
        ],
    );
    let merge_sha = if merge_ok { merge_sha } else { String::new() };
    let comment = format!(
        "Squash-merged into `{}` via PR #{}{} after this issue was closed.",
        repo.integration_branch,
        pr_number,
        if merge_sha.is_empty() {
            String::new()
        } else {
            format!(" (commit `{merge_sha}`)")
        }
    );
    let (comment_ok, comment_message) = run_capture_owned(
        &gh,
        &[
            "issue".into(),
            "comment".into(),
            issue_number.to_string(),
            "--repo".into(),
            repo.github_repository.clone(),
            "--body".into(),
            comment,
        ],
    );
    let comment_error = (!comment_ok).then(|| {
        format!(
            "PR #{pr_number} was merged and its branch was removed, but issue #{issue_number} could not be updated: {comment_message}"
        )
    });

    let (fetched, fetch_message) = git_c(&git, &ws, &["fetch", "--prune", &repo.remote_name]);
    if !fetched {
        return Err(format!(
            "PR #{pr_number} was merged, but its branch could not be checked for removal: {fetch_message}"
        ));
    }
    let remote_head = format!("{}/{}", repo.remote_name, head);
    if git_c(
        &git,
        &ws,
        &[
            "show-ref",
            "--verify",
            "--quiet",
            &format!("refs/remotes/{remote_head}"),
        ],
    )
    .0
    {
        let (deleted, delete_message) =
            git_c(&git, &ws, &["push", &repo.remote_name, "--delete", head]);
        if !deleted {
            return Err(format!(
                "PR #{pr_number} was merged, but its branch {head} could not be removed: {delete_message}"
            ));
        }
        let _ = git_c(&git, &ws, &["fetch", "--prune", &repo.remote_name]);
    }
    let current = git_c(&git, &ws, &["branch", "--show-current"]).1;
    let clean = git_c(&git, &ws, &["status", "--porcelain"]).1.is_empty();
    if current == head && clean {
        let remote_integration = format!("{}/{}", repo.remote_name, repo.integration_branch);
        if !git_c(&git, &ws, &["switch", &repo.integration_branch]).0 {
            let _ = git_c(
                &git,
                &ws,
                &[
                    "switch",
                    "-c",
                    &repo.integration_branch,
                    &remote_integration,
                ],
            );
        }
        let _ = git_c(&git, &ws, &["merge", "--ff-only", &remote_integration]);
        let _ = git_c(&git, &ws, &["branch", "-D", head]);
    } else if current != head {
        let _ = git_c(&git, &ws, &["branch", "-D", head]);
    }

    if let Some(error) = comment_error {
        return Err(error);
    }

    git_overview(app, state, repo_id)
}

#[tauri::command]
fn merge_integration_branch(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    repo_id: String,
    pr_number: u64,
) -> Result<RepoGitOverview, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?.clone();
    let gh = tools::configured_or_detected(&config.gh_bin, "gh")?;
    let git = tools::configured_or_detected("", "git")?;
    let (view_ok, view_out) = run_capture_owned(
        &gh,
        &[
            "pr".into(),
            "view".into(),
            pr_number.to_string(),
            "--repo".into(),
            repo.github_repository.clone(),
            "--json".into(),
            "state,headRefName,baseRefName,headRefOid".into(),
        ],
    );
    if !view_ok {
        return Err(format!("Could not inspect PR #{pr_number}: {view_out}"));
    }
    let pr: serde_json::Value = serde_json::from_str(&view_out)
        .map_err(|error| format!("GitHub returned invalid PR details: {error}"))?;
    if pr["state"].as_str() != Some("OPEN")
        || pr["headRefName"].as_str() != Some(repo.integration_branch.as_str())
        || pr["baseRefName"].as_str() != Some(repo.base_branch.as_str())
    {
        return Err(format!(
            "PR #{pr_number} is not the open {} -> {} promotion pull request.",
            repo.integration_branch, repo.base_branch
        ));
    }
    let head_sha = pr["headRefOid"].as_str().unwrap_or_default();
    let (ok, message) = run_capture_owned(
        &gh,
        &[
            "pr".into(),
            "merge".into(),
            pr_number.to_string(),
            "--repo".into(),
            repo.github_repository.clone(),
            "--merge".into(),
            "--match-head-commit".into(),
            head_sha.into(),
        ],
    );
    if !ok {
        return Err(format!(
            "Could not merge the promotion PR #{pr_number}: {message}"
        ));
    }
    if let Ok(workspace) = resolve_workspace(&app, &config, &repo) {
        let ws = workspace.to_string_lossy().into_owned();
        let _ = git_c(&git, &ws, &["fetch", "--prune", &repo.remote_name]);
    }
    git_overview(app, state, repo_id)
}

#[tauri::command]
fn open_integration_pr(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    repo_id: String,
) -> Result<String, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?.clone();
    let gh = tools::configured_or_detected(&config.gh_bin, "gh")?;

    // Reuse an open PR if there is one.
    let (list_ok, list_out) = run_capture_owned(
        &gh,
        &[
            "pr".into(),
            "list".into(),
            "--repo".into(),
            repo.github_repository.clone(),
            "--base".into(),
            repo.base_branch.clone(),
            "--head".into(),
            repo.integration_branch.clone(),
            "--state".into(),
            "open".into(),
            "--json".into(),
            "url".into(),
            "--jq".into(),
            ".[0].url // \"\"".into(),
        ],
    );
    let url = if list_ok && list_out.starts_with("https://") {
        list_out
    } else {
        let (create_ok, create_out) = run_capture_owned(
            &gh,
            &[
                "pr".into(),
                "create".into(),
                "--repo".into(),
                repo.github_repository.clone(),
                "--base".into(),
                repo.base_branch.clone(),
                "--head".into(),
                repo.integration_branch.clone(),
                "--title".into(),
                format!(
                    "Merge {} into {}",
                    repo.integration_branch, repo.base_branch
                ),
                "--body".into(),
                format!(
                    "Human review gate: promote AI-integration work from `{}` to `{}`. \
                     Review the checks and merge on GitHub.",
                    repo.integration_branch, repo.base_branch
                ),
            ],
        );
        if !create_ok {
            return Err(format!("Could not open the integration PR: {create_out}"));
        }
        create_out
            .lines()
            .rev()
            .find(|line| line.starts_with("https://"))
            .unwrap_or(&create_out)
            .to_string()
    };
    let _ = app.opener().open_url(url.clone(), None::<&str>);
    Ok(url)
}

#[tauri::command]
fn open_provider_login(state: State<'_, AppState>, provider: String) -> Result<(), String> {
    let config = current_config(&state)?;
    let provider_bin = |id: &str| {
        config
            .provider(id)
            .map(|provider| provider.bin.clone())
            .unwrap_or_default()
    };
    let (binary, arguments) = match provider.as_str() {
        "claude" => (
            tools::configured_or_detected(&provider_bin("claude"), "claude")?,
            Vec::<&str>::new(),
        ),
        "codex" => (
            tools::configured_or_detected(&provider_bin("codex"), "codex")?,
            vec!["login"],
        ),
        "grok" => (
            tools::configured_or_detected(&provider_bin("grok"), "grok")?,
            vec!["login"],
        ),
        "gh" => (
            tools::configured_or_detected(&config.gh_bin, "gh")?,
            vec!["auth", "login"],
        ),
        _ => return Err("Unknown login provider.".into()),
    };
    let command = std::iter::once(shell_quote(binary.to_string_lossy()))
        .chain(arguments.into_iter().map(shell_quote))
        .collect::<Vec<_>>()
        .join(" ");
    open_terminal_command(&command)
}

#[tauri::command]
fn set_smtp_password(state: State<'_, AppState>, password: String) -> Result<bool, String> {
    let entry = keyring::Entry::new(SMTP_KEYRING_SERVICE, SMTP_KEYRING_ACCOUNT)
        .map_err(|error| error.to_string())?;
    let configured = if password.is_empty() {
        match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => false,
            Err(error) => return Err(error.to_string()),
        }
    } else {
        entry
            .set_password(&password)
            .map_err(|error| error.to_string())?;
        true
    };
    set_cached_smtp_password_configured(state.inner(), configured)?;
    Ok(configured)
}

fn smtp_password() -> Result<String, String> {
    keyring::Entry::new(SMTP_KEYRING_SERVICE, SMTP_KEYRING_ACCOUNT)
        .map_err(|error| error.to_string())?
        .get_password()
        .map_err(|_| "No SMTP password is stored in macOS Keychain.".into())
}

#[tauri::command]
fn open_external_url(app: tauri::AppHandle, url: String) -> Result<(), String> {
    if !url.starts_with("https://") {
        return Err("Only HTTPS links may be opened.".into());
    }
    app.opener()
        .open_url(url, None::<&str>)
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn open_automation_folder(app: tauri::AppHandle) -> Result<(), String> {
    let folder = automation_log_path(&app)?
        .parent()
        .ok_or_else(|| "Automation log folder is unavailable".to_string())?
        .to_path_buf();
    app.opener()
        .open_path(folder.to_string_lossy().into_owned(), None::<&str>)
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn get_recent_logs(app: tauri::AppHandle) -> Result<Vec<String>, String> {
    let path = automation_log_path(&app)?;
    let text = std::fs::read_to_string(path).unwrap_or_default();
    let mut lines = text.lines().rev().take(300).collect::<Vec<_>>();
    lines.reverse();
    Ok(lines.into_iter().map(str::to_string).collect())
}

fn resolve_repo<'a>(config: &'a AppConfig, id: &str) -> Result<&'a RepoConfig, String> {
    config
        .repo(id)
        .ok_or_else(|| format!("No configured repository with id '{id}'."))
}

fn repo_host(repo: &RepoConfig) -> String {
    let _ = repo;
    "github.com".to_string()
}

/// Parent directory for managed clones — `workspace_root` when set, else
/// `<app-data-dir>/checkouts` (or the test data dir).
fn workspace_root<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    config: &AppConfig,
) -> Result<PathBuf, String> {
    let configured = config.workspace_root.trim();
    if !configured.is_empty() {
        return Ok(PathBuf::from(configured));
    }
    if let Some(state) = app.try_state::<AppState>() {
        if let Some(dir) = &state.test_data_dir {
            return Ok(dir.join("checkouts"));
        }
    }
    app.path()
        .app_data_dir()
        .map(|dir| dir.join("checkouts"))
        .map_err(|error| error.to_string())
}

/// The working copy for `repo`: the advanced `repo_dir` override when set,
/// otherwise `<workspace root>/<repo id>`.
fn resolve_workspace<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    config: &AppConfig,
    repo: &RepoConfig,
) -> Result<PathBuf, String> {
    let override_path = repo.repo_dir.trim();
    if !override_path.is_empty() {
        return Ok(PathBuf::from(override_path));
    }
    if !repo.github_repository.trim().contains('/') {
        return Err(format!(
            "Repository {}: set the GitHub repository (owner/name) first.",
            repo.label()
        ));
    }
    Ok(workspace_root(app, config)?.join(&repo.id))
}

/// Clone `repo`'s GitHub repository into `target` if it is not already a
/// checkout; otherwise fetch. Only for the managed-clone case.
fn ensure_workspace(config: &AppConfig, repo: &RepoConfig, target: &Path) -> Result<(), String> {
    let git = tools::configured_or_detected("", "git")?;
    if target.join(".git").is_dir() {
        let (ok, message) = run_capture(
            &git,
            &[
                "-C",
                &target.to_string_lossy(),
                "fetch",
                "--prune",
                &repo.remote_name,
            ],
        );
        if !ok {
            return Err(format!(
                "Could not fetch updates for the workspace: {message}"
            ));
        }
        return Ok(());
    }
    if target.exists() {
        return Err(format!(
            "{} already exists but is not a Git checkout — move or remove it, or set a different workspace folder.",
            target.display()
        ));
    }
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    let repository = repo.github_repository.trim();
    let target_string = target.to_string_lossy().into_owned();
    let host = repo_host(repo);
    // Prefer `gh repo clone` (existing gh auth, right protocol), fall back to
    // an HTTPS `git clone`.
    if let Some(gh) = tools::find_executable("gh", &config.gh_bin) {
        let (ok, message) = run_capture(&gh, &["repo", "clone", repository, &target_string]);
        if ok {
            return Ok(());
        }
        let url = format!("https://{host}/{repository}.git");
        let (git_ok, git_message) = run_capture(&git, &["clone", "--", &url, &target_string]);
        return if git_ok {
            Ok(())
        } else {
            Err(format!("Clone failed. gh: {message}. git: {git_message}"))
        };
    }
    let url = format!("https://{host}/{repository}.git");
    let (ok, message) = run_capture(&git, &["clone", "--", &url, &target_string]);
    if ok {
        Ok(())
    } else {
        Err(format!("Clone failed: {message}"))
    }
}

/// Resolve `repo`'s workspace and, when the app manages the clone, make sure
/// it exists on disk.
fn prepared_workspace<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    config: &AppConfig,
    repo: &RepoConfig,
) -> Result<PathBuf, String> {
    let workspace = resolve_workspace(app, config, repo)?;
    if repo.repo_dir.trim().is_empty() {
        ensure_workspace(config, repo, &workspace)?;
    }
    Ok(workspace)
}

#[tauri::command]
fn prepare_workspace(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    repo_id: String,
) -> Result<RepositoryInspection, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?;
    let workspace = prepared_workspace(&app, &config, repo)?;
    Ok(inspect_repository_path(&workspace))
}

#[tauri::command]
fn open_workspace_folder(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    repo_id: String,
) -> Result<(), String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?;
    let workspace = resolve_workspace(&app, &config, repo)?;
    let target = if workspace.is_dir() {
        workspace
    } else {
        workspace
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or(workspace)
    };
    if let Some(parent) = target.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let _ = std::fs::create_dir_all(&target);
    app.opener()
        .open_path(target.to_string_lossy().into_owned(), None::<&str>)
        .map_err(|error| error.to_string())
}

fn validate_worker_script_dir(bundled: &Path) -> Result<PathBuf, String> {
    let missing: Vec<&str> = REQUIRED_WORKER_RESOURCES
        .iter()
        .copied()
        .filter(|name| !bundled.join(name).is_file())
        .collect();
    if missing.is_empty() {
        Ok(bundled.to_path_buf())
    } else {
        Err(format!(
            "The bundled issue-worker resources are incomplete (missing {}). Reinstall or rebuild SWARM Automation.",
            missing.join(", ")
        ))
    }
}

fn worker_script_dir<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<PathBuf, String> {
    let bundled = app
        .path()
        .resource_dir()
        .map_err(|error| error.to_string())?
        .join("issue_worker");
    validate_worker_script_dir(&bundled)
}

fn repo_or_home(workspace: &Path) -> &Path {
    if workspace.is_dir() {
        workspace
    } else {
        Path::new("/")
    }
}

fn run_capture(program: &Path, arguments: &[&str]) -> (bool, String) {
    run_capture_owned(
        program,
        &arguments
            .iter()
            .map(|value| value.to_string())
            .collect::<Vec<_>>(),
    )
}

fn run_capture_owned(program: &Path, arguments: &[String]) -> (bool, String) {
    match Command::new(program)
        .args(arguments)
        .env("PATH", tools::enhanced_path())
        .output()
    {
        Ok(output) => {
            let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            let message = if stdout.is_empty() { stderr } else { stdout };
            (output.status.success(), message)
        }
        Err(error) => (false, error.to_string()),
    }
}

fn shell_quote(value: impl AsRef<str>) -> String {
    format!("'{}'", value.as_ref().replace('\'', "'\\''"))
}

#[cfg(target_os = "macos")]
fn open_terminal_command(command: &str) -> Result<(), String> {
    let script = format!(
        "tell application \"Terminal\"\nactivate\ndo script {}\nend tell",
        apple_script_string(command),
    );
    let status = Command::new("/usr/bin/osascript")
        .args(["-e", &script])
        .status()
        .map_err(|error| error.to_string())?;
    status
        .success()
        .then_some(())
        .ok_or_else(|| "Could not open Terminal.".into())
}

#[cfg(not(target_os = "macos"))]
fn open_terminal_command(_command: &str) -> Result<(), String> {
    Err("Interactive sign-in launch is currently implemented for macOS.".into())
}

fn apple_script_string(value: &str) -> String {
    format!("\"{}\"", value.replace('\\', "\\\\").replace('"', "\\\""))
}

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

#[tauri::command]
fn hide_to_tray(app: tauri::AppHandle) -> Result<(), String> {
    app.get_webview_window(MAIN_WINDOW)
        .ok_or_else(|| "Main window is unavailable.".to_string())?
        .hide()
        .map_err(|error| error.to_string())
}

fn install_tray(app: &mut tauri::App) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "Show SWARM Automation", true, None::<&str>)?;
    let note = MenuItem::with_id(
        app,
        "note",
        "Workers continue while this window is hidden",
        false,
        None::<&str>,
    )?;
    let separator = PredefinedMenuItem::separator(app)?;
    let quit = MenuItem::with_id(app, "quit", "Quit and stop workers", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &note, &separator, &quit])?;
    let mut tray = TrayIconBuilder::with_id("swarm-automation")
        .menu(&menu)
        .tooltip("SWARM Automation")
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "show" => show_main_window(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon().cloned() {
        tray = tray.icon(icon);
    }
    tray.build(app)?;
    Ok(())
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            show_main_window(app)
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(AppState::default())
        .setup(|app| {
            let loaded =
                config::load(&app_config_path(app.handle()).map_err(std::io::Error::other)?);
            *app.state::<AppState>()
                .config
                .lock()
                .map_err(|_| std::io::Error::other("Configuration lock was poisoned"))? = loaded;
            install_tray(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() == MAIN_WINDOW {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_config,
            save_config,
            choose_repository,
            inspect_repository,
            prepare_workspace,
            open_workspace_folder,
            detect_tools,
            detect_tools_background,
            get_automation_status,
            get_automation_status_background,
            start_issue_worker,
            start_uat_scheduler,
            pause_process,
            resume_process,
            stop_process,
            install_ai_cli,
            launch_bot_setup,
            verify_github_bots,
            git_overview,
            git_overview_background,
            refresh_repo,
            merge_issue_branch,
            merge_integration_branch,
            open_integration_pr,
            open_provider_login,
            set_smtp_password,
            open_external_url,
            open_automation_folder,
            get_recent_logs,
            hide_to_tray,
        ])
        .build(tauri::generate_context!())
        .expect("failed to build SWARM Automation");

    app.run(|app, event| {
        if matches!(
            event,
            tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. }
        ) {
            app.state::<AppState>().processes.stop_all();
        }
        #[cfg(target_os = "macos")]
        if let tauri::RunEvent::Reopen { .. } = event {
            show_main_window(app);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn github_remote_urls_are_normalized() {
        assert_eq!(
            github_slug("git@github.com:owner/repo.git\n").as_deref(),
            Some("owner/repo")
        );
        assert_eq!(
            github_slug("https://github.com/owner/repo.git").as_deref(),
            Some("owner/repo")
        );
        assert!(github_slug("https://example.com/owner/repo").is_none());
    }

    #[test]
    fn shell_quoting_preserves_spaces_and_quotes() {
        assert_eq!(shell_quote("a b'c"), "'a b'\\''c'");
    }
}

/// Backend UAT coverage for the safe, no-child-process commands: real
/// `#[tauri::command]` handlers invoked directly against a real, isolated
/// AppState/config-file/filesystem behind a mocked Tauri runtime — same
/// shape as apps/server/src/gui_tests (see that crate's `mod.rs` for why:
/// no reliable macOS UI-automation path today, and Tauri's simulated
/// IPC/ACL layer isn't usable under a bare `mock_context()`). Deliberately
/// does not cover start_issue_worker/start_uat_scheduler/install_ai_cli/
/// launch_bot_setup — those spawn real child processes (python3, bash, npm)
/// and are exercised by manual `npm run dev`/`npm run build` + launch
/// verification instead.
#[cfg(test)]
#[path = "command_tests.rs"]
mod command_tests;
