//! Web scheduler. One container per repository at a time, driven by GitHub
//! webhooks, with a poll tick that resumes checkpoint yields and stands in for
//! the desktop cron installer. Exit 13 and quota pauses launch a fresh
//! container from the stored checkpoint. Exit 14 holds until Run now, Resume,
//! a trusted follow-up, or a new image id.

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;
use std::sync::Arc;

use async_trait::async_trait;
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio::sync::{broadcast, Mutex};

use crate::clock::Clock;
use crate::lifecycle::{self, Decision, ParsedEvent};
use crate::model::{Provider, Tenant, TenantId, TenantStatus};
use crate::redact::redact_text;
use crate::runner::{checkpoint_env, JobRunner, JobSpec, JobState, RunnerError, WORKER_ENTRYPOINT};
use crate::secret::Secret;
use crate::store::{Store, StoreError};
use crate::usage::{Accounting, Denied};
use crate::vault::Vault;

const LOG_CAPACITY: usize = 256;

#[derive(Clone, Debug, Serialize)]
pub struct JobLog {
    pub tenant: String,
    pub repository: String,
    pub issue: u64,
    pub line: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct JobView {
    pub status: String,
    pub issue: u64,
    pub exit_code: Option<i32>,
    pub detail: String,
}

pub struct TokenRequest {
    pub app_id: u64,
    pub installation_id: u64,
    pub private_key_pem: Secret,
    pub repository: String,
    pub provider: String,
}

#[async_trait]
pub trait RepoTokenMinter: Send + Sync {
    async fn mint(&self, request: TokenRequest) -> Result<Secret, RunnerError>;
}

/// Shells out to `github_app_auth.py mint-repository-token`. The PEM is stdin,
/// never an argument, and neither stdout nor stderr is logged.
pub struct PythonTokenMinter {
    python: PathBuf,
    script: PathBuf,
}

impl PythonTokenMinter {
    pub fn new(python: impl Into<PathBuf>, script: impl Into<PathBuf>) -> Self {
        PythonTokenMinter {
            python: python.into(),
            script: script.into(),
        }
    }
}

#[async_trait]
impl RepoTokenMinter for PythonTokenMinter {
    async fn mint(&self, request: TokenRequest) -> Result<Secret, RunnerError> {
        let document = serde_json::json!({
            "app_id": request.app_id,
            "installation_id": request.installation_id,
            "private_key_pem": request.private_key_pem.expose(),
            "repository": request.repository,
            "provider": request.provider,
        });
        let body =
            serde_json::to_vec(&document).map_err(|error| RunnerError::new(error.to_string()))?;
        let mut child = tokio::process::Command::new(&self.python)
            .arg("-I")
            .arg(&self.script)
            .arg("mint-repository-token")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|error| RunnerError::new(format!("could not run token mint: {error}")))?;
        if let Some(mut stdin) = child.stdin.take() {
            use tokio::io::AsyncWriteExt;
            stdin.write_all(&body).await.map_err(|error| {
                RunnerError::new(format!("could not write token request: {error}"))
            })?;
        }
        let output = child
            .wait_with_output()
            .await
            .map_err(|error| RunnerError::new(format!("token mint failed: {error}")))?;
        if !output.status.success() {
            let detail = redact_text(&String::from_utf8_lossy(&output.stderr));
            return Err(RunnerError::new(format!(
                "token mint failed: {}",
                detail.chars().take(300).collect::<String>()
            )));
        }
        let parsed: Value = serde_json::from_slice(&output.stdout)
            .map_err(|_| RunnerError::new("token mint did not return JSON"))?;
        let token = parsed["token"]
            .as_str()
            .filter(|token| !token.is_empty())
            .ok_or_else(|| RunnerError::new("token mint returned no token"))?;
        Ok(Secret::new(token))
    }
}

pub struct JobSettings {
    pub image: String,
    pub provider: Provider,
    pub cpu_millis: u64,
    pub memory_mib: u64,
    pub max_runtime_secs: Option<u64>,
    pub quota_resume_secs: u64,
    pub poll_secs: u64,
    pub network: String,
    pub app_id: u64,
    pub private_key: Secret,
    pub trusted_authors: BTreeSet<String>,
    pub plain_env: BTreeMap<String, String>,
    pub secret_env: BTreeMap<String, Secret>,
}

pub struct OrchestratorDeps {
    pub runner: Arc<dyn JobRunner>,
    pub minter: Arc<dyn RepoTokenMinter>,
    pub store: Arc<dyn Store>,
    pub vault: Vault,
    pub accounting: Accounting,
    pub clock: Arc<dyn Clock>,
    pub settings: JobSettings,
}

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
struct RepoKey {
    tenant: TenantId,
    owner: String,
    repo: String,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Phase {
    Idle,
    Starting,
    Running,
    Paused,
    Quota,
    Held,
    Suspended,
}

struct Slot {
    issue: u64,
    job_id: String,
    generation: u64,
    phase: Phase,
    handle: Option<String>,
    dirty: bool,
    checkpoint: Option<crate::runner::Checkpoint>,
    held_image: Option<String>,
    resume_at: Option<u64>,
    next_poll: Option<u64>,
    last_exit: Option<i32>,
    admitted: bool,
}

impl Default for Slot {
    fn default() -> Self {
        Slot {
            issue: 0,
            job_id: String::new(),
            generation: 0,
            phase: Phase::Idle,
            handle: None,
            dirty: false,
            checkpoint: None,
            held_image: None,
            resume_at: None,
            next_poll: None,
            last_exit: None,
            admitted: false,
        }
    }
}

struct LaunchPlan {
    key: RepoKey,
    issue: u64,
    generation: u64,
    checkpoint: Option<crate::runner::Checkpoint>,
    admit: bool,
}

pub struct Orchestrator {
    runner: Arc<dyn JobRunner>,
    minter: Arc<dyn RepoTokenMinter>,
    store: Arc<dyn Store>,
    vault: Vault,
    accounting: Accounting,
    clock: Arc<dyn Clock>,
    settings: JobSettings,
    image: Mutex<String>,
    slots: Mutex<BTreeMap<RepoKey, Slot>>,
    logs: broadcast::Sender<JobLog>,
}

#[derive(Debug)]
pub enum JobError {
    Store(StoreError),
    Runner(String),
    Denied(Denied),
    NotFound,
    Inactive,
    Conflict(String),
}

impl std::fmt::Display for JobError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            JobError::Store(error) => write!(f, "{error}"),
            JobError::Runner(message) | JobError::Conflict(message) => {
                write!(f, "{}", redact_text(message))
            }
            JobError::Denied(denied) => write!(f, "{}", denial_code(denied)),
            JobError::NotFound => write!(f, "not found"),
            JobError::Inactive => write!(f, "tenant_inactive"),
        }
    }
}

impl std::error::Error for JobError {}

impl From<StoreError> for JobError {
    fn from(error: StoreError) -> Self {
        JobError::Store(error)
    }
}

impl From<crate::error::ApiError> for JobError {
    fn from(error: crate::error::ApiError) -> Self {
        match error {
            crate::error::ApiError::NotFound => JobError::NotFound,
            crate::error::ApiError::Conflict(message) => JobError::Conflict(message),
            crate::error::ApiError::Forbidden { .. } => JobError::Inactive,
            _ => JobError::Runner("job setup failed".into()),
        }
    }
}

impl JobError {
    pub fn denial_code(&self) -> Option<&'static str> {
        match self {
            JobError::Denied(denied) => Some(denial_code(denied)),
            JobError::Inactive => Some("tenant_inactive"),
            _ => None,
        }
    }
}

pub fn denial_code(denied: &Denied) -> &'static str {
    match denied {
        Denied::TenantInactive => "tenant_inactive",
        Denied::KeyMissing(_) => "denied_key",
        Denied::ConcurrentJobs { .. } => "denied_concurrency",
        Denied::MonthlySpendCap { .. } | Denied::ProviderBelowMinimum { .. } => "denied_quota",
    }
}

pub fn denial_message(denied: &Denied) -> String {
    match denied {
        Denied::TenantInactive => "This tenant's GitHub App installation is not active.".into(),
        Denied::KeyMissing(provider) => {
            format!(
                "No {} key is configured for this tenant.",
                provider.as_str()
            )
        }
        Denied::ConcurrentJobs { limit } => {
            format!("This tenant is at its limit of {limit} concurrent jobs.")
        }
        Denied::MonthlySpendCap { cap_usd } => {
            format!("This tenant reached its monthly spend cap of ${cap_usd:.2}.")
        }
        Denied::ProviderBelowMinimum {
            provider,
            remaining_percent,
            minimum,
        } => format!(
            "{} has {remaining_percent:.1}% of its budget left, below the {minimum}% minimum.",
            provider.as_str()
        ),
    }
}

impl Orchestrator {
    pub fn new(deps: OrchestratorDeps) -> Arc<Self> {
        let (logs, _) = broadcast::channel(LOG_CAPACITY);
        Arc::new(Orchestrator {
            image: Mutex::new(deps.settings.image.clone()),
            runner: deps.runner,
            minter: deps.minter,
            store: deps.store,
            vault: deps.vault,
            accounting: deps.accounting,
            clock: deps.clock,
            settings: deps.settings,
            slots: Mutex::new(BTreeMap::new()),
            logs,
        })
    }

    pub fn subscribe(&self) -> broadcast::Receiver<JobLog> {
        self.logs.subscribe()
    }

    pub fn publish_line(&self, mut entry: JobLog) {
        entry.line = redact_text(&entry.line);
        let _ = self.logs.send(entry);
    }

    pub async fn set_image(&self, image: String) {
        *self.image.lock().await = image;
    }

    pub async fn handle_delivery(
        &self,
        event: &str,
        payload: &Value,
    ) -> Result<&'static str, JobError> {
        let Some(installation) = payload["installation"]["id"].as_u64() else {
            return Ok("skipped_no_installation");
        };
        let Some(tenant) = self.store.tenant_by_installation(installation).await? else {
            return Ok("skipped_unknown_installation");
        };
        if tenant.status != TenantStatus::Active {
            return Ok("tenant_inactive");
        }
        let Some(parsed) = lifecycle::parse(event, payload, &self.settings.trusted_authors, false)
        else {
            return Ok("skipped_unhandled");
        };
        self.wake(&tenant, &parsed, false).await
    }

    pub async fn run_now(
        &self,
        tenant: &TenantId,
        owner: &str,
        repo: &str,
        issue: u64,
    ) -> Result<JobView, JobError> {
        self.control(tenant, owner, repo, issue, Control::Run).await
    }

    pub async fn pause(
        &self,
        tenant: &TenantId,
        owner: &str,
        repo: &str,
        issue: u64,
    ) -> Result<JobView, JobError> {
        self.control(tenant, owner, repo, issue, Control::Pause)
            .await
    }

    pub async fn resume_job(
        &self,
        tenant: &TenantId,
        owner: &str,
        repo: &str,
        issue: u64,
    ) -> Result<JobView, JobError> {
        self.control(tenant, owner, repo, issue, Control::Resume)
            .await
    }

    pub async fn stop(
        &self,
        tenant: &TenantId,
        owner: &str,
        repo: &str,
        issue: u64,
    ) -> Result<JobView, JobError> {
        self.control(tenant, owner, repo, issue, Control::Stop)
            .await
    }

    pub async fn status(
        &self,
        tenant: &TenantId,
        owner: &str,
        repo: &str,
        issue: u64,
    ) -> Result<JobView, JobError> {
        self.control(tenant, owner, repo, issue, Control::Status)
            .await
    }

    pub async fn logs(
        &self,
        tenant: &TenantId,
        owner: &str,
        repo: &str,
        issue: u64,
    ) -> Result<Vec<String>, JobError> {
        let slot = self.slot_for(tenant, owner, repo, issue).await?;
        let Some(handle) = slot.handle else {
            return Ok(Vec::new());
        };
        let lines = self
            .runner
            .logs(&handle)
            .await
            .map_err(|error| JobError::Runner(error.to_string()))?;
        Ok(lines.iter().map(|line| redact_text(line)).collect())
    }

    /// Supervise running containers and resume checkpoints whose delay has passed.
    /// This is the web replacement for the desktop cron installer.
    pub async fn tick(&self) -> Result<(), JobError> {
        self.observe().await?;
        self.resume_due().await?;
        Ok(())
    }

    async fn wake(
        &self,
        tenant: &Tenant,
        parsed: &ParsedEvent,
        force: bool,
    ) -> Result<&'static str, JobError> {
        if !force {
            if let Decision::Skip(reason) = parsed.decision {
                return Ok(reason);
            }
        }
        let key = repo_key(&tenant.id, &parsed.owner, &parsed.repo);
        let plan = {
            let mut slots = self.slots.lock().await;
            let slot = slots.entry(key.clone()).or_default();
            match slot.phase {
                Phase::Running | Phase::Paused | Phase::Starting if !force => {
                    slot.dirty = true;
                    return Ok("dirty");
                }
                Phase::Running | Phase::Paused if force && slot.issue == parsed.issue => {
                    return Ok("running");
                }
                Phase::Running | Phase::Paused | Phase::Starting if force => {
                    return Err(JobError::Conflict(
                        "This repository already has a job.".into(),
                    ));
                }
                Phase::Held | Phase::Suspended if !force && !parsed.trusted => {
                    return Ok("held");
                }
                Phase::Quota if !force => {
                    slot.dirty = true;
                    return Ok("quota");
                }
                _ => {}
            }
            let checkpoint = match slot.phase {
                Phase::Held | Phase::Suspended | Phase::Quota => slot.checkpoint.clone(),
                _ => None,
            };
            let admit = !slot.admitted;
            slot.generation = slot.generation.saturating_add(1);
            slot.issue = parsed.issue;
            slot.job_id = job_id(&tenant.id, &parsed.owner, &parsed.repo);
            slot.phase = Phase::Starting;
            slot.dirty = false;
            slot.handle = None;
            LaunchPlan {
                key,
                issue: parsed.issue,
                generation: slot.generation,
                checkpoint,
                admit,
            }
        };
        self.launch(tenant, plan).await
    }

    async fn launch(&self, tenant: &Tenant, plan: LaunchPlan) -> Result<&'static str, JobError> {
        if plan.admit {
            match self
                .accounting
                .admit(
                    &tenant.id,
                    &self.job_id_of(&plan.key),
                    self.settings.provider,
                )
                .await
            {
                Ok(Ok(())) => {
                    self.mark_admitted(&plan.key, true).await;
                }
                Ok(Err(denied)) => {
                    self.park_idle(&plan.key).await;
                    return Err(JobError::Denied(denied));
                }
                Err(error) => {
                    self.park_idle(&plan.key).await;
                    return Err(error.into());
                }
            }
        }
        match self.start_container(tenant, &plan).await {
            Ok(detail) => Ok(detail),
            Err(error) => {
                self.backoff(&plan.key, plan.checkpoint.clone()).await;
                Err(error)
            }
        }
    }

    async fn start_container(
        &self,
        tenant: &Tenant,
        plan: &LaunchPlan,
    ) -> Result<&'static str, JobError> {
        let repository = format!("{}/{}", plan.key.owner, plan.key.repo);
        let token = self
            .minter
            .mint(TokenRequest {
                app_id: self.settings.app_id,
                installation_id: tenant.installation_id,
                private_key_pem: self.settings.private_key.clone(),
                repository: repository.clone(),
                provider: self.settings.provider.as_str().to_string(),
            })
            .await
            .map_err(|error| JobError::Runner(error.to_string()))?;
        let environment = self
            .vault
            .job_environment(&tenant.id, self.settings.provider, false)
            .await?;
        let mut secret_env = self.settings.secret_env.clone();
        secret_env.insert("GH_TOKEN".into(), token);
        for (name, value) in environment.expose() {
            secret_env.insert(name.to_string(), Secret::new(value));
        }
        let mut plain_env = self.settings.plain_env.clone();
        plain_env.insert("SWARM_TENANT".into(), tenant.id.to_string());
        plain_env.insert("SWARM_GITHUB_REPOSITORY".into(), repository.clone());
        plain_env.insert(
            "SWARM_JOB_REPO_URL".into(),
            format!("https://github.com/{repository}.git"),
        );
        plain_env.insert("SWARM_WEB_WAKE_ISSUE".into(), plan.issue.to_string());
        if !plain_env.contains_key("SWARM_JOB_STORAGE")
            && plain_env
                .keys()
                .any(|key| key.starts_with("SWARM_STORAGE_"))
        {
            plain_env.insert("SWARM_JOB_STORAGE".into(), "hosted".into());
        }
        if let Some(checkpoint) = &plan.checkpoint {
            plain_env.extend(checkpoint_env(checkpoint));
        }
        let image = self.image.lock().await.clone();
        let name = container_name(plan.generation, &tenant.id, &plan.key.owner, &plan.key.repo);
        let spec = JobSpec {
            name,
            image,
            entrypoint: WORKER_ENTRYPOINT
                .iter()
                .map(|part| (*part).to_string())
                .collect(),
            command: Vec::new(),
            plain_env,
            secret_env,
            cpu_millis: self.settings.cpu_millis,
            memory_mib: self.settings.memory_mib,
            network: self.settings.network.clone(),
            tenant: tenant.id.to_string(),
            git_cache: None,
            checkpoint: plan.checkpoint.clone(),
            max_runtime_secs: self.settings.max_runtime_secs,
        };
        let handle = self
            .runner
            .start(spec)
            .await
            .map_err(|error| JobError::Runner(error.to_string()))?;
        {
            let mut slots = self.slots.lock().await;
            if let Some(slot) = slots.get_mut(&plan.key) {
                slot.phase = Phase::Running;
                slot.handle = Some(handle.id.clone());
                slot.held_image = None;
                slot.resume_at = None;
            }
        }
        self.follow(handle.id, tenant.id.to_string(), repository, plan.issue);
        tracing::info!(
            tenant = %tenant.id,
            repository = %format!("{}/{}", plan.key.owner, plan.key.repo),
            issue = plan.issue,
            "job container started"
        );
        Ok(if plan.checkpoint.is_some() {
            "resumed"
        } else {
            "started"
        })
    }

    fn follow(&self, id: String, tenant: String, repository: String, issue: u64) {
        let runner = self.runner.clone();
        let logs = self.logs.clone();
        tokio::spawn(async move {
            let Ok(mut lines) = runner.stream_logs(&id).await else {
                return;
            };
            while let Some(line) = lines.recv().await {
                let _ = logs.send(JobLog {
                    tenant: tenant.clone(),
                    repository: repository.clone(),
                    issue,
                    line: redact_text(&line),
                });
            }
        });
    }

    async fn observe(&self) -> Result<(), JobError> {
        let running: Vec<(RepoKey, String)> = {
            let slots = self.slots.lock().await;
            slots
                .iter()
                .filter_map(|(key, slot)| {
                    if matches!(slot.phase, Phase::Running | Phase::Paused) {
                        slot.handle.clone().map(|id| (key.clone(), id))
                    } else {
                        None
                    }
                })
                .collect()
        };
        for (key, id) in running {
            let status = self
                .runner
                .status(&id)
                .await
                .map_err(|error| JobError::Runner(error.to_string()))?;
            if status.state != JobState::Exited {
                continue;
            }
            self.on_exit(&key, status.exit_code.unwrap_or(1)).await?;
        }
        Ok(())
    }

    async fn on_exit(&self, key: &RepoKey, code: i32) -> Result<(), JobError> {
        let now = self.clock.now_secs();
        let image = self.image.lock().await.clone();
        let mut follow_up: Option<LaunchPlan> = None;
        let mut release = false;
        {
            let mut slots = self.slots.lock().await;
            let Some(slot) = slots.get_mut(key) else {
                return Ok(());
            };
            if !matches!(slot.phase, Phase::Running | Phase::Paused) {
                return Ok(());
            }
            slot.last_exit = Some(code);
            slot.handle = None;
            tracing::info!(
                tenant = %key.tenant,
                repository = %format!("{}/{}", key.owner, key.repo),
                issue = slot.issue,
                exit_code = code,
                "job container exited"
            );
            match code {
                0 | 10 => {
                    let dirty = slot.dirty;
                    slot.dirty = false;
                    release = true;
                    slot.admitted = false;
                    slot.phase = Phase::Idle;
                    if dirty {
                        follow_up = Some(claim(slot, key, None, true));
                        slot.phase = Phase::Starting;
                    } else {
                        slot.next_poll = Some(now.saturating_add(self.settings.poll_secs.max(1)));
                    }
                }
                11 => {
                    slot.phase = Phase::Quota;
                    slot.resume_at =
                        Some(now.saturating_add(self.settings.quota_resume_secs.max(1)));
                    slot.checkpoint = Some(checkpoint_for(slot.issue, "quota-paused"));
                }
                13 => {
                    let checkpoint = checkpoint_for(slot.issue, "in-progress");
                    slot.checkpoint = Some(checkpoint.clone());
                    follow_up = Some(claim(slot, key, Some(checkpoint), false));
                    slot.phase = Phase::Starting;
                }
                14 => {
                    release = true;
                    slot.admitted = false;
                    slot.phase = Phase::Held;
                    slot.held_image = Some(image);
                    slot.checkpoint = Some(checkpoint_for(slot.issue, "in-progress"));
                    slot.next_poll = None;
                }
                130 | 137 | 143 => {
                    release = true;
                    slot.admitted = false;
                    slot.phase = Phase::Idle;
                    slot.checkpoint = None;
                    slot.next_poll = None;
                }
                _ => {
                    release = true;
                    slot.admitted = false;
                    slot.phase = Phase::Idle;
                    slot.next_poll = None;
                }
            }
        }
        if release {
            let job = job_id(&key.tenant, &key.owner, &key.repo);
            self.accounting.release(&key.tenant, &job).await?;
        }
        if let Some(plan) = follow_up {
            let Some(tenant) = self.store.tenant(&key.tenant).await? else {
                return Ok(());
            };
            if let Err(error) = self.launch(&tenant, plan).await {
                tracing::warn!(error = %error, "job relaunch failed");
            }
        }
        Ok(())
    }

    async fn resume_due(&self) -> Result<(), JobError> {
        let now = self.clock.now_secs();
        let image = self.image.lock().await.clone();
        let due: Vec<LaunchPlan> = {
            let mut slots = self.slots.lock().await;
            let mut due = Vec::new();
            for (key, slot) in slots.iter_mut() {
                let resume_quota =
                    slot.phase == Phase::Quota && slot.resume_at.is_some_and(|at| at <= now);
                let image_changed =
                    slot.phase == Phase::Held && slot.held_image.as_deref() != Some(image.as_str());
                let poll = slot.phase == Phase::Idle
                    && slot.next_poll.is_some_and(|at| at <= now)
                    && slot.issue > 0;
                if !(resume_quota || image_changed || poll) {
                    continue;
                }
                let checkpoint = if poll {
                    None
                } else {
                    slot.checkpoint
                        .clone()
                        .or_else(|| Some(checkpoint_for(slot.issue, "in-progress")))
                };
                let admit = !slot.admitted;
                slot.generation = slot.generation.saturating_add(1);
                slot.phase = Phase::Starting;
                slot.next_poll = None;
                slot.resume_at = None;
                due.push(LaunchPlan {
                    key: key.clone(),
                    issue: slot.issue,
                    generation: slot.generation,
                    checkpoint,
                    admit,
                });
            }
            due
        };
        for plan in due {
            let Some(tenant) = self.store.tenant(&plan.key.tenant).await? else {
                continue;
            };
            if let Err(error) = self.launch(&tenant, plan).await {
                tracing::warn!(error = %error, "scheduled job did not start");
            }
        }
        Ok(())
    }

    async fn control(
        &self,
        tenant_id: &TenantId,
        owner: &str,
        repo: &str,
        issue: u64,
        action: Control,
    ) -> Result<JobView, JobError> {
        if !lifecycle::github_name(owner) || !lifecycle::github_name(repo) || issue == 0 {
            return Err(JobError::NotFound);
        }
        let Some(tenant) = self.store.tenant(tenant_id).await? else {
            return Err(JobError::NotFound);
        };
        if tenant.status != TenantStatus::Active {
            return Err(JobError::Inactive);
        }
        let key = repo_key(tenant_id, owner, repo);
        match action {
            Control::Status => Ok(self.view_for(&key, issue).await),
            Control::Run => {
                let parsed = ParsedEvent {
                    owner: owner.to_string(),
                    repo: repo.to_string(),
                    issue,
                    sender: String::new(),
                    trusted: true,
                    decision: Decision::Start,
                };
                let detail = self.wake(&tenant, &parsed, true).await?;
                let mut view = self.view_for(&key, issue).await;
                if view.detail.is_empty() {
                    view.detail = detail.to_string();
                }
                Ok(view)
            }
            Control::Pause => self.pause_slot(&key, issue).await,
            Control::Resume => self.resume_slot(&tenant, &key, issue).await,
            Control::Stop => self.stop_slot(&key, issue).await,
        }
    }

    async fn pause_slot(&self, key: &RepoKey, issue: u64) -> Result<JobView, JobError> {
        let handle = {
            let slots = self.slots.lock().await;
            let slot = slots.get(key).ok_or(JobError::NotFound)?;
            if slot.issue != issue || !matches!(slot.phase, Phase::Running) {
                return Err(JobError::Conflict("That issue has no running job.".into()));
            }
            slot.handle.clone().ok_or(JobError::NotFound)?
        };
        let status = self
            .runner
            .pause(&handle)
            .await
            .map_err(|error| JobError::Runner(error.to_string()))?;
        let mut slots = self.slots.lock().await;
        let Some(slot) = slots.get_mut(key) else {
            return Err(JobError::NotFound);
        };
        match status.state {
            JobState::Paused => {
                slot.phase = Phase::Paused;
                Ok(view_of(slot, "paused"))
            }
            JobState::Exited => {
                slot.phase = Phase::Suspended;
                slot.handle = None;
                slot.checkpoint = Some(checkpoint_for(slot.issue, "in-progress"));
                slot.last_exit = status.exit_code;
                Ok(view_of(slot, "paused"))
            }
            _ => Err(JobError::Runner("pause did not stop the container".into())),
        }
    }

    async fn resume_slot(
        &self,
        tenant: &Tenant,
        key: &RepoKey,
        issue: u64,
    ) -> Result<JobView, JobError> {
        let handle = {
            let slots = self.slots.lock().await;
            let slot = slots.get(key).ok_or(JobError::NotFound)?;
            if slot.issue != issue {
                return Err(JobError::NotFound);
            }
            match slot.phase {
                Phase::Paused => slot.handle.clone(),
                Phase::Held | Phase::Suspended | Phase::Quota => None,
                _ => return Err(JobError::Conflict("That issue is not paused.".into())),
            }
        };
        if let Some(handle) = handle {
            self.runner
                .resume_running(&handle)
                .await
                .map_err(|error| JobError::Runner(error.to_string()))?;
            let mut slots = self.slots.lock().await;
            if let Some(slot) = slots.get_mut(key) {
                slot.phase = Phase::Running;
                return Ok(view_of(slot, "running"));
            }
            return Err(JobError::NotFound);
        }
        let plan = {
            let mut slots = self.slots.lock().await;
            let slot = slots.get_mut(key).ok_or(JobError::NotFound)?;
            let checkpoint = slot
                .checkpoint
                .clone()
                .or_else(|| Some(checkpoint_for(issue, "in-progress")));
            let admit = !slot.admitted;
            slot.generation = slot.generation.saturating_add(1);
            slot.phase = Phase::Starting;
            LaunchPlan {
                key: key.clone(),
                issue,
                generation: slot.generation,
                checkpoint,
                admit,
            }
        };
        self.launch(tenant, plan).await?;
        Ok(self.view_for(key, issue).await)
    }

    async fn stop_slot(&self, key: &RepoKey, issue: u64) -> Result<JobView, JobError> {
        let handle = {
            let mut slots = self.slots.lock().await;
            let slot = slots.get_mut(key).ok_or(JobError::NotFound)?;
            if slot.issue != issue {
                return Err(JobError::NotFound);
            }
            let handle = slot.handle.clone();
            slot.phase = Phase::Idle;
            slot.handle = None;
            slot.dirty = false;
            slot.next_poll = None;
            slot.resume_at = None;
            slot.checkpoint = None;
            slot.admitted = false;
            slot.last_exit = Some(143);
            handle
        };
        if let Some(handle) = handle {
            let _ = self.runner.cancel(&handle).await;
        }
        let job = job_id(&key.tenant, &key.owner, &key.repo);
        self.accounting.release(&key.tenant, &job).await?;
        Ok(JobView {
            status: "idle".into(),
            issue,
            exit_code: Some(143),
            detail: "stopped".into(),
        })
    }

    async fn view_for(&self, key: &RepoKey, issue: u64) -> JobView {
        let slots = self.slots.lock().await;
        match slots.get(key) {
            Some(slot) if slot.issue == issue => view_of(slot, ""),
            _ => JobView {
                status: "idle".into(),
                issue,
                exit_code: None,
                detail: "idle".into(),
            },
        }
    }

    async fn slot_for(
        &self,
        tenant: &TenantId,
        owner: &str,
        repo: &str,
        issue: u64,
    ) -> Result<SlotSnapshot, JobError> {
        if !lifecycle::github_name(owner) || !lifecycle::github_name(repo) {
            return Err(JobError::NotFound);
        }
        let key = repo_key(tenant, owner, repo);
        let slots = self.slots.lock().await;
        let Some(slot) = slots.get(&key) else {
            return Ok(SlotSnapshot { handle: None });
        };
        if slot.issue != issue {
            return Ok(SlotSnapshot { handle: None });
        }
        Ok(SlotSnapshot {
            handle: slot.handle.clone(),
        })
    }

    fn job_id_of(&self, key: &RepoKey) -> String {
        job_id(&key.tenant, &key.owner, &key.repo)
    }

    async fn mark_admitted(&self, key: &RepoKey, admitted: bool) {
        if let Some(slot) = self.slots.lock().await.get_mut(key) {
            slot.admitted = admitted;
        }
    }

    async fn park_idle(&self, key: &RepoKey) {
        if let Some(slot) = self.slots.lock().await.get_mut(key) {
            if slot.phase == Phase::Starting {
                slot.phase = Phase::Idle;
            }
        }
    }

    async fn backoff(&self, key: &RepoKey, checkpoint: Option<crate::runner::Checkpoint>) {
        let now = self.clock.now_secs();
        if let Some(slot) = self.slots.lock().await.get_mut(key) {
            if slot.phase == Phase::Starting {
                slot.phase = Phase::Quota;
                slot.resume_at = Some(now.saturating_add(self.settings.poll_secs.max(1)));
                if checkpoint.is_some() {
                    slot.checkpoint = checkpoint;
                }
            }
        }
    }
}

struct SlotSnapshot {
    handle: Option<String>,
}

enum Control {
    Status,
    Run,
    Pause,
    Resume,
    Stop,
}

fn claim(
    slot: &mut Slot,
    key: &RepoKey,
    checkpoint: Option<crate::runner::Checkpoint>,
    admit: bool,
) -> LaunchPlan {
    slot.generation = slot.generation.saturating_add(1);
    slot.admitted = !admit && slot.admitted;
    LaunchPlan {
        key: key.clone(),
        issue: slot.issue,
        generation: slot.generation,
        checkpoint,
        admit,
    }
}

fn view_of(slot: &Slot, override_detail: &str) -> JobView {
    let (status, detail) = match slot.phase {
        Phase::Idle => ("idle", "idle"),
        Phase::Starting | Phase::Running => ("running", "running"),
        Phase::Paused | Phase::Suspended => ("paused", "paused"),
        Phase::Quota => ("quota", "quota"),
        Phase::Held => ("held", "held"),
    };
    JobView {
        status: status.into(),
        issue: slot.issue,
        exit_code: slot.last_exit,
        detail: if override_detail.is_empty() {
            detail.into()
        } else {
            override_detail.into()
        },
    }
}

fn repo_key(tenant: &TenantId, owner: &str, repo: &str) -> RepoKey {
    RepoKey {
        tenant: tenant.clone(),
        owner: owner.to_string(),
        repo: repo.to_string(),
    }
}

fn job_id(tenant: &TenantId, owner: &str, repo: &str) -> String {
    format!("{tenant}:{owner}/{repo}")
}

fn checkpoint_for(issue: u64, kind: &str) -> crate::runner::Checkpoint {
    let key = if kind == "quota-paused" {
        issue.to_string()
    } else {
        "current".into()
    };
    crate::runner::Checkpoint {
        kind: kind.to_string(),
        key,
    }
}

fn container_name(generation: u64, tenant: &TenantId, owner: &str, repo: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(tenant.as_str().as_bytes());
    hasher.update(b"/");
    hasher.update(owner.as_bytes());
    hasher.update(b"/");
    hasher.update(repo.as_bytes());
    let digest = hex::encode(hasher.finalize());
    format!("s{generation}-{}", &digest[..12])
}
