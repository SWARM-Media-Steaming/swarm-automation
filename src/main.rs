mod config;
mod processes;
mod test_discovery;
mod testing;
mod tools;

use config::{AppConfig, RepoConfig, CONFIG_FILE};
use processes::{process_is_running, ProcessManager, ProcessStatus};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Emitter, Manager, State};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;

const MAIN_WINDOW: &str = "main";
const REQUIRED_WORKER_RESOURCES: [&str; 9] = [
    "install_swarm_issue_cron.py",
    "swarm_issue_worker.py",
    "github_app_auth.py",
    "setup_github_bots.py",
    "codex_rate_limits.py",
    "grok_rate_limits.py",
    "ai_execution_history.py",
    "ai_test_assist.py",
    "issue_images.py",
];

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
    /// Non-secret values explicitly declared `session-only`, keyed by repo.
    test_input_sessions: Mutex<HashMap<String, HashMap<String, String>>>,
}

impl Default for AppState {
    fn default() -> Self {
        Self {
            config: Mutex::new(AppConfig::default()),
            processes: ProcessManager::default(),
            test_data_dir: None,
            test_input_sessions: Mutex::new(HashMap::new()),
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
    /// Set when the issue worker is silently skipping this repo every
    /// cycle because its checkout is dirty with no saved in-progress issue
    /// to explain why (see `deferred_checkout_reason`). Previously this was
    /// only visible by reading `cron.log`; surfaced here so it shows up as
    /// a dashboard warning instead of requiring someone to go looking.
    deferred_reason: Option<String>,
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

fn reconnect_issue_scheduler<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    state: &State<'_, AppState>,
    config: &AppConfig,
    log_path: &Path,
) -> Result<(), String> {
    let pid_path = PathBuf::from(&config.worker_state_dir).join("runner.lock/pid");
    let Ok(raw_pid) = std::fs::read_to_string(&pid_path) else {
        return Ok(());
    };
    let Ok(pid) = raw_pid.trim().parse::<u32>() else {
        return Ok(());
    };
    if !process_is_running(pid) {
        return Ok(());
    }
    state.processes.adopt_external(
        app,
        "issue",
        "Issue worker scheduler",
        pid,
        format!("Existing scheduler recorded by {}", pid_path.display()),
        log_path,
        Some(PathBuf::from(&config.worker_state_dir).join("cron.log")),
    )?;
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
    let _ = refresh_running_scheduler(&app, &state, &config);
    Ok(config)
}

/// A running scheduler re-reads `repos.json` at the start of each cycle, but
/// only the app writes it. Without this, a repository added (or a per-repo
/// setting changed) while the worker is running would be ignored until someone
/// stopped and restarted it. The write happens off the calling thread because
/// preparing a newly added repository can mean cloning it, and it re-reads the
/// saved config so overlapping saves cannot leave an older one on disk.
///
/// Returns the writer thread, or None when there was nothing to do. It does
/// nothing at all in tests (`test_data_dir` set): a test config uses the default
/// `worker_state_dir`, which is the developer's real one, so this would find
/// their real running scheduler and overwrite its real `repos.json` with the
/// test's fixture repositories.
fn refresh_running_scheduler<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    state: &State<'_, AppState>,
    config: &AppConfig,
) -> Option<std::thread::JoinHandle<()>> {
    if state.test_data_dir.is_some() {
        return None;
    }
    let log_path = automation_log_path(app).ok()?;
    let _ = reconnect_issue_scheduler(app, state, config, &log_path);
    let running = state
        .processes
        .status(app, "issue", "Issue worker scheduler", &log_path)
        .map(|status| status.state != "stopped")
        .unwrap_or(false);
    if !running {
        return None;
    }
    let app = app.clone();
    Some(std::thread::spawn(move || {
        let outcome = (|| {
            let state = app.state::<AppState>();
            let config = current_config(&state)?;
            config.validate()?;
            let git = tools::configured_or_detected("", "git")?;
            let gh = tools::configured_or_detected(&config.gh_bin, "gh")?;
            write_repos_file(&app, &config, &git, &gh).map(|_| config.enabled_repos().count())
        })();
        let message = match outcome {
            Ok(count) => format!(
                "Saved settings applied: the running worker will work {count} repository(ies) \
                 starting with its next cycle."
            ),
            Err(error) => format!(
                "Could not apply the saved settings to the running worker; restart it to pick \
                 them up: {error}"
            ),
        };
        processes::emit_log(
            &app,
            &log_path,
            "Issue worker scheduler",
            "stdout",
            &message,
        );
    }))
}

/// Serializes writers of `repos.json`: a save can overlap the worker starting.
static REPOS_FILE_LOCK: Mutex<()> = Mutex::new(());

/// Prepares every enabled repo's workspace and writes the scheduler's
/// per-repository spec (`repos.json`), returning its path. Written to a
/// temporary file and renamed so a scheduler reading it mid-write never sees a
/// truncated file.
fn write_repos_file<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    config: &AppConfig,
    git: &Path,
    gh: &Path,
) -> Result<PathBuf, String> {
    let _guard = REPOS_FILE_LOCK
        .lock()
        .map_err(|_| "Repository list lock was poisoned".to_string())?;
    let state_root = PathBuf::from(&config.worker_state_dir);
    let mut spec = Vec::new();
    for repo in config.enabled_repos() {
        let workspace = prepared_workspace(app, config, repo)?;
        let repo_state = state_root.join(&repo.id);
        std::fs::create_dir_all(&repo_state).map_err(|error| error.to_string())?;
        spec.push(serde_json::json!({
            "label": repo.label(),
            "workspace_dir": workspace.to_string_lossy(),
            "state_dir": repo_state.to_string_lossy(),
            "base_branch": repo.base_branch,
            "remote_name": repo.remote_name,
            "integration_branch": repo.integration_branch,
            "worker_args": repo_worker_args(config, repo, &workspace, git, gh),
        }));
    }
    if spec.is_empty() {
        return Err("Enable at least one repository before starting the worker.".into());
    }
    std::fs::create_dir_all(&state_root).map_err(|error| error.to_string())?;
    let repos_file = state_root.join("repos.json");
    let staging = state_root.join("repos.json.tmp");
    std::fs::write(
        &staging,
        serde_json::to_vec_pretty(&spec).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;
    std::fs::rename(&staging, &repos_file).map_err(|error| error.to_string())?;
    Ok(repos_file)
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

#[tauri::command]
async fn choose_test_input_path(
    app: tauri::AppHandle,
    kind: String,
) -> Result<Option<String>, String> {
    let (sender, receiver) = tokio_oneshot();
    if kind == "directory" {
        app.dialog().file().pick_folder(move |path| {
            let _ = sender.send(path.map(|value| value.to_string()));
        });
    } else {
        app.dialog().file().pick_file(move |path| {
            let _ = sender.send(path.map(|value| value.to_string()));
        });
    }
    receiver.recv().map_err(|error| error.to_string())
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
        uat_available: testing::available(&canonical),
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
    let mut config = current_config(&app.state())?;
    let host = config
        .repositories()
        .first()
        .map(repo_host)
        .unwrap_or_else(|| "github.com".into());
    let tools = tauri::async_runtime::spawn_blocking(move || tools::detect(&config, &host))
        .await
        .map_err(|error| format!("Tool detection background task failed: {error}"))?;

    // Installed CLIs are the source of truth for model ids and supported
    // efforts. Persist repairs so a removed model cannot fail every future
    // routing cycle, then refresh repos.json for an already-running scheduler.
    config = current_config(&app.state())?;
    let repairs = tools::reconcile_config_models(&mut config, &tools);
    if !repairs.is_empty() {
        config::save(&app_config_path(&app)?, &config)?;
        *app.state::<AppState>()
            .config
            .lock()
            .map_err(|_| "Configuration state lock was poisoned".to_string())? = config.clone();
        let _ = refresh_running_scheduler(&app, &app.state(), &config);
        for repair in repairs {
            eprintln!("SWARM model catalog repair: {repair}");
        }
    }
    Ok(tools)
}

#[tauri::command]
fn get_automation_status<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    state: State<'_, AppState>,
) -> Result<AutomationStatus, String> {
    let config = current_config(&state)?;
    let log_path = automation_log_path(&app)?;
    reconnect_issue_scheduler(&app, &state, &config, &log_path)?;
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
                &format!("Test scheduler · {}", repo.label()),
                &log_path,
            )?,
            workspace_path: workspace.to_string_lossy().into_owned(),
            workspace_ready,
            workspace_managed: managed,
            worker_available,
            uat_available: repository.uat_available,
            bot_config_exists: Path::new(&repo.effective_apps_config()).is_file(),
            repo_config_error: repo_error(&config, repo),
            deferred_reason: workspace_ready
                .then(|| deferred_checkout_reason(&config, repo, &workspace))
                .flatten(),
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

/// Mirrors `install_swarm_issue_cron.py`'s `synchronize_repository`
/// pre-flight: a dirty checkout with no `in-progress-issue.json` means the
/// scheduler is silently skipping this repo's issue worker every cycle
/// (logged, but only to `cron.log`, repeated every ~10 minutes with nothing
/// pointing anyone at it). Best-effort — any error just means "can't tell,"
/// not "this is broken," since the real pre-flight check already runs (and
/// logs) on every cycle regardless of whether the dashboard can see it.
fn deferred_checkout_reason(
    config: &AppConfig,
    repo: &RepoConfig,
    workspace: &Path,
) -> Option<String> {
    let state_dir = PathBuf::from(&config.worker_state_dir).join(&repo.id);
    if state_dir.join("in-progress-issue.json").is_file() {
        return None;
    }
    let git = tools::configured_or_detected("", "git").ok()?;
    let ws = workspace.to_string_lossy().into_owned();
    let (ok, status) = git_c(&git, &ws, &["status", "--porcelain"]);
    if !ok || status.trim().is_empty() {
        return None;
    }
    Some(
        "This repository's checkout has uncommitted changes with no saved work-round to \
         explain them, so the issue worker is skipping it every cycle instead of risking \
         someone's in-progress work. Check the checkout in Repository, or see cron.log."
            .to_string(),
    )
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
    let log_path = automation_log_path(&app)?;
    reconnect_issue_scheduler(&app, &state, &config, &log_path)?;
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

    // The desktop app and its Python worker form one versioned unit. Never
    // borrow automation scripts from a monitored repository: that copy may
    // implement a different command-line interface.
    let script_dir = worker_script_dir(&app)?;
    // Ensure every enabled repo's workspace, then write one spec entry each.
    let repos_file = write_repos_file(&app, &config, &git, &gh)?;
    let runner = script_dir.join("install_swarm_issue_cron.py");

    let mut arguments = scheduler_arguments(&config, &runner, &python, &git, &repos_file, run_once);
    arguments.extend(provider_scheduler_arguments(&config, &providers));
    arguments.extend([
        "--minimum-remaining-percent".into(),
        config.minimum_remaining_percent.to_string(),
        "--preferred-provider".into(),
        config.preferred_provider.clone(),
        "--gh-bin".into(),
        gh.to_string_lossy().into_owned(),
    ]);
    let environment = vec![
        ("PATH".into(), tools::enhanced_path()),
        (
            "SWARM_ISSUE_WORKER_SCRIPT_DIR".into(),
            script_dir.to_string_lossy().into_owned(),
        ),
    ];
    state.processes.spawn(
        &app,
        "issue",
        "Issue worker scheduler",
        &python,
        &arguments,
        &environment,
        &state_root,
        log_path,
    )
}

/// The file a running scheduler polls for an immediate scan, matching
/// `RUN_NOW_REQUEST_FILE` in `install_swarm_issue_cron.py`.
fn run_now_request_path(config: &AppConfig) -> PathBuf {
    PathBuf::from(&config.worker_state_dir).join("run-now.request")
}

/// "Run now" pressed while the scheduler is already running. Starting a second
/// scheduler is not an option — the runner lock would refuse it — so this
/// leaves a request the running one picks up: it abandons whatever wait it is
/// in (the poll interval, or a daily/weekday window), scans every enabled
/// repository immediately, and restarts its timer from the end of that cycle.
#[tauri::command]
fn request_issue_scan<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    state: State<'_, AppState>,
) -> Result<String, String> {
    let config = current_config(&state)?;
    let log_path = automation_log_path(&app)?;
    reconnect_issue_scheduler(&app, &state, &config, &log_path)?;
    let status = state
        .processes
        .status(&app, "issue", "Issue worker scheduler", &log_path)?;
    match status.state.as_str() {
        "running" => {}
        "paused" => {
            return Err(
                "The issue worker is paused. Resume it before asking for an immediate scan.".into(),
            )
        }
        _ => {
            return Err(
                "The issue worker is not running. Use Run now to start a single cycle.".into(),
            )
        }
    }
    let request_path = run_now_request_path(&config);
    if let Some(parent) = request_path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    std::fs::write(&request_path, "requested by the SWARM Automation app\n")
        .map_err(|error| format!("Could not ask the worker to scan now: {error}"))?;
    processes::emit_log(
        &app,
        &log_path,
        "Issue worker scheduler",
        "system",
        "Run now: asked the running scheduler to scan every repository immediately.",
    );
    Ok(
        "Scanning every repository now; the timer for the next check restarts after this cycle."
            .into(),
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
    router_model: String,
    router_effort: String,
    strengths: String,
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
            router_model: provider.router_model.clone(),
            router_effort: provider.router_effort.clone(),
            strengths: provider.strengths.clone(),
            bin: tools::find_executable(&provider.id, &provider.bin).unwrap_or_default(),
            enabled: provider.enabled,
        })
        .collect()
}

/// Provider flags forwarded by the scheduler to every repository worker.
/// Router settings travel even while dynamic routing is off so a later save
/// can turn it on without the worker forgetting the configured router.
fn provider_scheduler_arguments(config: &AppConfig, providers: &[ResolvedProvider]) -> Vec<String> {
    let mut arguments = Vec::new();
    for provider in providers {
        arguments.extend([format!("--{}-model", provider.id), provider.model.clone()]);
        arguments.extend([format!("--{}-effort", provider.id), provider.effort.clone()]);
        arguments.extend([
            format!("--{}-router-model", provider.id),
            provider.router_model.clone(),
        ]);
        arguments.extend([
            format!("--{}-router-effort", provider.id),
            provider.router_effort.clone(),
        ]);
        arguments.extend([
            format!("--{}-router-strengths", provider.id),
            provider.strengths.clone(),
        ]);
        arguments.extend([
            format!("--{}-bin", provider.id),
            provider.bin.to_string_lossy().into_owned(),
        ]);
        if provider.enabled {
            arguments.extend(["--enabled-provider".into(), provider.id.clone()]);
        }
    }
    arguments.push(
        if config.dynamic_model_routing {
            "--dynamic-model-routing"
        } else {
            "--no-dynamic-model-routing"
        }
        .into(),
    );
    arguments.extend([
        "--routing-tiers".into(),
        serde_json::to_string(&config.routing_tiers).unwrap_or_else(|_| "{}".into()),
    ]);
    arguments
}

/// Builds the test scheduler's view of "can AI help with a gap right now" —
/// the repository's own on/off switch plus every provider's resolved binary,
/// so `testing::check_ai_capability`/`generate_suite_ai_test_data` can probe
/// usage without needing an `AppConfig` of their own. `python3` falls back to
/// the bare command name (rather than erroring) so a missing interpreter
/// surfaces as "AI unavailable" for the suites that need it, not as a reason
/// to refuse deterministic test runs entirely.
fn resolve_ai_run_options(
    config: &AppConfig,
    repo: &RepoConfig,
    script_dir: &Path,
) -> testing::AiRunOptions {
    testing::AiRunOptions {
        enabled: repo.uat_ai_test_data_enabled,
        python_bin: tools::find_executable("python3", &config.python_bin)
            .map(|path| path.to_string_lossy().into_owned())
            .unwrap_or_else(|| "python3".into()),
        script_dir: script_dir.to_path_buf(),
        minimum_remaining_percent: config.minimum_remaining_percent,
        providers: resolve_providers(config)
            .into_iter()
            .map(|provider| testing::AiProviderOption {
                id: provider.id,
                bin: provider.bin.to_string_lossy().into_owned(),
                model: provider.model,
                enabled: provider.enabled,
            })
            .collect(),
    }
}

/// Global scheduler flags for `install_swarm_issue_cron.py`. Per-repo detail
/// lives in the `--repos-file`; unknown provider flags added by the
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
    if config.parallel_repo_workers {
        arguments.push("--parallel-repos".into());
    }
    if run_once {
        arguments.push("--once".into());
    }
    arguments
}

/// The single, app-wide SQLite database shared by every repository's AI
/// execution history — deliberately not per-repo, so the Feedback view can
/// query it without a workspace being prepared.
fn execution_history_db_path(config: &AppConfig) -> PathBuf {
    PathBuf::from(&config.worker_state_dir).join("swarm-automation.sqlite3")
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
        if repo.auto_approve {
            "--auto-merge"
        } else {
            "--no-auto-merge"
        }
        .into(),
        if repo.auto_promote {
            "--auto-promote"
        } else {
            "--no-auto-promote"
        }
        .into(),
        if repo.monitor_actions {
            "--monitor-actions"
        } else {
            "--no-monitor-actions"
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
        if config.ai_execution_history_enabled {
            "--ai-execution-history-enabled"
        } else {
            "--no-ai-execution-history-enabled"
        }
        .into(),
        if config.prompt_feedback_upload_enabled {
            "--prompt-feedback-upload-enabled"
        } else {
            "--no-prompt-feedback-upload-enabled"
        }
        .into(),
        "--application-version".into(),
        env!("CARGO_PKG_VERSION").into(),
        "--execution-history-db".into(),
        execution_history_db_path(config)
            .to_string_lossy()
            .into_owned(),
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
    let repo = resolve_repo(&config, &repo_id)?;
    let workspace = prepared_workspace(&app, &config, repo)?;
    if !testing::definition_path(&workspace).is_file() {
        return Err(format!(
            "Add {} to this repository to describe the tests to run.",
            testing::TEST_DEFINITION_PATH
        ));
    }
    // Validate before spawning so a malformed repository definition is a clear
    // configuration error, never an opaque failed test process.
    testing::load_definition(&workspace)?;
    // Refresh the non-secret runner handoff on every start. This deliberately
    // drops session-only values left by an earlier app process.
    let mut runner_inputs = repo.test_inputs.clone();
    if let Some(session) = state
        .test_input_sessions
        .lock()
        .map_err(|_| "Test input session lock was poisoned")?
        .get(&repo.id)
    {
        runner_inputs.extend(session.clone());
    }
    testing::save_inputs(&repo.effective_run_dir(&workspace), &runner_inputs)?;
    let program = std::env::current_exe()
        .map_err(|error| format!("Could not locate the test runner: {error}"))?;
    let mut arguments = vec![
        "--swarm-test-runner".into(),
        "--workspace".into(),
        workspace.to_string_lossy().into_owned(),
        "--run-dir".into(),
        repo.effective_run_dir(&workspace)
            .to_string_lossy()
            .into_owned(),
        "--repository".into(),
        repo.github_repository.clone(),
        "--device".into(),
        repo.test_inputs
            .get("fireTvSerial")
            .cloned()
            .unwrap_or_default(),
        "--hour".into(),
        repo.uat_hour.to_string(),
    ];
    if repo.allow_disruptive_tests {
        arguments.push("--allow-disruptive".into());
    }
    if repo.uat_triage_enabled {
        arguments.push("--triage".into());
    }
    if run_once {
        arguments.push("--once".into());
    }
    // AI gap-filling is best-effort: a repository with no Python/AI resources
    // bundled still gets an ordinary, fully deterministic test run — suites
    // that ask for `aiTestData` simply come back "Not executed".
    if let Ok(script_dir) = worker_script_dir(&app) {
        let ai = resolve_ai_run_options(&config, repo, &script_dir);
        if ai.enabled {
            arguments.push("--ai-test-data-enabled".into());
        }
        arguments.extend([
            "--python-bin".into(),
            ai.python_bin,
            "--script-dir".into(),
            ai.script_dir.to_string_lossy().into_owned(),
            "--minimum-remaining-percent".into(),
            ai.minimum_remaining_percent.to_string(),
        ]);
        for provider in &ai.providers {
            arguments.extend([format!("--{}-bin", provider.id), provider.bin.clone()]);
            arguments.extend([format!("--{}-model", provider.id), provider.model.clone()]);
            if provider.enabled {
                arguments.extend(["--enabled-provider".into(), provider.id.clone()]);
            }
        }
    }
    state.processes.spawn(
        &app,
        &format!("uat:{}", repo.id),
        &format!("Test scheduler · {}", repo.label()),
        &program,
        &arguments,
        &[("PATH".to_string(), tools::enhanced_path())],
        &workspace,
        automation_log_path(&app)?,
    )
}

#[tauri::command]
fn get_test_plan<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    state: State<'_, AppState>,
    repo_id: String,
) -> Result<testing::TestPlan, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?;
    let workspace = resolve_workspace(&app, &config, repo)?;
    let mut inputs = repo.test_inputs.clone();
    if let Some(session) = state
        .test_input_sessions
        .lock()
        .map_err(|_| "Test input session lock was poisoned")?
        .get(&repo.id)
    {
        inputs.extend(session.clone());
    }
    Ok(testing::build_plan(
        &workspace,
        &repo.effective_run_dir(&workspace),
        &repo.id,
        &inputs,
        repo.allow_disruptive_tests,
    ))
}

#[tauri::command]
fn detect_test_definition<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    state: State<'_, AppState>,
    repo_id: String,
) -> Result<testing::TestDefinitionDraft, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?;
    let workspace = prepared_workspace(&app, &config, repo)?;
    // AI-assisted discovery is best-effort: a repository with no Python/AI
    // resources bundled still gets ordinary deterministic detection.
    let ai = worker_script_dir(&app)
        .ok()
        .map(|script_dir| resolve_ai_run_options(&config, repo, &script_dir));
    let mut draft = testing::detect_definition(&workspace, ai.as_ref())?;
    // A committed definition is the authoritative one; detection may be
    // re-run for review at any time, but `create_test_definition` refuses to
    // overwrite it, so this is always safe to regenerate.
    if testing::definition_path(&workspace).exists() {
        draft.notes.insert(
            0,
            format!(
                "{} already exists and is authoritative; this draft is for review only and will not overwrite it.",
                testing::TEST_DEFINITION_PATH
            ),
        );
    }
    Ok(draft)
}

#[tauri::command]
fn audit_test_coverage<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    state: State<'_, AppState>,
    repo_id: String,
) -> Result<test_discovery::CoverageAudit, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?;
    let workspace = resolve_workspace(&app, &config, repo)?;
    let definition = testing::load_definition(&workspace)?;
    let candidates = test_discovery::discover_candidates(&workspace)?;
    Ok(test_discovery::audit_coverage(
        &candidates,
        &definition.suites,
    ))
}

#[tauri::command]
fn create_test_definition<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    state: State<'_, AppState>,
    repo_id: String,
    definition: String,
) -> Result<String, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?;
    let workspace = prepared_workspace(&app, &config, repo)?;
    testing::create_definition(&workspace, &definition)
        .map(|path| path.to_string_lossy().into_owned())
}

#[tauri::command]
async fn get_test_plan_background(
    app: tauri::AppHandle,
    repo_id: String,
) -> Result<testing::TestPlan, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        get_test_plan(app.clone(), state, repo_id)
    })
    .await
    .map_err(|error| format!("Test requirement discovery failed: {error}"))?
}

#[tauri::command]
fn get_test_runs<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    state: State<'_, AppState>,
    repo_id: String,
) -> Result<Vec<testing::TestRunResults>, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?;
    let workspace = resolve_workspace(&app, &config, repo)?;
    Ok(testing::list_runs(&repo.effective_run_dir(&workspace)))
}

#[tauri::command]
async fn get_test_runs_background(
    app: tauri::AppHandle,
    repo_id: String,
) -> Result<Vec<testing::TestRunResults>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        get_test_runs(app.clone(), state, repo_id)
    })
    .await
    .map_err(|error| format!("Test run history lookup failed: {error}"))?
}

/// One sanitized `ai_executions` row (see `ai_execution_history.py`). Field
/// names deserialize from the Python CLI's snake_case JSON but serialize to
/// the frontend as camelCase, matching every other struct in this file.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase", deserialize = "snake_case"))]
struct AiExecutionRecord {
    execution_id: String,
    repository: String,
    issue_number: i64,
    #[serde(default)]
    issue_url: String,
    issue_title: String,
    #[serde(default)]
    original_issue_body: String,
    #[serde(default)]
    effective_prompt: String,
    ai_provider: String,
    #[serde(default)]
    model: String,
    #[serde(default)]
    effort: String,
    #[serde(default)]
    reasoning_config: serde_json::Value,
    started_at: String,
    #[serde(default)]
    completed_at: Option<String>,
    #[serde(default)]
    duration_seconds: Option<f64>,
    #[serde(default)]
    requested_work_summary: String,
    #[serde(default)]
    changes_summary: String,
    #[serde(default)]
    files_changed: Vec<String>,
    #[serde(default)]
    branch_name: String,
    #[serde(default)]
    commit_shas: Vec<String>,
    #[serde(default)]
    pull_request_number: Option<i64>,
    #[serde(default)]
    pull_request_url: String,
    #[serde(default)]
    operational_notes: Vec<String>,
    #[serde(default)]
    warnings_errors: Vec<String>,
    final_status: String,
    attempt_number: i64,
    #[serde(default)]
    application_version: String,
    #[serde(default)]
    prompt_template_version: String,
    updated_at: String,
    #[serde(default)]
    uploaded_at: Option<String>,
    #[serde(default)]
    upload_status: String,
    #[serde(default)]
    upload_error: String,
    #[serde(default)]
    uploaded_record_updated_at: Option<String>,
    #[serde(default)]
    reviewer_feedback: String,
    #[serde(default)]
    reviewer_feedback_at: Option<String>,
    /// Dynamic-routing record for this attempt. Null when routing was off.
    #[serde(default)]
    routing_decision: serde_json::Value,
}

/// Feedback never asks the history CLI for more than this many rows.
const EXECUTION_HISTORY_PAGE_SIZE: i64 = 10;

/// One page of `ai_executions`. `records` deserialize from the Python CLI's
/// snake_case rows and serialize to the frontend as camelCase.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ExecutionHistoryPage {
    records: Vec<AiExecutionRecord>,
    total: i64,
    offset: i64,
    limit: i64,
}

fn normalize_execution_history_search(search: Option<String>) -> String {
    search
        .unwrap_or_default()
        .trim()
        .chars()
        .take(200)
        .collect()
}

fn execution_history_query_args(
    script: &Path,
    database: &Path,
    repository: &str,
    offset: Option<i64>,
    search: Option<String>,
) -> Vec<String> {
    // `--limit` is always sent. Omitting it makes the CLI print every row.
    vec![
        script.to_string_lossy().into_owned(),
        "--db".into(),
        database.to_string_lossy().into_owned(),
        "--repository".into(),
        repository.to_string(),
        "--limit".into(),
        EXECUTION_HISTORY_PAGE_SIZE.to_string(),
        "--offset".into(),
        offset.unwrap_or(0).max(0).to_string(),
        "--search".into(),
        normalize_execution_history_search(search),
    ]
}

fn empty_execution_history_page() -> ExecutionHistoryPage {
    ExecutionHistoryPage {
        records: Vec::new(),
        total: 0,
        offset: 0,
        limit: EXECUTION_HISTORY_PAGE_SIZE,
    }
}

#[tauri::command]
fn get_execution_history<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    state: State<'_, AppState>,
    repo_id: String,
    offset: Option<i64>,
    search: Option<String>,
) -> Result<ExecutionHistoryPage, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?;
    let database_path = execution_history_db_path(&config);
    if !database_path.is_file() {
        return Ok(empty_execution_history_page());
    }
    let script = worker_script_dir(&app)?.join("ai_execution_history.py");
    let python = tools::configured_or_detected(&config.python_bin, "python3")?;
    let (ok, raw) = run_capture_owned(
        &python,
        &execution_history_query_args(
            &script,
            &database_path,
            &repo.github_repository,
            offset,
            search,
        ),
    );
    if !ok {
        return Err(format!("Execution history lookup failed: {raw}"));
    }
    serde_json::from_str(raw.trim())
        .map_err(|error| format!("Execution history response could not be parsed: {error}"))
}

#[tauri::command]
async fn get_execution_history_background(
    app: tauri::AppHandle,
    repo_id: String,
    offset: Option<i64>,
    search: Option<String>,
) -> Result<ExecutionHistoryPage, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        get_execution_history(app.clone(), state, repo_id, offset, search)
    })
    .await
    .map_err(|error| format!("Execution history lookup failed: {error}"))?
}

/// One graded execution row from `ai_execution_history.py --grades`: just
/// what the Feedback grades panel shows, never the issue body or prompt.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all(serialize = "camelCase", deserialize = "snake_case"))]
struct PromptGradeRecord {
    issue_number: i64,
    #[serde(default)]
    issue_title: String,
    #[serde(default)]
    issue_url: String,
    attempt_number: i64,
    started_at: String,
    #[serde(default)]
    ai_provider: String,
    #[serde(default)]
    model: String,
    #[serde(default)]
    effort: String,
    #[serde(default)]
    final_status: String,
    /// The router's decision, including `prompt_grade` and `grade_reason`.
    #[serde(default)]
    routing_decision: serde_json::Value,
}

/// Every graded execution's tally; the desktop reads these camelCase keys.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PromptGradeSummary {
    #[serde(default)]
    graded: i64,
    #[serde(default)]
    average_points: Option<f64>,
    #[serde(default)]
    average_grade: String,
    #[serde(default)]
    distribution: std::collections::BTreeMap<String, i64>,
}

/// How often one grading platform picked one worker platform.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PromptGradeRouterSelection {
    #[serde(default)]
    provider: String,
    #[serde(default)]
    count: i64,
    /// Share of that router's own graded issues, already rounded by Python.
    #[serde(default)]
    percent: f64,
}

/// How often one grading platform used one router model.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PromptGradeRouterModel {
    #[serde(default)]
    model: String,
    #[serde(default)]
    count: i64,
    /// Share of that router's own graded issues, already rounded by Python.
    #[serde(default)]
    percent: f64,
}

/// One grading platform's picks: which AI tools its pre-flight router chose,
/// and how often. Lets the desktop show router bias without a second query.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PromptGradeRouterRow {
    #[serde(default)]
    router: String,
    #[serde(default)]
    graded: i64,
    #[serde(default)]
    models: Vec<PromptGradeRouterModel>,
    #[serde(default)]
    selections: Vec<PromptGradeRouterSelection>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PromptGradesPage {
    records: Vec<PromptGradeRecord>,
    total: i64,
    offset: i64,
    limit: i64,
    #[serde(default)]
    summary: PromptGradeSummary,
    /// Every grading platform in the current search, even when one of them is
    /// the active filter, so the desktop can always offer another.
    #[serde(default)]
    router_matrix: Vec<PromptGradeRouterRow>,
}

fn normalize_prompt_grade(grade: Option<String>) -> String {
    // The history CLI ignores anything that is not a router grade. Cap the
    // argument so a huge string cannot reach the process argv.
    grade.unwrap_or_default().trim().chars().take(8).collect()
}

fn normalize_router_filter(router: Option<String>) -> String {
    // Same contract as the grade filter: the history CLI ignores anything that
    // is not a provider key, and the argument is capped before it reaches argv.
    router
        .unwrap_or_default()
        .trim()
        .to_lowercase()
        .chars()
        .take(40)
        .collect()
}

fn normalize_router_model_filter(model: Option<String>) -> String {
    model.unwrap_or_default().trim().chars().take(120).collect()
}

/// Filters for one page of prompt grades, passed as a single command argument.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PromptGradesQuery {
    #[serde(default)]
    offset: Option<i64>,
    #[serde(default)]
    search: Option<String>,
    #[serde(default)]
    grade: Option<String>,
    #[serde(default)]
    router: Option<String>,
    #[serde(default)]
    router_model: Option<String>,
}

fn prompt_grades_query_args(
    script: &Path,
    database: &Path,
    repository: &str,
    query: PromptGradesQuery,
) -> Vec<String> {
    // `--limit` is always sent, matching execution history. Omitting it would
    // still page grades, but the desktop always asks for one page explicitly.
    vec![
        script.to_string_lossy().into_owned(),
        "--db".into(),
        database.to_string_lossy().into_owned(),
        "--repository".into(),
        repository.to_string(),
        "--grades".into(),
        "--limit".into(),
        EXECUTION_HISTORY_PAGE_SIZE.to_string(),
        "--offset".into(),
        query.offset.unwrap_or(0).max(0).to_string(),
        "--search".into(),
        normalize_execution_history_search(query.search),
        "--grade".into(),
        normalize_prompt_grade(query.grade),
        "--router".into(),
        normalize_router_filter(query.router),
        "--router-model".into(),
        normalize_router_model_filter(query.router_model),
    ]
}

#[tauri::command]
fn get_prompt_grades<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    state: State<'_, AppState>,
    repo_id: String,
    query: PromptGradesQuery,
) -> Result<PromptGradesPage, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?;
    let database_path = execution_history_db_path(&config);
    if !database_path.is_file() {
        return Ok(PromptGradesPage {
            records: Vec::new(),
            total: 0,
            offset: 0,
            limit: EXECUTION_HISTORY_PAGE_SIZE,
            summary: PromptGradeSummary::default(),
            router_matrix: Vec::new(),
        });
    }
    let script = worker_script_dir(&app)?.join("ai_execution_history.py");
    let python = tools::configured_or_detected(&config.python_bin, "python3")?;
    let (ok, raw) = run_capture_owned(
        &python,
        &prompt_grades_query_args(&script, &database_path, &repo.github_repository, query),
    );
    if !ok {
        return Err(format!("Prompt grades lookup failed: {raw}"));
    }
    serde_json::from_str(raw.trim())
        .map_err(|error| format!("Prompt grades response could not be parsed: {error}"))
}

#[tauri::command]
async fn get_prompt_grades_background(
    app: tauri::AppHandle,
    repo_id: String,
    query: PromptGradesQuery,
) -> Result<PromptGradesPage, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        get_prompt_grades(app.clone(), state, repo_id, query)
    })
    .await
    .map_err(|error| format!("Prompt grades lookup failed: {error}"))?
}

/// Summary of `ai_execution_history.py --import-from-github`: every open and
/// closed issue in the repo's GitHub backlog that had no existing execution
/// history row got a synthetic `imported` one added.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ExecutionHistoryImportSummary {
    total_issues: i64,
    imported: i64,
    skipped: i64,
}

#[tauri::command]
fn import_execution_history<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    state: State<'_, AppState>,
    repo_id: String,
) -> Result<ExecutionHistoryImportSummary, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?;
    let database_path = execution_history_db_path(&config);
    let script = worker_script_dir(&app)?.join("ai_execution_history.py");
    let python = tools::configured_or_detected(&config.python_bin, "python3")?;
    let gh = tools::configured_or_detected(&config.gh_bin, "gh")?;
    let (ok, raw) = run_capture_owned(
        &python,
        &[
            script.to_string_lossy().into_owned(),
            "--db".into(),
            database_path.to_string_lossy().into_owned(),
            "--repository".into(),
            repo.github_repository.clone(),
            "--import-from-github".into(),
            "--gh-bin".into(),
            gh.to_string_lossy().into_owned(),
        ],
    );
    if !ok {
        return Err(format!(
            "Importing GitHub issues into execution history failed: {raw}"
        ));
    }
    serde_json::from_str(raw.trim())
        .map_err(|error| format!("Import summary could not be parsed: {error}"))
}

#[tauri::command]
async fn import_execution_history_background(
    app: tauri::AppHandle,
    repo_id: String,
) -> Result<ExecutionHistoryImportSummary, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        import_execution_history(app.clone(), state, repo_id)
    })
    .await
    .map_err(|error| format!("Importing GitHub issues into execution history failed: {error}"))?
}

#[tauri::command]
fn save_test_input<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    state: State<'_, AppState>,
    repo_id: String,
    key: String,
    value: Option<String>,
) -> Result<AppConfig, String> {
    let mut config = current_config(&state)?;
    let repo_index = config
        .repositories
        .iter()
        .position(|repo| repo.id == repo_id)
        .ok_or_else(|| format!("Unknown repository id: {repo_id}"))?;
    let repo_snapshot = config.repositories[repo_index].clone();
    let workspace = resolve_workspace(&app, &config, &repo_snapshot)?;
    let definition = testing::load_definition(&workspace)?;
    let input = definition
        .inputs
        .iter()
        .find(|input| input.id == key)
        .ok_or_else(|| format!("The test definition does not declare input '{key}'"))?;
    let normalized = value
        .map(|value| {
            if input.input_type == "secret" {
                value
            } else {
                value.trim().to_string()
            }
        })
        .filter(|value| !value.is_empty());
    match input.persistence.as_str() {
        "keychain" | "os-keychain" | "osKeychain" => {
            testing::save_secret(&repo_id, &key, normalized.as_deref())?;
            config.repositories[repo_index].test_inputs.remove(&key);
            if let Some(session) = state
                .test_input_sessions
                .lock()
                .map_err(|_| "Test input session lock was poisoned")?
                .get_mut(&repo_id)
            {
                session.remove(&key);
            }
        }
        "session-only" => {
            let mut sessions = state
                .test_input_sessions
                .lock()
                .map_err(|_| "Test input session lock was poisoned")?;
            let repo_values = sessions.entry(repo_id.clone()).or_default();
            if let Some(value) = normalized {
                repo_values.insert(key.clone(), value);
            } else {
                repo_values.remove(&key);
            }
        }
        _ => {
            let repo = &mut config.repositories[repo_index];
            if let Some(value) = normalized {
                repo.test_inputs.insert(key.clone(), value);
            } else {
                repo.test_inputs.remove(&key);
            }
        }
    }
    // The runner is a separate closed-stdin process. This file is only the
    // non-secret handoff; keychain values are loaded by the runner itself.
    let mut selected_inputs = config.repositories[repo_index].test_inputs.clone();
    if let Some(session) = state
        .test_input_sessions
        .lock()
        .map_err(|_| "Test input session lock was poisoned")?
        .get(&repo_id)
    {
        selected_inputs.extend(session.clone());
    }
    if workspace.is_dir() {
        testing::save_inputs(
            &repo_snapshot.effective_run_dir(&workspace),
            &selected_inputs,
        )?;
    }
    config::save(&app_config_path(&app)?, &config)?;
    *state
        .config
        .lock()
        .map_err(|_| "Configuration state lock was poisoned".to_string())? = config.clone();
    Ok(config)
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

/// Per-provider readiness of the GitHub bot app for one repository, phrased for
/// a non-expert: which concrete GitHub step (if any) is still outstanding and
/// the URL that completes it.
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct BotReadiness {
    provider: String,
    provider_label: String,
    /// `ready` | `not_installed_on_owner` | `no_repo_access` | `unconfigured` | `error`
    state: String,
    ready: bool,
    owner: String,
    message: String,
    /// GitHub page that resolves this step (install / grant). Empty for
    /// `unconfigured`, where the manifest setup flow is the next step instead.
    action_url: String,
    action_label: String,
    needs_setup_flow: bool,
}

/// Argv for one `github_app_auth.py repo-status` call. `--repository` and
/// `--provider` belong to the `repo-status` subcommand, not to the top-level
/// parser, so the subcommand name has to come first -- argparse rejects the
/// whole invocation otherwise, and the desktop UI then renders that failure as
/// "this bot needs setup" even when the GitHub App is fully installed (#48).
fn repo_status_args(
    script: &Path,
    apps_config: &str,
    repository: &str,
    provider: &str,
) -> Vec<String> {
    vec![
        script.to_string_lossy().into_owned(),
        "--config".into(),
        apps_config.to_string(),
        "repo-status".into(),
        "--repository".into(),
        repository.to_string(),
        "--provider".into(),
        provider.to_string(),
    ]
}

#[tauri::command]
fn check_repo_bot_readiness<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    state: State<'_, AppState>,
    repo_id: String,
) -> Result<Vec<BotReadiness>, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?;
    let script = worker_script_dir(&app)?.join("github_app_auth.py");
    let python = tools::configured_or_detected(&config.python_bin, "python3")?;
    let apps_config = repo.effective_apps_config();
    let apps_config_exists = Path::new(&apps_config).is_file();
    let providers: Vec<String> = config
        .enabled_providers()
        .map(|provider| provider.id.clone())
        .collect();
    Ok(providers
        .into_iter()
        .map(|id| {
            let label = config::provider_label(&id).to_string();
            let default_url = format!("https://github.com/apps/swarm-{id}-bot/installations/new");
            if !apps_config_exists {
                return BotReadiness {
                    provider: id.clone(),
                    provider_label: label,
                    state: "unconfigured".into(),
                    ready: false,
                    owner: String::new(),
                    message: format!(
                        "The {id} bot app has not been created yet. Run “Set up GitHub Apps”."
                    ),
                    action_url: String::new(),
                    action_label: String::new(),
                    needs_setup_flow: true,
                };
            }
            let (_ok, raw) = run_capture_owned(
                &python,
                &repo_status_args(&script, &apps_config, &repo.github_repository, &id),
            );
            let parsed: serde_json::Value = serde_json::from_str(raw.trim()).unwrap_or_default();
            let field = |key: &str| {
                parsed
                    .get(key)
                    .and_then(|value| value.as_str())
                    .unwrap_or_default()
                    .to_string()
            };
            let state_name = match parsed.get("state").and_then(|v| v.as_str()) {
                Some(value) => value.to_string(),
                None => "error".to_string(),
            };
            let message = {
                let candidate = field("message");
                if candidate.is_empty() {
                    raw.trim().to_string()
                } else {
                    candidate
                }
            };
            let action_url = {
                let candidate = field("installUrl");
                if candidate.is_empty() {
                    default_url.clone()
                } else {
                    candidate
                }
            };
            let (action_label, needs_setup_flow) = match state_name.as_str() {
                "ready" => (String::new(), false),
                "unconfigured" => (String::new(), true),
                "no_repo_access" => ("Open GitHub to grant access".into(), false),
                _ => ("Open GitHub to install".into(), false),
            };
            BotReadiness {
                provider: id.clone(),
                provider_label: label,
                ready: state_name == "ready",
                owner: field("owner"),
                message,
                action_url: if state_name == "ready" || needs_setup_flow {
                    String::new()
                } else {
                    action_url
                },
                action_label,
                needs_setup_flow,
                state: state_name,
            }
        })
        .collect())
}

// ----- Software update ----------------------------------------------------

/// `owner/name` this app checks its own releases against — the GitHub side
/// of `tauri.conf.json`'s updater endpoint and Cargo.toml's `repository`.
const SELF_UPDATE_REPO: &str = "SWARM-Media-Steaming/swarm-automation";

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct UpdateSummary {
    current_version: String,
    version: String,
    notes: String,
    pub_date: String,
}

/// One release the "Check Now" version picker can show. `channel` is
/// `"release"` (`main`, never prerelease) or `"beta"` (`ai-main`, always
/// prerelease).
#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct UpdateCandidate {
    tag: String,
    version: String,
    channel: String,
    published_at: String,
    /// Strictly newer than the running version — true downgrade (installing
    /// something older) is out of scope for now; see `install_update_candidate`.
    installable: bool,
    /// The newest published entry in its channel. This app can only install
    /// these two today (see `install_update_candidate`'s doc comment); the
    /// rest of the 3+3 list is shown for context only.
    direct_install: bool,
}

#[derive(Deserialize, Clone)]
struct GhRelease {
    #[serde(rename = "tagName")]
    tag_name: String,
    #[serde(rename = "isDraft")]
    is_draft: bool,
    #[serde(rename = "isPrerelease")]
    is_prerelease: bool,
    #[serde(rename = "publishedAt", default)]
    published_at: Option<String>,
}

fn running_app_version() -> &'static str {
    env!("SWARM_APP_VERSION")
}

/// The exact version this build was published as, including a `-beta.<n>`
/// or `+main.<n>` suffix when present — baked in at compile time by
/// `build.rs` from the release workflow's `SWARM_APP_VERSION`, not just
/// whatever is hand-written in Cargo.toml.
#[tauri::command]
fn app_version() -> String {
    running_app_version().to_string()
}

fn parse_semver(raw: &str) -> Option<semver::Version> {
    semver::Version::parse(raw.trim().trim_start_matches('v')).ok()
}

async fn list_releases(gh: PathBuf) -> Result<Vec<GhRelease>, String> {
    let (ok, out) = tauri::async_runtime::spawn_blocking(move || {
        run_capture_owned(
            &gh,
            &[
                "release".into(),
                "list".into(),
                "--repo".into(),
                SELF_UPDATE_REPO.into(),
                "--limit".into(),
                "30".into(),
                "--json".into(),
                "tagName,isDraft,isPrerelease,publishedAt".into(),
            ],
        )
    })
    .await
    .map_err(|error| format!("Release lookup failed: {error}"))?;
    if !ok {
        return Err(format!("Could not list GitHub releases: {out}"));
    }
    let mut releases: Vec<GhRelease> = serde_json::from_str(&out)
        .map_err(|error| format!("Unexpected release list from GitHub: {error}"))?;
    releases.retain(|release| !release.is_draft);
    releases.sort_by(|a, b| b.published_at.cmp(&a.published_at));
    Ok(releases)
}

/// The newest non-prerelease and newest prerelease release currently
/// published, if any.
fn top_channel_releases(releases: &[GhRelease]) -> (Option<&GhRelease>, Option<&GhRelease>) {
    let top_release = releases.iter().find(|release| !release.is_prerelease);
    let top_beta = releases.iter().find(|release| release.is_prerelease);
    (top_release, top_beta)
}

#[tauri::command]
async fn list_update_candidates<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
) -> Result<Vec<UpdateCandidate>, String> {
    let gh = {
        let config = current_config(&app.state::<AppState>())?;
        tools::configured_or_detected(&config.gh_bin, "gh")?
    };
    let releases = list_releases(gh).await?;
    let current = parse_semver(running_app_version())
        .ok_or_else(|| "Could not parse the running app version.".to_string())?;

    let mut candidates = Vec::new();
    for prerelease in [false, true] {
        let channel = if prerelease { "beta" } else { "release" };
        for (index, release) in releases
            .iter()
            .filter(|release| release.is_prerelease == prerelease)
            .take(3)
            .enumerate()
        {
            let Some(version) = parse_semver(&release.tag_name) else {
                continue;
            };
            candidates.push(UpdateCandidate {
                tag: release.tag_name.clone(),
                version: version.to_string(),
                channel: channel.into(),
                published_at: release.published_at.clone().unwrap_or_default(),
                installable: version > current,
                direct_install: index == 0,
            });
        }
    }
    Ok(candidates)
}

/// Best-effort: a bundle updated from a downloaded archive can inherit the
/// `com.apple.quarantine` xattr, which makes Gatekeeper re-evaluate on the
/// next launch. Strip it from the running `.app` before we relaunch.
#[cfg(target_os = "macos")]
fn strip_quarantine() {
    if let Ok(exe) = std::env::current_exe() {
        // <App>.app/Contents/MacOS/<bin> -> <App>.app
        if let Some(bundle) = exe.ancestors().nth(3) {
            let _ = Command::new("/usr/bin/xattr")
                .args(["-dr", "com.apple.quarantine"])
                .arg(bundle)
                .status();
        }
    }
}

#[cfg(not(target_os = "macos"))]
fn strip_quarantine() {}

async fn pending_update<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
) -> Result<Option<tauri_plugin_updater::Update>, String> {
    use tauri_plugin_updater::UpdaterExt;
    app.updater()
        .map_err(|error| error.to_string())?
        .check()
        .await
        .map_err(|error| error.to_string())
}

#[tauri::command]
async fn check_for_update<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
) -> Result<Option<UpdateSummary>, String> {
    Ok(pending_update(&app).await?.map(|update| UpdateSummary {
        current_version: update.current_version.clone(),
        version: update.version.clone(),
        notes: update.body.clone().unwrap_or_default(),
        pub_date: update.date.map(|date| date.to_string()).unwrap_or_default(),
    }))
}

#[tauri::command]
async fn install_update<R: tauri::Runtime>(app: tauri::AppHandle<R>) -> Result<(), String> {
    let Some(update) = pending_update(&app).await? else {
        return Err("No update is available.".into());
    };
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|error| error.to_string())?;
    strip_quarantine();
    app.restart()
}

/// Install a specific "Check Now" candidate. Only the newest release and
/// newest beta build (`direct_install` in `UpdateCandidate`) can actually be
/// installed today: those are the only two manifests this app knows how to
/// resolve safely — the default endpoint for the release channel, and that
/// beta tag's own per-release manifest for the beta channel. Installing an
/// older, non-latest entry from either channel (context-only in the picker)
/// would need fetching and verifying that specific release's signed
/// artifact directly, bypassing the updater plugin's own "is this newer"
/// resolution — the same open-ended work this app's true-downgrade path
/// deliberately leaves for a follow-up issue, so it is out of scope here too.
#[tauri::command]
async fn install_update_candidate<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    tag: String,
    channel: String,
) -> Result<(), String> {
    use tauri_plugin_updater::UpdaterExt;

    let gh = {
        let config = current_config(&app.state::<AppState>())?;
        tools::configured_or_detected(&config.gh_bin, "gh")?
    };
    let releases = list_releases(gh).await?;
    let (top_release, top_beta) = top_channel_releases(&releases);
    let expected = match channel.as_str() {
        "release" => top_release,
        "beta" => top_beta,
        _ => return Err(format!("Unknown update channel: {channel}")),
    };
    if expected.map(|release| release.tag_name.as_str()) != Some(tag.as_str()) {
        return Err(
            "Only the newest release and newest beta build can be installed directly today; \
             this entry is shown for context only."
                .into(),
        );
    }

    let update = if channel == "beta" {
        let endpoint =
            format!("https://github.com/{SELF_UPDATE_REPO}/releases/download/{tag}/latest.json");
        let url = url::Url::parse(&endpoint).map_err(|error| error.to_string())?;
        app.updater_builder()
            .endpoints(vec![url])
            .map_err(|error| error.to_string())?
            .build()
            .map_err(|error| error.to_string())?
            .check()
            .await
            .map_err(|error| error.to_string())?
    } else {
        pending_update(&app).await?
    };
    let Some(update) = update else {
        return Err("That version is no longer available to install.".into());
    };
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|error| error.to_string())?;
    strip_quarantine();
    app.restart()
}

/// True once neither the issue worker nor any enabled repo's test scheduler
/// has a live process. Both are long-running, self-scheduling processes —
/// there is no finer "busy this instant" signal than that — but that is
/// also exactly the granularity that matters here: restarting this app to
/// apply an update always stops every tracked process (see the `RunEvent::
/// Exit` handler), so waiting for a slot to already be stopped is what
/// keeps that restart from ever cutting off live work.
fn workers_idle<R: tauri::Runtime>(app: &tauri::AppHandle<R>, config: &AppConfig) -> bool {
    let Ok(log_path) = automation_log_path(app) else {
        return false;
    };
    let state = app.state::<AppState>();
    let issue_idle = state
        .processes
        .status(app, "issue", "Issue worker scheduler", &log_path)
        .map(|status| status.state == "stopped")
        .unwrap_or(true);
    issue_idle
        && config.enabled_repos().all(|repo| {
            state
                .processes
                .status(
                    app,
                    &format!("uat:{}", repo.id),
                    &format!("Test scheduler · {}", repo.label()),
                    &log_path,
                )
                .map(|status| status.state == "stopped")
                .unwrap_or(true)
        })
}

/// Poll until `workers_idle` — never sends either process a stop signal.
/// Interrupting the issue worker mid-cycle can leave a git checkout stuck
/// with no recorded owner (`.claude/rules/issue-branch-delivery.md`), so an
/// "Automatically" install waits out real work instead of forcing it to
/// yield. Bails out if the user switches away from "Automatically" while
/// this is waiting.
async fn wait_for_workers_idle(app: &tauri::AppHandle) {
    loop {
        let config = {
            let state = app.state::<AppState>();
            let Ok(config) = state.config.lock() else {
                return;
            };
            if config.auto_update != "auto" {
                return;
            }
            config.clone()
        };
        if workers_idle(app, &config) {
            return;
        }
        tokio::time::sleep(std::time::Duration::from_secs(30)).await;
    }
}

/// Honour `auto_update` once at startup, off the UI thread. `"notify"`
/// emits `update-available` for the banner. `"auto"` ("Automatically" in
/// the UI) waits for the issue worker and every enabled repo's test
/// scheduler to go idle on their own (see `wait_for_workers_idle`), then
/// downloads, installs, and relaunches.
fn spawn_startup_update_check(app: &tauri::AppHandle) {
    let mode = {
        let state = app.state::<AppState>();
        let Ok(config) = state.config.lock() else {
            return;
        };
        config.auto_update.clone()
    };
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        let Ok(Some(update)) = pending_update(&handle).await else {
            return;
        };
        if mode == "auto" {
            wait_for_workers_idle(&handle).await;
            if update.download_and_install(|_, _| {}, || {}).await.is_ok() {
                strip_quarantine();
                handle.restart();
            }
        } else {
            let _ = handle.emit(
                "update-available",
                UpdateSummary {
                    current_version: update.current_version.clone(),
                    version: update.version.clone(),
                    notes: update.body.clone().unwrap_or_default(),
                    pub_date: update.date.map(|date| date.to_string()).unwrap_or_default(),
                },
            );
        }
    });
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

fn issue_branch_pr_is_visible(pull_request_state: Option<&str>) -> bool {
    // A branch may appear briefly before its PR is created. Keep that useful
    // in-progress state, but once GitHub associates a PR with the branch only
    // an open PR belongs in the active Branches and promotion tree.
    pull_request_state
        .map(|state| state.trim().eq_ignore_ascii_case("open"))
        .unwrap_or(true)
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

    // Closed and merged PRs are historical, not active promotion work. Hide
    // their remote refs even when GitHub branch deletion has not completed.
    // A branch with no PR remains visible while the worker is still preparing
    // or publishing its pull request.
    branches.retain(|(name, _, _)| {
        let head_ref = name
            .strip_prefix(&format!("{}/", repo.remote_name))
            .unwrap_or(name);
        issue_branch_pr_is_visible(pr_by_head.get(head_ref).map(|pr| pr.2.as_str()))
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
    // This used to also `git fetch --prune` the shared workspace here before
    // handing off to `git_overview` below — a second, redundant fetch of the
    // same workspace the issue worker may be actively committing in right
    // now, on top of the one `git_overview` already performs to build its
    // return value. Promotion can run concurrently with the issue worker
    // since it stopped requiring the worker to be stopped first (see
    // `reconcile_integration_for_promotion`'s doc comment); every avoidable
    // extra shared-workspace touch narrows that window, so this dropped the
    // redundant one. The remaining fetch inside `git_overview` is no more
    // than what any ordinary dashboard refresh already does at any time,
    // worker running or not.
    git_overview(app, state, repo_id)
}

/// Bring the human-owned branch into the AI integration branch inside a
/// throwaway clone of its own — never the shared workspace the issue worker
/// may be actively switching branches and committing in — so promotion can
/// run safely while the worker keeps running. When both branches changed the
/// same lines, the human-owned branch wins; non-overlapping AI work remains
/// intact. The caller can then create a conflict-free promotion PR without
/// disturbing the worker's or the user's checkout.
fn reconcile_integration_for_promotion(
    git: &Path,
    workspace: &Path,
    repo: &RepoConfig,
) -> Result<(), String> {
    // Resolve the remote URL from the shared workspace (a plain config read,
    // safe even while the issue worker is actively checking out and
    // committing there) so the isolated clone below authenticates and
    // targets the same remote, whatever it is — GitHub, a fork, or a local
    // path used in tests.
    let ws = workspace.to_string_lossy().into_owned();
    let (got_url, remote_url) = git_c(git, &ws, &["remote", "get-url", &repo.remote_name]);
    let remote_url = remote_url.trim().to_string();
    if !got_url || remote_url.is_empty() {
        return Err(format!(
            "Could not resolve the {} remote URL for promotion.",
            repo.remote_name
        ));
    }

    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let clone_dir =
        std::env::temp_dir().join(format!("swarm-promotion-{}-{nonce}", std::process::id()));
    let cleanup = || {
        let _ = std::fs::remove_dir_all(&clone_dir);
    };
    let (cloned, clone_message) = run_capture(
        git,
        &[
            "clone",
            "--origin",
            &repo.remote_name,
            "--",
            &remote_url,
            &clone_dir.to_string_lossy(),
        ],
    );
    if !cloned {
        cleanup();
        return Err(format!(
            "Could not prepare an isolated promotion clone: {clone_message}"
        ));
    }
    let cd = clone_dir.to_string_lossy().into_owned();

    let base_ref = format!("{}/{}", repo.remote_name, repo.base_branch);
    let integration_ref = format!("{}/{}", repo.remote_name, repo.integration_branch);
    for branch in [&base_ref, &integration_ref] {
        if !git_c(git, &cd, &["rev-parse", "--verify", "--quiet", branch]).0 {
            cleanup();
            return Err(format!("Remote branch {branch} does not exist."));
        }
    }
    let relation = ahead_behind(git, &cd, &base_ref, &integration_ref);
    if relation.behind == 0 {
        cleanup();
        return Ok(());
    }

    let (switched, switch_message) = git_c(git, &cd, &["switch", &repo.integration_branch]);
    if !switched {
        cleanup();
        return Err(format!(
            "Could not check out {} in the promotion clone: {switch_message}",
            repo.integration_branch
        ));
    }

    let merge_args = vec![
        "-C".to_string(),
        cd.clone(),
        "-c".into(),
        "user.name=SWARM Automation".into(),
        "-c".into(),
        "user.email=swarm-automation@users.noreply.github.com".into(),
        "merge".into(),
        "--no-edit".into(),
        "-X".into(),
        "theirs".into(),
        "-m".into(),
        format!("[{}] sync {}", repo.integration_branch, repo.base_branch),
        base_ref,
    ];
    let (merged, merge_message) = run_capture_owned(git, &merge_args);
    let result = if !merged {
        let _ = git_c(git, &cd, &["merge", "--abort"]);
        Err(format!(
            "Could not reconcile {} with {}: {merge_message}",
            repo.integration_branch, repo.base_branch
        ))
    } else {
        let (pushed, push_message) = git_c(
            git,
            &cd,
            &["push", &repo.remote_name, &repo.integration_branch],
        );
        if pushed {
            Ok(())
        } else {
            Err(format!(
                "Reconciled the branches locally but could not push {}: {push_message}",
                repo.integration_branch
            ))
        }
    };
    cleanup();
    result
}

fn ensure_integration_pr_ref(gh: &Path, repo: &RepoConfig) -> Result<(u64, String), String> {
    if let Some(reference) = open_integration_pr_ref(gh, repo) {
        return Ok(reference);
    }
    let (create_ok, create_out) = run_capture_owned(
        gh,
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
            format!("Merge {} into {}", repo.integration_branch, repo.base_branch),
            "--body".into(),
            format!(
                "Promote AI-integration work from `{}` to `{}` through the protected pull-request workflow.",
                repo.integration_branch, repo.base_branch
            ),
        ],
    );
    if !create_ok {
        return Err(format!("Could not create the integration PR: {create_out}"));
    }
    open_integration_pr_ref(gh, repo).ok_or_else(|| {
        format!("GitHub created the promotion PR but it could not be found: {create_out}")
    })
}

fn promotion_approval_args(
    script: &Path,
    repo: &RepoConfig,
    provider: &str,
    gh: &Path,
    pr_url: &str,
) -> Vec<String> {
    vec![
        script.to_string_lossy().into_owned(),
        "--config".into(),
        repo.effective_apps_config(),
        "exec".into(),
        "--provider".into(),
        provider.into(),
        "--repository".into(),
        repo.github_repository.clone(),
        "--".into(),
        gh.to_string_lossy().into_owned(),
        "pr".into(),
        "review".into(),
        pr_url.into(),
        "--repo".into(),
        repo.github_repository.clone(),
        "--approve".into(),
        "--body".into(),
        "Automated approval after synchronizing the human-owned branch into the AI integration branch.".into(),
    ]
}

fn approve_promotion_pr<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    config: &AppConfig,
    repo: &RepoConfig,
    gh: &Path,
    pr_url: &str,
) -> Result<(), String> {
    let python = tools::configured_or_detected(&config.python_bin, "python3")?;
    let script = worker_script_dir(app)?.join("github_app_auth.py");
    let mut failures = Vec::new();
    for provider in config.enabled_providers() {
        let (approved, message) = run_capture_owned(
            &python,
            &promotion_approval_args(&script, repo, &provider.id, gh, pr_url),
        );
        if approved {
            return Ok(());
        }
        failures.push(format!("{}: {message}", provider.id));
    }
    if failures.is_empty() {
        Err("Enable at least one AI provider to approve the promotion PR.".into())
    } else {
        Err(format!(
            "No configured bot could approve the promotion PR. {}",
            failures.join("; ")
        ))
    }
}

/// Whether the configured worker bots are allowed to merge into this
/// repository's human-owned branch. Uses the signed-in `gh` user, which
/// must be able to read branch protection.
#[tauri::command]
fn branch_push_access(
    state: State<'_, AppState>,
    repo_id: String,
) -> Result<BranchPushAccess, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?.clone();
    let gh = tools::configured_or_detected(&config.gh_bin, "gh")?;
    inspect_bot_branch_push(&gh, &repo)
}

/// Add the configured worker GitHub Apps to the human-owned branch's
/// existing push allow list. People and teams already on that list stay
/// there. A branch that does not restrict pushes is left unchanged.
#[tauri::command]
fn grant_bot_branch_push(
    state: State<'_, AppState>,
    repo_id: String,
) -> Result<BranchPushAccess, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?.clone();
    let gh = tools::configured_or_detected(&config.gh_bin, "gh")?;
    let slugs = match load_configured_bot_slugs(&repo) {
        Ok(slugs) => slugs,
        Err(_) => return Ok(unconfigured_push_access(&repo.base_branch)),
    };
    let protection = read_branch_protection(&gh, &repo.github_repository, &repo.base_branch)?;
    let current = access_from_protection(&repo.base_branch, protection, &slugs);
    if !current.can_grant {
        return Ok(current);
    }
    grant_apps_push_access(
        &gh,
        &repo.github_repository,
        &repo.base_branch,
        &current.apps,
    )?;
    inspect_bot_branch_push(&gh, &repo)
}

#[tauri::command]
fn promote_integration_branch(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    repo_id: String,
) -> Result<RepoGitOverview, String> {
    let config = current_config(&state)?;
    let repo = resolve_repo(&config, &repo_id)?.clone();
    let git = tools::configured_or_detected("", "git")?;
    let gh = tools::configured_or_detected(&config.gh_bin, "gh")?;
    let workspace = prepared_workspace(&app, &config, &repo)?;
    // Reconciliation and the merge itself only ever read the shared
    // workspace's config/refs and operate inside their own throwaway clone
    // (see reconcile_integration_for_promotion), so it is safe to run while
    // the issue worker is actively checking out and committing there.
    reconcile_integration_for_promotion(&git, &workspace, &repo)?;
    let (pr_number, pr_url) = ensure_integration_pr_ref(&gh, &repo)?;
    approve_promotion_pr(&app, &config, &repo, &gh, &pr_url)?;
    merge_integration_branch(app, state, repo_id, pr_number)
}

#[tauri::command]
async fn promote_integration_branch_background(
    app: tauri::AppHandle,
    repo_id: String,
) -> Result<RepoGitOverview, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        promote_integration_branch(app.clone(), state, repo_id)
    })
    .await
    .map_err(|error| format!("Promotion task failed: {error}"))?
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

    let (_, url) = ensure_integration_pr_ref(&gh, &repo)?;
    let _ = app.opener().open_url(url.clone(), None::<&str>);
    Ok(url)
}

/// Parse the `"<number>\t<url>"` line that `gh pr list --jq` emits for the open
/// promotion PR, rejecting anything that is not a real pull-request URL.
fn parse_pr_ref(raw: &str) -> Option<(u64, String)> {
    let (number, url) = raw.trim().split_once('\t')?;
    let url = url.trim();
    if !url.starts_with("https://") {
        return None;
    }
    Some((number.trim().parse().ok()?, url.to_string()))
}

/// GitHub App slug from a bot login (`swarm-claude-bot[bot]` → `swarm-claude-bot`).
fn bot_login_slug(login: &str) -> &str {
    login.trim().strip_suffix("[bot]").unwrap_or(login.trim())
}

fn path_segment(value: &str) -> String {
    let mut encoded = String::new();
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                encoded.push(byte as char);
            }
            _ => encoded.push_str(&format!("%{byte:02X}")),
        }
    }
    encoded
}

fn bot_app_slugs_from_config(raw: &str) -> Result<Vec<String>, String> {
    let value: serde_json::Value = serde_json::from_str(raw)
        .map_err(|error| format!("GitHub Apps config is not valid JSON: {error}"))?;
    let entries = value
        .as_object()
        .ok_or("GitHub Apps config must be an object of provider entries")?;
    let mut slugs = Vec::new();
    for entry in entries.values() {
        let login = entry
            .get("bot_login")
            .and_then(|item| item.as_str())
            .unwrap_or("");
        let slug = bot_login_slug(login);
        if slug.is_empty() || slugs.iter().any(|existing| existing == slug) {
            continue;
        }
        slugs.push(slug.to_string());
    }
    slugs.sort();
    if slugs.is_empty() {
        return Err("The GitHub Apps config has no bot identities to grant.".into());
    }
    Ok(slugs)
}

fn protection_api_path(repository: &str, branch: &str) -> String {
    format!(
        "repos/{}/branches/{}/protection",
        repository.trim().trim_matches('/'),
        path_segment(branch)
    )
}

#[derive(Debug, PartialEq, Eq)]
struct PushAccessDecision {
    state: &'static str,
    apps: Vec<String>,
    missing: Vec<String>,
    allowed_users: Vec<String>,
    can_grant: bool,
}

/// Decide whether the worker bots can be added to an existing push allow
/// list. A branch with no allow list is left alone: turning restrictions on
/// would lock out everyone who is not one of these apps.
fn decide_bot_push_access(
    protection: Option<&serde_json::Value>,
    wanted: &[String],
) -> PushAccessDecision {
    let Some(protection) = protection else {
        return PushAccessDecision {
            state: "unprotected",
            apps: Vec::new(),
            missing: Vec::new(),
            allowed_users: Vec::new(),
            can_grant: false,
        };
    };
    let Some(restrictions) = protection
        .get("restrictions")
        .filter(|value| !value.is_null())
    else {
        return PushAccessDecision {
            state: "unrestricted",
            apps: Vec::new(),
            missing: Vec::new(),
            allowed_users: Vec::new(),
            can_grant: false,
        };
    };
    let allowed_users = restrictions
        .get("users")
        .and_then(|value| value.as_array())
        .map(|users| {
            users
                .iter()
                .filter_map(|user| {
                    user.get("login")
                        .and_then(|login| login.as_str())
                        .or_else(|| user.as_str())
                        .map(|login| login.to_string())
                })
                .collect()
        })
        .unwrap_or_default();
    let mut apps: Vec<String> = restrictions
        .get("apps")
        .and_then(|value| value.as_array())
        .map(|items| {
            items
                .iter()
                .filter_map(|app| {
                    app.get("slug")
                        .and_then(|slug| slug.as_str())
                        .or_else(|| app.as_str())
                        .map(|slug| slug.to_string())
                })
                .collect()
        })
        .unwrap_or_default();
    let mut missing = Vec::new();
    for slug in wanted {
        if apps.iter().any(|existing| existing == slug) {
            continue;
        }
        missing.push(slug.clone());
        apps.push(slug.clone());
    }
    apps.sort();
    apps.dedup();
    missing.sort();
    if missing.is_empty() {
        PushAccessDecision {
            state: "allowed",
            apps,
            missing,
            allowed_users,
            can_grant: false,
        }
    } else {
        PushAccessDecision {
            state: "missing",
            apps,
            missing,
            allowed_users,
            can_grant: true,
        }
    }
}

fn push_access_message(branch: &str, decision: &PushAccessDecision) -> String {
    let people = if decision.allowed_users.is_empty() {
        "the current allow list".to_string()
    } else {
        decision.allowed_users.join(", ")
    };
    match decision.state {
        "allowed" => format!(
            "The worker bots can merge into {branch}. They are on the push allow list with {people}."
        ),
        "missing" => format!(
            "{branch} only lets {people} merge. The worker bots are not on that list, so GitHub rejects promotion merges because the base branch policy prohibits the merge."
        ),
        "unrestricted" => format!(
            "{branch} does not restrict who can push. There is no allow list to add the worker bots to."
        ),
        "unprotected" => format!(
            "{branch} is not a protected branch. This grant applies only when GitHub restricts who can push."
        ),
        "unconfigured" => {
            "Set up GitHub Apps before granting them merge access on the human-owned branch."
                .into()
        }
        _ => format!("Could not determine push access for {branch}."),
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct BranchPushAccess {
    branch: String,
    state: String,
    message: String,
    apps: Vec<String>,
    missing: Vec<String>,
    allowed_users: Vec<String>,
    can_grant: bool,
}

fn branch_push_status(branch: &str, decision: PushAccessDecision) -> BranchPushAccess {
    BranchPushAccess {
        branch: branch.to_string(),
        state: decision.state.to_string(),
        message: push_access_message(branch, &decision),
        apps: decision.apps,
        missing: decision.missing,
        allowed_users: decision.allowed_users,
        can_grant: decision.can_grant,
    }
}

fn unconfigured_push_access(branch: &str) -> BranchPushAccess {
    let decision = PushAccessDecision {
        state: "unconfigured",
        apps: Vec::new(),
        missing: Vec::new(),
        allowed_users: Vec::new(),
        can_grant: false,
    };
    branch_push_status(branch, decision)
}

fn gh_api(gh: &Path, args: &[String]) -> Result<serde_json::Value, String> {
    let (ok, message) = run_capture_owned(gh, args);
    parse_gh_api_output(ok, message)
}

fn parse_gh_api_output(ok: bool, message: String) -> Result<serde_json::Value, String> {
    let trimmed = message.trim();
    if let Ok(value) = serde_json::from_str::<serde_json::Value>(trimmed) {
        if !ok {
            let text = value
                .get("message")
                .and_then(|item| item.as_str())
                .unwrap_or(trimmed);
            let status_404 = value.get("status").and_then(|item| item.as_u64()) == Some(404)
                || value.get("status").and_then(|item| item.as_str()) == Some("404");
            if text.contains("Branch not protected") || status_404 {
                return Ok(serde_json::Value::Null);
            }
            return Err(text.to_string());
        }
        return Ok(value);
    }
    if !ok && message.contains("Branch not protected") {
        return Ok(serde_json::Value::Null);
    }
    if !ok {
        return Err(message);
    }
    Err(format!("GitHub returned unexpected data: {message}"))
}

fn read_branch_protection(
    gh: &Path,
    repository: &str,
    branch: &str,
) -> Result<Option<serde_json::Value>, String> {
    let value = gh_api(gh, &["api".into(), protection_api_path(repository, branch)])
        .map_err(|error| format!("Could not read branch protection for {branch}: {error}"))?;
    if value.is_null() {
        Ok(None)
    } else {
        Ok(Some(value))
    }
}

fn load_configured_bot_slugs(repo: &RepoConfig) -> Result<Vec<String>, String> {
    let path = repo.effective_apps_config();
    let raw = std::fs::read_to_string(&path).map_err(|_| {
        format!("GitHub Apps are not set up yet ({path}). Use Set up GitHub Apps first.")
    })?;
    bot_app_slugs_from_config(&raw)
}

fn access_from_protection(
    branch: &str,
    protection: Option<serde_json::Value>,
    slugs: &[String],
) -> BranchPushAccess {
    let decision = decide_bot_push_access(protection.as_ref(), slugs);
    branch_push_status(branch, decision)
}

fn inspect_bot_branch_push(gh: &Path, repo: &RepoConfig) -> Result<BranchPushAccess, String> {
    let branch = repo.base_branch.clone();
    let slugs = match load_configured_bot_slugs(repo) {
        Ok(slugs) => slugs,
        Err(_) => return Ok(unconfigured_push_access(&branch)),
    };
    let protection = read_branch_protection(gh, &repo.github_repository, &branch)?;
    Ok(access_from_protection(&branch, protection, &slugs))
}

fn grant_apps_push_access(
    gh: &Path,
    repository: &str,
    branch: &str,
    apps: &[String],
) -> Result<(), String> {
    let (args, body) = grant_apps_request(repository, branch, apps);
    gh_api_with_input(gh, &args, &body)
        .map(|_| ())
        .map_err(|error| format!("Could not update who can push to {branch}: {error}"))
}

/// The `gh api` arguments and JSON body for replacing a branch's push allow
/// list of apps. GitHub expects the body to be a bare JSON array of slugs,
/// so it is sent on stdin instead of as `--field` pairs (which build an
/// object and are rejected with "is not an array").
fn grant_apps_request(repository: &str, branch: &str, apps: &[String]) -> (Vec<String>, String) {
    let args = vec![
        "api".into(),
        "--method".into(),
        "PUT".into(),
        format!(
            "{}/restrictions/apps",
            protection_api_path(repository, branch)
        ),
        "--input".into(),
        "-".into(),
    ];
    (args, serde_json::json!(apps).to_string())
}

fn gh_api_with_input(gh: &Path, args: &[String], input: &str) -> Result<serde_json::Value, String> {
    let (ok, message) = run_capture_with_input(gh, args, input);
    parse_gh_api_output(ok, message)
}

/// The open `integration -> base` promotion pull request for `repo`, as
/// `(number, url)`, or `None` when GitHub has no such PR (or `gh` fails).
fn open_integration_pr_ref(gh: &Path, repo: &RepoConfig) -> Option<(u64, String)> {
    let (ok, out) = run_capture_owned(
        gh,
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
            "number,url".into(),
            "--jq".into(),
            r#".[0] | select(.url != null) | "\(.number)\t\(.url)""#.into(),
        ],
    );
    ok.then(|| parse_pr_ref(&out)).flatten()
}

/// A configured repository belongs in the promotion panel when its AI
/// integration branch exists and carries commits the human-owned branch lacks.
fn needs_promotion(integration_exists: bool, vs_base: &BranchAheadBehind) -> bool {
    integration_exists && vs_base.ahead > 0
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct RepoPromotion {
    repo_id: String,
    label: String,
    github_repository: String,
    base_branch: String,
    integration_branch: String,
    /// Commits on `origin/<integration>` that `origin/<base>` does not have.
    ahead: u32,
    /// Commits on `origin/<base>` that `origin/<integration>` does not have.
    behind: u32,
    integration_pr_url: String,
    integration_pr_number: Option<u64>,
    /// Non-empty when the branch counts shown could not be refreshed.
    error: String,
}

/// Every configured repository whose AI integration branch is ahead of its
/// human-owned branch and waiting to be promoted. Backs the Overview page's
/// "Repositories ready to promote" panel; selecting a row runs the same
/// `open_integration_pr` flow as the Repository branch tree button.
#[tauri::command]
fn promotion_overview<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    state: State<'_, AppState>,
) -> Result<Vec<RepoPromotion>, String> {
    let config = current_config(&state)?;
    let git = tools::configured_or_detected("", "git")?;
    let gh = tools::configured_or_detected(&config.gh_bin, "gh").ok();
    let mut promotions = Vec::new();
    for repo in config.repositories() {
        let Ok(workspace) = resolve_workspace(&app, &config, repo) else {
            continue;
        };
        if !workspace.join(".git").is_dir() {
            continue;
        }
        let ws = workspace.to_string_lossy().into_owned();
        let fetch_failed = !git_c(&git, &ws, &["fetch", "--prune", &repo.remote_name]).0;
        let base_ref = format!("{}/{}", repo.remote_name, repo.base_branch);
        let integ_ref = format!("{}/{}", repo.remote_name, repo.integration_branch);
        let integration_exists =
            git_c(&git, &ws, &["rev-parse", "--verify", "--quiet", &integ_ref]).0;
        let vs_base = if integration_exists {
            ahead_behind(&git, &ws, &base_ref, &integ_ref)
        } else {
            BranchAheadBehind::default()
        };
        if !needs_promotion(integration_exists, &vs_base) {
            continue;
        }
        let pull_request = gh
            .as_deref()
            .and_then(|gh| open_integration_pr_ref(gh, repo));
        promotions.push(RepoPromotion {
            repo_id: repo.id.clone(),
            label: repo.label(),
            github_repository: repo.github_repository.clone(),
            base_branch: repo.base_branch.clone(),
            integration_branch: repo.integration_branch.clone(),
            ahead: vs_base.ahead,
            behind: vs_base.behind,
            integration_pr_url: pull_request
                .as_ref()
                .map(|(_, url)| url.clone())
                .unwrap_or_default(),
            integration_pr_number: pull_request.as_ref().map(|(number, _)| *number),
            error: if fetch_failed {
                format!(
                    "Could not refresh {} — showing the last known branch state.",
                    repo.remote_name
                )
            } else {
                String::new()
            },
        });
    }
    Ok(promotions)
}

#[tauri::command]
async fn promotion_overview_background(
    app: tauri::AppHandle,
) -> Result<Vec<RepoPromotion>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        promotion_overview(app.clone(), state)
    })
    .await
    .map_err(|error| format!("Promotion overview background task failed: {error}"))?
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
fn get_recent_logs(app: tauri::AppHandle, limit: Option<usize>) -> Result<Vec<String>, String> {
    let path = automation_log_path(&app)?;
    let text = std::fs::read_to_string(path).unwrap_or_default();
    let limit = limit.unwrap_or(300).clamp(1, 20_000);
    let mut lines = text.lines().rev().take(limit).collect::<Vec<_>>();
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

fn run_capture_with_input(program: &Path, arguments: &[String], input: &str) -> (bool, String) {
    use std::io::Write;
    use std::process::Stdio;
    let mut child = match Command::new(program)
        .args(arguments)
        .env("PATH", tools::enhanced_path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(child) => child,
        Err(error) => return (false, error.to_string()),
    };
    if let Some(mut stdin) = child.stdin.take() {
        if let Err(error) = stdin.write_all(input.as_bytes()) {
            let _ = child.kill();
            let _ = child.wait();
            return (false, error.to_string());
        }
    }
    match child.wait_with_output() {
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

const PERMISSION_PRIMED_MESSAGE: &str = "SWARM Automation requested macOS's one-time permission to control Terminal, used for provider and GitHub sign-in. Approve it once and the app won't ask again.";

/// Sends a harmless Apple Event to Terminal purely to surface macOS's
/// Automation permission prompt for controlling other apps. `open_provider_login`
/// needs that same permission to run sign-in commands, but asking for it lazily
/// mid sign-in is exactly the surprise popup issue #85 asks to avoid — doing it
/// once on startup instead front-loads the interruption to a moment the user
/// expects it.
#[cfg(target_os = "macos")]
fn prime_terminal_automation_permission() {
    let _ = Command::new("/usr/bin/osascript")
        .args(["-e", "tell application \"Terminal\" to get name"])
        .status();
}

#[cfg(not(target_os = "macos"))]
fn prime_terminal_automation_permission() {}

/// Records that permission priming has run (successful or not — macOS itself
/// remembers the user's answer from here on) so it is never attempted again.
fn mark_permission_primed<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    let state = app.state::<AppState>();
    let saved = {
        let Ok(mut config) = state.config.lock() else {
            return;
        };
        config.terminal_automation_permission_primed = true;
        config.clone()
    };
    if let Ok(path) = app_config_path(app) {
        let _ = config::save_unchecked(&path, &saved);
    }
}

/// Runs once per install, on startup: front-loads the macOS Automation
/// permission prompt that `open_provider_login` would otherwise trigger the
/// first time a user signs in to a provider mid-task. Non-macOS platforms
/// need no such permission, so they're marked primed immediately.
fn spawn_permission_priming(app: &tauri::AppHandle) {
    let state = app.state::<AppState>();
    let already_primed = match state.config.lock() {
        Ok(config) => config.terminal_automation_permission_primed,
        Err(_) => return,
    };
    if already_primed {
        return;
    }
    if cfg!(not(target_os = "macos")) {
        mark_permission_primed(app);
        return;
    }
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        let _ = tauri::async_runtime::spawn_blocking(prime_terminal_automation_permission).await;
        mark_permission_primed(&handle);
        let _ = handle.emit("system-permission-primed", PERMISSION_PRIMED_MESSAGE);
    });
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
    let arguments: Vec<String> = std::env::args().collect();
    if let Some(exit_code) = testing::run_cli(&arguments) {
        std::process::exit(exit_code);
    }
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            show_main_window(app)
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(AppState::default())
        .setup(|app| {
            let loaded =
                config::load(&app_config_path(app.handle()).map_err(std::io::Error::other)?);
            *app.state::<AppState>()
                .config
                .lock()
                .map_err(|_| std::io::Error::other("Configuration lock was poisoned"))? = loaded;
            install_tray(app)?;
            spawn_startup_update_check(app.handle());
            spawn_permission_priming(app.handle());
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
            request_issue_scan,
            start_uat_scheduler,
            get_test_plan,
            get_test_plan_background,
            detect_test_definition,
            create_test_definition,
            audit_test_coverage,
            get_test_runs,
            get_test_runs_background,
            get_execution_history,
            get_execution_history_background,
            get_prompt_grades,
            get_prompt_grades_background,
            import_execution_history,
            import_execution_history_background,
            save_test_input,
            choose_test_input_path,
            pause_process,
            resume_process,
            stop_process,
            install_ai_cli,
            launch_bot_setup,
            verify_github_bots,
            check_repo_bot_readiness,
            git_overview,
            git_overview_background,
            refresh_repo,
            merge_issue_branch,
            merge_integration_branch,
            promote_integration_branch,
            promote_integration_branch_background,
            open_integration_pr,
            branch_push_access,
            grant_bot_branch_push,
            promotion_overview,
            promotion_overview_background,
            open_provider_login,
            open_external_url,
            open_automation_folder,
            get_recent_logs,
            hide_to_tray,
            check_for_update,
            install_update,
            app_version,
            list_update_candidates,
            install_update_candidate,
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
