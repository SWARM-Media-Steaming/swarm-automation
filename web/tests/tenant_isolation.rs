//! Cross-tenant access must be impossible, at the HTTP layer, the store layer
//! and the cryptographic layer.

mod common;

use axum::http::{Method, StatusCode};
use common::*;
use serde_json::json;
use swarm_web::crypto::{open, seal, secret_aad, LocalKeyWrapper};
use swarm_web::model::{Provider, Role, TenantId};
use swarm_web::store::Store;

struct World {
    app: TestApp,
}

impl World {
    /// alice owns tenant A; bob owns tenant B; carol is a *member* of A only.
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
        World { app }
    }
}

const CANARY_A: &str = "sk-ant-api03-ALICE-CANARY-0123456789";
const CANARY_B: &str = "sk-ant-api03-BOB-CANARY-9876543210";

/// Every tenant-scoped endpoint, as (method, path suffix, body).
fn tenant_endpoints() -> Vec<(Method, &'static str, Option<serde_json::Value>)> {
    vec![
        (Method::GET, "", None),
        (Method::GET, "/members", None),
        (Method::GET, "/provider-keys", None),
        (
            Method::PUT,
            "/provider-keys/claude",
            Some(json!({ "key": "sk-ant-api03-ATTACKER-WRITE-1234" })),
        ),
        (Method::DELETE, "/provider-keys/claude", None),
        (Method::GET, "/quotas", None),
        (
            Method::PUT,
            "/budgets",
            Some(json!({ "minimum_remaining_percent": 99.0 })),
        ),
        (Method::GET, "/usage", None),
        (Method::GET, "/work/acme/demo/issues/1", None),
        (Method::POST, "/work/acme/demo/issues/1/run", None),
        (Method::POST, "/work/acme/demo/issues/1/pause", None),
        (Method::POST, "/work/acme/demo/issues/1/resume", None),
        (Method::POST, "/work/acme/demo/issues/1/stop", None),
        (Method::GET, "/work/acme/demo/issues/1/logs", None),
    ]
}

#[tokio::test]
async fn a_user_cannot_touch_another_tenant_through_any_endpoint() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let bob = w.app.sign_in("code-bob").await;
    let tenant_a = alice.tenant_for("alice").await;
    let tenant_b = bob.tenant_for("bob").await;
    assert_ne!(tenant_a, tenant_b);
    assert_eq!(
        alice
            .put(
                &format!("/api/v1/tenants/{tenant_b}/provider-keys/claude"),
                json!({ "key": CANARY_A })
            )
            .await
            .status,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        bob.put(
            &format!("/api/v1/tenants/{tenant_b}/provider-keys/claude"),
            json!({ "key": CANARY_B })
        )
        .await
        .status,
        StatusCode::OK
    );

    for (method, suffix, body) in tenant_endpoints() {
        let path = format!("/api/v1/tenants/{tenant_b}{suffix}");
        let reply = alice.send(method.clone(), &path, body.clone()).await;
        assert_eq!(
            reply.status,
            StatusCode::NOT_FOUND,
            "{method} {path} must look nonexistent to alice"
        );
        assert_eq!(reply.json()["code"], "not_found");
        assert!(
            !reply.text().contains("bob"),
            "no data about tenant B in the refusal"
        );
    }

    // The attempts changed nothing: bob's key and budgets are untouched.
    let keys = bob
        .get(&format!("/api/v1/tenants/{tenant_b}/provider-keys"))
        .await
        .json();
    let claude = keys["keys"]
        .as_array()
        .unwrap()
        .iter()
        .find(|k| k["provider"] == "claude")
        .unwrap()
        .clone();
    assert_eq!(claude["configured"], true);
    assert_eq!(claude["updated_by"], "bob");
    assert_eq!(
        bob.get(&format!("/api/v1/tenants/{tenant_b}/quotas"))
            .await
            .json()["budgets"]["minimum_remaining_percent"],
        10.0
    );
}

#[tokio::test]
async fn tenant_ids_cannot_be_probed_or_smuggled() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let tenant_a = alice.tenant_for("alice").await;
    let long = "x".repeat(200);
    for bogus in [
        "t0000000000000000",
        "default",
        "..",
        "%2e%2e",
        "A",
        "t1/../",
        "t1%2f..%2f",
        long.as_str(),
    ] {
        let reply = alice.get(&format!("/api/v1/tenants/{bogus}")).await;
        assert!(
            matches!(
                reply.status,
                StatusCode::NOT_FOUND | StatusCode::BAD_REQUEST
            ),
            "{bogus} -> {}",
            reply.status
        );
    }
    // A tenant id in a body or query string is never consulted.
    let reply = alice
        .send(
            Method::PUT,
            &format!("/api/v1/tenants/{tenant_a}/budgets?tenant=other"),
            Some(json!({ "minimum_remaining_percent": 5.0, "tenant": "other" })),
        )
        .await;
    assert_eq!(reply.status, StatusCode::OK);
    // Listing shows only the caller's tenants.
    let listed = alice.get("/api/v1/tenants").await.json();
    assert_eq!(listed["tenants"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn members_see_their_tenant_but_cannot_change_it() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let carol = w.app.sign_in("code-carol").await;
    let tenant_a = alice.tenant_for("alice").await;
    assert_eq!(
        carol.tenant_for("alice").await,
        tenant_a,
        "same installation, same tenant"
    );
    alice
        .put(
            &format!("/api/v1/tenants/{tenant_a}/provider-keys/codex"),
            json!({ "key": "sk-codex-CANARY-aaaaaaaa" }),
        )
        .await;

    let base = format!("/api/v1/tenants/{tenant_a}");
    assert_eq!(carol.get(&base).await.json()["role"], "member");
    assert_eq!(
        carol.get(&format!("{base}/provider-keys")).await.status,
        StatusCode::OK,
        "members see which keys exist"
    );
    assert_eq!(
        carol.get(&format!("{base}/usage")).await.status,
        StatusCode::OK
    );
    let members = carol.get(&format!("{base}/members")).await.json();
    assert_eq!(members["members"].as_array().unwrap().len(), 2);

    for (method, suffix, body) in [
        (
            Method::PUT,
            "/provider-keys/claude",
            Some(json!({ "key": "sk-ant-api03-MEMBER-WRITE-123" })),
        ),
        (Method::DELETE, "/provider-keys/codex", None),
        (
            Method::PUT,
            "/budgets",
            Some(json!({ "minimum_remaining_percent": 0.0 })),
        ),
    ] {
        let reply = carol
            .send(method.clone(), &format!("{base}{suffix}"), body)
            .await;
        assert_eq!(
            (reply.status, reply.json()["code"].clone()),
            (StatusCode::FORBIDDEN, json!("owner_required")),
            "{method} {suffix}"
        );
    }
    let keys = alice.get(&format!("{base}/provider-keys")).await.json();
    let codex = keys["keys"]
        .as_array()
        .unwrap()
        .iter()
        .find(|k| k["provider"] == "codex")
        .unwrap()
        .clone();
    assert_eq!(codex["configured"], true, "the member's delete did nothing");
}

#[tokio::test]
async fn losing_access_on_github_removes_access_here() {
    let w = World::new();
    let carol = w.app.sign_in("code-carol").await;
    let tenant_a = carol.tenant_for("alice").await;
    assert_eq!(
        carol
            .get(&format!("/api/v1/tenants/{tenant_a}"))
            .await
            .status,
        StatusCode::OK
    );

    // GitHub no longer lists the installation for carol; her next sign-in drops it.
    w.app.github.set_installations("code-carol", vec![]);
    let carol = w.app.sign_in("code-carol").await;
    assert_eq!(
        carol
            .get(&format!("/api/v1/tenants/{tenant_a}"))
            .await
            .status,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        carol.get("/api/v1/tenants").await.json()["tenants"]
            .as_array()
            .unwrap()
            .len(),
        0
    );

    // A role change on GitHub is followed too (owner demoted to member).
    w.app.github.set_installations(
        "code-carol",
        vec![installation(100, "alice", "User", Role::Owner)],
    );
    let carol = w.app.sign_in("code-carol").await;
    assert_eq!(
        carol
            .get(&format!("/api/v1/tenants/{tenant_a}"))
            .await
            .json()["role"],
        "owner"
    );
    w.app.github.set_installations(
        "code-carol",
        vec![installation(100, "alice", "User", Role::Member)],
    );
    let carol = w.app.sign_in("code-carol").await;
    assert_eq!(
        carol
            .get(&format!("/api/v1/tenants/{tenant_a}"))
            .await
            .json()["role"],
        "member"
    );
}

#[tokio::test]
async fn a_suspended_installation_freezes_writes_but_not_reads() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let tenant_a = alice.tenant_for("alice").await;
    let id = TenantId::parse(&tenant_a).unwrap();
    w.app
        .store
        .set_tenant_status(&id, swarm_web::model::TenantStatus::Suspended)
        .await
        .unwrap();
    let write = alice
        .put(
            &format!("/api/v1/tenants/{tenant_a}/provider-keys/claude"),
            json!({ "key": CANARY_A }),
        )
        .await;
    assert_eq!(
        (write.status, write.json()["code"].clone()),
        (StatusCode::FORBIDDEN, json!("tenant_inactive"))
    );
    assert_eq!(
        alice
            .get(&format!("/api/v1/tenants/{tenant_a}"))
            .await
            .json()["status"],
        "suspended"
    );
}

#[tokio::test]
async fn the_store_only_answers_for_the_tenant_it_is_asked_about() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let bob = w.app.sign_in("code-bob").await;
    let a = TenantId::parse(&alice.tenant_for("alice").await).unwrap();
    let b = TenantId::parse(&bob.tenant_for("bob").await).unwrap();
    let store = &w.app.store;
    w.app
        .state
        .vault
        .put(
            &a,
            Provider::Claude,
            &swarm_web::secret::Secret::new(CANARY_A),
            "alice",
        )
        .await
        .unwrap();
    store
        .set_provider_report(
            &a,
            Provider::Claude,
            swarm_web::model::ProviderReport {
                remaining_percent: 5.0,
                detail: None,
                reported_at: START,
            },
        )
        .await
        .unwrap();
    assert!(store
        .reserve_job(&a, "job-1", Provider::Claude, 5)
        .await
        .unwrap());
    store
        .record_usage(
            &a,
            "2026-01",
            &[swarm_web::model::LedgerEntry {
                id: "u1".into(),
                provider: Provider::Claude,
                cost_usd: Some(9.0),
            }],
        )
        .await
        .unwrap();

    assert!(store
        .provider_key(&b, Provider::Claude)
        .await
        .unwrap()
        .is_none());
    assert!(store
        .provider_report(&b, Provider::Claude)
        .await
        .unwrap()
        .is_none());
    assert_eq!(store.active_jobs(&b).await.unwrap(), 0);
    assert_eq!(store.spend(&b, "2026-01").await.unwrap().total_usd(), 0.0);
    assert!(
        !store.release_job(&b, "job-1").await.unwrap(),
        "a job id is scoped to its tenant"
    );
    assert!(!store
        .delete_provider_key(&b, Provider::Claude)
        .await
        .unwrap());
    assert!(
        store
            .provider_key(&a, Provider::Claude)
            .await
            .unwrap()
            .is_some(),
        "A's key survived B's delete"
    );
    assert!(store
        .role_in(&b, &alice_user_id(&w).await)
        .await
        .unwrap()
        .is_none());
    // The same ledger id under B is B's own record, not a duplicate of A's.
    let added = store
        .record_usage(
            &b,
            "2026-01",
            &[swarm_web::model::LedgerEntry {
                id: "u1".into(),
                provider: Provider::Claude,
                cost_usd: Some(1.0),
            }],
        )
        .await
        .unwrap();
    assert_eq!(added, 1);
    assert_eq!(store.spend(&a, "2026-01").await.unwrap().total_usd(), 9.0);
}

async fn alice_user_id(w: &World) -> String {
    w.app.store.upsert_user(1, "alice").await.unwrap().id
}

#[tokio::test]
async fn a_sealed_key_cannot_be_moved_to_another_tenant_or_provider() {
    let w = World::new();
    let alice = w.app.sign_in("code-alice").await;
    let bob = w.app.sign_in("code-bob").await;
    let a = TenantId::parse(&alice.tenant_for("alice").await).unwrap();
    let b = TenantId::parse(&bob.tenant_for("bob").await).unwrap();
    w.app
        .state
        .vault
        .put(
            &a,
            Provider::Claude,
            &swarm_web::secret::Secret::new(CANARY_A),
            "alice",
        )
        .await
        .unwrap();
    let stolen = w
        .app
        .store
        .provider_key(&a, Provider::Claude)
        .await
        .unwrap()
        .unwrap();

    // An attacker with database write access copies A's row into B's (or onto
    // another provider). The ciphertext no longer authenticates.
    w.app
        .store
        .put_provider_key(
            &b,
            Provider::Claude,
            stolen.sealed.clone(),
            "mallory",
            START,
        )
        .await
        .unwrap();
    w.app
        .store
        .put_provider_key(&a, Provider::Grok, stolen.sealed, "mallory", START)
        .await
        .unwrap();
    assert!(w
        .app
        .state
        .vault
        .job_environment(&b, Provider::Claude, false)
        .await
        .is_err());
    assert!(w
        .app
        .state
        .vault
        .job_environment(&a, Provider::Grok, false)
        .await
        .is_err());
    let honest = w
        .app
        .state
        .vault
        .job_environment(&a, Provider::Claude, false)
        .await
        .unwrap();
    assert_eq!(honest.expose(), vec![("ANTHROPIC_API_KEY", CANARY_A)]);

    // And directly, with the same wrapper key.
    let wrapper = LocalKeyWrapper::new([1u8; 32]);
    let sealed = seal(&wrapper, b"x-secret", &secret_aad(a.as_str(), "claude"))
        .await
        .unwrap();
    assert!(open(&wrapper, &sealed, &secret_aad(b.as_str(), "claude"))
        .await
        .is_err());
}
