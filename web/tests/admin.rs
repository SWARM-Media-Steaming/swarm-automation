//! Platform admins and the first-admin bootstrap (issue #441): the bootstrap
//! happens once and only for a listed account, promote and demote work and are
//! audited, the last admin cannot be demoted, and everyone else is turned away
//! at every `/admin` route. Everything goes through HTTP like a browser would.

mod common;

use axum::body::Body;
use axum::http::{header, Method, Request, StatusCode};
use common::*;
use serde_json::{json, Value};
use swarm_web::catalog::{self, ADMIN_ROUTES};
use swarm_web::store::Store;

/// alice (id 1), bob (2), carol (3) and dave (4) can sign in; nobody has a tenant
/// installation, which an admin does not need.
fn world(bootstrap: &str) -> TestApp {
    let app = if bootstrap.is_empty() {
        TestApp::new()
    } else {
        TestApp::with_env(&[("SWARM_WEB_BOOTSTRAP_ADMINS", bootstrap)])
    };
    for (id, login) in [(1, "alice"), (2, "bob"), (3, "carol"), (4, "dave")] {
        app.github
            .add_user(&format!("code-{login}"), id, login, vec![]);
    }
    app
}

fn uid(app: &TestApp, login: &str) -> String {
    app.store.user_of_login(login)
}

async fn is_admin(app: &TestApp, login: &str) -> bool {
    app.store
        .user(&uid(app, login))
        .await
        .unwrap()
        .expect("user")
        .is_platform_admin
}

async fn audit(app: &TestApp) -> Vec<(String, Option<String>, Option<String>)> {
    app.store
        .admin_audit_log(100)
        .await
        .unwrap()
        .into_iter()
        .map(|row| (row.action, row.actor_user_id, row.target_user_id))
        .collect()
}

fn promote(app: &TestApp, login: &str) -> String {
    format!("/api/v1/admin/users/{}/promote", uid(app, login))
}

fn demote(app: &TestApp, login: &str) -> String {
    format!("/api/v1/admin/users/{}/demote", uid(app, login))
}

async fn session_flag(client: &Client<'_>) -> Value {
    client.get("/api/v1/session").await.json()["user"]["is_platform_admin"].clone()
}

// ---- bootstrap ----------------------------------------------------------------

#[tokio::test]
async fn a_listed_login_becomes_the_first_admin_at_sign_in() {
    let app = world("alice");
    let alice = app.sign_in("code-alice").await;
    assert_eq!(session_flag(&alice).await, json!(true));
    assert!(is_admin(&app, "alice").await);
    let users = alice.get("/api/v1/admin/users").await;
    assert_eq!(users.status, StatusCode::OK, "{}", users.text());
    let entry = &users.json()["users"][0];
    assert_eq!(entry["login"], "alice");
    assert_eq!(entry["is_platform_admin"], json!(true));

    let alice_id = uid(&app, "alice");
    assert_eq!(
        audit(&app).await,
        vec![(
            "admin.bootstrap".to_string(),
            Some(alice_id.clone()),
            Some(alice_id)
        )]
    );
}

#[tokio::test]
async fn a_numeric_id_matches_whatever_the_login_is_now() {
    // `1` is alice's GitHub id. The login is not listed at all.
    let app = world("1");
    app.sign_in("code-bob").await;
    assert!(!is_admin(&app, "bob").await);
    app.sign_in("code-alice").await;
    assert!(is_admin(&app, "alice").await);
}

#[tokio::test]
async fn logins_match_without_regard_to_case_and_an_at_sign() {
    let app = world("@ALICE");
    app.sign_in("code-alice").await;
    assert!(is_admin(&app, "alice").await);
}

#[tokio::test]
async fn a_user_who_is_not_listed_is_never_made_admin() {
    let app = world("alice");
    for code in ["code-bob", "code-carol", "code-dave"] {
        let client = app.sign_in(code).await;
        assert_eq!(session_flag(&client).await, json!(false));
    }
    for login in ["bob", "carol", "dave"] {
        assert!(!is_admin(&app, login).await, "{login}");
    }
    assert!(audit(&app).await.is_empty(), "nothing was changed");
}

#[tokio::test]
async fn with_no_list_nobody_becomes_admin_and_admin_routes_are_closed() {
    let app = world("");
    let alice = app.sign_in("code-alice").await;
    assert!(!is_admin(&app, "alice").await);
    assert_eq!(
        alice.get("/api/v1/admin/users").await.status,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn the_bootstrap_happens_once_and_only_while_no_admin_exists() {
    let app = world("alice,bob");
    app.sign_in("code-alice").await;
    // bob is listed too, but an admin exists now.
    let bob = app.sign_in("code-bob").await;
    assert!(!is_admin(&app, "bob").await);
    assert_eq!(session_flag(&bob).await, json!(false));
    assert_eq!(audit(&app).await.len(), 1);

    // Repeated sign-ins are idempotent: still one admin, still one audit row.
    app.sign_in("code-alice").await;
    app.sign_in("code-alice").await;
    assert!(is_admin(&app, "alice").await);
    assert_eq!(audit(&app).await.len(), 1);
}

#[tokio::test]
async fn the_bootstrap_never_comes_back_after_the_admin_is_replaced() {
    let app = world("alice");
    let alice = app.sign_in("code-alice").await;
    app.sign_in("code-bob").await;
    assert_eq!(
        alice
            .send(Method::POST, &promote(&app, "bob"), None)
            .await
            .status,
        StatusCode::OK
    );
    assert_eq!(
        alice
            .send(Method::POST, &demote(&app, "alice"), None)
            .await
            .status,
        StatusCode::OK
    );
    // alice is still listed and signs in again: bob is the admin, so she stays a user.
    let alice = app.sign_in("code-alice").await;
    assert!(!is_admin(&app, "alice").await);
    assert!(is_admin(&app, "bob").await);
    assert_eq!(session_flag(&alice).await, json!(false));
    assert_eq!(
        alice.get("/api/v1/admin/users").await.status,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn a_failed_sign_in_never_bootstraps() {
    let app = world("alice");
    // An unknown code is refused before any user exists.
    let reply = app.callback_for("github", "code-nobody").await;
    assert_ne!(reply.status, StatusCode::FOUND);
    assert_eq!(app.store.user_count(), 0);
    assert!(audit(&app).await.is_empty());
}

// ---- promote and demote ---------------------------------------------------------

#[tokio::test]
async fn an_admin_lists_promotes_and_demotes_and_each_change_is_audited() {
    let app = world("alice");
    let alice = app.sign_in("code-alice").await;
    let bob = app.sign_in("code-bob").await;
    let alice_id = uid(&app, "alice");
    let bob_id = uid(&app, "bob");

    let listed = alice.get("/api/v1/admin/users").await.json();
    let logins: Vec<_> = listed["users"]
        .as_array()
        .unwrap()
        .iter()
        .map(|u| {
            (
                u["login"].as_str().unwrap(),
                u["is_platform_admin"].as_bool().unwrap(),
            )
        })
        .collect();
    assert_eq!(logins, vec![("alice", true), ("bob", false)]);

    // bob is turned away until promoted, and the promotion works on his live session.
    assert_eq!(
        bob.get("/api/v1/admin/users").await.status,
        StatusCode::NOT_FOUND
    );
    let promoted = alice.send(Method::POST, &promote(&app, "bob"), None).await;
    assert_eq!(promoted.status, StatusCode::OK, "{}", promoted.text());
    assert_eq!(promoted.json()["changed"], json!(true));
    assert_eq!(promoted.json()["user"]["login"], "bob");
    assert_eq!(promoted.json()["user"]["is_platform_admin"], json!(true));
    assert_eq!(bob.get("/api/v1/admin/users").await.status, StatusCode::OK);

    // ... and the demotion takes effect on his next request, same session.
    let demoted = alice.send(Method::POST, &demote(&app, "bob"), None).await;
    assert_eq!(demoted.status, StatusCode::OK, "{}", demoted.text());
    assert_eq!(demoted.json()["user"]["is_platform_admin"], json!(false));
    assert_eq!(
        bob.get("/api/v1/admin/users").await.status,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        bob.send(Method::POST, &promote(&app, "bob"), None)
            .await
            .status,
        StatusCode::NOT_FOUND,
        "a demoted admin cannot promote themselves back"
    );

    // Newest first: demote, promote, bootstrap.
    let log = audit(&app).await;
    assert_eq!(
        log,
        vec![
            (
                "admin.demote".to_string(),
                Some(alice_id.clone()),
                Some(bob_id.clone())
            ),
            (
                "admin.promote".to_string(),
                Some(alice_id.clone()),
                Some(bob_id.clone())
            ),
            (
                "admin.bootstrap".to_string(),
                Some(alice_id.clone()),
                Some(alice_id)
            ),
        ]
    );
    let detail = &app.store.admin_audit_log(1).await.unwrap()[0];
    assert_eq!(detail.detail["target_login"], "bob");
}

#[tokio::test]
async fn asking_for_the_state_a_user_is_already_in_changes_nothing() {
    let app = world("alice");
    let alice = app.sign_in("code-alice").await;
    app.sign_in("code-bob").await;
    let again = alice
        .send(Method::POST, &promote(&app, "alice"), None)
        .await;
    assert_eq!(again.status, StatusCode::OK);
    assert_eq!(again.json()["changed"], json!(false));
    let not_admin = alice.send(Method::POST, &demote(&app, "bob"), None).await;
    assert_eq!(not_admin.status, StatusCode::OK);
    assert_eq!(not_admin.json()["changed"], json!(false));
    assert_eq!(audit(&app).await.len(), 1, "only the bootstrap row");
}

#[tokio::test]
async fn promoting_or_demoting_an_unknown_user_is_not_found() {
    let app = world("alice");
    let alice = app.sign_in("code-alice").await;
    for action in ["promote", "demote"] {
        let reply = alice
            .send(
                Method::POST,
                &format!("/api/v1/admin/users/u-nobody/{action}"),
                None,
            )
            .await;
        assert_eq!(reply.status, StatusCode::NOT_FOUND, "{action}");
    }
    assert_eq!(audit(&app).await.len(), 1);
}

#[tokio::test]
async fn the_last_admin_cannot_be_demoted_even_by_themselves() {
    let app = world("alice");
    let alice = app.sign_in("code-alice").await;
    let refused = alice.send(Method::POST, &demote(&app, "alice"), None).await;
    assert_eq!(refused.status, StatusCode::CONFLICT, "{}", refused.text());
    assert!(refused.text().contains("last platform admin"));
    assert!(is_admin(&app, "alice").await);
    assert_eq!(audit(&app).await.len(), 1, "a refusal is not a change");

    // With a second admin the first may step down ...
    app.sign_in("code-bob").await;
    alice.send(Method::POST, &promote(&app, "bob"), None).await;
    let stepped = alice.send(Method::POST, &demote(&app, "alice"), None).await;
    assert_eq!(stepped.status, StatusCode::OK);
    // ... and then bob is the last.
    let bob = app.sign_in("code-bob").await;
    let refused = bob.send(Method::POST, &demote(&app, "bob"), None).await;
    assert_eq!(refused.status, StatusCode::CONFLICT);
    assert!(is_admin(&app, "bob").await);
    assert_eq!(
        alice.get("/api/v1/admin/users").await.status,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn two_admins_demoting_each_other_at_once_leave_one() {
    let app = world("alice");
    let alice = app.sign_in("code-alice").await;
    let bob = app.sign_in("code-bob").await;
    alice.send(Method::POST, &promote(&app, "bob"), None).await;
    let (demote_bob, demote_alice) = (demote(&app, "bob"), demote(&app, "alice"));
    let (a, b) = tokio::join!(
        alice.send(Method::POST, &demote_bob, None),
        bob.send(Method::POST, &demote_alice, None),
    );
    let statuses = [a.status, b.status];
    assert!(
        statuses.contains(&StatusCode::OK),
        "one demotion lands: {statuses:?}"
    );
    let admins = [is_admin(&app, "alice").await, is_admin(&app, "bob").await];
    assert_eq!(
        admins.iter().filter(|admin| **admin).count(),
        1,
        "exactly one admin remains: {admins:?}"
    );
}

// ---- who is turned away -----------------------------------------------------------

/// Every admin route, as (method, path), with a real user id filled in.
fn admin_requests(app: &TestApp) -> Vec<(Method, String)> {
    let target = uid(app, "bob");
    ADMIN_ROUTES
        .iter()
        .map(|route| {
            (
                Method::from_bytes(route.method.as_bytes()).unwrap(),
                catalog::admin_full_path(route).replace("{userId}", &target),
            )
        })
        .collect()
}

#[tokio::test]
async fn anonymous_requests_are_unauthorized_on_every_admin_route() {
    let app = world("alice");
    app.sign_in("code-bob").await;
    for (method, path) in admin_requests(&app) {
        let reply = app
            .call(
                Request::builder()
                    .method(&method)
                    .uri(&path)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await;
        assert_eq!(reply.status, StatusCode::UNAUTHORIZED, "{method} {path}");
        assert_eq!(reply.json()["code"], "unauthorized");
    }
}

#[tokio::test]
async fn a_signed_in_user_who_is_not_an_admin_gets_404_on_every_admin_route() {
    let app = world("alice");
    app.sign_in("code-alice").await;
    let bob = app.sign_in("code-bob").await;
    for (method, path) in admin_requests(&app) {
        let reply = bob.send(method.clone(), &path, None).await;
        assert_eq!(reply.status, StatusCode::NOT_FOUND, "{method} {path}");
        assert_eq!(reply.json()["code"], "not_found");
        assert!(
            !reply.text().contains("admin"),
            "the denial does not mention the admin API: {}",
            reply.text()
        );
    }
    // Nothing happened, in particular no self-promotion.
    assert!(!is_admin(&app, "bob").await);
    assert_eq!(audit(&app).await.len(), 1);
}

#[tokio::test]
async fn a_tenant_owner_is_not_a_platform_admin() {
    let app = world("alice");
    app.github.add_user(
        "code-owner",
        9,
        "owner9",
        vec![installation(
            900,
            "acme",
            "Organization",
            swarm_web::model::Role::Owner,
        )],
    );
    app.sign_in("code-alice").await;
    let owner = app.sign_in("code-owner").await;
    let tenant = owner.tenant_for("acme").await;
    assert_eq!(
        owner.get(&format!("/api/v1/tenants/{tenant}")).await.json()["role"],
        "owner"
    );
    assert_eq!(
        owner.get("/api/v1/admin/users").await.status,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn admin_changes_need_the_csrf_token_and_origin_like_every_other_write() {
    let app = world("alice");
    let alice = app.sign_in("code-alice").await;
    app.sign_in("code-bob").await;
    let path = promote(&app, "bob");
    let session = format!(
        "{}={}",
        swarm_web::auth::session_cookie_name(app.secure),
        alice.session
    );

    let no_token = app
        .call(
            Request::builder()
                .method(Method::POST)
                .uri(&path)
                .header(header::COOKIE, &session)
                .body(Body::empty())
                .unwrap(),
        )
        .await;
    assert_eq!(no_token.status, StatusCode::FORBIDDEN);
    assert_eq!(no_token.json()["code"], "csrf_token");

    let wrong_token = app
        .call(
            Request::builder()
                .method(Method::POST)
                .uri(&path)
                .header(header::COOKIE, &session)
                .header("x-csrf-token", "not-the-token")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
    assert_eq!(wrong_token.json()["code"], "csrf_token");

    let foreign_origin = app
        .call(
            Request::builder()
                .method(Method::POST)
                .uri(&path)
                .header(header::COOKIE, &session)
                .header(header::ORIGIN, "https://evil.example")
                .header("x-csrf-token", &alice.csrf)
                .body(Body::empty())
                .unwrap(),
        )
        .await;
    assert_eq!(foreign_origin.json()["code"], "csrf_origin");
    assert!(
        !is_admin(&app, "bob").await,
        "none of them changed anything"
    );
}

#[tokio::test]
async fn the_admin_api_never_returns_secrets_or_session_data() {
    let app = world("alice");
    let alice = app.sign_in("code-alice").await;
    app.sign_in("code-bob").await;
    let text = alice.get("/api/v1/admin/users").await.text();
    for forbidden in ["csrf", "token", "session", "key"] {
        assert!(
            !text.to_lowercase().contains(forbidden),
            "{forbidden}: {text}"
        );
    }
}

#[tokio::test]
async fn the_admin_routes_come_from_the_catalog_and_are_all_exercised_here() {
    assert_eq!(ADMIN_ROUTES.len(), 4);
    let app = world("alice");
    let alice = app.sign_in("code-alice").await;
    app.sign_in("code-bob").await;
    for (method, path) in admin_requests(&app) {
        let reply = alice.send(method.clone(), &path, None).await;
        assert_eq!(
            reply.status,
            StatusCode::OK,
            "{method} {path}: {}",
            reply.text()
        );
    }
}

#[tokio::test]
async fn the_audit_log_route_lists_newest_first_with_logins_and_no_internals() {
    let app = world("alice");
    let alice = app.sign_in("code-alice").await;
    app.sign_in("code-bob").await;
    alice.send(Method::POST, &promote(&app, "bob"), None).await;
    let reply = alice.get("/api/v1/admin/audit-log").await;
    assert_eq!(reply.status, StatusCode::OK, "{}", reply.text());
    let entries = reply.json()["entries"].as_array().unwrap().clone();
    let summary: Vec<_> = entries
        .iter()
        .map(|e| {
            (
                e["action"].as_str().unwrap(),
                e["actor_login"].as_str().unwrap(),
                e["target_login"].as_str().unwrap(),
            )
        })
        .collect();
    assert_eq!(
        summary,
        vec![
            ("admin.promote", "alice", "bob"),
            ("admin.bootstrap", "alice", "alice")
        ]
    );
    assert!(entries[0]["created_at"].is_u64());
    let text = reply.text().to_lowercase();
    for forbidden in ["csrf", "token", "session", "detail"] {
        assert!(!text.contains(forbidden), "{forbidden}: {text}");
    }
}
