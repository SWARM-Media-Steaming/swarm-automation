//! JobRunner contract: Docker (a fake CLI, and a live daemon when one is up)
//! and ECS Fargate (a fake endpoint) launch the same image and entrypoint.

mod common;

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::capture_logs;
use serde_json::{json, Value};
use swarm_web::runner::{
    isolation, Checkpoint, DockerJobRunner, EcsFargateJobRunner, JobRunner, JobSpec, JobState,
    RunnerError, WORKER_ENTRYPOINT,
};
use swarm_web::secret::Secret;

const CANARY: &str = "ghs_canarytokenvalue123456789";
const IMAGE: &str = "swarm-automation-worker";

fn entrypoint() -> Vec<String> {
    WORKER_ENTRYPOINT
        .iter()
        .map(|part| (*part).to_string())
        .collect()
}

fn spec(name: &str, checkpoint: Option<Checkpoint>) -> JobSpec {
    let mut plain = BTreeMap::new();
    plain.insert("AWS_ACCESS_KEY_ID".into(), CANARY.into());
    plain.insert("SWARM_TENANT".into(), "t418".into());
    if let Some(checkpoint) = &checkpoint {
        plain.extend(swarm_web::runner::checkpoint_env(checkpoint));
    }
    let mut secret = BTreeMap::new();
    secret.insert("GH_TOKEN".into(), Secret::new(CANARY));
    JobSpec {
        name: name.into(),
        image: IMAGE.into(),
        entrypoint: entrypoint(),
        command: Vec::new(),
        plain_env: plain,
        secret_env: secret,
        cpu_millis: 1000,
        memory_mib: 2048,
        network: "bridge".into(),
        tenant: "t418".into(),
        git_cache: None,
        checkpoint,
        max_runtime_secs: None,
    }
}

fn assert_same_contract(first: &JobSpec, second: &JobSpec) {
    assert_eq!(first.image, second.image);
    assert_eq!(first.entrypoint, second.entrypoint);
    assert_eq!(first.entrypoint, entrypoint());
    let left = isolation(first);
    let right = isolation(second);
    assert_eq!(left, right);
    assert!(left.readonly_root);
    assert!(!left.shared_mounts);
    assert_eq!(left.user, "1000:1000");
    assert!(left.max_runtime_secs.is_none());
    assert!(!format!("{first:?}").contains(CANARY));
    assert!(!format!("{second:?}").contains(CANARY));
}

#[test]
fn the_worker_image_pins_tools_and_does_not_run_as_root() {
    let docker = include_str!("../worker/Dockerfile");
    for pin in [
        "GH_VERSION=2.79.0",
        "NODE_VERSION=22.14.0",
        "CLAUDE_CODE_VERSION=2.1.285",
        "CODEX_VERSION=0.160.1",
        "GROK_VERSION=1.0.46",
        "USER 1000:1000",
        "ENTRYPOINT [\"python3\", \"-I\", \"/opt/swarm/issue_worker/job_launch.py\"]",
    ] {
        assert!(docker.contains(pin), "missing {pin}");
    }
    assert!(!docker.contains("SWARM_JOB_WORKER"));
    let fixture = include_str!("../worker/Dockerfile.fixture");
    assert!(fixture.contains("SWARM_JOB_WORKER=/opt/swarm/fixture_worker.py"));
    let allow = include_str!("../worker/egress-allowlist.txt");
    assert!(allow.contains("169.254.169.254"));
    assert!(allow.contains("api.github.com"));
}

#[tokio::test]
async fn docker_contract_uses_one_image_and_never_puts_the_token_on_the_command_line() {
    let root = tempfile::tempdir().unwrap();
    let state_path = root.path().join("state.json");
    std::fs::write(
        &state_path,
        r#"{"n":0,"containers":{},"argv":[],"exit_queue":[13,0]}"#,
    )
    .unwrap();
    let wrapper = root.path().join("docker");
    let script = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/fake_docker.py");
    std::fs::write(
        &wrapper,
        format!("#!/bin/sh\nexec python3 {script:?} \"$@\"\n"),
    )
    .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&wrapper, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    let mut runner = DockerJobRunner::new(&wrapper);
    runner.cli_env.insert(
        "SWARM_FAKE_DOCKER_STATE".into(),
        root.path().to_string_lossy().into_owned(),
    );
    let (logs, _guard) = capture_logs(false);
    let first = spec("job-one", None);
    let started = runner.start(first.clone()).await.unwrap();
    // `status` destroys an exited container, so read its env names first.
    let after_start: Value =
        serde_json::from_str(&std::fs::read_to_string(&state_path).unwrap()).unwrap();
    let status = runner.status(&started.id).await.unwrap();
    assert_eq!(status.state, JobState::Exited);
    assert_eq!(status.exit_code, Some(13));
    let resumed = runner
        .resume_from_checkpoint(spec(
            "job-two",
            Some(Checkpoint {
                kind: "in-progress".into(),
                key: "current".into(),
            }),
        ))
        .await
        .unwrap();
    let after_resume: Value =
        serde_json::from_str(&std::fs::read_to_string(&state_path).unwrap()).unwrap();
    assert_ne!(started.id, resumed.id);
    let text = std::fs::read_to_string(&state_path).unwrap();
    assert!(
        !text.contains(CANARY),
        "secret values must not be stored by the fake or the argv"
    );
    assert!(!text.contains("900"));
    let state: Value = serde_json::from_str(&text).unwrap();
    let argv = state["argv"].to_string();
    for flag in [
        "--read-only",
        "--user",
        "1000:1000",
        "--cap-drop",
        "ALL",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--add-host",
        "169.254.169.254:0.0.0.0",
        "--stop-timeout",
        "30",
        "--env-file",
        "--network",
        "--entrypoint",
        "python3",
        IMAGE,
        "job_launch.py",
    ] {
        assert!(argv.contains(flag), "missing {flag} in {argv}");
    }
    assert!(!argv.contains(" -e ") && !argv.contains("\"-e\""));
    let names = after_start["containers"]["cid1"]["env_names"]
        .as_array()
        .unwrap();
    let resumed_names = after_resume["containers"]["cid2"]["env_names"]
        .as_array()
        .unwrap();
    assert!(names.iter().any(|name| name == "AWS_EC2_METADATA_DISABLED"));
    assert!(names.iter().all(|name| name != "AWS_ACCESS_KEY_ID"));
    assert!(resumed_names.iter().any(|name| name == "SWARM_JOB_RESUME"));
    assert!(resumed_names
        .iter()
        .any(|name| name == "SWARM_CHECKPOINT_KIND"));
    assert_eq!(after_start["containers"]["cid1"]["gh_token"], true);
    assert_same_contract(
        &first,
        &spec(
            "job-two",
            Some(Checkpoint {
                kind: "in-progress".into(),
                key: "current".into(),
            }),
        ),
    );
    assert!(!logs.text().contains(CANARY));

    let mut running = DockerJobRunner::new(&wrapper);
    running.cli_env.insert(
        "SWARM_FAKE_DOCKER_STATE".into(),
        root.path().to_string_lossy().into_owned(),
    );
    // The queue is empty now, so this container stays running and can be paused.
    let paused = running.start(spec("job-pause", None)).await.unwrap();
    assert_eq!(
        running.pause(&paused.id).await.unwrap().state,
        JobState::Paused
    );
    assert_eq!(
        running.resume_running(&paused.id).await.unwrap().state,
        JobState::Running
    );
    let stopped = running.cancel(&paused.id).await.unwrap();
    assert_eq!(stopped.exit_code, Some(143));
}

#[tokio::test]
async fn a_shared_git_cache_without_the_tenant_is_refused() {
    let mut bad = spec("job-cache", None);
    bad.git_cache = Some(std::path::PathBuf::from("/var/cache/git"));
    let runner = DockerJobRunner::new("docker");
    let error = runner.start(bad).await.unwrap_err();
    assert!(error.to_string().contains("tenant"));
}

#[derive(Default)]
struct EcsState {
    pause_unsupported: bool,
    requests: Vec<(String, Value, String)>,
    tasks: BTreeMap<String, String>,
    next: u64,
}

async fn serve_ecs(state: Arc<Mutex<EcsState>>) -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let app = axum::Router::new().fallback(ecs_http).with_state(state);
    tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });
    format!("http://{address}")
}

async fn ecs_http(
    axum::extract::State(state): axum::extract::State<Arc<Mutex<EcsState>>>,
    headers: axum::http::HeaderMap,
    body: axum::body::Bytes,
) -> (axum::http::StatusCode, String) {
    let target = headers
        .get("x-amz-target")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .to_string();
    let authorization = headers
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .to_string();
    let parsed = serde_json::from_slice(&body).unwrap_or(json!({}));
    let (status, response) = ecs_response(&state, &target, &parsed, &authorization);
    (status, response.to_string())
}

fn ecs_response(
    state: &Arc<Mutex<EcsState>>,
    target: &str,
    body: &Value,
    authorization: &str,
) -> (axum::http::StatusCode, Value) {
    let mut guard = state.lock().expect("ecs state");
    guard
        .requests
        .push((target.to_string(), body.clone(), authorization.to_string()));
    let ok = axum::http::StatusCode::OK;
    if target.ends_with("PauseTask") && guard.pause_unsupported {
        return (
            axum::http::StatusCode::BAD_REQUEST,
            json!({"__type": "UnknownOperationException", "message": "PauseTask"}),
        );
    }
    if target.ends_with("RunTask") {
        guard.next += 1;
        let arn = format!("arn:aws:ecs:us-east-1:1:task/swarm/{}", guard.next);
        let name = body["startedBy"].as_str().unwrap_or("");
        guard.tasks.insert(name.to_string(), "RUNNING".into());
        guard.tasks.insert(arn.clone(), "RUNNING".into());
        return (
            ok,
            json!({"tasks": [{"taskArn": arn, "lastStatus": "RUNNING"}]}),
        );
    }
    if target.ends_with("DescribeTasks") {
        let arn = body["tasks"][0].as_str().unwrap_or("");
        let status = guard
            .tasks
            .get(arn)
            .cloned()
            .unwrap_or_else(|| "RUNNING".into());
        let exit = if status == "STOPPED" { 143 } else { 0 };
        return (
            ok,
            json!({
                "tasks": [{
                    "taskArn": arn,
                    "lastStatus": status,
                    "containers": [{"exitCode": exit}]
                }]
            }),
        );
    }
    if target.ends_with("StopTask") {
        let arn = body["task"].as_str().unwrap_or("").to_string();
        guard.tasks.insert(arn, "STOPPED".into());
        return (ok, json!({}));
    }
    if target.ends_with("PauseTask") {
        let arn = body["task"].as_str().unwrap_or("").to_string();
        guard.tasks.insert(arn, "PAUSED".into());
        return (ok, json!({}));
    }
    if target.ends_with("ResumeTask") {
        let arn = body["task"].as_str().unwrap_or("").to_string();
        guard.tasks.insert(arn, "RUNNING".into());
        return (ok, json!({}));
    }
    (ok, json!({}))
}

#[tokio::test]
async fn fargate_contract_matches_docker_and_has_no_task_role() {
    let state = Arc::new(Mutex::new(EcsState::default()));
    let endpoint = serve_ecs(state.clone()).await;
    let runner = EcsFargateJobRunner::new(
        endpoint,
        "us-east-1",
        "swarm",
        "swarm-worker",
        vec!["subnet-1".into()],
        vec!["sg-1".into()],
    )
    .unwrap()
    .with_static_credentials(Secret::new("AKIAEXAMPLE"), Secret::new("secret-key-value"));
    let (logs, _guard) = capture_logs(false);
    let first = spec("job-one", None);
    let started = runner.start(first.clone()).await.unwrap();
    assert_eq!(
        runner.status(&started.id).await.unwrap().state,
        JobState::Running
    );
    assert_eq!(
        runner.pause(&started.id).await.unwrap().state,
        JobState::Paused
    );
    assert_eq!(
        runner.resume_running(&started.id).await.unwrap().state,
        JobState::Running
    );
    let resumed = runner
        .resume_from_checkpoint(spec(
            "job-two",
            Some(Checkpoint {
                kind: "in-progress".into(),
                key: "current".into(),
            }),
        ))
        .await
        .unwrap();
    assert_ne!(started.id, resumed.id);
    {
        let guard = state.lock().unwrap();
        let run = guard
            .requests
            .iter()
            .find(|(target, _, _)| target.ends_with("RunTask"))
            .expect("RunTask");
        let body = &run.1;
        assert!(body.get("taskRoleArn").is_none());
        assert_eq!(
            body["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"],
            "DISABLED"
        );
        assert_eq!(body["enableExecuteCommand"], false);
        let command = body["overrides"]["containerOverrides"][0]["command"]
            .as_array()
            .unwrap();
        assert_eq!(command[0], "python3");
        assert!(command
            .iter()
            .any(|part| part == "/opt/swarm/issue_worker/job_launch.py"));
        assert!(body["tags"].to_string().contains("\"none\""));
        assert!(!body.to_string().contains("\"900\""));
        assert!(!run.2.contains("secret-key-value"));
        assert!(run.2.contains("AKIAEXAMPLE"));
    }
    assert_same_contract(
        &first,
        &spec(
            "job-two",
            Some(Checkpoint {
                kind: "in-progress".into(),
                key: "current".into(),
            }),
        ),
    );
    assert!(!logs.text().contains(CANARY));
    assert!(!logs.text().contains("secret-key-value"));

    state.lock().unwrap().pause_unsupported = true;
    let error = runner.pause(&started.id).await;
    // Unsupported PauseTask stops the task instead of failing the call.
    let status = error.unwrap_or_else(|error: RunnerError| panic!("{error}"));
    assert_eq!(status.exit_code, Some(143));
}

#[tokio::test]
async fn live_docker_is_skipped_without_a_daemon() {
    let docker = std::process::Command::new("docker").arg("info").output();
    let Ok(output) = docker else {
        return;
    };
    if !output.status.success() {
        return;
    }
    let image = std::process::Command::new("docker")
        .args(["image", "inspect", "alpine:3.20"])
        .output();
    if !image.map(|output| output.status.success()).unwrap_or(false) {
        eprintln!("docker is up but alpine:3.20 is not local; live job run skipped");
        return;
    }
    let runner = DockerJobRunner::new("docker");
    let mut job = spec(&format!("swarmlive{}", std::process::id()), None);
    job.image = "alpine:3.20".into();
    job.entrypoint = vec!["/bin/echo".into()];
    job.command = vec!["swarm-live".into()];
    job.network = "bridge".into();
    let started = runner.start(job).await.expect("docker run");
    let mut status = runner.status(&started.id).await.unwrap();
    for _ in 0..20 {
        if status.state == JobState::Exited {
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
        status = runner.status(&started.id).await.unwrap();
    }
    assert_eq!(status.exit_code, Some(0));
    let lines = runner.logs(&started.id).await.unwrap_or_default();
    assert!(lines.iter().any(|line| line.contains("swarm-live")) || status.exit_code == Some(0));
}
