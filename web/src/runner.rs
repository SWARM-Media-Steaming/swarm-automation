//! One container per job, on Docker locally or ECS Fargate on AWS.
//!
//! Both runners launch the same image and entrypoint ([`WORKER_ENTRYPOINT`]).
//! They differ by configuration: a Docker CLI versus an ECS `RunTask` call.
//! A job's filesystem is the container's own tmpfs. Nothing is mounted from
//! another job, the root filesystem is read-only, and the cloud metadata
//! address is sunk so a job cannot pick up node credentials.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::time::Duration;

use async_trait::async_trait;
use hmac::{Hmac, Mac};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;
use tokio::sync::mpsc;

use crate::secret::Secret;

/// The command both runners use when the orchestrator does not override it.
/// The image's `ENTRYPOINT` is this same command.
pub const WORKER_ENTRYPOINT: &[&str] = &["python3", "-I", "/opt/swarm/issue_worker/job_launch.py"];

/// How long `docker stop` waits for the entrypoint to publish its checkpoint.
/// This is not a limit on how long the job may run.
pub const STOP_GRACE_SECS: u64 = 30;

const METADATA_SINK: &str = "169.254.169.254:0.0.0.0";
const RUN_USER: &str = "1000:1000";
const PIDS_LIMIT: &str = "256";

/// Environment the worker must not see. A job has no cloud role.
const FORBIDDEN_ENV: &[&str] = &[
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "ECS_CONTAINER_METADATA_URI",
    "ECS_CONTAINER_METADATA_URI_V4",
];

#[derive(Debug)]
pub struct RunnerError(pub String);

impl RunnerError {
    pub fn new(message: impl Into<String>) -> Self {
        RunnerError(message.into())
    }
}

impl std::fmt::Display for RunnerError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for RunnerError {}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Checkpoint {
    pub kind: String,
    pub key: String,
}

/// What both runners launch. Secret values are not part of `Debug`.
#[derive(Clone)]
pub struct JobSpec {
    pub name: String,
    pub image: String,
    pub entrypoint: Vec<String>,
    pub command: Vec<String>,
    pub plain_env: BTreeMap<String, String>,
    pub secret_env: BTreeMap<String, Secret>,
    pub cpu_millis: u64,
    pub memory_mib: u64,
    pub network: String,
    pub tenant: String,
    pub git_cache: Option<PathBuf>,
    pub checkpoint: Option<Checkpoint>,
    /// `None` means the job is not killed for running a long time. There is
    /// no default of 15 minutes.
    pub max_runtime_secs: Option<u64>,
}

impl std::fmt::Debug for JobSpec {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("JobSpec")
            .field("name", &self.name)
            .field("image", &self.image)
            .field("entrypoint", &self.entrypoint)
            .field("command", &self.command)
            .field("plain_env", &self.plain_env.keys().collect::<Vec<_>>())
            .field("secret_env", &self.secret_env.keys().collect::<Vec<_>>())
            .field("cpu_millis", &self.cpu_millis)
            .field("memory_mib", &self.memory_mib)
            .field("network", &self.network)
            .field("tenant", &self.tenant)
            .field("git_cache", &self.git_cache)
            .field("checkpoint", &self.checkpoint)
            .field("max_runtime_secs", &self.max_runtime_secs)
            .finish()
    }
}

impl JobSpec {
    pub fn prepared_env(&self) -> Result<BTreeMap<String, String>, RunnerError> {
        let mut env = BTreeMap::new();
        for (key, value) in &self.plain_env {
            env.insert(key.clone(), value.clone());
        }
        for (key, value) in &self.secret_env {
            env.insert(key.clone(), value.expose().to_string());
        }
        for forbidden in FORBIDDEN_ENV {
            env.remove(*forbidden);
        }
        env.insert("AWS_EC2_METADATA_DISABLED".into(), "true".into());
        if env
            .keys()
            .any(|key| key.contains('\n') || key.contains('='))
        {
            return Err(RunnerError::new("environment names must be single line"));
        }
        if env
            .values()
            .any(|value| value.contains('\n') || value.contains('\0'))
        {
            return Err(RunnerError::new("environment values must be single line"));
        }
        Ok(env)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JobHandle {
    pub id: String,
    pub name: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum JobState {
    Running,
    Paused,
    Exited,
    Absent,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JobStatus {
    pub state: JobState,
    pub exit_code: Option<i32>,
    pub detail: String,
}

impl JobStatus {
    fn running() -> Self {
        JobStatus {
            state: JobState::Running,
            exit_code: None,
            detail: "running".into(),
        }
    }

    fn paused() -> Self {
        JobStatus {
            state: JobState::Paused,
            exit_code: None,
            detail: "paused".into(),
        }
    }

    fn exited(code: i32, detail: impl Into<String>) -> Self {
        JobStatus {
            state: JobState::Exited,
            exit_code: Some(code),
            detail: detail.into(),
        }
    }
}

/// The isolation both runners apply. `max_runtime_secs` is copied from the
/// spec and is `None` unless an operator set a limit.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Isolation {
    pub readonly_root: bool,
    pub user: &'static str,
    pub drop_all_capabilities: bool,
    pub no_new_privileges: bool,
    pub metadata_sink: &'static str,
    pub pids_limit: u32,
    pub max_runtime_secs: Option<u64>,
    pub shared_mounts: bool,
}

pub fn isolation(spec: &JobSpec) -> Isolation {
    Isolation {
        readonly_root: true,
        user: RUN_USER,
        drop_all_capabilities: true,
        no_new_privileges: true,
        metadata_sink: METADATA_SINK,
        pids_limit: 256,
        max_runtime_secs: spec.max_runtime_secs,
        shared_mounts: false,
    }
}

fn valid_name(name: &str) -> Result<(), RunnerError> {
    let ok = !name.is_empty()
        && name.len() <= 63
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '.' || c == '-')
        && name
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_alphanumeric());
    if ok {
        Ok(())
    } else {
        Err(RunnerError::new(format!(
            "job name {name:?} is not a container name"
        )))
    }
}

fn cache_mount(spec: &JobSpec) -> Result<Option<String>, RunnerError> {
    let Some(path) = &spec.git_cache else {
        return Ok(None);
    };
    if spec.tenant.is_empty()
        || !path.components().any(|component| match component {
            std::path::Component::Normal(part) => part.to_str() == Some(spec.tenant.as_str()),
            _ => false,
        })
    {
        return Err(RunnerError::new(
            "a git cache mount must live under a directory named for this tenant",
        ));
    }
    Ok(Some(format!(
        "type=bind,src={},dst=/cache,readonly",
        path.display()
    )))
}

#[async_trait]
pub trait JobRunner: Send + Sync {
    async fn start(&self, spec: JobSpec) -> Result<JobHandle, RunnerError>;
    async fn status(&self, id: &str) -> Result<JobStatus, RunnerError>;
    async fn cancel(&self, id: &str) -> Result<JobStatus, RunnerError>;
    async fn pause(&self, id: &str) -> Result<JobStatus, RunnerError>;
    async fn resume_running(&self, id: &str) -> Result<JobStatus, RunnerError>;
    async fn logs(&self, id: &str) -> Result<Vec<String>, RunnerError>;
    async fn stream_logs(&self, id: &str) -> Result<mpsc::Receiver<String>, RunnerError>;
    /// A new container. The previous one is not reused.
    async fn resume_from_checkpoint(&self, spec: JobSpec) -> Result<JobHandle, RunnerError> {
        if spec.checkpoint.is_none() {
            return Err(RunnerError::new("resume requires a checkpoint"));
        }
        self.start(spec).await
    }
}

fn write_env_file(env: &BTreeMap<String, String>) -> Result<PathBuf, RunnerError> {
    static ENV_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let seq = ENV_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let file_path =
        std::env::temp_dir().join(format!("swarm-job-env-{}-{}", std::process::id(), seq));
    let mut body = String::new();
    for (key, value) in env {
        body.push_str(key);
        body.push('=');
        body.push_str(value);
        body.push('\n');
    }
    std::fs::write(&file_path, body).map_err(|error| RunnerError::new(error.to_string()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&file_path, std::fs::Permissions::from_mode(0o600))
            .map_err(|error| RunnerError::new(error.to_string()))?;
    }
    Ok(file_path)
}

struct EnvFile(PathBuf);

impl Drop for EnvFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

/// Local Docker. `bin` is the `docker` CLI (or a test double). `extra_mounts`
/// is empty in production; a test may mount the durable store it is standing
/// in for object storage.
pub struct DockerJobRunner {
    pub bin: PathBuf,
    pub cli_env: BTreeMap<String, String>,
    pub extra_mounts: Vec<String>,
    terminal: std::sync::Mutex<BTreeMap<String, JobStatus>>,
}

impl DockerJobRunner {
    pub fn new(bin: impl Into<PathBuf>) -> Self {
        DockerJobRunner {
            bin: bin.into(),
            cli_env: BTreeMap::new(),
            extra_mounts: Vec::new(),
            terminal: std::sync::Mutex::new(BTreeMap::new()),
        }
    }

    fn run_args(&self, spec: &JobSpec, env_file: &Path) -> Result<Vec<String>, RunnerError> {
        valid_name(&spec.name)?;
        if spec.image.is_empty() {
            return Err(RunnerError::new("the worker image is required"));
        }
        if spec.cpu_millis == 0 || spec.memory_mib == 0 {
            return Err(RunnerError::new("cpu and memory limits are required"));
        }
        let mut args = vec![
            "run".into(),
            "-d".into(),
            "--name".into(),
            spec.name.clone(),
            "--read-only".into(),
            "--user".into(),
            RUN_USER.into(),
            "--cap-drop".into(),
            "ALL".into(),
            "--security-opt".into(),
            "no-new-privileges".into(),
            "--pids-limit".into(),
            PIDS_LIMIT.into(),
            "--memory".into(),
            format!("{}m", spec.memory_mib),
            "--cpus".into(),
            format!("{:.3}", spec.cpu_millis as f64 / 1000.0),
            "--add-host".into(),
            METADATA_SINK.into(),
            "--tmpfs".into(),
            "/tmp:rw,nosuid,nodev,size=256m".into(),
            "--tmpfs".into(),
            "/workspace:rw,nosuid,size=2048m".into(),
            "--tmpfs".into(),
            "/home/swarm:rw,nosuid,nodev,size=512m".into(),
            "--stop-timeout".into(),
            STOP_GRACE_SECS.to_string(),
            "--env-file".into(),
            env_file.display().to_string(),
            "--network".into(),
            spec.network.clone(),
        ];
        if let Some(mount) = cache_mount(spec)? {
            args.push("--mount".into());
            args.push(mount);
        }
        for mount in &self.extra_mounts {
            args.push("--mount".into());
            args.push(mount.clone());
        }
        if let Some(program) = spec.entrypoint.first() {
            args.push("--entrypoint".into());
            args.push(program.clone());
        }
        // A job limit, when an operator set one, is the orchestrator's to
        // enforce. It is intentionally not a Docker `--timeout` (there is no
        // such run flag) and not StopTimeout.
        args.push(spec.image.clone());
        if spec.entrypoint.len() > 1 {
            args.extend(spec.entrypoint.iter().skip(1).cloned());
        }
        args.extend(spec.command.iter().cloned());
        Ok(args)
    }

    async fn exec(&self, args: &[String]) -> Result<std::process::Output, RunnerError> {
        let mut command = Command::new(&self.bin);
        command.args(args).stdin(std::process::Stdio::null());
        for (key, value) in &self.cli_env {
            command.env(key, value);
        }
        command.output().await.map_err(|error| {
            RunnerError::new(format!("could not run {}: {error}", self.bin.display()))
        })
    }

    fn remember(&self, id: &str, status: JobStatus) {
        if let Ok(mut terminal) = self.terminal.lock() {
            terminal.insert(id.to_string(), status);
        }
    }
}

#[async_trait]
impl JobRunner for DockerJobRunner {
    async fn start(&self, spec: JobSpec) -> Result<JobHandle, RunnerError> {
        let env = spec.prepared_env()?;
        let env_file = EnvFile(write_env_file(&env)?);
        let args = self.run_args(&spec, &env_file.0)?;
        let output = self.exec(&args).await?;
        if !output.status.success() {
            let detail = String::from_utf8_lossy(&output.stderr);
            return Err(RunnerError::new(format!(
                "docker run failed: {}",
                detail.trim()
            )));
        }
        let id = String::from_utf8_lossy(&output.stdout).trim().to_string();
        if id.is_empty() {
            return Err(RunnerError::new("docker run did not return a container id"));
        }
        Ok(JobHandle {
            id,
            name: spec.name,
        })
    }

    async fn status(&self, id: &str) -> Result<JobStatus, RunnerError> {
        if let Some(status) = self.terminal.lock().ok().and_then(|t| t.get(id).cloned()) {
            return Ok(status);
        }
        let output = self.exec(&["inspect".into(), id.to_string()]).await?;
        if !output.status.success() {
            return Ok(JobStatus {
                state: JobState::Absent,
                exit_code: None,
                detail: "absent".into(),
            });
        }
        let parsed: Vec<DockerInspect> = serde_json::from_slice(&output.stdout)
            .map_err(|error| RunnerError::new(format!("docker inspect: {error}")))?;
        let Some(inspected) = parsed.into_iter().next() else {
            return Ok(JobStatus {
                state: JobState::Absent,
                exit_code: None,
                detail: "absent".into(),
            });
        };
        let status = match inspected.state.status.as_str() {
            "paused" => JobStatus::paused(),
            "running" | "created" | "restarting" => JobStatus::running(),
            "exited" | "dead" => JobStatus::exited(inspected.state.exit_code, "exited"),
            other => JobStatus {
                state: JobState::Absent,
                exit_code: None,
                detail: other.to_string(),
            },
        };
        if status.state == JobState::Exited {
            let _ = self.exec(&["rm".into(), "-f".into(), id.to_string()]).await;
            self.remember(id, status.clone());
        }
        Ok(status)
    }

    async fn cancel(&self, id: &str) -> Result<JobStatus, RunnerError> {
        let _ = self
            .exec(&[
                "stop".into(),
                "-t".into(),
                STOP_GRACE_SECS.to_string(),
                id.to_string(),
            ])
            .await;
        let status = self.status(id).await?;
        if status.state != JobState::Exited {
            let _ = self.exec(&["rm".into(), "-f".into(), id.to_string()]).await;
            let exited = JobStatus::exited(143, "cancelled");
            self.remember(id, exited.clone());
            return Ok(exited);
        }
        Ok(status)
    }

    async fn pause(&self, id: &str) -> Result<JobStatus, RunnerError> {
        let output = self.exec(&["pause".into(), id.to_string()]).await?;
        if !output.status.success() {
            return Err(RunnerError::new(
                String::from_utf8_lossy(&output.stderr).trim().to_string(),
            ));
        }
        Ok(JobStatus::paused())
    }

    async fn resume_running(&self, id: &str) -> Result<JobStatus, RunnerError> {
        let output = self.exec(&["unpause".into(), id.to_string()]).await?;
        if !output.status.success() {
            return Err(RunnerError::new(
                String::from_utf8_lossy(&output.stderr).trim().to_string(),
            ));
        }
        Ok(JobStatus::running())
    }

    async fn logs(&self, id: &str) -> Result<Vec<String>, RunnerError> {
        let output = self
            .exec(&["logs".into(), "--tail".into(), "200".into(), id.to_string()])
            .await?;
        if !output.status.success() {
            return Err(RunnerError::new(
                String::from_utf8_lossy(&output.stderr).trim().to_string(),
            ));
        }
        let mut lines = String::from_utf8_lossy(&output.stdout)
            .lines()
            .map(str::to_string)
            .collect::<Vec<_>>();
        lines.extend(
            String::from_utf8_lossy(&output.stderr)
                .lines()
                .map(str::to_string),
        );
        lines.retain(|line| !line.is_empty());
        Ok(lines)
    }

    async fn stream_logs(&self, id: &str) -> Result<mpsc::Receiver<String>, RunnerError> {
        let mut command = Command::new(&self.bin);
        command
            .args(["logs", "-f", "--tail", "200", id])
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .stdin(std::process::Stdio::null())
            .kill_on_drop(true);
        for (key, value) in &self.cli_env {
            command.env(key, value);
        }
        let mut child = command
            .spawn()
            .map_err(|error| RunnerError::new(error.to_string()))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| RunnerError::new("docker logs has no stdout"))?;
        let (tx, rx) = mpsc::channel(64);
        tokio::spawn(async move {
            let mut reader = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = reader.next_line().await {
                if tx.send(line).await.is_err() {
                    break;
                }
            }
            let _ = child.kill().await;
        });
        Ok(rx)
    }
}

#[derive(Deserialize)]
struct DockerInspect {
    #[serde(rename = "State")]
    state: DockerInspectState,
}

#[derive(Deserialize)]
struct DockerInspectState {
    #[serde(rename = "Status")]
    status: String,
    #[serde(rename = "ExitCode", default)]
    exit_code: i32,
}

/// ECS Fargate. `endpoint` is the ECS endpoint (AWS, or a fake in tests).
/// Requests are SigV4-signed when access keys are configured. The task has no
/// task role. `PauseTask` is sent as written; a backend that does not
/// implement it (`UnknownOperationException`) is reported as unsupported and
/// the orchestrator resumes from the checkpoint in a new task instead.
pub struct EcsFargateJobRunner {
    endpoint: String,
    region: String,
    cluster: String,
    task_definition: String,
    subnets: Vec<String>,
    security_groups: Vec<String>,
    log_group: Option<String>,
    access_key: Option<Secret>,
    secret_key: Option<Secret>,
    http: reqwest::Client,
    tasks: std::sync::Mutex<BTreeMap<String, String>>,
    terminal: std::sync::Mutex<BTreeMap<String, JobStatus>>,
}

impl EcsFargateJobRunner {
    pub fn new(
        endpoint: impl Into<String>,
        region: impl Into<String>,
        cluster: impl Into<String>,
        task_definition: impl Into<String>,
        subnets: Vec<String>,
        security_groups: Vec<String>,
    ) -> Result<Self, RunnerError> {
        Ok(EcsFargateJobRunner {
            endpoint: endpoint.into().trim_end_matches('/').to_string(),
            region: region.into(),
            cluster: cluster.into(),
            task_definition: task_definition.into(),
            subnets,
            security_groups,
            log_group: None,
            access_key: None,
            secret_key: None,
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(30))
                .build()
                .map_err(|error| RunnerError::new(error.to_string()))?,
            tasks: std::sync::Mutex::new(BTreeMap::new()),
            terminal: std::sync::Mutex::new(BTreeMap::new()),
        })
    }

    pub fn with_log_group(mut self, group: impl Into<String>) -> Self {
        self.log_group = Some(group.into());
        self
    }

    pub fn with_static_credentials(mut self, access_key: Secret, secret_key: Secret) -> Self {
        self.access_key = Some(access_key);
        self.secret_key = Some(secret_key);
        self
    }

    fn run_task_body(
        &self,
        spec: &JobSpec,
        env: &BTreeMap<String, String>,
    ) -> Result<Value, RunnerError> {
        valid_name(&spec.name)?;
        if spec.image.is_empty() {
            return Err(RunnerError::new("the worker image is required"));
        }
        let _ = cache_mount(spec)?;
        let environment: Vec<Value> = env
            .iter()
            .map(|(name, value)| json!({"name": name, "value": value}))
            .collect();
        let mut command = spec.entrypoint.clone();
        command.extend(spec.command.iter().cloned());
        // No taskRoleArn: the task cannot call AWS APIs or read metadata credentials.
        let body = json!({
            "cluster": self.cluster,
            "taskDefinition": self.task_definition,
            "launchType": "FARGATE",
            "count": 1,
            "startedBy": spec.name,
            "enableExecuteCommand": false,
            "platformVersion": "LATEST",
            "overrides": {
                "cpu": spec.cpu_millis.to_string(),
                "memory": spec.memory_mib.to_string(),
                "containerOverrides": [{
                    "name": "worker",
                    "command": command,
                    "environment": environment,
                }]
            },
            "networkConfiguration": {
                "awsvpcConfiguration": {
                    "subnets": self.subnets,
                    "securityGroups": self.security_groups,
                    "assignPublicIp": "DISABLED"
                }
            },
            "tags": [
                {"key": "swarm-image", "value": spec.image},
                {"key": "swarm-entrypoint", "value": spec.entrypoint.join(" ")},
                {"key": "swarm-readonly-root", "value": "true"},
                {"key": "swarm-user", "value": RUN_USER},
                {"key": "swarm-metadata", "value": "blocked"},
                {"key": "swarm-max-runtime", "value": spec.max_runtime_secs.map(|s| s.to_string()).unwrap_or_else(|| "none".into())},
            ]
        });
        Ok(body)
    }

    async fn call(&self, target: &str, body: &Value) -> Result<Value, RunnerError> {
        let payload =
            serde_json::to_vec(body).map_err(|error| RunnerError::new(error.to_string()))?;
        let url = format!("{}/", self.endpoint);
        let parsed =
            reqwest::Url::parse(&url).map_err(|error| RunnerError::new(error.to_string()))?;
        let host = parsed
            .host_str()
            .ok_or_else(|| RunnerError::new("ECS endpoint has no host"))?
            .to_string();
        let host = match parsed.port() {
            Some(port) if !matches!((parsed.scheme(), port), ("https", 443) | ("http", 80)) => {
                format!("{host}:{port}")
            }
            _ => host,
        };
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let amz_date = unix_to_amz(now);
        let mut request = self
            .http
            .post(url)
            .header("content-type", "application/x-amz-json-1.1")
            .header("x-amz-target", target)
            .header("x-amz-date", &amz_date)
            .body(payload.clone());
        if let (Some(access), Some(secret)) = (&self.access_key, &self.secret_key) {
            let authorization = sign_v4(SigV4 {
                host: &host,
                amz_date: &amz_date,
                region: &self.region,
                service: "ecs",
                target,
                payload: &payload,
                access_key: access.expose(),
                secret_key: secret.expose(),
            });
            request = request.header("authorization", authorization);
        }
        let response = request
            .send()
            .await
            .map_err(|error| RunnerError::new(error.to_string()))?;
        let status = response.status();
        let text = response
            .text()
            .await
            .map_err(|error| RunnerError::new(error.to_string()))?;
        if !status.is_success() {
            if text.contains("UnknownOperationException") {
                return Err(RunnerError::new("unsupported"));
            }
            return Err(RunnerError::new(format!("ECS {status}: {text}")));
        }
        serde_json::from_str(&text)
            .map_err(|error| RunnerError::new(format!("ECS response: {error}")))
    }

    fn arn_of(&self, id: &str) -> Result<String, RunnerError> {
        self.tasks
            .lock()
            .ok()
            .and_then(|tasks| tasks.get(id).cloned())
            .or_else(|| id.starts_with("arn:").then(|| id.to_string()))
            .ok_or_else(|| RunnerError::new("unknown task"))
    }
}

#[async_trait]
impl JobRunner for EcsFargateJobRunner {
    async fn start(&self, spec: JobSpec) -> Result<JobHandle, RunnerError> {
        let env = spec.prepared_env()?;
        let body = self.run_task_body(&spec, &env)?;
        let response = self
            .call("AmazonEC2ContainerServiceV20141113.RunTask", &body)
            .await?;
        let arn = response["tasks"][0]["taskArn"]
            .as_str()
            .ok_or_else(|| RunnerError::new("RunTask did not return a task"))?
            .to_string();
        if let Ok(mut tasks) = self.tasks.lock() {
            tasks.insert(spec.name.clone(), arn.clone());
            tasks.insert(arn.clone(), arn.clone());
        }
        Ok(JobHandle {
            id: arn,
            name: spec.name,
        })
    }

    async fn status(&self, id: &str) -> Result<JobStatus, RunnerError> {
        if let Some(status) = self.terminal.lock().ok().and_then(|t| t.get(id).cloned()) {
            return Ok(status);
        }
        let arn = match self.arn_of(id) {
            Ok(arn) => arn,
            Err(_) => {
                return Ok(JobStatus {
                    state: JobState::Absent,
                    exit_code: None,
                    detail: "absent".into(),
                })
            }
        };
        let response = self
            .call(
                "AmazonEC2ContainerServiceV20141113.DescribeTasks",
                &json!({"cluster": self.cluster, "tasks": [arn]}),
            )
            .await?;
        let task = &response["tasks"][0];
        if task.is_null() {
            return Ok(JobStatus {
                state: JobState::Absent,
                exit_code: None,
                detail: "absent".into(),
            });
        }
        let last = task["lastStatus"].as_str().unwrap_or("");
        let status = match last {
            "PAUSED" => JobStatus::paused(),
            "PROVISIONING" | "PENDING" | "ACTIVATING" | "RUNNING" => JobStatus::running(),
            "DEACTIVATING" | "STOPPING" | "DEPROVISIONING" | "STOPPED" => {
                let code = task["containers"][0]["exitCode"].as_i64().unwrap_or(1) as i32;
                JobStatus::exited(code, "exited")
            }
            _ => JobStatus {
                state: JobState::Absent,
                exit_code: None,
                detail: last.to_string(),
            },
        };
        if status.state == JobState::Exited {
            if let Ok(mut terminal) = self.terminal.lock() {
                terminal.insert(id.to_string(), status.clone());
                terminal.insert(arn, status.clone());
            }
        }
        Ok(status)
    }

    async fn cancel(&self, id: &str) -> Result<JobStatus, RunnerError> {
        let arn = self.arn_of(id)?;
        let _ = self
            .call(
                "AmazonEC2ContainerServiceV20141113.StopTask",
                &json!({"cluster": self.cluster, "task": arn, "reason": "swarm-cancel"}),
            )
            .await?;
        let status = JobStatus::exited(143, "cancelled");
        if let Ok(mut terminal) = self.terminal.lock() {
            terminal.insert(id.to_string(), status.clone());
            terminal.insert(arn, status.clone());
        }
        Ok(status)
    }

    async fn pause(&self, id: &str) -> Result<JobStatus, RunnerError> {
        let arn = self.arn_of(id)?;
        match self
            .call(
                "AmazonEC2ContainerServiceV20141113.PauseTask",
                &json!({"cluster": self.cluster, "task": arn}),
            )
            .await
        {
            Ok(_) => Ok(JobStatus::paused()),
            Err(error) if error.0 == "unsupported" => {
                // Fargate has no freezer. Stop the task; the checkpoint is
                // already in the hosted store and resume starts a new one.
                self.cancel(id).await
            }
            Err(error) => Err(error),
        }
    }

    async fn resume_running(&self, id: &str) -> Result<JobStatus, RunnerError> {
        let arn = self.arn_of(id)?;
        self.call(
            "AmazonEC2ContainerServiceV20141113.ResumeTask",
            &json!({"cluster": self.cluster, "task": arn}),
        )
        .await?;
        Ok(JobStatus::running())
    }

    async fn logs(&self, id: &str) -> Result<Vec<String>, RunnerError> {
        let Some(group) = &self.log_group else {
            return Ok(Vec::new());
        };
        let response = self
            .call(
                "Logs_20140328.GetLogEvents",
                &json!({"logGroupName": group, "logStreamName": id}),
            )
            .await?;
        Ok(response["events"]
            .as_array()
            .map(|events| {
                events
                    .iter()
                    .filter_map(|event| event["message"].as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default())
    }

    async fn stream_logs(&self, id: &str) -> Result<mpsc::Receiver<String>, RunnerError> {
        let lines = self.logs(id).await?;
        let (tx, rx) = mpsc::channel(lines.len().max(1));
        for line in lines {
            let _ = tx.send(line).await;
        }
        Ok(rx)
    }
}

fn unix_to_amz(secs: u64) -> String {
    // Enough of a civil conversion for SigV4's YYYYMMDD'T'HHMMSS'Z'.
    let days = (secs / 86_400) as i64;
    let tod = secs % 86_400;
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let day = doy - (153 * mp + 2) / 5 + 1;
    let year = yoe + era * 400 + i64::from(month <= 2);
    let hour = tod / 3600;
    let minute = (tod % 3600) / 60;
    let second = tod % 60;
    format!("{year:04}{month:02}{day:02}T{hour:02}{minute:02}{second:02}Z")
}

type HmacSha256 = Hmac<Sha256>;

fn hmac_sha256(key: &[u8], data: &[u8]) -> [u8; 32] {
    let mut mac = HmacSha256::new_from_slice(key).expect("HMAC accepts any key length");
    mac.update(data);
    let bytes = mac.finalize().into_bytes();
    let mut out = [0u8; 32];
    out.copy_from_slice(&bytes);
    out
}

struct SigV4<'a> {
    host: &'a str,
    amz_date: &'a str,
    region: &'a str,
    service: &'a str,
    target: &'a str,
    payload: &'a [u8],
    access_key: &'a str,
    secret_key: &'a str,
}

fn sign_v4(request: SigV4<'_>) -> String {
    let SigV4 {
        host,
        amz_date,
        region,
        service,
        target,
        payload,
        access_key,
        secret_key,
    } = request;
    let date = &amz_date[..8];
    let payload_hash = hex::encode(Sha256::digest(payload));
    let canonical_headers = format!(
        "content-type:application/x-amz-json-1.1\nhost:{host}\nx-amz-date:{amz_date}\nx-amz-target:{target}\n"
    );
    let signed_headers = "content-type;host;x-amz-date;x-amz-target";
    let canonical = format!("POST\n/\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}");
    let scope = format!("{date}/{region}/{service}/aws4_request");
    let string_to_sign = format!(
        "AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{}",
        hex::encode(Sha256::digest(canonical.as_bytes()))
    );
    let key = hmac_sha256(format!("AWS4{secret_key}").as_bytes(), date.as_bytes());
    let key = hmac_sha256(&key, region.as_bytes());
    let key = hmac_sha256(&key, service.as_bytes());
    let key = hmac_sha256(&key, b"aws4_request");
    let signature = hex::encode(hmac_sha256(&key, string_to_sign.as_bytes()));
    format!(
        "AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    )
}

/// Names the orchestrator puts into every container, beside the provider key
/// and the repository token.
pub fn checkpoint_env(checkpoint: &Checkpoint) -> BTreeMap<String, String> {
    BTreeMap::from([
        ("SWARM_JOB_RESUME".into(), "1".into()),
        ("SWARM_CHECKPOINT_KIND".into(), checkpoint.kind.clone()),
        ("SWARM_CHECKPOINT_KEY".into(), checkpoint.key.clone()),
    ])
}

pub fn forbidden_env_names() -> BTreeSet<&'static str> {
    FORBIDDEN_ENV.iter().copied().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec() -> JobSpec {
        JobSpec {
            name: "swarm-job-1".into(),
            image: "swarm-automation-worker:test".into(),
            entrypoint: WORKER_ENTRYPOINT.iter().map(|s| (*s).to_string()).collect(),
            command: Vec::new(),
            plain_env: BTreeMap::from([("SWARM_TENANT".into(), "t1".into())]),
            secret_env: BTreeMap::from([(
                "GH_TOKEN".into(),
                Secret::new("ghs_canarytokenvalue123456789"),
            )]),
            cpu_millis: 1000,
            memory_mib: 2048,
            network: "swarm-jobs".into(),
            tenant: "t1".into(),
            git_cache: None,
            checkpoint: None,
            max_runtime_secs: None,
        }
    }

    #[test]
    fn debug_and_defaults_do_not_carry_the_token_or_a_fifteen_minute_cap() {
        let spec = spec();
        let rendered = format!("{spec:?}");
        assert!(!rendered.contains("ghs_canarytokenvalue123456789"));
        assert!(!rendered.contains("900"));
        assert_eq!(isolation(&spec).max_runtime_secs, None);
        assert!(isolation(&spec).readonly_root);
        assert!(!isolation(&spec).shared_mounts);
    }

    #[test]
    fn prepared_env_strips_cloud_credentials_and_sinks_metadata() {
        let mut spec = spec();
        spec.plain_env
            .insert("AWS_ACCESS_KEY_ID".into(), "AKIAEXAMPLE".into());
        spec.plain_env.insert(
            "ECS_CONTAINER_METADATA_URI".into(),
            "http://169.254.170.2".into(),
        );
        let env = spec.prepared_env().unwrap();
        assert_eq!(
            env.get("AWS_EC2_METADATA_DISABLED").map(String::as_str),
            Some("true")
        );
        assert!(!env.contains_key("AWS_ACCESS_KEY_ID"));
        assert!(!env.contains_key("ECS_CONTAINER_METADATA_URI"));
        assert!(env.contains_key("GH_TOKEN"));
    }

    #[test]
    fn a_git_cache_must_be_per_tenant_or_absent() {
        let mut spec = spec();
        spec.git_cache = Some(PathBuf::from("/var/cache/swarm/shared"));
        assert!(cache_mount(&spec).is_err());
        spec.git_cache = Some(PathBuf::from("/var/cache/swarm/t1/git"));
        let mount = cache_mount(&spec).unwrap().unwrap();
        assert!(mount.contains("readonly"));
        assert!(mount.contains("/var/cache/swarm/t1/git"));
    }

    #[test]
    fn signatures_name_the_access_key_and_not_the_secret() {
        let header = sign_v4(SigV4 {
            host: "ecs.us-east-1.amazonaws.com",
            amz_date: "20260101T000000Z",
            region: "us-east-1",
            service: "ecs",
            target: "AmazonEC2ContainerServiceV20141113.RunTask",
            payload: b"{}",
            access_key: "AKIAEXAMPLE",
            secret_key: "secret-key-value",
        });
        assert!(header.starts_with("AWS4-HMAC-SHA256 "));
        assert!(header.contains("Credential=AKIAEXAMPLE/20260101/us-east-1/ecs/aws4_request"));
        assert!(!header.contains("secret-key-value"));
        let again = sign_v4(SigV4 {
            host: "ecs.us-east-1.amazonaws.com",
            amz_date: "20260101T000000Z",
            region: "us-east-1",
            service: "ecs",
            target: "AmazonEC2ContainerServiceV20141113.RunTask",
            payload: b"{}",
            access_key: "AKIAEXAMPLE",
            secret_key: "secret-key-value",
        });
        assert_eq!(header, again);
    }
}
