//! Usage accounting, per-tenant budgets and quotas, enforced before a job starts.

mod common;

use axum::http::{Method, StatusCode};
use common::*;
use serde_json::{json, Value};
use swarm_web::model::{Role, TenantStatus};
use swarm_web::store::Store;

struct Setup<'a> {
    app: &'a TestApp,
    alice: Client<'a>,
    tenant: String,
}

async fn setup(app: &TestApp) -> Setup<'_> {
    app.github.add_user(
        "code-alice",
        1,
        "alice",
        vec![installation(100, "alice", "User", Role::Owner)],
    );
    let alice = app.sign_in("code-alice").await;
    let tenant = alice.tenant_for("alice").await;
    for (provider, key) in [
        ("claude", "sk-ant-api03-quota-claude-1234"),
        ("codex", "sk-proj-quota-codex-1234567"),
    ] {
        let reply = alice
            .put(
                &format!("/api/v1/tenants/{tenant}/provider-keys/{provider}"),
                json!({ "key": key }),
            )
            .await;
        assert_eq!(reply.status, StatusCode::OK);
    }
    Setup { app, alice, tenant }
}

impl Setup<'_> {
    fn internal(&self, suffix: &str) -> String {
        format!("/api/v1/internal/tenants/{}{suffix}", self.tenant)
    }

    async fn call(&self, method: Method, suffix: &str, body: Option<Value>) -> Reply {
        self.app
            .call(internal(method, &self.internal(suffix), body))
            .await
    }

    async fn ingest(&self, body: Value) -> Value {
        let reply = self.call(Method::POST, "/usage", Some(body)).await;
        assert_eq!(reply.status, StatusCode::OK, "{}", reply.text());
        reply.json()
    }

    async fn usage(&self) -> Value {
        self.alice
            .get(&format!("/api/v1/tenants/{}/usage", self.tenant))
            .await
            .json()
    }

    async fn admit(&self, job: &str, provider: &str) -> Reply {
        self.call(
            Method::POST,
            "/jobs",
            Some(json!({ "job_id": job, "provider": provider })),
        )
        .await
    }

    fn provider<'v>(usage: &'v Value, name: &str) -> &'v Value {
        usage["providers"]
            .as_array()
            .unwrap()
            .iter()
            .find(|p| p["provider"] == name)
            .unwrap()
    }
}

fn record(id: &str, provider: &str, cost: Option<f64>, tokens: Option<u64>) -> Value {
    json!({ "id": id, "provider": provider, "estimated_cost": cost, "input_tokens": tokens, "currency": "USD" })
}

#[tokio::test]
async fn usage_is_summed_per_provider_and_ingest_is_idempotent() {
    let app = TestApp::new();
    let s = setup(&app).await;
    let batch = json!({ "records": [
        record("r1", "claude", Some(1.25), Some(100)),
        record("r2", "claude", Some(0.75), Some(50)),
        record("r3", "codex", Some(3.0), Some(10)),
        record("r4", "codex", None, Some(10)),
        record("r5", "claude", Some(9.0), None),
        record("bad-cost", "claude", Some(-1.0), Some(1)),
        record("bad-provider", "gemini", Some(1.0), Some(1)),
    ] });
    let first = s.ingest(batch.clone()).await;
    assert_eq!(
        first,
        json!({ "recorded": 5, "duplicates": 0, "rejected": 2 })
    );
    let again = s.ingest(batch).await;
    assert_eq!(
        again,
        json!({ "recorded": 0, "duplicates": 5, "rejected": 2 }),
        "re-sending a batch changes nothing"
    );

    let usage = s.usage().await;
    assert_eq!(usage["period"], "2026-01");
    let claude = Setup::provider(&usage, "claude");
    assert_eq!(claude["spend_usd"], 2.0);
    assert_eq!(claude["priced_invocations"], 2);
    assert_eq!(
        claude["unpriced_invocations"], 1,
        "a cost with no token usage is not priced spend (usage_report.py)"
    );
    let codex = Setup::provider(&usage, "codex");
    assert_eq!(
        (
            codex["spend_usd"].clone(),
            codex["unpriced_invocations"].clone()
        ),
        (json!(3.0), json!(1)),
        "unpriced is counted, never as zero spend"
    );
    assert_eq!(usage["total_spend_usd"], 5.0);
}

#[tokio::test]
async fn remaining_is_unavailable_until_a_budget_or_provider_limit_exists() {
    let app = TestApp::new();
    let s = setup(&app).await;
    let usage = s.usage().await;
    let claude = Setup::provider(&usage, "claude");
    assert_eq!(claude["status"], 2);
    assert_eq!(claude["source"], "unavailable");
    assert!(claude["remaining_percent"].is_null(), "never fabricated");
    assert_eq!(
        s.admit("job-1", "claude").await.status,
        StatusCode::OK,
        "no budget means no budget gate"
    );
}

#[tokio::test]
async fn a_provider_budget_pauses_at_the_minimum_and_resumes_when_raised_or_next_month() {
    let app = TestApp::new();
    let s = setup(&app).await;
    let budgets = format!("/api/v1/tenants/{}/budgets", s.tenant);
    let set = s.alice.put(&budgets, json!({ "minimum_remaining_percent": 10.0, "provider_budgets_usd": { "claude": 100.0 } })).await;
    assert_eq!(set.status, StatusCode::OK);

    s.ingest(json!({ "records": [record("a", "claude", Some(85.0), Some(1))] }))
        .await;
    let usage = s.usage().await;
    let claude = Setup::provider(&usage, "claude");
    assert_eq!(
        (
            claude["remaining_percent"].clone(),
            claude["status"].clone(),
            claude["source"].clone()
        ),
        (json!(15.0), json!(0), json!("budget"))
    );
    assert_eq!(s.admit("job-ok", "claude").await.status, StatusCode::OK);
    s.call(Method::DELETE, "/jobs/job-ok", None).await;

    s.ingest(json!({ "records": [record("b", "claude", Some(6.0), Some(1))] }))
        .await;
    let low = s.admit("job-low", "claude").await;
    assert_eq!(
        (low.status, low.json()["code"].clone()),
        (StatusCode::TOO_MANY_REQUESTS, json!("provider_budget_low"))
    );
    assert_eq!(Setup::provider(&s.usage().await, "claude")["status"], 1);
    assert_eq!(
        s.admit("job-codex", "codex").await.status,
        StatusCode::OK,
        "another provider is unaffected"
    );

    // Resumes when the owner raises the budget...
    s.alice.put(&budgets, json!({ "minimum_remaining_percent": 10.0, "provider_budgets_usd": { "claude": 200.0 } })).await;
    assert_eq!(s.admit("job-raised", "claude").await.status, StatusCode::OK);
    s.alice.put(&budgets, json!({ "minimum_remaining_percent": 10.0, "provider_budgets_usd": { "claude": 100.0 } })).await;
    s.call(Method::DELETE, "/jobs/job-raised", None).await;
    assert_eq!(
        s.admit("job-again", "claude").await.status,
        StatusCode::TOO_MANY_REQUESTS
    );

    // ...or when the month rolls over.
    s.app.clock.advance(31 * 86_400);
    let february = s.app.sign_in("code-alice").await; // the old session expired with the month
    let usage = february
        .get(&format!("/api/v1/tenants/{}/usage", s.tenant))
        .await
        .json();
    assert_eq!(usage["period"], "2026-02");
    assert_eq!(
        Setup::provider(&usage, "claude")["remaining_percent"],
        100.0
    );
    assert_eq!(s.admit("job-feb", "claude").await.status, StatusCode::OK);
}

#[tokio::test]
async fn provider_reported_limits_gate_when_available_and_expire_when_stale() {
    let app = TestApp::new();
    let s = setup(&app).await;
    s.ingest(json!({ "records": [], "provider_limits": { "claude": { "remaining_percent": 4.0, "detail": "session 4% remaining" } } })).await;
    let usage = s.usage().await;
    let claude = Setup::provider(&usage, "claude");
    assert_eq!(
        (
            claude["status"].clone(),
            claude["source"].clone(),
            claude["detail"].clone()
        ),
        (json!(1), json!("provider"), json!("session 4% remaining"))
    );
    assert_eq!(
        s.admit("job-1", "claude").await.status,
        StatusCode::TOO_MANY_REQUESTS
    );

    // A report older than an hour is "unavailable", not trusted and not zero.
    s.app.clock.advance(3601);
    let stale = s.usage().await;
    assert!(Setup::provider(&stale, "claude")["remaining_percent"].is_null());
    assert_eq!(s.admit("job-2", "claude").await.status, StatusCode::OK);

    // With a budget too, the lower of the two wins.
    let budgets = format!("/api/v1/tenants/{}/budgets", s.tenant);
    s.alice.put(&budgets, json!({ "minimum_remaining_percent": 10.0, "provider_budgets_usd": { "codex": 100.0 } })).await;
    s.ingest(json!({ "records": [record("c", "codex", Some(50.0), Some(1))], "provider_limits": { "codex": { "remaining_percent": 30.0 } } })).await;
    let codex = Setup::provider(&s.usage().await, "codex").clone();
    assert_eq!(
        (codex["remaining_percent"].clone(), codex["source"].clone()),
        (json!(30.0), json!("provider"))
    );
    let rejected = s.ingest(json!({ "records": [], "provider_limits": { "grok": { "remaining_percent": 140.0 }, "nope": { "remaining_percent": 1.0 } } })).await;
    assert_eq!(rejected["rejected"], 2);
}

#[tokio::test]
async fn the_monthly_spend_cap_stops_new_jobs_until_the_next_month() {
    let app = TestApp::new();
    let s = setup(&app).await;
    let plan = s
        .call(
            Method::PUT,
            "/quotas",
            Some(json!({ "max_concurrent_jobs": 5, "monthly_spend_cap_usd": 10.0 })),
        )
        .await;
    assert_eq!(plan.status, StatusCode::OK);
    s.ingest(json!({ "records": [record("a", "claude", Some(9.99), Some(1))] }))
        .await;
    assert_eq!(s.admit("job-1", "claude").await.status, StatusCode::OK);
    s.ingest(json!({ "records": [record("b", "codex", Some(0.01), Some(1))] }))
        .await;
    let capped = s.admit("job-2", "codex").await;
    assert_eq!(
        (capped.status, capped.json()["code"].clone()),
        (StatusCode::TOO_MANY_REQUESTS, json!("monthly_spend_cap"))
    );
    assert!(capped.json()["error"].as_str().unwrap().contains("$10.00"));
    assert_eq!(
        s.alice
            .get(&format!("/api/v1/tenants/{}/quotas", s.tenant))
            .await
            .json()["plan"]["monthly_spend_cap_usd"],
        10.0,
        "members and owners read the plan"
    );
    s.app.clock.advance(31 * 86_400);
    assert_eq!(s.admit("job-3", "codex").await.status, StatusCode::OK);
}

#[tokio::test]
async fn the_concurrent_job_limit_is_enforced_and_slots_are_released() {
    let app = TestApp::new();
    let s = setup(&app).await;
    assert_eq!(
        s.alice
            .get(&format!("/api/v1/tenants/{}/quotas", s.tenant))
            .await
            .json()["plan"]["max_concurrent_jobs"],
        2,
        "the default plan"
    );
    assert_eq!(s.admit("j1", "claude").await.status, StatusCode::OK);
    assert_eq!(s.admit("j2", "codex").await.status, StatusCode::OK);
    assert_eq!(
        s.admit("j1", "claude").await.status,
        StatusCode::OK,
        "re-admitting a running job takes no second slot"
    );
    let third = s.admit("j3", "claude").await;
    assert_eq!(
        (third.status, third.json()["code"].clone()),
        (StatusCode::TOO_MANY_REQUESTS, json!("concurrent_job_limit"))
    );
    assert_eq!(s.usage().await["active_jobs"], 2);
    assert_eq!(
        s.call(Method::DELETE, "/jobs/j1", None).await.status,
        StatusCode::NO_CONTENT
    );
    assert_eq!(
        s.call(Method::DELETE, "/jobs/j1", None).await.status,
        StatusCode::NOT_FOUND
    );
    assert_eq!(s.admit("j3", "claude").await.status, StatusCode::OK);
    // A denied job never holds a slot.
    s.call(
        Method::PUT,
        "/quotas",
        Some(json!({ "max_concurrent_jobs": 0, "monthly_spend_cap_usd": null })),
    )
    .await;
    assert_eq!(
        s.admit("j9", "claude").await.status,
        StatusCode::TOO_MANY_REQUESTS
    );
    assert_eq!(s.usage().await["active_jobs"], 2);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn racing_admissions_never_exceed_the_limit() {
    let app = TestApp::new();
    let s = setup(&app).await;
    s.call(
        Method::PUT,
        "/quotas",
        Some(json!({ "max_concurrent_jobs": 3, "monthly_spend_cap_usd": null })),
    )
    .await;
    let mut tasks = tokio::task::JoinSet::new();
    for n in 0..24 {
        let router = app.router.clone();
        let path = s.internal("/jobs");
        tasks.spawn(async move {
            use tower::ServiceExt;
            let request = internal(
                Method::POST,
                &path,
                Some(json!({ "job_id": format!("race-{n}"), "provider": "claude" })),
            );
            router.oneshot(request).await.unwrap().status()
        });
    }
    let mut admitted = 0;
    while let Some(status) = tasks.join_next().await {
        match status.unwrap() {
            StatusCode::OK => admitted += 1,
            StatusCode::TOO_MANY_REQUESTS => {}
            other => panic!("unexpected {other}"),
        }
    }
    assert_eq!(admitted, 3);
    assert_eq!(s.usage().await["active_jobs"], 3);
}

#[tokio::test]
async fn admission_needs_the_providers_key_and_an_active_tenant() {
    let app = TestApp::new();
    let s = setup(&app).await;
    let no_key = s.admit("j1", "grok").await;
    assert_eq!(
        (no_key.status, no_key.json()["code"].clone()),
        (StatusCode::CONFLICT, json!("conflict"))
    );
    assert_eq!(
        s.admit("j1", "model-data").await.status,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        s.admit("bad job id!", "claude").await.status,
        StatusCode::BAD_REQUEST
    );
    let id = swarm_web::model::TenantId::parse(&s.tenant).unwrap();
    app.store
        .set_tenant_status(&id, TenantStatus::Suspended)
        .await
        .unwrap();
    let inactive = s.admit("j2", "claude").await;
    assert_eq!(
        (inactive.status, inactive.json()["code"].clone()),
        (StatusCode::FORBIDDEN, json!("tenant_inactive"))
    );
    assert_eq!(s.usage().await["active_jobs"], 0);
}

#[tokio::test]
async fn usage_is_isolated_per_tenant() {
    let app = TestApp::new();
    let s = setup(&app).await;
    app.github.add_user(
        "code-bob",
        2,
        "bob",
        vec![installation(200, "bob", "User", Role::Owner)],
    );
    let bob = app.sign_in("code-bob").await;
    let bob_tenant = bob.tenant_for("bob").await;
    s.ingest(json!({ "records": [record("shared-id", "claude", Some(40.0), Some(1))] }))
        .await;
    let bobs = bob
        .get(&format!("/api/v1/tenants/{bob_tenant}/usage"))
        .await
        .json();
    assert_eq!(bobs["total_spend_usd"], 0.0);
    let ingest = app
        .call(internal(
            Method::POST,
            &format!("/api/v1/internal/tenants/{bob_tenant}/usage"),
            Some(json!({ "records": [record("shared-id", "claude", Some(1.0), Some(1))] })),
        ))
        .await;
    assert_eq!(
        ingest.json()["recorded"],
        1,
        "record ids are scoped to a tenant"
    );
    assert_eq!(s.usage().await["total_spend_usd"], 40.0);
}

#[tokio::test]
async fn budgets_and_plans_are_validated_and_owner_only() {
    let app = TestApp::new();
    let s = setup(&app).await;
    let budgets = format!("/api/v1/tenants/{}/budgets", s.tenant);
    for bad in [
        json!({ "minimum_remaining_percent": 150.0 }),
        json!({ "minimum_remaining_percent": -1.0 }),
        json!({ "minimum_remaining_percent": 10.0, "provider_budgets_usd": { "claude": 0.0 } }),
        json!({ "minimum_remaining_percent": 10.0, "provider_budgets_usd": { "model-data": 5.0 } }),
        json!({ "minimum_remaining_percent": 10.0, "provider_budgets_usd": { "gemini": 5.0 } }),
    ] {
        assert_eq!(
            s.alice.put(&budgets, bad.clone()).await.status,
            StatusCode::BAD_REQUEST,
            "{bad}"
        );
    }
    for bad in [
        json!({ "max_concurrent_jobs": 5000, "monthly_spend_cap_usd": null }),
        json!({ "max_concurrent_jobs": 1, "monthly_spend_cap_usd": 0.0 }),
    ] {
        assert_eq!(
            s.call(Method::PUT, "/quotas", Some(bad)).await.status,
            StatusCode::BAD_REQUEST
        );
    }
    // Plan quotas belong to the operator: no session-authenticated route sets them.
    let tenant_route = format!("/api/v1/tenants/{}/quotas", s.tenant);
    let attempt = s
        .alice
        .put(
            &tenant_route,
            json!({ "plan": { "max_concurrent_jobs": 1000, "monthly_spend_cap_usd": null } }),
        )
        .await;
    assert_eq!(attempt.status, StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn the_internal_api_is_for_the_operator_only() {
    let app = TestApp::new();
    let s = setup(&app).await;
    let path = s.internal("/usage");
    let body = || axum::body::Body::from(json!({ "records": [] }).to_string());
    let request = |auth: Option<String>| {
        let mut builder = axum::http::Request::builder()
            .method(Method::POST)
            .uri(&path)
            .header("content-type", "application/json");
        if let Some(auth) = auth {
            builder = builder.header("authorization", auth);
        }
        builder.body(body()).unwrap()
    };
    assert_eq!(
        app.call(request(None)).await.status,
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        app.call(request(Some("Bearer wrong-token-0123456789abcdef".into())))
            .await
            .status,
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        app.call(request(Some(INTERNAL_TOKEN.into()))).await.status,
        StatusCode::UNAUTHORIZED,
        "must be a Bearer credential"
    );
    assert_eq!(
        app.call(request(Some(format!("Bearer {INTERNAL_TOKEN}"))))
            .await
            .status,
        StatusCode::OK
    );
    // A tenant owner's browser session is not the operator.
    let session_only = axum::http::Request::builder()
        .method(Method::POST)
        .uri(&path)
        .header("cookie", format!("swarm_session={}", s.alice.session))
        .header("x-csrf-token", &s.alice.csrf)
        .header("content-type", "application/json")
        .body(body())
        .unwrap();
    assert_eq!(
        app.call(session_only).await.status,
        StatusCode::UNAUTHORIZED
    );
    let unknown = app
        .call(internal(
            Method::POST,
            "/api/v1/internal/tenants/t0000000000000000/usage",
            Some(json!({ "records": [] })),
        ))
        .await;
    assert_eq!(unknown.status, StatusCode::NOT_FOUND);

    let off = TestApp::without_internal_api();
    let reply = off
        .call(internal(
            Method::POST,
            "/api/v1/internal/tenants/t0000000000000000/usage",
            Some(json!({ "records": [] })),
        ))
        .await;
    assert_eq!(
        reply.status,
        StatusCode::NOT_FOUND,
        "disabled without a configured token"
    );
}

#[tokio::test]
async fn an_oversized_batch_is_refused() {
    let app = TestApp::new();
    let s = setup(&app).await;
    let records: Vec<Value> = (0..501)
        .map(|n| record(&format!("r{n}"), "claude", Some(0.0), Some(1)))
        .collect();
    let reply = s
        .call(Method::POST, "/usage", Some(json!({ "records": records })))
        .await;
    assert_eq!(reply.status, StatusCode::BAD_REQUEST);
    assert_eq!(s.usage().await["total_spend_usd"], 0.0);
}
