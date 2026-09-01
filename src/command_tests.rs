use super::{
    detect_tools, get_config, get_test_plan, get_test_runs, inspect_repository,
    issue_branch_pr_is_visible,
    repo_worker_args, require_closed_issue, save_config, save_test_device, scheduler_arguments,
    validate_worker_script_dir, AppState, ResolvedProvider,
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
            smtp_password_configured: std::sync::Mutex::new(None),
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
        bin: PathBuf::from(bin),
        enabled: true,
    }
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
    assert!(!inspection.uat_available);
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
    std::fs::create_dir_all(repo.path().join(".swarm")).unwrap();
    std::fs::write(
        repo.path().join(".swarm/tests.json"),
        r#"{"version":1,"suites":[{"id":"unit","name":"Unit","command":["/usr/bin/true"]}]}"#,
    )
    .unwrap();

    let inspection = inspect_repository(repo.path().to_string_lossy().into_owned());
    assert!(inspection.valid);
    assert!(inspection.worker_available);
    assert!(inspection.uat_available);
}

#[test]
fn repository_test_definition_is_discovered_through_the_command_layer() {
    let test_app = test_app();
    let app = test_app.handle();
    let repo_dir = real_git_checkout();
    std::fs::create_dir_all(repo_dir.path().join(".swarm")).unwrap();
    std::fs::write(
        repo_dir.path().join(".swarm/tests.json"),
        r#"{"version":1,"suites":[{"id":"integration","name":"Integration","command":["/usr/bin/true"],"requirements":{"files":["Cargo.toml"]}}]}"#,
    )
    .unwrap();
    std::fs::write(repo_dir.path().join("Cargo.toml"), "[package]\n").unwrap();
    let config = valid_config(repo_dir.path());
    save_config(app.clone(), app.state(), config).unwrap();

    let plan = get_test_plan(app.clone(), app.state(), "octocat__example".into()).unwrap();
    assert!(plan.available);
    assert_eq!(plan.suites.len(), 1);
    assert_eq!(plan.suites[0].result.state, "Ready");

    // History starts empty and `get_test_runs` is a safe read.
    let runs = get_test_runs(app.clone(), app.state(), "octocat__example".into()).unwrap();
    assert!(runs.is_empty());
}

#[test]
fn device_choice_round_trips_per_repository_without_touching_other_profiles() {
    let test_app = test_app();
    let app = test_app.handle();
    let first = real_git_checkout();
    let second = real_git_checkout();
    let mut config = valid_config(first.path());
    config.repositories.push(RepoConfig {
        repo_dir: second.path().to_string_lossy().into_owned(),
        ..repo("octocat/second")
    });
    save_config(app.clone(), app.state(), config).unwrap();

    let saved = save_test_device(
        app.clone(),
        app.state(),
        "octocat__example".into(),
        "192.0.2.8:5555".into(),
    )
    .unwrap();
    assert_eq!(
        saved.repositories[0].test_inputs.get("fireTvSerial"),
        Some(&"192.0.2.8:5555".to_string())
    );
    assert!(saved.repositories[1].test_inputs.is_empty());
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

#[test]
fn smtp_password_status_probe_is_cached_after_the_first_check() {
    let test_app = test_app();
    let mut probes = 0;

    let first =
        super::cached_smtp_password_configured(test_app.app.state::<AppState>().inner(), || {
            probes += 1;
            true
        })
        .expect("first SMTP password status check succeeds");
    let second =
        super::cached_smtp_password_configured(test_app.app.state::<AppState>().inner(), || {
            probes += 1;
            false
        })
        .expect("second SMTP password status check succeeds");

    assert!(first);
    assert!(second);
    assert_eq!(probes, 1, "status polling should not keep probing Keychain");
}

#[test]
fn smtp_password_status_cache_can_be_updated_after_explicit_password_actions() {
    let test_app = test_app();

    super::set_cached_smtp_password_configured(test_app.app.state::<AppState>().inner(), false)
        .expect("cache false");
    assert!(!super::cached_smtp_password_configured(
        test_app.app.state::<AppState>().inner(),
        || true
    )
    .expect("cached false should be returned"));

    super::set_cached_smtp_password_configured(test_app.app.state::<AppState>().inner(), true)
        .expect("cache true");
    assert!(super::cached_smtp_password_configured(
        test_app.app.state::<AppState>().inner(),
        || false
    )
    .expect("cached true should be returned"));
}

#[test]
fn automation_status_uses_cached_smtp_password_state() {
    let test_app = test_app();
    let app = test_app.handle();
    let repo_dir = real_git_checkout();
    let config = valid_config(repo_dir.path());
    save_config(app.clone(), app.state(), config).expect("config saves");
    super::set_cached_smtp_password_configured(app.state::<AppState>().inner(), true)
        .expect("cache true");

    let status = super::get_automation_status(app.clone(), app.state())
        .expect("status should use cached SMTP password state");

    assert!(status.smtp_password_configured);
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
    assert!(args.contains(&"--no-require-issue-tests".to_string()));
    assert!(args.contains(&"--no-allow-environment-only-summary".to_string()));
    // The old delivery/merge flags are gone.
    assert!(!args.iter().any(|value| value == "--delivery-mode"));
    assert!(!args.iter().any(|value| value == "--merge-method"));
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
    let mut config = AppConfig::default();
    config.preferred_provider = "codex".into();
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
fn repo_worker_args_carries_advanced_issue_policy() {
    let repo = RepoConfig {
        require_issue_tests: true,
        allow_environment_only_summary: true,
        ..repo("octocat/example")
    };
    let args = args_for(&repo);
    assert!(args.contains(&"--require-issue-tests".to_string()));
    assert!(args.contains(&"--allow-environment-only-summary".to_string()));
}

#[test]
fn scheduler_arguments_degrade_manual_to_continuous_and_pass_the_repos_file() {
    let mut config = AppConfig::default();
    config.schedule_mode = "manual".into();
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
