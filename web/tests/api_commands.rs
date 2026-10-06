//! The REST endpoints for the desktop's commands (issue #419): tenancy, roles,
//! CSRF, settings, the worker bridge and the native endpoints, all through the
//! real router.

mod common;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use axum::body::Body;
use axum::http::{header, Method, Request, StatusCode};
use common::*;
use serde_json::{json, Value};
use swarm_web::auth::session_cookie_name;
use swarm_web::bridge::{Bridge, BridgeError, BridgeRequest};
use swarm_web::catalog::{self, Access, Handler, Native, Scope};
use swarm_web::model::Role;

const CANARY: &str = "sk-ant-api03-CONFIG-CANARY-0123456789";

#[derive(Default)]
struct FakeBridge {
    calls: Mutex<Vec<(String, String, Value)>>,
    failures: Mutex<HashMap<&'static str, &'static str>>,
}

impl FakeBridge {
    fn calls(&self) -> Vec<(String, String, Value)> {
        self.calls.lock().unwrap().clone()
    }

    fn fail(&self, op: &'static str, kind: &'static str) {
        self.failures.lock().unwrap().insert(op, kind);
    }
}

#[async_trait]
impl Bridge for FakeBridge {
    async fn call(&self, request: BridgeRequest) -> Result<Value, BridgeError> {
        self.calls.lock().unwrap().push((
            request.tenant.to_string(),
            request.op.to_string(),
            request.args.clone(),
        ));
        match self.failures.lock().unwrap().get(request.op).copied() {
            Some("unavailable") => Err(BridgeError::NotAvailable("Not hosted yet.".into())),
            Some("bad") => Err(BridgeError::BadRequest(
                "offset must be a whole number.".into(),
            )),
            Some("failed") => Err(BridgeError::Failed(format!("boom {CANARY}"))),
            _ => Ok(json!({ "op": request.op, "tenant": request.tenant.as_str() })),
        }
    }
}

struct World {
    app: TestApp,
    bridge: Arc<FakeBridge>,
}

impl World {
    fn new() -> World {
        let app = TestApp::new();
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
        let bridge = Arc::new(FakeBridge::default());
        app.state.set_bridge(bridge.clone());
        World { app, bridge }
    }
}

fn concrete(path: &str, tenant: &str) -> String {
    path.replace("{tenant}", tenant)
        .replace("{repoId}", "acme__demo")
        .replace("{process}", "issue")
}

fn config() -> Value {
    json!({
        "minimum_remaining_percent": 20,
        "workspace_root": "/Users/alice/work",
        "python_bin": "/usr/bin/python3",
        "providers": [
            { "id": "claude", "enabled": true, "bin": "/opt/claude", "model": "m" },
            { "id": "codex", "enabled": false }
        ],
        "model_data_api_key": CANARY,
        "repositories": [{
            "id": "acme__demo",
            "github_repository": "acme/demo",
            "repo_dir": "/Users/alice/demo",
            "adversarial_uat_enabled": true,
            "github_token": CANARY,
            "notes": "state: draft",
            "nested": { "client_secret": CANARY, "keep": 1 }
        }]
    })
}

fn tenant_routes() -> impl Iterator<Item = &'static catalog::Route> {
    catalog::ROUTES.iter().filter(|r| r.scope == Scope::Tenant)
}

fn is_hidden(status: StatusCode) -> bool {
    status == StatusCode::NOT_FOUND
}

async fn request(
    app: &TestApp,
    method: &str,
    path: &str,
    cookie: Option<&str>,
    csrf: Option<&str>,
) -> Reply {
    let mut builder = Request::builder().method(method).uri(path);
    if let Some(session) = cookie {
        builder = builder.header(
            header::COOKIE,
            format!("{}={session}", session_cookie_name(app.secure)),
        );
    }
    if let Some(token) = csrf {
        builder = builder
            .header(header::ORIGIN, app.state.config.origin())
            .header("x-csrf-token", token);
    }
    app.call(
        builder
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::empty())
            .unwrap(),
    )
    .await
}

fn method(route: &catalog::Route) -> Method {
    Method::from_bytes(route.method.as_bytes()).unwrap()
}

#[tokio::test]
async fn every_tenant_route_is_mounted_and_needs_a_session() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let tenant = alice.tenant_for("alice").await;
    for route in tenant_routes() {
        let path = concrete(
            &format!("/api/v1/tenants/{{tenant}}{}", route.path),
            &tenant,
        );
        let reply = request(&w.app, route.method, &path, None, None).await;
        assert_eq!(
            reply.status,
            StatusCode::UNAUTHORIZED,
            "{} {path}: {}",
            route.method,
            reply.text()
        );
    }
    let version = w.app.get("/api/v1/version").await;
    assert_eq!(version.status, StatusCode::OK);
    assert!(version.json().is_string());
}

#[tokio::test]
async fn another_tenants_member_gets_404_on_every_route() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let bob = w.app.sign_in("code-bob").await;
    let tenant_a = alice.tenant_for("alice").await;
    alice
        .put(
            &format!("/api/v1/tenants/{tenant_a}/config"),
            json!({ "config": config() }),
        )
        .await;
    for route in tenant_routes() {
        let path = concrete(
            &format!("/api/v1/tenants/{{tenant}}{}", route.path),
            &tenant_a,
        );
        let reply = bob.send(method(route), &path, Some(json!({}))).await;
        assert!(
            is_hidden(reply.status),
            "bob reached {} {path}: {} {}",
            route.method,
            reply.status,
            reply.text()
        );
        assert_eq!(reply.json()["code"], "not_found");
    }
    assert!(
        w.bridge.calls().is_empty(),
        "no bridge call was made for a foreign tenant"
    );
}

#[tokio::test]
async fn owner_routes_refuse_members_and_every_mutation_needs_csrf() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let carol = w.app.sign_in("code-carol").await;
    let tenant = alice.tenant_for("alice").await;
    alice
        .put(
            &format!("/api/v1/tenants/{tenant}/config"),
            json!({ "config": config() }),
        )
        .await;
    for route in tenant_routes() {
        let path = concrete(
            &format!("/api/v1/tenants/{{tenant}}{}", route.path),
            &tenant,
        );
        // The account routes parse their body before the handler checks the role.
        let body = if path.ends_with("/provider-keys/model-data") {
            json!({ "key": "aa-model-data-key-0123456789" })
        } else {
            json!({})
        };
        let as_member = carol.send(method(route), &path, Some(body)).await;
        if route.access == Access::Owner {
            assert_eq!(
                as_member.status,
                StatusCode::FORBIDDEN,
                "{} {path}",
                route.method
            );
            assert_eq!(
                as_member.json()["code"],
                "owner_required",
                "{} {path}",
                route.method
            );
        } else {
            assert!(
                !matches!(
                    as_member.status,
                    StatusCode::FORBIDDEN | StatusCode::NOT_FOUND | StatusCode::UNAUTHORIZED
                ),
                "a member can use {} {path}: {}",
                route.method,
                as_member.status
            );
        }
        if route.method != "GET" {
            let no_token = request(&w.app, route.method, &path, Some(&alice.session), None).await;
            // Without the CSRF token and Origin no mutation is accepted, owner or not.
            assert_eq!(
                no_token.status,
                StatusCode::FORBIDDEN,
                "{} {path}",
                route.method
            );
            assert_eq!(no_token.json()["code"], "csrf_token");
        }
    }
    let calls_before = w.bridge.calls().len();
    let _ = request(
        &w.app,
        "POST",
        &format!("/api/v1/tenants/{tenant}/calibration/refresh"),
        Some(&alice.session),
        None,
    )
    .await;
    assert_eq!(
        w.bridge.calls().len(),
        calls_before,
        "a CSRF failure never reaches the worker"
    );
}

#[tokio::test]
async fn a_suspended_tenant_refuses_mutations_but_can_still_be_read() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let tenant = alice.tenant_for("alice").await;
    let id = swarm_web::model::TenantId::parse(&tenant).unwrap();
    use swarm_web::store::Store;
    w.app
        .store
        .set_tenant_status(&id, swarm_web::model::TenantStatus::Suspended)
        .await
        .unwrap();
    let read = alice.get(&format!("/api/v1/tenants/{tenant}/config")).await;
    assert_eq!(read.status, StatusCode::OK);
    let write = alice
        .put(
            &format!("/api/v1/tenants/{tenant}/config"),
            json!({ "config": {} }),
        )
        .await;
    assert_eq!(write.status, StatusCode::FORBIDDEN);
    assert_eq!(write.json()["code"], "tenant_inactive");
}

#[tokio::test]
async fn settings_round_trip_without_credentials_or_machine_paths() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let tenant = alice.tenant_for("alice").await;
    let base = format!("/api/v1/tenants/{tenant}");
    assert_eq!(
        alice.get(&format!("{base}/config")).await.json(),
        json!({ "repositories": [] })
    );

    let saved = alice
        .put(&format!("{base}/config"), json!({ "config": config() }))
        .await;
    assert_eq!(saved.status, StatusCode::OK, "{}", saved.text());
    let body = saved.text();
    assert!(!body.contains(CANARY), "{body}");
    for local in [
        "/Users/alice",
        "workspace_root",
        "python_bin",
        "repo_dir",
        "/opt/claude",
    ] {
        assert!(!body.contains(local), "{local} survived: {body}");
    }
    let loaded = alice.get(&format!("{base}/config")).await.json();
    assert_eq!(loaded["minimum_remaining_percent"], 20);
    assert_eq!(
        loaded["providers"][0],
        json!({ "id": "claude", "enabled": true, "model": "m" })
    );
    let repo = &loaded["repositories"][0];
    assert_eq!(repo["id"], "acme__demo");
    assert_eq!(repo["adversarial_uat_enabled"], true);
    assert_eq!(repo["notes"], "state: draft", "prose is not mangled");
    assert_eq!(repo["nested"], json!({ "keep": 1 }));
    assert!(repo.get("github_token").is_none());
    assert!(loaded.get("model_data_api_key").is_none());
    assert!(
        !w.app.store.debug_dump().contains(CANARY),
        "the store never held the credential"
    );

    // The same layout the importer and a hosted job read.
    use swarm_web::store::Store;
    let id = swarm_web::model::TenantId::parse(&tenant).unwrap();
    let app_doc = w
        .app
        .store
        .document(&id, "tenant_config", "app")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(app_doc["schema"], 1);
    assert!(w
        .app
        .store
        .document(&id, "tenant_config", "repo-acme__demo")
        .await
        .unwrap()
        .is_some());

    // Per-repository settings, with the id from the path.
    let repo_path = format!("{base}/repos/acme__demo/config");
    let one = alice.get(&repo_path).await;
    assert_eq!(one.json()["github_repository"], "acme/demo");
    let updated = alice
        .put(&repo_path, json!({ "id": "someone__else", "github_repository": "acme/demo", "ready_label": "Ready", "api_key": CANARY }))
        .await;
    assert_eq!(updated.status, StatusCode::OK, "{}", updated.text());
    assert_eq!(
        updated.json()["id"],
        "acme__demo",
        "the path decides the id"
    );
    assert!(!updated.text().contains(CANARY));
    assert_eq!(
        alice
            .get(&format!("{base}/repos/acme__missing/config"))
            .await
            .status,
        StatusCode::NOT_FOUND
    );
    let listed = alice.get(&format!("{base}/repos")).await.json();
    assert_eq!(listed["repositories"][0]["githubRepository"], "acme/demo");

    // Dropping a repository from the saved configuration removes its document.
    alice
        .put(
            &format!("{base}/config"),
            json!({ "config": { "repositories": [] } }),
        )
        .await;
    assert!(w
        .app
        .store
        .document(&id, "tenant_config", "repo-acme__demo")
        .await
        .unwrap()
        .is_none());
}

#[tokio::test]
async fn settings_are_per_tenant_and_validated() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let bob = w.app.sign_in("code-bob").await;
    let (a, b) = (alice.tenant_for("alice").await, bob.tenant_for("bob").await);
    alice
        .put(
            &format!("/api/v1/tenants/{a}/config"),
            json!({ "config": config() }),
        )
        .await;
    assert_eq!(
        bob.get(&format!("/api/v1/tenants/{b}/config")).await.json(),
        json!({ "repositories": [] })
    );

    let config_path = format!("/api/v1/tenants/{a}/config");
    let put = |body: Value| alice.put(&config_path, body);
    for bad in [
        json!({}),
        json!({ "config": [] }),
        json!({ "config": { "repositories": "x" } }),
        json!({ "config": { "repositories": [{ "github_repository": "not-a-repo" }] } }),
        json!({ "config": { "repositories": [{ "id": "../x", "github_repository": "a/b" }] } }),
        json!({ "config": { "repositories": [{ "github_repository": "a/b" }, { "github_repository": "a/b" }] } }),
        json!({ "config": { "providers": "claude" } }),
    ] {
        let reply = put(bad.clone()).await;
        assert_eq!(
            reply.status,
            StatusCode::BAD_REQUEST,
            "{bad}: {}",
            reply.text()
        );
    }
    // A rejected save changes nothing.
    assert_eq!(
        alice
            .get(&format!("/api/v1/tenants/{a}/config"))
            .await
            .json()["repositories"][0]["id"],
        "acme__demo"
    );

    let filter = alice
        .put(
            &format!("/api/v1/tenants/{a}/feedback-repo-filter"),
            json!({ "repoIds": ["acme__demo"] }),
        )
        .await;
    assert_eq!(filter.json()["feedback_repo_filter"], json!(["acme__demo"]));
    let bad_filter = alice
        .put(
            &format!("/api/v1/tenants/{a}/feedback-repo-filter"),
            json!({ "repoIds": [1] }),
        )
        .await;
    assert_eq!(bad_filter.status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn bridge_routes_resolve_repositories_from_the_tenants_own_settings() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let tenant = alice.tenant_for("alice").await;
    let base = format!("/api/v1/tenants/{tenant}");
    alice
        .put(&format!("{base}/config"), json!({ "config": config() }))
        .await;

    let ids = urlencoding("[\"acme__demo\"]");
    let reply = alice
        .get(&format!(
            "{base}/history?offset=10&repoIds={ids}&tenant=t0000000000000000"
        ))
        .await;
    assert_eq!(reply.status, StatusCode::OK, "{}", reply.text());
    assert_eq!(
        reply.json()["tenant"],
        tenant,
        "the answer is for the path's tenant"
    );
    let calls = w.bridge.calls();
    let (call_tenant, op, args) = calls.last().unwrap();
    assert_eq!(
        (call_tenant.as_str(), op.as_str()),
        (tenant.as_str(), "execution_history")
    );
    assert_eq!(args["repositories"], json!(["acme/demo"]));
    assert_eq!(args["offset"], "10");

    // Empty means every configured repository; an unknown id is refused before the worker runs.
    alice
        .get(&format!("{base}/history?repoIds={}", urlencoding("[]")))
        .await;
    assert_eq!(
        w.bridge.calls().last().unwrap().2["repositories"],
        json!(["acme/demo"])
    );
    let before = w.bridge.calls().len();
    let unknown = alice
        .get(&format!(
            "{base}/history?repoIds={}",
            urlencoding("[\"other__repo\"]")
        ))
        .await;
    assert_eq!(unknown.status, StatusCode::BAD_REQUEST);
    assert_eq!(w.bridge.calls().len(), before);

    // Structured query objects (usage, grades) arrive parsed.
    alice
        .get(&format!(
            "{base}/usage-report?repoIds={}&query={}",
            urlencoding("[]"),
            urlencoding("{\"groupBy\":\"model\"}")
        ))
        .await;
    assert_eq!(
        w.bridge.calls().last().unwrap().2["query"]["groupBy"],
        "model"
    );

    // Architecture docs only for a configured repository.
    let docs = alice
        .get(&format!("{base}/architecture-docs?repository=acme/demo"))
        .await;
    assert_eq!(docs.status, StatusCode::OK, "{}", docs.text());
    for repository in ["acme/other", "../../etc/passwd", "a"] {
        let refused = alice
            .get(&format!(
                "{base}/architecture-docs?repository={}",
                urlencoding(repository)
            ))
            .await;
        assert_eq!(refused.status, StatusCode::NOT_FOUND, "{repository}");
    }

    // Path parameters win over a body field; the worker never sees a body tenant.
    alice
        .send(
            Method::POST,
            &format!("{base}/repos/acme__demo/integration-pr"),
            Some(json!({ "repoId": "other__repo", "tenant": "t1" })),
        )
        .await;
    let (_, op, args) = w.bridge.calls().last().unwrap().clone();
    assert_eq!(op, "open_integration_pr");
    assert_eq!(args["repoId"], "acme__demo");
    assert_eq!(args["repository"], "acme/demo");
    let missing = alice
        .send(
            Method::POST,
            &format!("{base}/repos/nope__nope/promote"),
            None,
        )
        .await;
    assert_eq!(missing.status, StatusCode::NOT_FOUND);
}

fn urlencoding(value: &str) -> String {
    url::form_urlencoded::byte_serialize(value.as_bytes()).collect()
}

#[tokio::test]
async fn bridge_failures_map_to_honest_statuses_and_leak_nothing() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let tenant = alice.tenant_for("alice").await;
    let base = format!("/api/v1/tenants/{tenant}");
    w.bridge.fail("knowledge_status", "unavailable");
    let reply = alice.get(&format!("{base}/knowledge")).await;
    assert_eq!(reply.status, StatusCode::NOT_IMPLEMENTED);
    assert_eq!(reply.json()["code"], "not_available_yet");
    assert_eq!(reply.json()["error"], "Not hosted yet.");
    w.bridge.fail("execution_history", "bad");
    assert_eq!(
        alice.get(&format!("{base}/history")).await.status,
        StatusCode::BAD_REQUEST
    );
    w.bridge.fail("jev_feedback", "failed");
    let failed = alice.get(&format!("{base}/jev-feedback")).await;
    assert_eq!(failed.status, StatusCode::INTERNAL_SERVER_ERROR);
    assert!(!failed.text().contains(CANARY) && !failed.text().contains("boom"));

    // A deployment with no bridge says so instead of answering with nothing.
    let bare = TestApp::new();
    bare.github.add_user(
        "code-alice",
        1,
        "alice",
        vec![installation(100, "alice", "User", Role::Owner)],
    );
    let client = bare.sign_in("code-alice").await;
    let id = client.tenant_for("alice").await;
    let none = client.get(&format!("/api/v1/tenants/{id}/history")).await;
    assert_eq!(none.status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(none.json()["code"], "bridge_unconfigured");
}

#[tokio::test]
async fn request_bodies_and_arguments_are_bounded() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let tenant = alice.tenant_for("alice").await;
    let base = format!("/api/v1/tenants/{tenant}");
    let big = "x".repeat(70 * 1024);
    let reply = alice
        .send(
            Method::POST,
            &format!("{base}/routing/simulate"),
            Some(json!({ "inputs": { "note": big } })),
        )
        .await;
    assert_eq!(reply.status, StatusCode::PAYLOAD_TOO_LARGE);
    let not_object = alice
        .send(Method::POST, &format!("{base}/routing/simulate"), None)
        .await;
    assert_eq!(
        not_object.status,
        StatusCode::OK,
        "an empty body is an empty argument list"
    );
    let array = {
        let builder = Request::builder()
            .method(Method::POST)
            .uri(format!("{base}/routing/simulate"))
            .header(
                header::COOKIE,
                format!("{}={}", session_cookie_name(w.app.secure), alice.session),
            )
            .header(header::ORIGIN, w.app.state.config.origin())
            .header("x-csrf-token", &alice.csrf);
        w.app.call(builder.body(Body::from("[1,2]")).unwrap()).await
    };
    assert_eq!(array.status, StatusCode::BAD_REQUEST);
    assert!(
        !array.text().contains("[1,2]"),
        "a rejected body is never echoed"
    );
}

#[tokio::test]
async fn native_endpoints_report_what_the_tenant_has() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let tenant = alice.tenant_for("alice").await;
    let base = format!("/api/v1/tenants/{tenant}");
    alice
        .put(&format!("{base}/config"), json!({ "config": config() }))
        .await;

    let tools = alice.get(&format!("{base}/tools")).await.json();
    let claude = tools
        .as_array()
        .unwrap()
        .iter()
        .find(|t| t["id"] == "claude")
        .unwrap();
    assert_eq!(claude["authenticated"], false);
    assert_eq!(
        alice.get(&format!("{base}/model-data-key")).await.json(),
        json!(false)
    );

    // `save_model_data_key` is the existing write-only provider-key route.
    let saved = alice
        .put(
            &format!("{base}/provider-keys/model-data"),
            json!({ "key": "aa-model-data-key-0123456789" }),
        )
        .await;
    assert_eq!(saved.status, StatusCode::OK);
    assert!(!saved.text().contains("0123456789"));
    assert_eq!(
        alice.get(&format!("{base}/model-data-key")).await.json(),
        json!(true)
    );
    alice
        .put(
            &format!("{base}/provider-keys/claude"),
            json!({ "key": "sk-ant-api03-READY-0123456789" }),
        )
        .await;
    let tools = alice.get(&format!("{base}/tools")).await.json();
    let claude = tools
        .as_array()
        .unwrap()
        .iter()
        .find(|t| t["id"] == "claude")
        .unwrap();
    assert_eq!(claude["authenticated"], true);
    assert!(!tools.to_string().contains("READY-0123456789"));
    assert_eq!(
        alice
            .delete(&format!("{base}/provider-keys/model-data"))
            .await
            .status,
        StatusCode::NO_CONTENT
    );

    let usage = alice.get(&format!("{base}/provider-usage")).await.json();
    let row = usage
        .as_array()
        .unwrap()
        .iter()
        .find(|p| p["provider"] == "claude")
        .unwrap();
    assert_eq!(row["status"], 2, "no budget and no report: unavailable");
    assert!(
        row["remainingPercent"].is_null(),
        "never a fabricated number"
    );
    assert_eq!(row["usable"], false);

    let bots = alice
        .get(&format!("{base}/repos/acme__demo/readiness"))
        .await
        .json();
    let claude = bots
        .as_array()
        .unwrap()
        .iter()
        .find(|p| p["provider"] == "claude")
        .unwrap();
    assert_eq!(
        (claude["configured"].as_bool(), claude["valid"].as_bool()),
        (Some(true), Some(true))
    );
    let codex = bots
        .as_array()
        .unwrap()
        .iter()
        .find(|p| p["provider"] == "codex")
        .unwrap();
    assert_eq!(codex["valid"], false);
    assert!(codex["message"]
        .as_str()
        .unwrap()
        .contains("No Codex API key"));
    assert_eq!(
        alice
            .get(&format!("{base}/repos/nope__nope/bots"))
            .await
            .status,
        StatusCode::NOT_FOUND
    );

    let status = alice.get(&format!("{base}/status")).await.json();
    assert_eq!(status["issue"]["state"], "stopped");
    assert_eq!(status["schedulerRepoCount"], 1);
    assert_eq!(status["repos"][0]["githubRepository"], "acme/demo");

    // No job runner on this deployment: the controls say so rather than pretending.
    for action in ["pause", "resume", "stop"] {
        let reply = alice
            .send(
                Method::POST,
                &format!("{base}/processes/issue/{action}"),
                None,
            )
            .await;
        assert_eq!(reply.status, StatusCode::SERVICE_UNAVAILABLE, "{action}");
    }
    let other = alice
        .send(
            Method::POST,
            &format!("{base}/processes/uat%3Aacme/pause"),
            None,
        )
        .await;
    assert_ne!(other.status, StatusCode::OK);
    let scan = alice
        .send(Method::POST, &format!("{base}/scan"), None)
        .await;
    assert_eq!(scan.status, StatusCode::SERVICE_UNAVAILABLE);
}

#[tokio::test]
async fn logs_are_the_tenants_own_redacted_and_in_the_desktop_format() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let bob = w.app.sign_in("code-bob").await;
    let (a, b) = (alice.tenant_for("alice").await, bob.tenant_for("bob").await);
    let uat = "Adversarial UAT for issue #419: fixer Claude model claude-sonnet with effort high.";
    let cyber = "Adversarial Cybersecurity for issue #419: round 2 of 3 begins.";
    w.app
        .state
        .events
        .publish_log(&a, "acme/demo", 419, uat, 1_000);
    w.app.state.events.publish_log(
        &a,
        "acme/demo",
        419,
        &format!("token {CANARY} leaked"),
        1_001,
    );
    w.app
        .state
        .events
        .publish_log(&a, "acme/demo", 419, cyber, 1_002);
    w.app
        .state
        .events
        .publish_log(&b, "bob/secret", 7, "bob only", 1_003);

    let logs = alice.get(&format!("/api/v1/tenants/{a}/logs")).await;
    let lines: Vec<String> = serde_json::from_value(logs.json()).unwrap();
    assert_eq!(lines.len(), 3);
    assert_eq!(
        lines[0],
        format!("[1000] [Issue worker/stdout] {uat}"),
        "the stable boundary log passes through verbatim"
    );
    assert_eq!(lines[2], format!("[1002] [Issue worker/stdout] {cyber}"));
    assert!(!logs.text().contains(CANARY) && !logs.text().contains("bob only"));
    let limited: Vec<String> = serde_json::from_value(
        alice
            .get(&format!("/api/v1/tenants/{a}/logs?limit=1"))
            .await
            .json(),
    )
    .unwrap();
    assert_eq!(limited, vec![lines[2].clone()]);
    assert_eq!(
        alice
            .get(&format!("/api/v1/tenants/{a}/logs?limit=abc"))
            .await
            .status,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        bob.get(&format!("/api/v1/tenants/{a}/logs")).await.status,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn refreshing_model_data_announces_it_on_the_calibration_stream() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let tenant = alice.tenant_for("alice").await;
    let mut live = w.app.state.events.subscribe();
    let reply = alice
        .send(
            Method::POST,
            &format!("/api/v1/tenants/{tenant}/calibration/refresh"),
            Some(json!({ "force": true })),
        )
        .await;
    assert_eq!(reply.status, StatusCode::OK);
    let frame = live.try_recv().expect("a calibration frame was published");
    assert_eq!(frame.tenant, tenant);
    assert!(matches!(frame.topic, swarm_web::events::Topic::Calibration));
    assert_eq!(frame.payload["tenant"], tenant);
}

#[test]
fn existing_account_routes_are_not_registered_twice() {
    // `save_model_data_key` / `clear_model_data_key` are the provider-key routes.
    let existing: Vec<_> = catalog::ROUTES
        .iter()
        .filter(|r| r.handler == Handler::Native(Native::Existing))
        .flat_map(|r| r.commands.iter().copied())
        .collect();
    assert_eq!(existing, ["save_model_data_key", "clear_model_data_key"]);
}

#[tokio::test]
async fn the_document_store_only_answers_for_the_tenant_it_is_asked_about() {
    use swarm_web::model::TenantId;
    use swarm_web::store::Store;
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let bob = w.app.sign_in("code-bob").await;
    let a = TenantId::parse(&alice.tenant_for("alice").await).unwrap();
    let b = TenantId::parse(&bob.tenant_for("bob").await).unwrap();
    w.app
        .store
        .put_document(
            &a,
            "tenant_config",
            "app",
            json!({ "settings": { "x": 1 } }),
        )
        .await
        .unwrap();
    assert!(w
        .app
        .store
        .document(&b, "tenant_config", "app")
        .await
        .unwrap()
        .is_none());
    assert!(w
        .app
        .store
        .documents(&b, "tenant_config")
        .await
        .unwrap()
        .is_empty());
    assert!(!w
        .app
        .store
        .delete_document(&b, "tenant_config", "app")
        .await
        .unwrap());
    assert!(w
        .app
        .store
        .document(&a, "tenant_config", "app")
        .await
        .unwrap()
        .is_some());
    assert_eq!(
        w.app
            .store
            .documents(&a, "other_collection")
            .await
            .unwrap()
            .len(),
        0
    );
}
