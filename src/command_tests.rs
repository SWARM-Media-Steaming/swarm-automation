use super::{
    detect_tools, get_config, inspect_repository, issue_worker_arguments, save_config, AppState,
};
use crate::config::AppConfig;
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

fn valid_config(repo_dir: &Path) -> AppConfig {
    AppConfig {
        repo_dir: repo_dir.to_string_lossy().into_owned(),
        github_repository: "octocat/example".into(),
        assignee: "octocat".into(),
        ..AppConfig::default()
    }
}

#[test]
fn save_config_then_get_config_round_trips_through_a_real_file() {
    let test_app = test_app();
    let app = test_app.handle();
    let repo = real_git_checkout();
    let config = valid_config(repo.path());

    let saved = save_config(app.clone(), app.state(), config.clone())
        .expect("save_config should succeed against a real, valid repo checkout");
    assert_eq!(saved.repo_dir, config.repo_dir);

    let loaded = get_config(app.state()).expect("get_config should succeed");
    assert_eq!(loaded.repo_dir, config.repo_dir);
    assert_eq!(loaded.github_repository, "octocat/example");

    // The config file this wrote must be real, present on disk, and
    // owner-only (0600) — matches config.rs's own save() contract, and is
    // the whole reason a per-test isolated data dir exists: this assertion
    // would be meaningless (or worse, flaky) against a shared real path.
    let config_path = test_app
        ._data_dir
        .path()
        .join(crate::config::CONFIG_FILE);
    assert!(config_path.is_file(), "config.json should exist on disk");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = std::fs::metadata(&config_path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "config.json must be owner-only");
    }
}

#[test]
fn save_config_rejects_a_repo_dir_that_does_not_exist() {
    let test_app = test_app();
    let app = test_app.handle();
    let config = valid_config(Path::new("/definitely/not/a/real/path"));

    let error = save_config(app.clone(), app.state(), config)
        .expect_err("a nonexistent repo_dir must be rejected, not silently saved");
    assert!(
        error.contains("existing local repository folder"),
        "expected the real validation message, got: {error}"
    );
}

#[test]
fn inspect_repository_on_a_real_git_checkout_reports_valid_with_no_scripts() {
    let repo = real_git_checkout();
    let inspection = inspect_repository(repo.path().to_string_lossy().into_owned());
    assert!(inspection.valid, "a real `git init`-ed folder must be valid");
    assert!(
        !inspection.worker_available,
        "a bare git init has no scripts/issue_worker"
    );
    assert!(
        !inspection.uat_available,
        "a bare git init has no scripts/tests/full_uat_cron.sh"
    );
    assert!(inspection.error.is_empty());
}

#[test]
fn inspect_repository_on_a_plain_folder_reports_invalid() {
    let dir = tempfile::tempdir().expect("create temp dir");
    let inspection = inspect_repository(dir.path().to_string_lossy().into_owned());
    assert!(!inspection.valid);
    assert!(
        inspection.error.contains("not a Git checkout"),
        "expected the real not-a-git-checkout message, got: {}",
        inspection.error
    );
}

/// Proof that a target repository shipping its own issue-worker/UAT
/// scripts (the SWARM convention this tool was originally built against)
/// is detected correctly — the exact case worker_script_dir in main.rs
/// prefers over its own bundled resources. A synthetic fixture, not the
/// real SWARM checkout: this app no longer lives in that tree, so nothing
/// here may assume it's available at test time.
#[test]
fn inspect_repository_detects_a_target_repos_own_script_bundles() {
    let repo = real_git_checkout();
    std::fs::create_dir_all(repo.path().join("scripts/issue_worker"))
        .expect("create scripts/issue_worker fixture dir");
    std::fs::write(
        repo.path()
            .join("scripts/issue_worker/install_swarm_issue_cron.py"),
        "#!/usr/bin/env python3\n",
    )
    .expect("write fixture worker script");
    std::fs::create_dir_all(repo.path().join("scripts/tests"))
        .expect("create scripts/tests fixture dir");
    std::fs::write(
        repo.path().join("scripts/tests/full_uat_cron.sh"),
        "#!/usr/bin/env bash\n",
    )
    .expect("write fixture UAT cron script");

    let inspection = inspect_repository(repo.path().to_string_lossy().into_owned());
    assert!(inspection.valid);
    assert!(
        inspection.worker_available,
        "a target repo with scripts/issue_worker/install_swarm_issue_cron.py must be detected"
    );
    assert!(
        inspection.uat_available,
        "a target repo with scripts/tests/full_uat_cron.sh must be detected"
    );
}

#[test]
fn detect_tools_finds_real_git_on_this_machine_without_panicking() {
    let test_app = test_app();
    let tools = detect_tools(test_app.handle().state())
        .expect("detect_tools should never itself error, even if individual tools are missing");
    let git = tools
        .iter()
        .find(|tool| tool.id == "git")
        .expect("git should always be a reported tool");
    assert!(
        git.installed,
        "this dev machine has git on PATH; detect_tools should find it"
    );
    assert!(!git.path.is_empty());
}

#[test]
fn issue_worker_arguments_carries_schedule_and_delivery_config_through() {
    let mut config = valid_config(Path::new("/tmp/example"));
    config.schedule_mode = "weekdays".into();
    config.schedule_time = "07:30".into();
    config.delivery_mode = "pull-request".into();
    config.auto_merge = true;

    let arguments = issue_worker_arguments(
        &config,
        &PathBuf::from("/bin/runner.py"),
        &PathBuf::from("/usr/bin/python3"),
        &PathBuf::from("/usr/bin/git"),
        &PathBuf::from("/usr/bin/gh"),
        &PathBuf::from("/usr/bin/claude"),
        &PathBuf::from("/usr/bin/codex"),
        false,
    );

    let pair = |flag: &str| -> Option<String> {
        arguments
            .iter()
            .position(|value| value == flag)
            .and_then(|index| arguments.get(index + 1).cloned())
    };
    assert_eq!(pair("--schedule-mode").as_deref(), Some("weekdays"));
    assert_eq!(pair("--schedule-time").as_deref(), Some("07:30"));
    assert_eq!(pair("--github-repository").as_deref(), Some("octocat/example"));
    assert_eq!(pair("--assignee").as_deref(), Some("octocat"));
    assert!(arguments.contains(&"--auto-merge".to_string()));
    assert!(
        !arguments.contains(&"--once".to_string()),
        "run_once=false must not add --once"
    );
}

#[test]
fn issue_worker_arguments_defaults_trusted_and_completion_authors_to_the_assignee() {
    let config = valid_config(Path::new("/tmp/example"));
    assert!(config.trusted_followup_authors.is_empty());
    assert!(config.completion_authors.is_empty());

    let arguments = issue_worker_arguments(
        &config,
        &PathBuf::from("/bin/runner.py"),
        &PathBuf::from("/usr/bin/python3"),
        &PathBuf::from("/usr/bin/git"),
        &PathBuf::from("/usr/bin/gh"),
        &PathBuf::from("/usr/bin/claude"),
        &PathBuf::from("/usr/bin/codex"),
        false,
    );

    let values_for = |flag: &str| -> Vec<String> {
        arguments
            .iter()
            .enumerate()
            .filter(|(_, value)| *value == flag)
            .map(|(index, _)| arguments[index + 1].clone())
            .collect()
    };
    assert_eq!(
        values_for("--trusted-followup-author"),
        vec!["octocat".to_string()],
        "a blank trusted-authors list must fall back to the configured assignee, \
         not silently trust no one"
    );
    assert_eq!(
        values_for("--completion-author"),
        vec!["octocat".to_string()],
        "a blank completion-authors list must fall back to the configured assignee"
    );
}

#[test]
fn issue_worker_arguments_manual_schedule_runs_as_continuous_with_once() {
    let mut config = valid_config(Path::new("/tmp/example"));
    config.schedule_mode = "manual".into();

    let arguments = issue_worker_arguments(
        &config,
        &PathBuf::from("/bin/runner.py"),
        &PathBuf::from("/usr/bin/python3"),
        &PathBuf::from("/usr/bin/git"),
        &PathBuf::from("/usr/bin/gh"),
        &PathBuf::from("/usr/bin/claude"),
        &PathBuf::from("/usr/bin/codex"),
        true,
    );

    let schedule_mode_index = arguments
        .iter()
        .position(|value| value == "--schedule-mode")
        .expect("--schedule-mode flag should be present");
    assert_eq!(
        arguments[schedule_mode_index + 1], "continuous",
        "a manual-profile Run Now must not pass --schedule-mode manual to the worker, \
         which has no such mode — it degrades to a one-shot continuous check"
    );
    assert!(arguments.contains(&"--once".to_string()));
}
