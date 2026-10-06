//! Web scheduler: webhooks and member controls drive one container per repository.
//! Exit 13 and quota pauses resume on a fresh container. Exit 14 holds.

mod common;

use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use axum::body::Body;
use axum::http::{header, Method, Request, StatusCode};
use common::*;
use http_body_util::BodyExt;
use serde_json::{json, Value};
use swarm_web::auth::session_cookie_name;
use swarm_web::clock::Clock;
use swarm_web::model::{Provider, Role, TenantId};
use swarm_web::orchestrator::{
    JobLog, JobSettings, Orchestrator, OrchestratorDeps, RepoTokenMinter, TokenRequest,
};
use swarm_web::runner::{
    Checkpoint, JobHandle, JobRunner, JobSpec, JobState, JobStatus, RunnerError, WORKER_ENTRYPOINT,
};
use swarm_web::secret::Secret;
use swarm_web::store::Store;
use tower::ServiceExt;

const CANARY: &str = "ghs_canarytokenvalue123456789";
const PROVIDER_KEY: &str = "sk-ant-api03-tenant-key-418";
const PEM: &str = "-----BEGIN PRIVATE KEY-----\nnot-used\n-----END PRIVATE KEY-----";
const IMAGE: &str = "swarm-automation-worker";

#[derive(Clone)]
struct Launch {
    image: String,
    entrypoint: Vec<String>,
    checkpoint: Option<Checkpoint>,
    max_runtime_secs: Option<u64>,
    plain: BTreeMap<String, String>,
    secret_names: BTreeSet<String>,
    gh_nonempty: bool,
    provider_key_nonempty: bool,
    name: String,
    id: String,
    tenant: String,
}

struct Rec {
    state: JobState,
    exit_code: Option<i32>,
}

struct Scripted {
    launches: Mutex<Vec<Launch>>,
    jobs: Mutex<BTreeMap<String, Rec>>,
    order: Mutex<Vec<String>>,
    next: AtomicU64,
}

impl Scripted {
    fn new() -> Arc<Self> {
        Arc::new(Scripted {
            launches: Mutex::new(Vec::new()),
            jobs: Mutex::new(BTreeMap::new()),
            order: Mutex::new(Vec::new()),
            next: AtomicU64::new(1),
        })
    }

    fn launches(&self) -> Vec<Launch> {
        self.launches.lock().expect("launches").clone()
    }

    fn finish(&self, code: i32) {
        let order = self.order.lock().expect("order");
        let mut jobs = self.jobs.lock().expect("jobs");
        for id in order.iter().rev() {
            if let Some(rec) = jobs.get_mut(id) {
                if matches!(rec.state, JobState::Running | JobState::Paused) {
                    rec.state = JobState::Exited;
                    rec.exit_code = Some(code);
                    return;
                }
            }
        }
        panic!("no running job to finish with {code}");
    }
}

#[async_trait]
impl JobRunner for Scripted {
    async fn start(&self, spec: JobSpec) -> Result<JobHandle, RunnerError> {
        let id = format!("c{}", self.next.fetch_add(1, Ordering::Relaxed));
        let launch = Launch {
            image: spec.image.clone(),
            entrypoint: spec.entrypoint.clone(),
            checkpoint: spec.checkpoint.clone(),
            max_runtime_secs: spec.max_runtime_secs,
            plain: spec.plain_env.clone(),
            secret_names: spec.secret_env.keys().cloned().collect(),
            gh_nonempty: spec
                .secret_env
                .get("GH_TOKEN")
                .is_some_and(|value| !value.expose().is_empty()),
            provider_key_nonempty: spec
                .secret_env
                .get("ANTHROPIC_API_KEY")
                .is_some_and(|value| !value.expose().is_empty()),
            name: spec.name.clone(),
            id: id.clone(),
            tenant: spec.tenant.clone(),
        };
        let rendered = format!("{launch:?}");
        assert!(!rendered.contains(CANARY), "launch record kept a token");
        assert!(
            !rendered.contains(PROVIDER_KEY),
            "launch record kept a provider key"
        );
        self.launches.lock().expect("launches").push(launch);
        self.jobs.lock().expect("jobs").insert(
            id.clone(),
            Rec {
                state: JobState::Running,
                exit_code: None,
            },
        );
        self.order.lock().expect("order").push(id.clone());
        Ok(JobHandle {
            id,
            name: spec.name,
        })
    }

    async fn status(&self, id: &str) -> Result<JobStatus, RunnerError> {
        let jobs = self.jobs.lock().expect("jobs");
        let rec = jobs
            .get(id)
            .ok_or_else(|| RunnerError::new("unknown task"))?;
        Ok(JobStatus {
            state: rec.state,
            exit_code: rec.exit_code,
            detail: "scripted".into(),
        })
    }

    async fn cancel(&self, id: &str) -> Result<JobStatus, RunnerError> {
        let mut jobs = self.jobs.lock().expect("jobs");
        let rec = jobs
            .get_mut(id)
            .ok_or_else(|| RunnerError::new("unknown task"))?;
        rec.state = JobState::Exited;
        rec.exit_code = Some(143);
        Ok(JobStatus {
            state: JobState::Exited,
            exit_code: Some(143),
            detail: "cancelled".into(),
        })
    }

    async fn pause(&self, id: &str) -> Result<JobStatus, RunnerError> {
        let mut jobs = self.jobs.lock().expect("jobs");
        let rec = jobs
            .get_mut(id)
            .ok_or_else(|| RunnerError::new("unknown task"))?;
        rec.state = JobState::Paused;
        Ok(JobStatus {
            state: JobState::Paused,
            exit_code: None,
            detail: "paused".into(),
        })
    }

    async fn resume_running(&self, id: &str) -> Result<JobStatus, RunnerError> {
        let mut jobs = self.jobs.lock().expect("jobs");
        let rec = jobs
            .get_mut(id)
            .ok_or_else(|| RunnerError::new("unknown task"))?;
        rec.state = JobState::Running;
        Ok(JobStatus {
            state: JobState::Running,
            exit_code: None,
            detail: "running".into(),
        })
    }

    async fn logs(&self, _id: &str) -> Result<Vec<String>, RunnerError> {
        Ok(vec![format!("worker {CANARY}")])
    }

    async fn stream_logs(
        &self,
        _id: &str,
    ) -> Result<tokio::sync::mpsc::Receiver<String>, RunnerError> {
        let (tx, rx) = tokio::sync::mpsc::channel(2);
        let _ = tx.send(format!("worker {CANARY}")).await;
        Ok(rx)
    }
}

impl std::fmt::Debug for Launch {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Launch")
            .field("image", &self.image)
            .field("entrypoint", &self.entrypoint)
            .field("checkpoint", &self.checkpoint)
            .field("max_runtime_secs", &self.max_runtime_secs)
            .field("plain_keys", &self.plain.keys().collect::<Vec<_>>())
            .field("secret_names", &self.secret_names)
            .field("gh_nonempty", &self.gh_nonempty)
            .field("provider_key_nonempty", &self.provider_key_nonempty)
            .field("name", &self.name)
            .field("id", &self.id)
            .field("tenant", &self.tenant)
            .finish()
    }
}

struct FakeMinter {
    repos: Mutex<Vec<String>>,
}

#[async_trait]
impl RepoTokenMinter for FakeMinter {
    async fn mint(&self, request: TokenRequest) -> Result<Secret, RunnerError> {
        if !request.private_key_pem.expose().contains("BEGIN") {
            return Err(RunnerError::new("mint saw no private key"));
        }
        if request.repository.matches('/').count() != 1 {
            return Err(RunnerError::new("token repository must be one owner/name"));
        }
        self.repos.lock().expect("repos").push(request.repository);
        Ok(Secret::new(CANARY))
    }
}

fn users(app: &TestApp) {
    app.github.add_user(
        "code-alice",
        1,
        "alice",
        vec![installation(100, "alice", "User", Role::Owner)],
    );
    app.github.add_user(
        "code-bob",
        2,
        "bob",
        vec![installation(200, "bob", "User", Role::Owner)],
    );
    app.github.add_user(
        "code-carol",
        3,
        "carol",
        vec![installation(100, "alice", "User", Role::Member)],
    );
}

fn attach(app: &TestApp, runner: Arc<Scripted>) -> Arc<Orchestrator> {
    let mut trusted = BTreeSet::new();
    trusted.insert("octocat".to_string());
    let store: Arc<dyn Store> = app.store.clone();
    let clock: Arc<dyn Clock> = app.clock.clone();
    let jobs = Orchestrator::new(OrchestratorDeps {
        runner,
        minter: Arc::new(FakeMinter {
            repos: Mutex::new(Vec::new()),
        }),
        store,
        vault: app.state.vault.clone(),
        accounting: app.state.accounting.clone(),
        clock,
        settings: JobSettings {
            image: IMAGE.into(),
            provider: Provider::Claude,
            cpu_millis: 1000,
            memory_mib: 2048,
            max_runtime_secs: None,
            quota_resume_secs: 60,
            poll_secs: 60,
            network: "bridge".into(),
            app_id: 418,
            private_key: Secret::new(PEM),
            trusted_authors: trusted,
            plain_env: BTreeMap::new(),
            secret_env: BTreeMap::new(),
        },
    });
    app.state.set_orchestrator(jobs.clone());
    jobs
}

fn opened(issue: u64, sender: &str) -> Value {
    json!({
        "action": "opened",
        "installation": {"id": 100},
        "issue": {"number": issue},
        "repository": {"full_name": "acme/demo"},
        "sender": {"login": sender}
    })
}

fn comment(sender: &str, body: &str) -> Value {
    json!({
        "action": "created",
        "installation": {"id": 100},
        "issue": {"number": 418},
        "comment": {"body": body},
        "repository": {"full_name": "acme/demo"},
        "sender": {"login": sender}
    })
}

async fn deliver(app: &TestApp, event: &str, delivery: &str, body: Value) -> Value {
    let text = body.to_string();
    let reply = app.call(signed_webhook(event, delivery, &text)).await;
    assert_eq!(reply.status, StatusCode::OK, "{}", reply.text());
    reply.json()
}

fn work(tenant: &str, issue: u64, action: &str) -> String {
    let base = format!("/api/v1/tenants/{tenant}/work/acme/demo/issues/{issue}");
    if action.is_empty() {
        base
    } else {
        format!("{base}/{action}")
    }
}

fn assert_contract(launch: &Launch, tenant: &str) {
    assert_eq!(launch.image, IMAGE);
    assert_eq!(
        launch.entrypoint,
        WORKER_ENTRYPOINT
            .iter()
            .map(|part| (*part).to_string())
            .collect::<Vec<_>>()
    );
    assert!(launch.max_runtime_secs.is_none());
    assert!(launch.gh_nonempty);
    assert!(launch.provider_key_nonempty);
    assert!(launch.secret_names.contains("GH_TOKEN"));
    assert!(launch.secret_names.contains("ANTHROPIC_API_KEY"));
    assert_eq!(
        launch
            .plain
            .get("SWARM_GITHUB_REPOSITORY")
            .map(String::as_str),
        Some("acme/demo")
    );
    assert_eq!(
        launch.plain.get("SWARM_JOB_REPO_URL").map(String::as_str),
        Some("https://github.com/acme/demo.git")
    );
    assert_eq!(launch.tenant, tenant);
    assert!(!launch.plain.contains_key("GH_TOKEN"));
    assert!(!launch.plain.values().any(|value| value.contains(CANARY)));
}

fn assert_checkpoint(launch: &Launch, kind: &str, key: &str) {
    let checkpoint = launch.checkpoint.as_ref().expect("checkpoint");
    assert_eq!(checkpoint.kind, kind);
    assert_eq!(checkpoint.key, key);
    assert_eq!(
        launch
            .plain
            .get("SWARM_CHECKPOINT_KIND")
            .map(String::as_str),
        Some(kind)
    );
    assert_eq!(
        launch.plain.get("SWARM_CHECKPOINT_KEY").map(String::as_str),
        Some(key)
    );
    assert_eq!(
        launch.plain.get("SWARM_JOB_RESUME").map(String::as_str),
        Some("1")
    );
}

async fn put_key(app: &TestApp, tenant: &TenantId) {
    app.state
        .vault
        .put(tenant, Provider::Claude, &Secret::new(PROVIDER_KEY), "test")
        .await
        .unwrap();
}

#[tokio::test]
async fn an_epoch_yield_relaunches_from_the_checkpoint_and_a_clean_exit_polls_later() {
    let app = TestApp::new();
    users(&app);
    let runner = Scripted::new();
    let jobs = attach(&app, runner.clone());
    let (logs, _guard) = capture_logs(false);
    let alice = app.sign_in("code-alice").await;
    let tenant_s = alice.tenant_for("alice").await;
    let tenant = TenantId::parse(&tenant_s).unwrap();
    put_key(&app, &tenant).await;

    let started = deliver(&app, "issues", "delivery-opened", opened(418, "octocat")).await;
    assert_eq!(started["detail"], "started");
    let first = runner.launches();
    assert_eq!(first.len(), 1);
    assert!(first[0].checkpoint.is_none());
    assert_contract(&first[0], &tenant_s);
    assert_eq!(app.store.active_jobs(&tenant).await.unwrap(), 1);

    runner.finish(13);
    jobs.tick().await.unwrap();
    let second = runner.launches();
    assert_eq!(second.len(), 2);
    assert_ne!(second[0].name, second[1].name);
    assert_checkpoint(&second[1], "in-progress", "current");
    assert_contract(&second[1], &tenant_s);
    assert_eq!(app.store.active_jobs(&tenant).await.unwrap(), 1);

    runner.finish(0);
    jobs.tick().await.unwrap();
    assert_eq!(runner.launches().len(), 2);
    assert_eq!(app.store.active_jobs(&tenant).await.unwrap(), 0);

    app.clock.advance(59);
    jobs.tick().await.unwrap();
    assert_eq!(
        runner.launches().len(),
        2,
        "poll waits for the full interval"
    );
    app.clock.advance(1);
    jobs.tick().await.unwrap();
    let third = runner.launches();
    assert_eq!(third.len(), 3);
    assert!(third[2].checkpoint.is_none());
    assert!(!logs.text().contains(CANARY));
    assert!(!logs.text().contains(PROVIDER_KEY));
    assert!(!logs.text().contains("not-used"));
}

#[tokio::test]
async fn a_quota_pause_resumes_after_the_configured_delay_not_fifteen_minutes() {
    let app = TestApp::new();
    users(&app);
    let runner = Scripted::new();
    let jobs = attach(&app, runner.clone());
    let alice = app.sign_in("code-alice").await;
    let tenant = TenantId::parse(&alice.tenant_for("alice").await).unwrap();
    put_key(&app, &tenant).await;
    deliver(&app, "issues", "delivery-quota", opened(418, "octocat")).await;
    runner.finish(11);
    jobs.tick().await.unwrap();
    assert_eq!(runner.launches().len(), 1);
    assert_eq!(app.store.active_jobs(&tenant).await.unwrap(), 1);
    app.clock.advance(59);
    jobs.tick().await.unwrap();
    assert_eq!(runner.launches().len(), 1);
    app.clock.advance(1);
    jobs.tick().await.unwrap();
    let launches = runner.launches();
    assert_eq!(launches.len(), 2);
    assert_checkpoint(&launches[1], "quota-paused", "418");
    assert_eq!(app.store.active_jobs(&tenant).await.unwrap(), 1);
}

#[tokio::test]
async fn a_hold_stays_down_until_a_new_image_or_a_trusted_follow_up() {
    let app = TestApp::new();
    users(&app);
    let runner = Scripted::new();
    let jobs = attach(&app, runner.clone());
    let alice = app.sign_in("code-alice").await;
    let tenant = TenantId::parse(&alice.tenant_for("alice").await).unwrap();
    put_key(&app, &tenant).await;
    deliver(&app, "issues", "delivery-hold", opened(418, "octocat")).await;
    runner.finish(14);
    jobs.tick().await.unwrap();
    assert_eq!(runner.launches().len(), 1);
    assert_eq!(app.store.active_jobs(&tenant).await.unwrap(), 0);

    let held = deliver(
        &app,
        "issue_comment",
        "delivery-stranger",
        comment("stranger", "go on"),
    )
    .await;
    assert_eq!(held["detail"], "held");
    assert_eq!(runner.launches().len(), 1);

    jobs.set_image(format!("{IMAGE}:next")).await;
    jobs.tick().await.unwrap();
    let relaunched = runner.launches();
    assert_eq!(relaunched.len(), 2);
    assert_eq!(relaunched[1].image, format!("{IMAGE}:next"));
    assert_checkpoint(&relaunched[1], "in-progress", "current");

    runner.finish(14);
    jobs.tick().await.unwrap();
    assert_eq!(runner.launches().len(), 2);
    let trusted = deliver(
        &app,
        "issue_comment",
        "delivery-trusted",
        comment("octocat", "resume this"),
    )
    .await;
    assert_eq!(trusted["detail"], "resumed");
    let resumed = runner.launches();
    assert_eq!(resumed.len(), 3);
    assert_checkpoint(&resumed[2], "in-progress", "current");
}

#[tokio::test]
async fn pause_resume_and_stop_act_on_the_running_container() {
    let app = TestApp::new();
    users(&app);
    let runner = Scripted::new();
    attach(&app, runner.clone());
    let alice = app.sign_in("code-alice").await;
    let tenant_s = alice.tenant_for("alice").await;
    let tenant = TenantId::parse(&tenant_s).unwrap();
    put_key(&app, &tenant).await;

    let first = alice
        .send(Method::POST, &work(&tenant_s, 418, "run"), None)
        .await;
    assert_eq!(first.status, StatusCode::OK, "{}", first.text());
    assert_eq!(first.json()["status"], "running");
    let again = alice
        .send(Method::POST, &work(&tenant_s, 418, "run"), None)
        .await;
    assert_eq!(again.status, StatusCode::OK, "{}", again.text());
    assert_eq!(again.json()["detail"], "running");
    assert_eq!(runner.launches().len(), 1);
    let other = alice
        .send(Method::POST, &work(&tenant_s, 419, "run"), None)
        .await;
    assert_eq!(other.status, StatusCode::CONFLICT);
    assert!(other.text().contains("already has a job"));

    let paused = alice
        .send(Method::POST, &work(&tenant_s, 418, "pause"), None)
        .await;
    assert_eq!(paused.status, StatusCode::OK, "{}", paused.text());
    assert_eq!(paused.json()["status"], "paused");
    let resumed = alice
        .send(Method::POST, &work(&tenant_s, 418, "resume"), None)
        .await;
    assert_eq!(resumed.json()["status"], "running");
    let stopped = alice
        .send(Method::POST, &work(&tenant_s, 418, "stop"), None)
        .await;
    assert_eq!(stopped.json()["status"], "idle");
    assert_eq!(stopped.json()["exit_code"], 143);
    app.state.orchestrator().unwrap().tick().await.unwrap();
    assert_eq!(runner.launches().len(), 1, "stop does not relaunch");
    assert_eq!(app.store.active_jobs(&tenant).await.unwrap(), 0);
}

#[tokio::test]
async fn a_webhook_during_a_run_waits_and_starts_one_follow_up() {
    let app = TestApp::new();
    users(&app);
    let runner = Scripted::new();
    let jobs = attach(&app, runner.clone());
    let alice = app.sign_in("code-alice").await;
    let tenant = TenantId::parse(&alice.tenant_for("alice").await).unwrap();
    put_key(&app, &tenant).await;
    deliver(&app, "issues", "delivery-dirty-1", opened(418, "octocat")).await;
    let dirty = deliver(&app, "issues", "delivery-dirty-2", opened(418, "alice")).await;
    assert_eq!(dirty["detail"], "dirty");
    assert_eq!(runner.launches().len(), 1);
    runner.finish(0);
    jobs.tick().await.unwrap();
    let launches = runner.launches();
    assert_eq!(launches.len(), 2);
    assert!(launches[1].checkpoint.is_none());
    assert_eq!(app.store.active_jobs(&tenant).await.unwrap(), 1);
}

#[tokio::test]
async fn a_missing_key_is_a_recorded_webhook_and_a_conflict_on_run_now() {
    let app = TestApp::new();
    users(&app);
    let runner = Scripted::new();
    attach(&app, runner.clone());
    let alice = app.sign_in("code-alice").await;
    let tenant_s = alice.tenant_for("alice").await;
    let denied = deliver(&app, "issues", "delivery-nokey", opened(418, "octocat")).await;
    assert_eq!(denied["status"], "processed");
    assert_eq!(denied["detail"], "denied_key");
    assert!(runner.launches().is_empty());
    let run = alice
        .send(Method::POST, &work(&tenant_s, 418, "run"), None)
        .await;
    assert_eq!(run.status, StatusCode::CONFLICT);
    assert!(run.text().contains("claude"));
    assert!(!run.text().contains(PROVIDER_KEY));
}

#[tokio::test]
async fn a_member_can_run_a_job_and_another_tenant_cannot_see_it() {
    let app = TestApp::new();
    users(&app);
    let runner = Scripted::new();
    attach(&app, runner.clone());
    let alice = app.sign_in("code-alice").await;
    let carol = app.sign_in("code-carol").await;
    let bob = app.sign_in("code-bob").await;
    let tenant_a = alice.tenant_for("alice").await;
    let tenant_b = bob.tenant_for("bob").await;
    put_key(&app, &TenantId::parse(&tenant_a).unwrap()).await;

    let run = carol
        .send(Method::POST, &work(&tenant_a, 418, "run"), None)
        .await;
    assert_ne!(run.status, StatusCode::FORBIDDEN);
    assert_eq!(run.status, StatusCode::OK, "{}", run.text());
    assert_eq!(runner.launches().len(), 1);

    let crossed = alice
        .send(Method::POST, &work(&tenant_b, 418, "run"), None)
        .await;
    assert_eq!(crossed.status, StatusCode::NOT_FOUND);
    assert_eq!(runner.launches().len(), 1);
}

#[tokio::test]
async fn log_snapshots_and_the_event_stream_redact_the_installation_token() {
    let app = TestApp::new();
    users(&app);
    let runner = Scripted::new();
    let jobs = attach(&app, runner.clone());
    let alice = app.sign_in("code-alice").await;
    let tenant_s = alice.tenant_for("alice").await;
    let tenant = TenantId::parse(&tenant_s).unwrap();
    put_key(&app, &tenant).await;
    alice
        .send(Method::POST, &work(&tenant_s, 418, "run"), None)
        .await;

    let logs = alice.get(&work(&tenant_s, 418, "logs")).await;
    assert_eq!(logs.status, StatusCode::OK, "{}", logs.text());
    assert!(!logs.text().contains(CANARY));
    assert!(logs.text().contains("[REDACTED]"));

    let request = Request::builder()
        .method(Method::GET)
        .uri("/api/v1/events/jobs")
        .header(
            header::COOKIE,
            format!("{}={}", session_cookie_name(app.secure), alice.session),
        )
        .body(Body::empty())
        .unwrap();
    let response = app.router.clone().oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    jobs.publish_line(JobLog {
        tenant: tenant_s,
        repository: "acme/demo".into(),
        issue: 418,
        line: format!("saw {CANARY}"),
    });
    let mut body = response.into_body();
    let mut saw_ready = false;
    let mut saw_log = false;
    for _ in 0..5 {
        let frame = tokio::time::timeout(Duration::from_secs(2), body.frame())
            .await
            .expect("sse frame timed out")
            .expect("sse ended")
            .expect("sse frame");
        let bytes = frame.into_data().unwrap_or_default();
        let text = String::from_utf8_lossy(&bytes);
        if text.contains("ready") {
            saw_ready = true;
        }
        if text.contains("job-log") {
            assert!(!text.contains(CANARY), "{text}");
            assert!(text.contains("[REDACTED]"));
            saw_log = true;
        }
        if saw_ready && saw_log {
            break;
        }
    }
    assert!(saw_ready, "the stream starts with a ready comment");
    assert!(saw_log, "a job-log frame was delivered");
}
