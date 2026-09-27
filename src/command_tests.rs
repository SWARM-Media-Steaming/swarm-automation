use super::{
    automation_log_path, bot_app_slugs_from_config, decide_bot_push_access, detect_tools,
    execution_history_query_args, feedback_repository_names, get_config, get_execution_history,
    get_prompt_grades, grant_apps_request, inspect_repository, issue_branch_pr_is_visible,
    mark_permission_primed, needs_promotion, parse_pr_ref, promotion_approval_args,
    prompt_grades_query_args, provider_scheduler_arguments, push_access_message,
    reconcile_integration_for_promotion, refresh_running_scheduler, repo_status_args,
    repo_worker_args, request_issue_scan, require_closed_issue, run_now_request_path, save_config,
    save_feedback_repo_filter, scheduler_arguments, validate_worker_script_dir, write_repos_file,
    AiExecutionRecord, AppState, BranchAheadBehind, ExecutionHistoryPage, PromptGradesQuery,
    ResolvedProvider,
};
use crate::config::{AppConfig, RepoConfig};
use std::path::{Path, PathBuf};
use tauri::test::{mock_builder, mock_context, noop_assets, MockRuntime};
use tauri::Manager;

struct TestApp {
    app: tauri::App<MockRuntime>,
    _data_dir: tempfile::TempDir,
}

impl TestApp {
    fn handle(&self) -> tauri::AppHandle<MockRuntime> {
        self.app.handle().clone()
    }
}

fn test_app() -> TestApp {
    let data_dir = tempfile::tempdir().expect("create temp data dir");
    let app = mock_builder()
        .manage(AppState {
            config: std::sync::Mutex::new(AppConfig::default()),
            processes: Default::default(),
            test_data_dir: Some(data_dir.path().to_path_buf()),
        })
        .build(mock_context(noop_assets()))
        .expect("build mock tauri app");
    TestApp {
        app,
        _data_dir: data_dir,
    }
}

#[test]
fn process_manager_reconnects_to_a_live_external_process() {
    let test_app = test_app();
    let app = test_app.handle();
    let log_path = test_app._data_dir.path().join("automation.log");
    let status = app
        .state::<AppState>()
        .processes
        .adopt_external(
            &app,
            "issue",
            "Issue worker scheduler",
            std::process::id(),
            "test external scheduler".into(),
            &log_path,
            None,
        )
        .expect("adopt live process");

    assert_eq!(status.state, "running");
    assert_eq!(status.pid, Some(std::process::id()));
    assert!(std::fs::read_to_string(log_path)
        .unwrap()
        .contains("Reconnected to existing Issue worker scheduler"));
}

/// "Run now" pressed while the scheduler is already running. The scheduler is
/// represented the way a real one is after an app restart: a live PID in the
/// runner lock, which `request_issue_scan` reconnects to before leaving the
/// request file `install_swarm_issue_cron.py` polls for.
#[test]
fn request_issue_scan_leaves_a_request_for_a_running_scheduler() {
    let test_app = test_app();
    let app = test_app.handle();
    let repo_dir = real_git_checkout();
    let state_dir = tempfile::tempdir().expect("create temp worker state dir");
    let mut config = valid_config(repo_dir.path());
    config.worker_state_dir = state_dir.path().to_string_lossy().into_owned();
    save_config(app.clone(), app.state(), config.clone()).expect("save a valid config");
    std::fs::create_dir_all(state_dir.path().join("runner.lock")).expect("create runner lock");
    std::fs::write(
        state_dir.path().join("runner.lock/pid"),
        format!("{}\n", std::process::id()),
    )
    .expect("record a live scheduler PID");

    let message = request_issue_scan(app.clone(), app.state()).expect("request an immediate scan");

    assert!(
        message.contains("Scanning every repository now"),
        "the user should be told the scan started: {message}"
    );
    assert!(
        run_now_request_path(&config).is_file(),
        "the running scheduler's run-now request should be on disk"
    );
    let log = std::fs::read_to_string(automation_log_path(&app).expect("log path")).unwrap();
    assert!(
        log.contains("Run now: asked the running scheduler"),
        "the request should be visible in Info & Debug: {log}"
    );
}

/// With no scheduler running there is nobody to receive the request, so the
/// command refuses rather than leaving a file that would make the next started
/// scheduler run an extra cycle nobody asked for. (The UI calls
/// `start_issue_worker` in that state instead — see `runNowMode`.)
#[test]
fn request_issue_scan_refuses_when_no_scheduler_is_running() {
    let test_app = test_app();
    let app = test_app.handle();
    let repo_dir = real_git_checkout();
    let state_dir = tempfile::tempdir().expect("create temp worker state dir");
    let mut config = valid_config(repo_dir.path());
    config.worker_state_dir = state_dir.path().to_string_lossy().into_owned();
    save_config(app.clone(), app.state(), config.clone()).expect("save a valid config");

    let error = request_issue_scan(app.clone(), app.state())
        .expect_err("a stopped worker cannot scan on request");

    assert!(error.contains("not running"), "{error}");
    assert!(
        !run_now_request_path(&config).exists(),
        "a refused request must not be left behind for a later scheduler"
    );
}

/// A real, on-disk `git init`-ed directory — enough for `inspect_repository`
/// to see a genuine Git checkout without needing a real GitHub remote.
fn real_git_checkout() -> tempfile::TempDir {
    let dir = tempfile::tempdir().expect("create temp repo dir");
    let status = std::process::Command::new("git")
        .args(["init", "--quiet"])
        .current_dir(dir.path())
        .status()
        .expect("git init should run on this machine");
    assert!(status.success(), "git init failed");
    dir
}

fn repo(github_repository: &str) -> RepoConfig {
    RepoConfig {
        id: crate::config::repo_slug(github_repository),
        github_repository: github_repository.into(),
        assignee: "octocat".into(),
        ..RepoConfig::default()
    }
}

/// A valid single-repo config whose repo uses `repo_dir` as an override so no
/// clone is needed.
fn valid_config(repo_dir: &Path) -> AppConfig {
    let mut config = AppConfig::default();
    config.repositories.push(RepoConfig {
        repo_dir: repo_dir.to_string_lossy().into_owned(),
        ..repo("octocat/example")
    });
    config
}

#[allow(dead_code)]
fn resolved_provider(id: &str, bin: &str) -> ResolvedProvider {
    ResolvedProvider {
        id: id.into(),
        model: format!("{id}-model"),
        effort: "high".into(),
        router_model: format!("{id}-router"),
        router_effort: "low".into(),
        strengths: format!("{id} is best at tests"),
        bin: PathBuf::from(bin),
        enabled: true,
        minimum_remaining_percent: 10,
    }
}

#[test]
fn provider_scheduler_arguments_carry_dynamic_routing_settings() {
    let mut config = AppConfig {
        dynamic_model_routing: false,
        ..AppConfig::default()
    };
    let providers = vec![resolved_provider("codex", "/usr/bin/false")];
    let off = provider_scheduler_arguments(&config, &providers);
    assert!(off.iter().any(|arg| arg == "--no-dynamic-model-routing"));
    assert!(off
        .windows(2)
        .any(|pair| pair[0] == "--codex-model" && pair[1] == "codex-model"));
    assert!(off
        .windows(2)
        .any(|pair| pair[0] == "--codex-router-model" && pair[1] == "codex-router"));
    assert!(off
        .windows(2)
        .any(|pair| pair[0] == "--codex-router-effort" && pair[1] == "low"));
    // The router needs every tool's strengths to choose between the tools.
    assert!(off
        .windows(2)
        .any(|pair| pair[0] == "--codex-router-strengths" && pair[1] == "codex is best at tests"));
    let tiers = off
        .windows(2)
        .find(|pair| pair[0] == "--routing-tiers")
        .unwrap()[1]
        .clone();
    let parsed: serde_json::Value = serde_json::from_str(&tiers).unwrap();
    assert_eq!(parsed["codex"][2]["model"], "gpt-5.6-sol");

    // The router picks the worker model itself, so it needs both the cost
    // preference and the same credit-model filter the desktop applies.
    assert!(off
        .windows(2)
        .any(|pair| pair[0] == "--routing-optimization" && pair[1] == "best"));
    assert!(off
        .iter()
        .any(|arg| arg == "--no-allow-usage-credit-models"));

    config.dynamic_model_routing = true;
    config.routing_optimization = "cost".into();
    config.allow_usage_credit_models = true;
    let on = provider_scheduler_arguments(&config, &providers);
    assert!(on.iter().any(|arg| arg == "--dynamic-model-routing"));
    assert!(!on.iter().any(|arg| arg == "--no-dynamic-model-routing"));
    assert!(on
        .windows(2)
        .any(|pair| pair[0] == "--routing-optimization" && pair[1] == "cost"));
    assert!(on.iter().any(|arg| arg == "--allow-usage-credit-models"));
    assert!(!on.iter().any(|arg| arg == "--no-allow-usage-credit-models"));
}

#[test]
fn provider_scheduler_arguments_carry_each_provider_quota_floor() {
    let config = AppConfig::default();
    let providers = vec![
        ResolvedProvider {
            minimum_remaining_percent: 0,
            ..resolved_provider("claude", "/usr/bin/false")
        },
        ResolvedProvider {
            minimum_remaining_percent: 25,
            ..resolved_provider("grok", "/usr/bin/false")
        },
    ];
    let args = provider_scheduler_arguments(&config, &providers);
    assert_eq!(pair(&args, "--claude-minimum-remaining-percent"), Some("0"));
    assert_eq!(pair(&args, "--grok-minimum-remaining-percent"), Some("25"));
}

#[test]
fn the_cost_routing_preference_persists_to_disk_as_a_string() {
    let test_app = test_app();
    let app = test_app.handle();
    let repo_dir = real_git_checkout();
    let mut config = valid_config(repo_dir.path());
    assert_eq!(config.routing_optimization, "best");

    config.routing_optimization = "cost".into();
    let saved = save_config(app.clone(), app.state(), config).expect("save_config should succeed");
    assert_eq!(saved.routing_optimization, "cost");

    let config_path = test_app._data_dir.path().join(crate::config::CONFIG_FILE);
    let raw = std::fs::read_to_string(&config_path).expect("config.json should exist");
    let parsed: serde_json::Value = serde_json::from_str(&raw).unwrap();
    assert_eq!(parsed["routing_optimization"], "cost");
    assert_eq!(
        crate::config::load(&config_path).routing_optimization,
        "cost"
    );
    assert_eq!(
        get_config(app.state())
            .expect("get_config should succeed")
            .routing_optimization,
        "cost"
    );
}

#[test]
fn save_config_then_get_config_round_trips_through_a_real_file() {
    let test_app = test_app();
    let app = test_app.handle();
    let repo_dir = real_git_checkout();
    let config = valid_config(repo_dir.path());

    let saved = save_config(app.clone(), app.state(), config.clone())
        .expect("save_config should succeed against a real, valid repo checkout");
    assert_eq!(saved.repositories[0].github_repository, "octocat/example");
    assert_eq!(saved.repositories[0].id, "octocat__example");

    let loaded = get_config(app.state()).expect("get_config should succeed");
    assert_eq!(loaded.repositories.len(), 1);
    assert_eq!(loaded.repositories[0].integration_branch, "ai-main");

    let config_path = test_app._data_dir.path().join(crate::config::CONFIG_FILE);
    assert!(config_path.is_file(), "config.json should exist on disk");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = std::fs::metadata(&config_path)
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600, "config.json must be owner-only");
    }
}

#[test]
fn adversarial_security_toggle_persists_through_a_real_config_file() {
    let test_app = test_app();
    let app = test_app.handle();
    let repo_dir = real_git_checkout();
    let mut config = valid_config(repo_dir.path());
    assert!(
        !config.repositories[0].adversarial_security_enabled,
        "the review is off until an operator turns it on"
    );
    config.repositories[0].adversarial_security_enabled = true;

    save_config(app.clone(), app.state(), config).expect("save a valid config");
    // Read back through the file, not the in-memory state, so a missing
    // serde field would surface as the setting silently reverting.
    let on_disk: crate::config::AppConfig = serde_json::from_str(
        &std::fs::read_to_string(test_app._data_dir.path().join(crate::config::CONFIG_FILE))
            .expect("config.json is readable"),
    )
    .expect("config.json parses");
    assert!(on_disk.repositories[0].adversarial_security_enabled);

    let loaded = get_config(app.state()).expect("get_config should succeed");
    assert!(loaded.repositories[0].adversarial_security_enabled);
}

#[test]
fn mark_permission_primed_persists_the_flag_and_is_idempotent() {
    let test_app = test_app();
    let app = test_app.handle();
    assert!(
        !get_config(app.state())
            .expect("get_config should succeed")
            .terminal_automation_permission_primed
    );

    mark_permission_primed(&app);

    assert!(
        get_config(app.state())
            .expect("get_config should succeed")
            .terminal_automation_permission_primed,
        "in-memory config should reflect priming immediately"
    );
    let config_path = test_app._data_dir.path().join(crate::config::CONFIG_FILE);
    let persisted = crate::config::load(&config_path);
    assert!(
        persisted.terminal_automation_permission_primed,
        "priming must be persisted so it never runs again on the next launch"
    );

    // Calling it again (e.g. a second `spawn_permission_priming` guard check
    // racing at startup) must stay a harmless no-op, not toggle anything off.
    mark_permission_primed(&app);
    assert!(
        get_config(app.state())
            .expect("get_config should succeed")
            .terminal_automation_permission_primed
    );
}

#[test]
fn save_config_round_trips_two_repositories() {
    let test_app = test_app();
    let app = test_app.handle();
    let mut config = AppConfig::default();
    config.repositories.push(repo("octocat/one"));
    config.repositories.push(RepoConfig {
        integration_branch: "integration".into(),
        branch_prefix: "bots".into(),
        ..repo("octocat/two")
    });

    save_config(app.clone(), app.state(), config).expect("two-repo config is valid");
    let loaded = get_config(app.state()).expect("get_config");
    assert_eq!(loaded.repositories.len(), 2);
    assert_eq!(loaded.repositories[1].integration_branch, "integration");
    assert_eq!(loaded.repositories[1].branch_prefix, "bots");
}

#[test]
fn write_repos_file_lists_every_enabled_repo_and_is_rewritten_when_one_is_added() {
    let test_app = test_app();
    let app = test_app.handle();
    let first = real_git_checkout();
    let second = real_git_checkout();
    let state_dir = tempfile::tempdir().expect("worker state dir");
    let git = PathBuf::from("/usr/bin/git");
    let gh = PathBuf::from("/usr/bin/gh");

    let mut config = valid_config(first.path());
    config.worker_state_dir = state_dir.path().to_string_lossy().into_owned();
    let path = write_repos_file(&app, &config, &git, &gh).expect("write repos file");
    assert_eq!(path, state_dir.path().join("repos.json"));
    let labels = |path: &Path| -> Vec<String> {
        let entries: Vec<serde_json::Value> =
            serde_json::from_slice(&std::fs::read(path).unwrap()).expect("valid JSON");
        entries
            .iter()
            .map(|entry| entry["label"].as_str().unwrap().to_string())
            .collect()
    };
    assert_eq!(labels(&path), ["octocat/example"]);

    // A repository added later (and a disabled one, which must be skipped).
    config.repositories.push(RepoConfig {
        repo_dir: second.path().to_string_lossy().into_owned(),
        ..repo("octocat/feedback")
    });
    config.repositories.push(RepoConfig {
        enabled: false,
        ..repo("octocat/dormant")
    });
    write_repos_file(&app, &config, &git, &gh).expect("rewrite repos file");
    assert_eq!(labels(&path), ["octocat/example", "octocat/feedback"]);
    assert!(
        !state_dir.path().join("repos.json.tmp").exists(),
        "the staging file is renamed into place"
    );
}

/// Regression: test configs default to the developer's real `worker_state_dir`.
/// Saving one while their real scheduler was running used to adopt that
/// scheduler and overwrite its real repos.json with the test's repositories,
/// so the live worker switched to a repo that did not exist.
#[test]
fn saving_a_config_in_a_test_never_touches_a_running_schedulers_repos_file() {
    let test_app = test_app();
    let app = test_app.handle();
    let checkout = real_git_checkout();
    let state_dir = tempfile::tempdir().expect("worker state dir");
    // A scheduler "running" for this state dir: its lock records a live PID.
    let lock = state_dir.path().join("runner.lock");
    std::fs::create_dir_all(&lock).unwrap();
    std::fs::write(lock.join("pid"), std::process::id().to_string()).unwrap();
    let mut config = valid_config(checkout.path());
    config.worker_state_dir = state_dir.path().to_string_lossy().into_owned();

    save_config(app.clone(), app.state(), config.clone()).expect("save");
    assert!(refresh_running_scheduler(&app, &app.state(), &config).is_none());
    assert!(
        !state_dir.path().join("repos.json").exists(),
        "a test must not write the scheduler's repos.json"
    );
}

#[test]
fn write_repos_file_refuses_a_config_with_no_enabled_repository() {
    let test_app = test_app();
    let app = test_app.handle();
    let state_dir = tempfile::tempdir().expect("worker state dir");
    let config = AppConfig {
        worker_state_dir: state_dir.path().to_string_lossy().into_owned(),
        ..AppConfig::default()
    };
    let error = write_repos_file(
        &app,
        &config,
        &PathBuf::from("/usr/bin/git"),
        &PathBuf::from("/usr/bin/gh"),
    )
    .expect_err("nothing to schedule");
    assert!(error.contains("Enable at least one repository"));
    assert!(!state_dir.path().join("repos.json").exists());
}

#[test]
fn save_config_rejects_a_working_copy_override_that_does_not_exist() {
    let test_app = test_app();
    let app = test_app.handle();
    let config = valid_config(Path::new("/definitely/not/a/real/path"));

    let error = save_config(app.clone(), app.state(), config)
        .expect_err("a nonexistent working-copy override must be rejected");
    assert!(error.contains("working-copy override"), "got: {error}");
}

#[test]
fn save_config_needs_no_local_checkout_when_the_app_will_clone() {
    let test_app = test_app();
    let app = test_app.handle();
    let mut config = AppConfig::default();
    config.repositories.push(repo("octocat/example"));
    let saved =
        save_config(app.clone(), app.state(), config).expect("clone-managed config is valid");
    assert!(saved.repositories[0].repo_dir.is_empty());
}

#[test]
fn save_config_rejects_no_repositories() {
    let test_app = test_app();
    let app = test_app.handle();
    let error = save_config(app.clone(), app.state(), AppConfig::default())
        .expect_err("a config with no repositories must be rejected");
    assert!(
        error.contains("at least one GitHub repository"),
        "got: {error}"
    );
}

#[test]
fn save_config_rejects_base_equal_to_integration_branch() {
    let test_app = test_app();
    let app = test_app.handle();
    let mut config = AppConfig::default();
    config.repositories.push(RepoConfig {
        integration_branch: "main".into(),
        ..repo("octocat/example")
    });
    let error = save_config(app.clone(), app.state(), config)
        .expect_err("integration branch must differ from base branch");
    assert!(
        error.contains("must differ from the base branch"),
        "got: {error}"
    );
}

#[test]
fn inspect_repository_on_a_real_git_checkout_reports_valid_with_no_scripts() {
    let repo = real_git_checkout();
    let inspection = inspect_repository(repo.path().to_string_lossy().into_owned());
    assert!(
        inspection.valid,
        "a real `git init`-ed folder must be valid"
    );
    assert!(!inspection.worker_available);
    assert!(inspection.error.is_empty());
}

#[test]
fn inspect_repository_on_a_plain_folder_reports_invalid() {
    let dir = tempfile::tempdir().expect("create temp dir");
    let inspection = inspect_repository(dir.path().to_string_lossy().into_owned());
    assert!(!inspection.valid);
    assert!(
        inspection.error.contains("not a Git checkout"),
        "got: {}",
        inspection.error
    );
}

#[test]
fn inspect_repository_detects_a_target_repos_own_script_bundles() {
    let repo = real_git_checkout();
    std::fs::create_dir_all(repo.path().join("scripts/issue_worker")).unwrap();
    std::fs::write(
        repo.path()
            .join("scripts/issue_worker/install_swarm_issue_cron.py"),
        "#!/usr/bin/env python3\n",
    )
    .unwrap();

    let inspection = inspect_repository(repo.path().to_string_lossy().into_owned());
    assert!(inspection.valid);
    assert!(inspection.worker_available);
}

#[test]
fn execution_history_lookup_is_safe_before_any_execution_exists() {
    let test_app = test_app();
    let app = test_app.handle();
    let repo_dir = real_git_checkout();
    let mut config = valid_config(repo_dir.path());
    // No `ai_executions.sqlite3` has ever been written at this path, so the
    // command must return an empty list without needing python or the
    // bundled issue-worker scripts (unavailable under the mock runtime).
    config.worker_state_dir = test_app
        ._data_dir
        .path()
        .join("worker-state")
        .to_string_lossy()
        .into_owned();
    config.repositories.push(RepoConfig {
        repo_dir: repo_dir.path().to_string_lossy().into_owned(),
        ..repo("octocat/other")
    });
    save_config(app.clone(), app.state(), config).unwrap();

    let history = get_execution_history(
        app.clone(),
        app.state(),
        vec!["octocat__example".into()],
        None,
        None,
        None,
    )
    .unwrap();
    assert!(history.records.is_empty());
    assert_eq!(history.total, 0);
    assert_eq!(history.offset, 0);
    assert_eq!(history.limit, 10);
    let global = get_execution_history(app.clone(), app.state(), vec![], None, None, None).unwrap();
    assert!(global.records.is_empty());
    let multiple = get_execution_history(
        app.clone(),
        app.state(),
        vec!["octocat__example".into(), "octocat__other".into()],
        None,
        None,
        None,
    )
    .unwrap();
    assert!(multiple.records.is_empty());
}

#[test]
fn prompt_grades_lookup_is_safe_before_any_execution_exists() {
    let test_app = test_app();
    let app = test_app.handle();
    let repo_dir = real_git_checkout();
    let mut config = valid_config(repo_dir.path());
    config.worker_state_dir = test_app
        ._data_dir
        .path()
        .join("worker-state")
        .to_string_lossy()
        .into_owned();
    save_config(app.clone(), app.state(), config).unwrap();

    let grades = get_prompt_grades(
        app.clone(),
        app.state(),
        vec!["octocat__example".into()],
        PromptGradesQuery::default(),
    )
    .unwrap();
    assert!(grades.records.is_empty());
    assert_eq!(grades.total, 0);
    assert_eq!(grades.summary.graded, 0);
    assert_eq!(grades.summary.average_points, None);
    assert!(grades.router_matrix.is_empty());
    let global = get_prompt_grades(
        app.clone(),
        app.state(),
        vec![],
        PromptGradesQuery::default(),
    )
    .unwrap();
    assert!(global.records.is_empty());
}

#[test]
fn prompt_grades_query_asks_the_history_cli_for_one_page_of_grades() {
    let args = prompt_grades_query_args(
        Path::new("ai_execution_history.py"),
        Path::new("history.sqlite3"),
        &["octocat/example".into()],
        PromptGradesQuery {
            offset: Some(-4),
            search: Some("  Widget  ".into()),
            grade: Some(" B- ".into()),
            router: Some("  Claude ".into()),
            router_model: Some("  claude-opus-4-1  ".into()),
        },
    );
    assert!(args.contains(&"--grades".to_string()));
    let value_after = |flag: &str| {
        args.iter()
            .position(|arg| arg == flag)
            .map(|index| args[index + 1].as_str())
    };
    assert_eq!(value_after("--limit"), Some("10"));
    assert_eq!(value_after("--offset"), Some("0"));
    assert_eq!(value_after("--search"), Some("Widget"));
    assert_eq!(value_after("--grade"), Some("B-"));
    // The grades panel filters by the platform that graded the issue, which is
    // a different axis from the provider the search box already matches.
    assert_eq!(value_after("--router"), Some("claude"));
    assert_eq!(value_after("--router-model"), Some("claude-opus-4-1"));

    let unfiltered = prompt_grades_query_args(
        Path::new("ai_execution_history.py"),
        Path::new("history.sqlite3"),
        &[],
        PromptGradesQuery::default(),
    );
    assert!(unfiltered
        .windows(2)
        .any(|pair| pair[0] == "--search" && pair[1].is_empty()));
    assert!(unfiltered
        .windows(2)
        .any(|pair| pair[0] == "--grade" && pair[1].is_empty()));
    assert!(unfiltered
        .windows(2)
        .any(|pair| pair[0] == "--router" && pair[1].is_empty()));
    assert!(unfiltered
        .windows(2)
        .any(|pair| pair[0] == "--router-model" && pair[1].is_empty()));
    assert!(!unfiltered.iter().any(|argument| argument == "--repository"));
}

#[test]
fn prompt_grades_page_decodes_the_router_matrix_the_history_cli_prints() {
    let page: super::PromptGradesPage = serde_json::from_str(
        r#"{"records":[],"total":0,"offset":0,"limit":10,
            "summary":{"graded":3,"averagePoints":3.0,"averageGrade":"B","distribution":{"B":3}},
            "routerMatrix":[{"router":"claude","graded":3,
              "models":[{"model":"opus","count":2,"percent":66.7},
                         {"model":"haiku","count":1,"percent":33.3}],
              "selections":[{"provider":"codex","count":2,"percent":66.7},
                            {"provider":"claude","count":1,"percent":33.3}]}]}"#,
    )
    .expect("router matrix should decode");
    assert_eq!(page.router_matrix.len(), 1);
    let row = &page.router_matrix[0];
    assert_eq!(row.router, "claude");
    assert_eq!(row.graded, 3);
    assert_eq!(row.models[0].model, "opus");
    assert_eq!(row.models[0].count, 2);
    assert_eq!(row.selections[0].provider, "codex");
    assert_eq!(row.selections[0].count, 2);
    assert!((row.selections[0].percent - 66.7).abs() < f64::EPSILON);
}

#[test]
fn execution_history_query_always_requests_one_page() {
    let args = execution_history_query_args(
        Path::new("ai_execution_history.py"),
        Path::new("history.sqlite3"),
        &["octocat/example".into()],
        Some(-4),
        Some("  Widget  ".into()),
        Some("rounds_desc".into()),
    );
    let value_after = |flag: &str| {
        args.iter()
            .position(|arg| arg == flag)
            .map(|index| args[index + 1].as_str())
    };
    assert_eq!(value_after("--sort"), Some("rounds_desc"));
    assert_eq!(value_after("--limit"), Some("10"));
    assert_eq!(value_after("--offset"), Some("0"));
    assert_eq!(value_after("--search"), Some("Widget"));
    assert_eq!(value_after("--repository"), Some("octocat/example"));

    let unfiltered = execution_history_query_args(
        Path::new("ai_execution_history.py"),
        Path::new("history.sqlite3"),
        &[],
        None,
        None,
        None,
    );
    assert!(unfiltered
        .windows(2)
        .any(|pair| pair[0] == "--limit" && pair[1] == "10"));
    assert!(unfiltered
        .windows(2)
        .any(|pair| pair[0] == "--search" && pair[1].is_empty()));
    assert!(!unfiltered.iter().any(|argument| argument == "--repository"));
}

#[test]
fn feedback_repository_ids_resolve_to_deduplicated_cli_filters() {
    let config = AppConfig {
        repositories: vec![repo("octocat/one"), repo("octocat/two")],
        ..AppConfig::default()
    };
    let names = feedback_repository_names(
        &config,
        &[
            "octocat__one".into(),
            "octocat__two".into(),
            "octocat__one".into(),
        ],
    )
    .unwrap();
    assert_eq!(names, vec!["octocat/one", "octocat/two"]);
    assert!(feedback_repository_names(&config, &[]).unwrap().is_empty());

    let arguments = execution_history_query_args(
        Path::new("ai_execution_history.py"),
        Path::new("history.sqlite3"),
        &names,
        None,
        None,
        None,
    );
    let filters: Vec<_> = arguments
        .windows(2)
        .filter(|pair| pair[0] == "--repository")
        .map(|pair| pair[1].as_str())
        .collect();
    assert_eq!(filters, vec!["octocat/one", "octocat/two"]);
}

#[test]
fn feedback_repository_filter_persists_without_replacing_other_config() {
    let test_app = test_app();
    let app = test_app.handle();
    let repo_dir = real_git_checkout();
    let mut config = valid_config(repo_dir.path());
    config.repositories.push(RepoConfig {
        repo_dir: repo_dir.path().to_string_lossy().into_owned(),
        ..repo("octocat/other")
    });
    config.schedule_time = "08:30".into();
    save_config(app.clone(), app.state(), config).unwrap();

    let saved =
        save_feedback_repo_filter(app.clone(), app.state(), vec!["octocat__other".into()]).unwrap();
    assert_eq!(saved.feedback_repo_filter, vec!["octocat__other"]);
    assert_eq!(saved.schedule_time, "08:30");

    let loaded = get_config(app.state()).unwrap();
    assert_eq!(loaded.feedback_repo_filter, vec!["octocat__other"]);
    assert_eq!(loaded.schedule_time, "08:30");
}

#[test]
fn execution_history_page_deserializes_the_python_page_and_serializes_camel_case() {
    let json = r#"{
        "records": [{
            "execution_id": "11111111-1111-1111-1111-111111111111",
            "repository": "octocat/example",
            "issue_number": 63,
            "issue_title": "Store prompt",
            "original_issue_body": "Original body",
            "ai_provider": "Claude",
            "started_at": "2026-09-14T10:00:00-05:00",
            "final_status": "completed",
            "attempt_number": 1,
            "updated_at": "2026-09-14T10:05:00-05:00"
        }],
        "total": 25,
        "offset": 10,
        "limit": 10
    }"#;
    let page: ExecutionHistoryPage = serde_json::from_str(json).expect("deserialize python page");
    assert_eq!(page.total, 25);
    assert_eq!(page.offset, 10);
    assert_eq!(page.limit, 10);
    assert_eq!(page.records[0].issue_number, 63);

    let camel = serde_json::to_value(&page).expect("serialize for the frontend");
    assert_eq!(camel["total"], 25);
    assert_eq!(camel["records"][0]["issueNumber"], 63);
    assert!(camel["records"][0].get("issue_number").is_none());
}

#[test]
fn ai_execution_record_deserializes_the_python_export_shape_into_camel_case() {
    // Shape mirrors `ai_execution_history.row_to_dict`'s output for one row:
    // snake_case keys from SQLite, JSON-text columns already decoded to arrays.
    let json = r#"{
        "execution_id": "11111111-1111-1111-1111-111111111111",
        "repository": "octocat/example",
        "issue_number": 63,
        "issue_url": "https://github.com/octocat/example/issues/63",
        "issue_title": "Store prompt",
        "original_issue_body": "Original body",
        "effective_prompt": "Final prompt",
        "ai_provider": "Claude",
        "model": "claude-sonnet-5",
        "effort": "high",
        "adversarial_round_count": 3,
        "adversarial_outcome": "resolved_after_n",
        "capacity_consumed_percent": 4.5,
        "adversarial_rounds": [{"round_number": 1, "tester_provider": "Codex"}],
        "reasoning_config": {"effort": "high"},
        "started_at": "2026-09-14T10:00:00-05:00",
        "completed_at": "2026-09-14T10:05:00-05:00",
        "duration_seconds": 300.0,
        "requested_work_summary": "Do the thing",
        "changes_summary": "Did the thing",
        "files_changed": ["src/main.rs"],
        "branch_name": "ai/claude/issue-63",
        "commit_shas": ["deadbeef"],
        "pull_request_number": 64,
        "pull_request_url": "https://github.com/octocat/example/pull/64",
        "operational_notes": ["Issue accepted"],
        "warnings_errors": [],
        "final_status": "completed",
        "attempt_number": 1,
        "application_version": "1.2.3",
        "prompt_template_version": "issue-worker-v1",
        "updated_at": "2026-09-14T10:05:00-05:00",
        "uploaded_at": null,
        "upload_status": "never_uploaded",
        "upload_error": "",
        "uploaded_record_updated_at": null,
        "reviewer_feedback": "",
        "reviewer_feedback_at": null
    }"#;

    let record: AiExecutionRecord = serde_json::from_str(json).expect("deserialize python row");
    assert_eq!(record.repository, "octocat/example");
    assert_eq!(record.files_changed, vec!["src/main.rs".to_string()]);

    let camel = serde_json::to_value(&record).expect("serialize for the frontend");
    assert_eq!(camel["adversarialRoundCount"], 3);
    assert_eq!(camel["adversarialRounds"][0]["tester_provider"], "Codex");
    assert!(camel.get("executionId").is_some(), "{camel}");
    assert!(camel.get("filesChanged").is_some(), "{camel}");
    assert!(camel.get("execution_id").is_none(), "{camel}");
}

#[test]
fn bundled_worker_resources_are_validated_as_one_versioned_set() {
    let resources = tempfile::tempdir().expect("create resource dir");
    for name in super::REQUIRED_WORKER_RESOURCES {
        std::fs::write(resources.path().join(name), "# bundled\n").unwrap();
    }
    let resolved = validate_worker_script_dir(resources.path()).expect("complete worker bundle");
    assert_eq!(resolved, resources.path());

    std::fs::remove_file(resources.path().join("swarm_issue_worker.py")).unwrap();
    let error = validate_worker_script_dir(resources.path())
        .expect_err("an incomplete app bundle must fail before launch");
    assert!(error.contains("swarm_issue_worker.py"), "got: {error}");
    assert!(error.contains("Reinstall or rebuild"), "got: {error}");
}

#[test]
fn detect_tools_finds_real_git_on_this_machine_without_panicking() {
    let test_app = test_app();
    let tools =
        detect_tools(test_app.handle().state()).expect("detect_tools should never itself error");
    let git = tools
        .iter()
        .find(|tool| tool.id == "git")
        .expect("git should always be a reported tool");
    assert!(git.installed, "this dev machine has git on PATH");
    assert!(!git.path.is_empty());
}

// ----- scheduler / repo worker args --------------------------------------

fn args_for(repo: &RepoConfig) -> Vec<String> {
    let config = {
        let mut config = AppConfig::default();
        config.repositories.push(repo.clone());
        config
    };
    repo_worker_args(
        &config,
        repo,
        &PathBuf::from("/tmp/workspace"),
        &PathBuf::from("/usr/bin/git"),
        &PathBuf::from("/usr/bin/gh"),
    )
}

fn pair<'a>(args: &'a [String], flag: &str) -> Option<&'a str> {
    args.iter()
        .position(|value| value == flag)
        .and_then(|index| args.get(index + 1))
        .map(String::as_str)
}

#[test]
fn repo_worker_args_carries_per_repo_branch_config() {
    let repo = RepoConfig {
        base_branch: "trunk".into(),
        integration_branch: "ai-main".into(),
        branch_prefix: "ai".into(),
        ..repo("octocat/example")
    };
    let args = args_for(&repo);
    assert_eq!(pair(&args, "--repo-dir"), Some("/tmp/workspace"));
    assert_eq!(pair(&args, "--base-branch"), Some("trunk"));
    assert_eq!(pair(&args, "--integration-branch"), Some("ai-main"));
    assert_eq!(pair(&args, "--branch-prefix"), Some("ai"));
    assert_eq!(pair(&args, "--github-repository"), Some("octocat/example"));
    assert_eq!(pair(&args, "--assignee"), Some("octocat"));
    assert!(args.contains(&"--auto-approve".to_string()));
    assert!(args.contains(&"--auto-merge".to_string()));
    assert!(
        args.contains(&"--no-auto-promote".to_string()),
        "promotion into the human-owned branch is off unless asked for"
    );
    assert!(
        args.contains(&"--no-monitor-actions".to_string()),
        "Actions monitoring is off unless asked for"
    );
    assert!(args.contains(&"--no-require-issue-tests".to_string()));
    assert!(args.contains(&"--no-allow-environment-only-summary".to_string()));
    // The old delivery/merge flags are gone.
    assert!(!args.iter().any(|value| value == "--delivery-mode"));
    assert!(!args.iter().any(|value| value == "--merge-method"));
}

#[test]
fn repo_worker_args_enables_auto_promote_only_when_configured() {
    let repo = RepoConfig {
        auto_promote: true,
        ..repo("octocat/example")
    };
    let args = args_for(&repo);
    assert!(args.contains(&"--auto-promote".to_string()));
    assert!(!args.contains(&"--no-auto-promote".to_string()));
}

#[test]
fn repo_worker_args_enables_monitor_actions_only_when_configured() {
    let repo = RepoConfig {
        monitor_actions: true,
        ..repo("octocat/example")
    };
    let args = args_for(&repo);
    assert!(args.contains(&"--monitor-actions".to_string()));
    assert!(!args.contains(&"--no-monitor-actions".to_string()));
}

#[test]
fn repo_worker_args_defaults_authors_to_the_assignee() {
    let repo = repo("octocat/example");
    let args = args_for(&repo);
    let all = |flag: &str| -> Vec<&str> {
        args.iter()
            .enumerate()
            .filter(|(_, value)| *value == flag)
            .map(|(index, _)| args[index + 1].as_str())
            .collect()
    };
    assert_eq!(all("--trusted-followup-author"), vec!["octocat"]);
    assert_eq!(all("--completion-author"), vec!["octocat"]);
}

#[test]
fn repo_worker_args_uses_the_global_preferred_provider_when_unset() {
    let mut config = AppConfig {
        preferred_provider: "codex".into(),
        ..AppConfig::default()
    };
    let repo = repo("octocat/example");
    config.repositories.push(repo.clone());
    let args = repo_worker_args(
        &config,
        &repo,
        &PathBuf::from("/tmp/ws"),
        &PathBuf::from("/usr/bin/git"),
        &PathBuf::from("/usr/bin/gh"),
    );
    assert_eq!(pair(&args, "--preferred-provider"), Some("codex"));
}

#[test]
fn repo_worker_args_passes_no_preference_through() {
    let mut config = AppConfig {
        preferred_provider: "auto".into(),
        ..AppConfig::default()
    };
    let repo = repo("octocat/example");
    config.repositories.push(repo.clone());
    let args = repo_worker_args(
        &config,
        &repo,
        &PathBuf::from("/tmp/ws"),
        &PathBuf::from("/usr/bin/git"),
        &PathBuf::from("/usr/bin/gh"),
    );
    assert_eq!(pair(&args, "--preferred-provider"), Some("auto"));
}

#[test]
fn bot_app_slugs_strip_the_bot_suffix_and_stay_unique() {
    let raw = r#"{
        "grok": {"bot_login": "swarm-media-steaming-swarm-grok[bot]"},
        "claude": {"bot_login": "swarm-claude-bot[bot]"},
        "again": {"bot_login": "swarm-claude-bot[bot]"}
    }"#;
    let slugs = bot_app_slugs_from_config(raw).expect("slugs");
    assert_eq!(
        slugs,
        vec![
            "swarm-claude-bot".to_string(),
            "swarm-media-steaming-swarm-grok".to_string(),
        ]
    );
}

#[test]
fn push_access_adds_missing_bots_and_keeps_existing_people() {
    let protection = serde_json::json!({
        "restrictions": {
            "users": [{"login": "DotNetRockStar"}],
            "teams": [],
            "apps": [{"slug": "already-there"}]
        }
    });
    let wanted = vec![
        "swarm-claude-bot".to_string(),
        "swarm-codex-bot".to_string(),
        "already-there".to_string(),
    ];
    let decision = decide_bot_push_access(Some(&protection), &wanted);
    assert_eq!(decision.state, "missing");
    assert!(decision.can_grant);
    assert_eq!(decision.allowed_users, vec!["DotNetRockStar".to_string()]);
    assert_eq!(
        decision.apps,
        vec![
            "already-there".to_string(),
            "swarm-claude-bot".to_string(),
            "swarm-codex-bot".to_string(),
        ]
    );
    assert_eq!(
        decision.missing,
        vec![
            "swarm-claude-bot".to_string(),
            "swarm-codex-bot".to_string()
        ]
    );
    let message = push_access_message("main", &decision);
    assert!(message.contains("DotNetRockStar"), "{message}");
    assert!(message.contains("prohibits the merge"), "{message}");
}

#[test]
fn push_access_is_already_allowed_when_every_bot_is_listed() {
    let protection = serde_json::json!({
        "restrictions": {
            "users": [{"login": "DotNetRockStar"}],
            "apps": [{"slug": "swarm-claude-bot"}]
        }
    });
    let decision = decide_bot_push_access(Some(&protection), &["swarm-claude-bot".to_string()]);
    assert_eq!(decision.state, "allowed");
    assert!(!decision.can_grant);
    assert!(decision.missing.is_empty());
}

#[test]
fn push_access_does_not_invent_a_restriction() {
    let protection = serde_json::json!({
        "restrictions": null,
        "enforce_admins": {"enabled": true}
    });
    let decision = decide_bot_push_access(Some(&protection), &["swarm-claude-bot".to_string()]);
    assert_eq!(decision.state, "unrestricted");
    assert!(!decision.can_grant);
    assert!(decide_bot_push_access(None, &["swarm-claude-bot".to_string()]).state == "unprotected");
}

#[test]
fn grant_apps_request_sends_a_bare_json_array_on_stdin() {
    let apps = vec![
        "swarm-claude-bot".to_string(),
        "swarm-codex-bot".to_string(),
    ];
    let (args, body) = grant_apps_request("owner/repo", "main", &apps);
    assert!(args.ends_with(&["--input".to_string(), "-".to_string()]));
    assert!(args
        .iter()
        .any(|arg| arg.ends_with("/branches/main/protection/restrictions/apps")));
    assert!(!args.iter().any(|arg| arg.contains("apps[]")));
    let parsed: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(
        parsed,
        serde_json::json!(["swarm-claude-bot", "swarm-codex-bot"])
    );
}

#[test]
fn repo_worker_args_carries_advanced_issue_policy() {
    let repo = RepoConfig {
        require_issue_tests: true,
        adversarial_uat_enabled: true,
        allow_environment_only_summary: true,
        ..repo("octocat/example")
    };
    let args = args_for(&repo);
    assert!(args.contains(&"--require-issue-tests".to_string()));
    assert!(args.contains(&"--adversarial-uat-enabled".to_string()));
    assert!(args.contains(&"--allow-environment-only-summary".to_string()));
}

#[test]
fn repo_worker_args_carries_each_adversarial_stage_independently() {
    let both = args_for(&RepoConfig {
        adversarial_uat_enabled: true,
        adversarial_security_enabled: true,
        ..repo("octocat/example")
    });
    assert!(both.contains(&"--adversarial-uat-enabled".to_string()));
    assert!(both.contains(&"--adversarial-security-enabled".to_string()));

    // Security is its own switch: a repository can attack a change for
    // vulnerabilities without also running the UAT loop, and vice versa.
    let security_only = args_for(&RepoConfig {
        adversarial_security_enabled: true,
        ..repo("octocat/example")
    });
    assert!(security_only.contains(&"--no-adversarial-uat-enabled".to_string()));
    assert!(security_only.contains(&"--adversarial-security-enabled".to_string()));

    let neither = args_for(&repo("octocat/example"));
    assert!(neither.contains(&"--no-adversarial-uat-enabled".to_string()));
    assert!(neither.contains(&"--no-adversarial-security-enabled".to_string()));
}

#[test]
fn repo_worker_args_carries_independent_history_settings() {
    let mut config = AppConfig {
        ai_execution_history_enabled: true,
        prompt_feedback_upload_enabled: false,
        ..AppConfig::default()
    };
    let repo = repo("octocat/example");
    config.repositories.push(repo.clone());
    let args = repo_worker_args(
        &config,
        &repo,
        &PathBuf::from("/tmp/ws"),
        &PathBuf::from("/usr/bin/git"),
        &PathBuf::from("/usr/bin/gh"),
    );
    assert!(args.contains(&"--ai-execution-history-enabled".to_string()));
    assert!(args.contains(&"--no-prompt-feedback-upload-enabled".to_string()));
    assert_eq!(
        pair(&args, "--application-version"),
        Some(env!("CARGO_PKG_VERSION"))
    );
    assert!(pair(&args, "--execution-history-db")
        .expect("history database path")
        .ends_with("swarm-automation.sqlite3"));
}

#[test]
fn repo_worker_args_carries_the_saved_routing_toggle_and_preference() {
    let repo = repo("octocat/example");
    let mut config = AppConfig {
        dynamic_model_routing: false,
        routing_optimization: "best".into(),
        ..AppConfig::default()
    };
    config.repositories.push(repo.clone());
    let off = repo_worker_args(
        &config,
        &repo,
        &PathBuf::from("/tmp/ws"),
        &PathBuf::from("/usr/bin/git"),
        &PathBuf::from("/usr/bin/gh"),
    );
    assert!(off.contains(&"--no-dynamic-model-routing".to_string()));
    assert!(!off.contains(&"--dynamic-model-routing".to_string()));
    assert_eq!(pair(&off, "--routing-optimization"), Some("best"));

    config.dynamic_model_routing = true;
    config.routing_optimization = "cost".into();
    let on = repo_worker_args(
        &config,
        &repo,
        &PathBuf::from("/tmp/ws"),
        &PathBuf::from("/usr/bin/git"),
        &PathBuf::from("/usr/bin/gh"),
    );
    assert!(on.contains(&"--dynamic-model-routing".to_string()));
    assert!(!on.contains(&"--no-dynamic-model-routing".to_string()));
    assert_eq!(pair(&on, "--routing-optimization"), Some("cost"));
}

#[test]
fn repo_worker_args_carries_each_provider_quota_floor() {
    let repo = repo("octocat/example");
    let mut config = AppConfig::default();
    config.repositories.push(repo.clone());
    config
        .provider_mut("claude")
        .unwrap()
        .minimum_remaining_percent = Some(0);
    config
        .provider_mut("codex")
        .unwrap()
        .minimum_remaining_percent = Some(10);
    config
        .provider_mut("grok")
        .unwrap()
        .minimum_remaining_percent = Some(0);
    let args = repo_worker_args(
        &config,
        &repo,
        &PathBuf::from("/tmp/ws"),
        &PathBuf::from("/usr/bin/git"),
        &PathBuf::from("/usr/bin/gh"),
    );
    assert_eq!(pair(&args, "--claude-minimum-remaining-percent"), Some("0"));
    assert_eq!(pair(&args, "--codex-minimum-remaining-percent"), Some("10"));
    assert_eq!(pair(&args, "--grok-minimum-remaining-percent"), Some("0"));
}

#[test]
fn scheduler_arguments_degrade_manual_to_continuous_and_pass_the_repos_file() {
    let config = AppConfig {
        schedule_mode: "manual".into(),
        ..AppConfig::default()
    };
    let args = scheduler_arguments(
        &config,
        &PathBuf::from("/bin/runner.py"),
        &PathBuf::from("/usr/bin/python3"),
        &PathBuf::from("/usr/bin/git"),
        &PathBuf::from("/state/repos.json"),
        true,
    );
    assert_eq!(pair(&args, "--repos-file"), Some("/state/repos.json"));
    assert_eq!(pair(&args, "--schedule-mode"), Some("continuous"));
    assert!(args.contains(&"--once".to_string()));
}

#[test]
fn scheduler_arguments_request_one_worker_per_repository_only_when_enabled() {
    let mut config = AppConfig::default();
    let runner = PathBuf::from("/bin/runner.py");
    let python = PathBuf::from("/usr/bin/python3");
    let git = PathBuf::from("/usr/bin/git");
    let repos = PathBuf::from("/state/repos.json");

    let off = scheduler_arguments(&config, &runner, &python, &git, &repos, false);
    assert!(
        !off.contains(&"--parallel-repos".to_string()),
        "the single shared worker is the default"
    );

    config.parallel_repo_workers = true;
    let on = scheduler_arguments(&config, &runner, &python, &git, &repos, false);
    assert!(on.contains(&"--parallel-repos".to_string()));
}

#[test]
fn save_config_round_trips_the_parallel_repo_worker_toggle() {
    let test_app = test_app();
    let app = test_app.handle();
    let mut config = AppConfig::default();
    config.repositories.push(repo("octocat/one"));
    config.parallel_repo_workers = true;

    save_config(app.clone(), app.state(), config).expect("config with the toggle on is valid");
    let loaded = get_config(app.state()).expect("get_config");
    assert!(loaded.parallel_repo_workers);
}

#[test]
fn issue_branch_merge_requires_the_issue_to_be_closed() {
    let error = require_closed_issue(42, "OPEN", "ai-main")
        .expect_err("an open issue must block its branch merge");
    assert!(error.contains("Issue #42 must be closed"), "got: {error}");
    require_closed_issue(42, "CLOSED", "ai-main").expect("closed issue may merge");
}

#[test]
fn issue_branch_tree_only_shows_open_pull_requests() {
    assert!(issue_branch_pr_is_visible(None));
    assert!(issue_branch_pr_is_visible(Some("OPEN")));
    assert!(!issue_branch_pr_is_visible(Some("CLOSED")));
    assert!(!issue_branch_pr_is_visible(Some("MERGED")));
}

#[test]
fn promotion_panel_only_lists_integration_branches_ahead_of_base() {
    // Integration branch exists and carries unmerged commits -> show it, even
    // when it also trails the base branch.
    assert!(needs_promotion(
        true,
        &BranchAheadBehind {
            ahead: 3,
            behind: 0
        }
    ));
    assert!(needs_promotion(
        true,
        &BranchAheadBehind {
            ahead: 1,
            behind: 5
        }
    ));
    // Nothing to promote, or the branch does not exist yet -> hide it.
    assert!(!needs_promotion(
        true,
        &BranchAheadBehind {
            ahead: 0,
            behind: 4
        }
    ));
    assert!(!needs_promotion(
        false,
        &BranchAheadBehind {
            ahead: 2,
            behind: 0
        }
    ));
}

#[test]
fn promotion_pr_reference_parses_number_and_url() {
    assert_eq!(
        parse_pr_ref("42\thttps://github.com/octocat/example/pull/42"),
        Some((42, "https://github.com/octocat/example/pull/42".to_string())),
    );
    assert_eq!(
        parse_pr_ref("  7 \t  https://github.com/o/r/pull/7  "),
        Some((7, "https://github.com/o/r/pull/7".to_string())),
    );
    // `gh` printed nothing (no open PR) or a value that is not a PR URL.
    assert_eq!(parse_pr_ref(""), None);
    assert_eq!(parse_pr_ref("42\tnot-a-url"), None);
    assert_eq!(parse_pr_ref("https://github.com/o/r/pull/9"), None);
}

#[test]
fn promotion_approval_uses_repository_scoped_bot_auth() {
    let mut repository = repo("octocat/example");
    repository.github_apps_config = "/private/apps.json".into();
    let args = promotion_approval_args(
        Path::new("/app/github_app_auth.py"),
        &repository,
        "codex",
        Path::new("/usr/bin/gh"),
        "https://github.com/octocat/example/pull/42",
    );
    assert!(args.windows(2).any(|pair| pair == ["--provider", "codex"]));
    assert!(args
        .windows(2)
        .any(|pair| pair == ["--repository", "octocat/example"]));
    assert!(args
        .windows(2)
        .any(|pair| pair == ["--config", "/private/apps.json"]));
    assert!(args.iter().any(|value| value == "--approve"));
}

#[test]
fn repo_status_args_place_the_subcommand_before_its_flags() {
    let args = repo_status_args(
        Path::new("/app/github_app_auth.py"),
        "/private/apps.json",
        "octocat/example",
        "claude",
    );
    let subcommand = args
        .iter()
        .position(|value| value == "repo-status")
        .expect("repo-status subcommand present");
    let repository = args
        .iter()
        .position(|value| value == "--repository")
        .expect("--repository present");
    let provider = args
        .iter()
        .position(|value| value == "--provider")
        .expect("--provider present");
    // argparse rejects the whole call unless `repo-status` precedes the
    // arguments that belong to that subcommand (#48).
    assert!(
        subcommand < repository,
        "repo-status must precede --repository"
    );
    assert!(subcommand < provider, "repo-status must precede --provider");
    assert!(args
        .windows(2)
        .any(|pair| pair == ["--repository", "octocat/example"]));
    assert!(args.windows(2).any(|pair| pair == ["--provider", "claude"]));
    assert!(args
        .windows(2)
        .any(|pair| pair == ["--config", "/private/apps.json"]));
}

#[test]
fn repo_status_args_are_accepted_by_the_vendored_worker_script() {
    let python = match which_python() {
        Some(python) => python,
        None => return,
    };
    let script = Path::new(env!("CARGO_MANIFEST_DIR")).join("issue_worker/github_app_auth.py");
    let scratch = tempfile::tempdir().expect("temp dir");
    // A config path that does not exist: repository_status returns the
    // "unconfigured" state without any network call, so this stays hermetic
    // while still exercising the real argument parser end to end.
    let missing_config = scratch.path().join("github-apps.json");
    let output = std::process::Command::new(&python)
        .args(repo_status_args(
            &script,
            &missing_config.to_string_lossy(),
            "octocat/example",
            "claude",
        ))
        .output()
        .expect("run github_app_auth.py");
    assert!(
        output.status.success(),
        "repo-status exited with {:?}: {}",
        output.status.code(),
        String::from_utf8_lossy(&output.stderr),
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    let parsed: serde_json::Value =
        serde_json::from_str(stdout.trim()).expect("repo-status prints JSON");
    assert_eq!(parsed["state"], "unconfigured");
}

#[test]
fn check_usage_probe_args_are_accepted_by_the_vendored_worker_script_and_list_only_enabled_providers(
) {
    let python = match which_python() {
        Some(python) => python,
        None => return,
    };
    let script = Path::new(env!("CARGO_MANIFEST_DIR")).join("issue_worker/swarm_issue_worker.py");
    let scratch = tempfile::tempdir().expect("temp dir");
    let config = AppConfig::default();
    // Claude disabled, Codex enabled: since this command never passes
    // `--preferred-provider` itself, the script falls back to its own
    // default ("claude") and, finding it disabled here, used to log a
    // tie-break notice to stdout ahead of check_usage's JSON line. Exercises
    // that regression at the same Rust/Python argv boundary
    // `check_provider_usage` actually calls through.
    let providers = vec![
        ResolvedProvider {
            enabled: false,
            ..resolved_provider("claude", "/nonexistent/claude")
        },
        resolved_provider("codex", "/nonexistent/codex"),
    ];
    let mut arguments = vec![
        script.to_string_lossy().into_owned(),
        "--check-usage".into(),
    ];
    arguments.extend(provider_scheduler_arguments(&config, &providers));
    arguments.extend([
        "--minimum-remaining-percent".into(),
        "10".into(),
        "--state-dir".into(),
        scratch.path().to_string_lossy().into_owned(),
    ]);
    let output = std::process::Command::new(&python)
        .args(&arguments)
        .output()
        .expect("run swarm_issue_worker.py --check-usage");
    assert!(
        output.status.success(),
        "--check-usage exited with {:?}: {}",
        output.status.code(),
        String::from_utf8_lossy(&output.stderr),
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    // The probed providers each narrate their own progress through the
    // worker's normal log() calls, which print to stdout — this asserts
    // check_usage's contract that its own stdout is nonetheless exactly one
    // JSON line, never that narration interleaved with (or instead of) it.
    assert_eq!(stdout.trim().lines().count(), 1, "stdout was: {stdout}");
    let parsed: serde_json::Value =
        serde_json::from_str(stdout.trim()).expect("--check-usage prints one JSON line");
    let providers = parsed["providers"].as_array().expect("providers array");
    assert_eq!(providers.len(), 1);
    assert_eq!(providers[0]["provider"], "codex");
    assert_eq!(providers[0]["status"], 2);
    assert!(providers[0]["remaining_percent"].is_null());
}

fn which_python() -> Option<PathBuf> {
    for candidate in ["python3", "/usr/bin/python3", "/opt/homebrew/bin/python3"] {
        let found = std::process::Command::new(candidate)
            .arg("--version")
            .output()
            .map(|output| output.status.success())
            .unwrap_or(false);
        if found {
            return Some(PathBuf::from(candidate));
        }
    }
    None
}

#[test]
fn promotion_reconciliation_preserves_checkout_and_favors_human_conflicts() {
    let root = tempfile::tempdir().expect("temporary git repository");
    let remote = root.path().join("remote.git");
    let workspace = root.path().join("workspace");
    let run = |directory: &Path, arguments: &[&str]| {
        let output = std::process::Command::new("git")
            .args(arguments)
            .current_dir(directory)
            .output()
            .expect("run git");
        assert!(
            output.status.success(),
            "git {} failed: {}",
            arguments.join(" "),
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8_lossy(&output.stdout).trim().to_string()
    };
    run(
        root.path(),
        &[
            "init",
            "--bare",
            "--initial-branch=main",
            remote.to_str().unwrap(),
        ],
    );
    run(
        root.path(),
        &[
            "clone",
            remote.to_str().unwrap(),
            workspace.to_str().unwrap(),
        ],
    );
    run(&workspace, &["config", "user.name", "Test User"]);
    run(&workspace, &["config", "user.email", "test@example.com"]);
    std::fs::write(workspace.join("shared.txt"), "initial\n").unwrap();
    run(&workspace, &["add", "shared.txt"]);
    run(&workspace, &["commit", "-m", "initial"]);
    run(&workspace, &["push", "origin", "main"]);
    run(&workspace, &["switch", "-c", "ai-main"]);
    std::fs::write(workspace.join("shared.txt"), "ai change\n").unwrap();
    std::fs::write(workspace.join("ai-only.txt"), "keep me\n").unwrap();
    run(&workspace, &["add", "."]);
    run(&workspace, &["commit", "-m", "ai work"]);
    run(&workspace, &["push", "origin", "ai-main"]);
    run(&workspace, &["switch", "main"]);
    std::fs::write(workspace.join("shared.txt"), "human change\n").unwrap();
    run(&workspace, &["add", "shared.txt"]);
    run(&workspace, &["commit", "-m", "human work"]);
    run(&workspace, &["push", "origin", "main"]);

    let repository = RepoConfig {
        repo_dir: workspace.to_string_lossy().into_owned(),
        ..repo("octocat/example")
    };
    reconcile_integration_for_promotion(Path::new("git"), &workspace, &repository)
        .expect("reconcile promotion branches");

    assert_eq!(run(&workspace, &["branch", "--show-current"]), "main");
    run(&workspace, &["fetch", "origin"]);
    assert_eq!(
        run(&workspace, &["show", "origin/ai-main:shared.txt"]),
        "human change"
    );
    assert_eq!(
        run(&workspace, &["show", "origin/ai-main:ai-only.txt"]),
        "keep me"
    );
    run(
        &workspace,
        &[
            "merge-base",
            "--is-ancestor",
            "origin/main",
            "origin/ai-main",
        ],
    );
}

// ----- provider round-trips (unchanged behaviour) ----------------------

#[test]
fn save_config_round_trips_a_provider_the_user_excluded_from_the_flow() {
    let test_app = test_app();
    let app = test_app.handle();
    let repo_dir = real_git_checkout();
    let mut config = valid_config(repo_dir.path());
    for provider in &mut config.providers {
        provider.enabled = provider.id != "codex";
        if provider.id == "grok" {
            provider.effort = "xhigh".into();
        }
    }

    save_config(app.clone(), app.state(), config).expect("valid provider set saves");
    let loaded = get_config(app.state()).expect("get_config");
    assert!(!loaded.provider("codex").unwrap().enabled);
    assert!(loaded.provider("claude").unwrap().enabled);
    assert_eq!(loaded.provider("grok").unwrap().effort, "xhigh");
}

#[test]
fn save_config_rejects_a_preferred_provider_that_is_not_enabled() {
    let test_app = test_app();
    let app = test_app.handle();
    let repo_dir = real_git_checkout();
    let mut config = valid_config(repo_dir.path());
    config.preferred_provider = "grok".into();
    for provider in &mut config.providers {
        provider.enabled = provider.id == "claude";
    }
    let error = save_config(app.clone(), app.state(), config)
        .expect_err("preferred provider must be one of the enabled ones");
    assert!(error.contains("preferred provider"), "got: {error}");
}
