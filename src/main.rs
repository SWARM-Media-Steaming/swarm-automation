mod config;
mod processes;
mod tools;

use config::{AppConfig, CONFIG_FILE};
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

struct AppState {
    config: Mutex<AppConfig>,
    processes: ProcessManager,
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
struct AutomationStatus {
    issue: ProcessStatus,
    uat: ProcessStatus,
    task: ProcessStatus,
    worker_available: bool,
    uat_available: bool,
    bot_config_exists: bool,
    smtp_password_configured: bool,
    config_error: String,
    repository: RepositoryInspection,
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

#[tauri::command]
fn get_config(state: State<'_, AppState>) -> Result<AppConfig, String> {
    current_config(&state)
}

#[tauri::command]
fn save_config<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    state: State<'_, AppState>,
    config: AppConfig,
) -> Result<AppConfig, String> {
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
    Ok(tools::detect(&current_config(&state)?))
}

#[tauri::command]
fn get_automation_status(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
) -> Result<AutomationStatus, String> {
    let config = current_config(&state)?;
    let log_path = automation_log_path(&app)?;
    let repository = inspect_repository_path(Path::new(&config.repo_dir));
    let config_error = config.validate().err().unwrap_or_default();
    Ok(AutomationStatus {
        issue: state
            .processes
            .status(&app, "issue", "Issue worker", &log_path)?,
        uat: state
            .processes
            .status(&app, "uat", "UAT scheduler", &log_path)?,
        task: state
            .processes
            .status(&app, "task", "Setup task", &log_path)?,
        worker_available: worker_script_dir(&app, &config).is_ok(),
        uat_available: repository.uat_available,
        bot_config_exists: Path::new(&config.github_apps_config).is_file(),
        smtp_password_configured: smtp_password().is_ok(),
        config_error,
        repository,
        log_path: log_path.to_string_lossy().into_owned(),
    })
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
        return Err("This profile is set to Manual only. Use Run now or choose a schedule.".into());
    }
    let script_dir = worker_script_dir(&app, &config)?;
    let runner = script_dir.join("install_swarm_issue_cron.py");
    let python = tools::configured_or_detected(&config.python_bin, "python3")?;
    let git = tools::configured_or_detected("", "git")?;
    let gh = tools::configured_or_detected(&config.gh_bin, "gh")?;
    let claude = tools::find_executable("claude", &config.claude_bin).unwrap_or_default();
    let codex = tools::find_executable("codex", &config.codex_bin).unwrap_or_default();
    if claude.as_os_str().is_empty() && codex.as_os_str().is_empty() {
        return Err(
            "Install and sign in to Claude Code or Codex CLI before starting the worker.".into(),
        );
    }
    let mut arguments = issue_worker_arguments(
        &config, &runner, &python, &git, &gh, &claude, &codex, run_once,
    );
    let mut environment = vec![
        ("PATH".into(), tools::enhanced_path()),
        (
            "SWARM_ISSUE_WORKER_SCRIPT_DIR".into(),
            script_dir.to_string_lossy().into_owned(),
        ),
        ("SWARM_TRUSTED_FOLLOWUP_AUTHORS".into(), String::new()),
        ("SWARM_COMPLETION_AUTHORS".into(), String::new()),
    ];
    if config.email_enabled {
        environment.push(("SWARM_SMTP_PASSWORD".into(), smtp_password()?));
    } else if !arguments.iter().any(|argument| argument == "--no-email") {
        arguments.push("--no-email".into());
    }
    state.processes.spawn(
        &app,
        "issue",
        "Issue worker",
        &python,
        &arguments,
        &environment,
        Path::new(&config.repo_dir),
        automation_log_path(&app)?,
    )
}

#[allow(clippy::too_many_arguments)]
fn issue_worker_arguments(
    config: &AppConfig,
    runner: &Path,
    python: &Path,
    git: &Path,
    gh: &Path,
    claude: &Path,
    codex: &Path,
    run_once: bool,
) -> Vec<String> {
    let mut arguments = vec![
        runner.to_string_lossy().into_owned(),
        "--repo-dir".into(),
        config.repo_dir.clone(),
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
        "--base-branch".into(),
        config.base_branch.clone(),
        "--remote-name".into(),
        config.remote_name.clone(),
        "--github-repository".into(),
        config.github_repository.clone(),
        "--assignee".into(),
        config.assignee.clone(),
        "--ready-label".into(),
        config.ready_label.clone(),
        "--minimum-remaining-percent".into(),
        config.minimum_remaining_percent.to_string(),
        "--preferred-provider".into(),
        config.preferred_provider.clone(),
        "--claude-model".into(),
        config.claude_model.clone(),
        "--claude-effort".into(),
        config.claude_effort.clone(),
        "--codex-model".into(),
        config.codex_model.clone(),
        "--codex-effort".into(),
        config.codex_effort.clone(),
        "--claude-bin".into(),
        claude.to_string_lossy().into_owned(),
        "--codex-bin".into(),
        codex.to_string_lossy().into_owned(),
        "--gh-bin".into(),
        gh.to_string_lossy().into_owned(),
        "--github-apps-config".into(),
        config.github_apps_config.clone(),
        if config.require_bot_auth {
            "--require-bot-auth"
        } else {
            "--no-require-bot-auth"
        }
        .into(),
        "--delivery-mode".into(),
        config.delivery_mode.clone(),
        if config.auto_approve {
            "--auto-approve"
        } else {
            "--no-auto-approve"
        }
        .into(),
        if config.auto_merge {
            "--auto-merge"
        } else {
            "--no-auto-merge"
        }
        .into(),
        "--branch-prefix".into(),
        config.branch_prefix.clone(),
        "--merge-method".into(),
        config.merge_method.clone(),
        "--github-host".into(),
        config.github_host.clone(),
    ];
    // An empty list here isn't a harmless "no restriction" default — the
    // worker's env-var fallback for these flags only applies when the var
    // is truly unset, and the environment this process launches with
    // always sets both explicitly (see `start_issue_worker`'s
    // `environment` vec), so an empty config list reaches the worker as a
    // genuinely empty list, trusting no one's follow-ups and crediting no
    // one's commits — a silent dead end for a first-run profile that never
    // touched these optional-looking fields. Falling back to the
    // configured assignee (which validate() already requires to be
    // non-empty) keeps that first run actually functional without forcing
    // every new profile to fill in two more fields before its first use.
    let trusted_followup_authors = if config.trusted_followup_authors.is_empty() {
        std::slice::from_ref(&config.assignee)
    } else {
        config.trusted_followup_authors.as_slice()
    };
    for author in trusted_followup_authors {
        arguments.extend(["--trusted-followup-author".into(), author.clone()]);
    }
    let completion_authors = if config.completion_authors.is_empty() {
        std::slice::from_ref(&config.assignee)
    } else {
        config.completion_authors.as_slice()
    };
    for author in completion_authors {
        arguments.extend(["--completion-author".into(), author.clone()]);
    }
    if config.email_enabled {
        arguments.extend([
            "--smtp-credentials-file".into(),
            config.smtp_credentials_file.clone(),
            "--email-to".into(),
            config.email_to.clone(),
        ]);
    } else {
        arguments.push("--no-email".into());
    }
    if run_once {
        arguments.push("--once".into());
    }
    arguments
}

#[tauri::command]
fn start_uat_scheduler(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    run_once: bool,
) -> Result<ProcessStatus, String> {
    let config = current_config(&state)?;
    config.validate()?;
    if !config.uat_enabled {
        return Err("UAT scheduling is disabled in this profile.".into());
    }
    let script = Path::new(&config.repo_dir).join("scripts/tests/full_uat_cron.sh");
    if !script.is_file() {
        return Err("This repository does not contain scripts/tests/full_uat_cron.sh.".into());
    }
    let mut arguments = vec![script.to_string_lossy().into_owned()];
    if run_once {
        arguments.push("--once".into());
    }
    let environment = uat_environment(&config);
    state.processes.spawn(
        &app,
        "uat",
        "UAT scheduler",
        Path::new("/bin/bash"),
        &arguments,
        &environment,
        Path::new(&config.repo_dir),
        automation_log_path(&app)?,
    )
}

fn uat_environment(config: &AppConfig) -> Vec<(String, String)> {
    let mut environment = vec![
        ("PATH".into(), tools::enhanced_path()),
        (
            "SWARM_FULL_UAT_CRON_HOUR".into(),
            config.uat_hour.to_string(),
        ),
        (
            "SWARM_GITHUB_REPOSITORY".into(),
            config.github_repository.clone(),
        ),
        (
            "SWARM_E2E_ISSUE_LABEL".into(),
            config.uat_issue_label.clone(),
        ),
        (
            "SWARM_RUN_DIR".into(),
            config.effective_run_dir().to_string_lossy().into_owned(),
        ),
        (
            "SWARM_UAT_BATOCERA_HOST".into(),
            config.uat_batocera_host.clone(),
        ),
        (
            "SWARM_UAT_TRIAGE_ENABLED".into(),
            if config.uat_triage_enabled { "1" } else { "0" }.into(),
        ),
        (
            "SWARM_MIN_REMAINING_PERCENT".into(),
            config.minimum_remaining_percent.to_string(),
        ),
        ("SWARM_CLAUDE_MODEL".into(), config.claude_model.clone()),
        ("SWARM_CLAUDE_EFFORT".into(), config.claude_effort.clone()),
        ("SWARM_CODEX_MODEL".into(), config.codex_model.clone()),
        ("SWARM_CODEX_EFFORT".into(), config.codex_effort.clone()),
    ];
    if let Some(path) = tools::find_executable("claude", &config.claude_bin) {
        environment.push(("CLAUDE_BIN".into(), path.to_string_lossy().into_owned()));
    }
    if let Some(path) = tools::find_executable("codex", &config.codex_bin) {
        environment.push(("CODEX_BIN".into(), path.to_string_lossy().into_owned()));
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
    let (program, arguments) = tools::install_spec(&provider)?;
    let config = current_config(&state)?;
    state.processes.spawn(
        &app,
        "task",
        &format!("Install {provider}"),
        &program,
        &arguments,
        &[("PATH".into(), tools::enhanced_path())],
        repo_or_home(&config),
        automation_log_path(&app)?,
    )
}

#[tauri::command]
fn launch_bot_setup(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
) -> Result<ProcessStatus, String> {
    let config = current_config(&state)?;
    config.validate()?;
    let script = worker_script_dir(&app, &config)?.join("setup_github_bots.py");
    let python = tools::configured_or_detected(&config.python_bin, "python3")?;
    let arguments = vec![
        script.to_string_lossy().into_owned(),
        "--repository".into(),
        config.github_repository.clone(),
        "--config".into(),
        config.github_apps_config.clone(),
    ];
    state.processes.spawn(
        &app,
        "task",
        "GitHub bot setup",
        &python,
        &arguments,
        &[("PATH".into(), tools::enhanced_path())],
        Path::new(&config.repo_dir),
        automation_log_path(&app)?,
    )
}

#[tauri::command]
fn verify_github_bots(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
) -> Result<Vec<BotVerification>, String> {
    let config = current_config(&state)?;
    let script = worker_script_dir(&app, &config)?.join("github_app_auth.py");
    let python = tools::configured_or_detected(&config.python_bin, "python3")?;
    Ok(["claude", "codex"]
        .into_iter()
        .map(|provider| {
            if !Path::new(&config.github_apps_config).is_file() {
                return BotVerification {
                    provider: provider.into(),
                    configured: false,
                    valid: false,
                    message: "GitHub Apps configuration has not been created.".into(),
                };
            }
            let (valid, message) = run_capture_owned(
                &python,
                &[
                    script.to_string_lossy().into_owned(),
                    "--config".into(),
                    config.github_apps_config.clone(),
                    "check".into(),
                    "--provider".into(),
                    provider.into(),
                ],
            );
            BotVerification {
                provider: provider.into(),
                configured: true,
                valid,
                message: message.trim().to_string(),
            }
        })
        .collect())
}

#[tauri::command]
fn open_provider_login(state: State<'_, AppState>, provider: String) -> Result<(), String> {
    let config = current_config(&state)?;
    let (binary, arguments) = match provider.as_str() {
        "claude" => (
            tools::configured_or_detected(&config.claude_bin, "claude")?,
            Vec::<&str>::new(),
        ),
        "codex" => (
            tools::configured_or_detected(&config.codex_bin, "codex")?,
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
fn set_smtp_password(password: String) -> Result<bool, String> {
    let entry = keyring::Entry::new(SMTP_KEYRING_SERVICE, SMTP_KEYRING_ACCOUNT)
        .map_err(|error| error.to_string())?;
    if password.is_empty() {
        match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(false),
            Err(error) => Err(error.to_string()),
        }
    } else {
        entry
            .set_password(&password)
            .map_err(|error| error.to_string())?;
        Ok(true)
    }
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

fn worker_script_dir(app: &tauri::AppHandle, config: &AppConfig) -> Result<PathBuf, String> {
    let checkout = Path::new(&config.repo_dir).join("scripts/issue_worker");
    if checkout.join("install_swarm_issue_cron.py").is_file() {
        return Ok(checkout);
    }
    let bundled = app
        .path()
        .resource_dir()
        .map_err(|error| error.to_string())?
        .join("issue_worker");
    if bundled.join("install_swarm_issue_cron.py").is_file() {
        Ok(bundled)
    } else {
        Err("The bundled issue-worker resources could not be found.".into())
    }
}

fn repo_or_home(config: &AppConfig) -> &Path {
    let repo = Path::new(&config.repo_dir);
    if repo.is_dir() {
        repo
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
            detect_tools,
            get_automation_status,
            start_issue_worker,
            start_uat_scheduler,
            pause_process,
            resume_process,
            stop_process,
            install_ai_cli,
            launch_bot_setup,
            verify_github_bots,
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
