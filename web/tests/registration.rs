//! First sign-in registers the user and a personal tenant (issue #440, part of
//! #413), through the real router: first login, repeat login, a renamed login,
//! an id collision, racing first logins and what the logs and errors may say.

mod common;

use std::sync::Arc;

use async_trait::async_trait;
use axum::http::StatusCode;
use common::*;
use swarm_web::identity::{IdentityError, IdentityProvider};
use swarm_web::model::{IdentityProfile, Role};
use swarm_web::secret::Secret;

fn tenants(body: &serde_json::Value) -> Vec<&serde_json::Value> {
    body["tenants"].as_array().unwrap().iter().collect()
}

#[tokio::test]
async fn a_first_sign_in_creates_a_personal_tenant_named_after_the_login() {
    let app = TestApp::new();
    // No App installation at all: the user still gets somewhere to work.
    app.github.add_user("code-octo", 7, "Octo-Cat", vec![]);
    let octo = app.sign_in("code-octo").await;

    let session = octo.get("/api/v1/session").await.json();
    assert_eq!(session["user"]["login"], "Octo-Cat");
    assert_eq!(session["user"]["display_name"], "Octo-Cat Display");
    assert_eq!(session["user"]["avatar_url"], "https://avatars.test/u/7");
    let listed = tenants(&session);
    assert_eq!(listed.len(), 1);
    assert_eq!(listed[0]["id"], "octo-cat", "the login, slugified");
    assert_eq!(listed[0]["account_type"], "Personal");
    assert_eq!(listed[0]["role"], "owner");
    assert_eq!(listed[0]["status"], "active");

    let id = octo.personal_tenant().await;
    assert_eq!(id, "octo-cat");
    let members = octo.get(&format!("/api/v1/tenants/{id}/members")).await;
    assert_eq!(members.json()["members"][0]["login"], "Octo-Cat");
    assert_eq!(members.json()["members"][0]["role"], "owner");
    // The owner can use it like any tenant: owner-only routes answer.
    let quotas = octo.get(&format!("/api/v1/tenants/{id}/quotas")).await;
    assert_eq!(quotas.status, StatusCode::OK);
}

#[tokio::test]
async fn a_personal_tenant_is_private_to_its_owner() {
    let app = TestApp::new();
    app.github.add_user("code-a", 1, "alice", vec![]);
    app.github.add_user("code-b", 2, "bob", vec![]);
    let alice = app.sign_in("code-a").await;
    let bob = app.sign_in("code-b").await;
    let a = alice.personal_tenant().await;
    let b = bob.personal_tenant().await;
    assert_ne!(a, b);
    for path in ["", "/members", "/quotas", "/provider-keys"] {
        let reply = bob.get(&format!("/api/v1/tenants/{a}{path}")).await;
        assert_eq!(reply.status, StatusCode::NOT_FOUND, "{path}");
    }
}

#[tokio::test]
async fn a_repeat_sign_in_reuses_the_user_and_tenant_and_refreshes_the_profile() {
    let app = TestApp::new();
    app.github.add_user("code-1", 7, "octocat", vec![]);
    let first = app.sign_in("code-1").await;
    let tenant = first.personal_tenant().await;
    let user = app.store.user_of_login("octocat");

    app.clock.advance(3600);
    let again = app.sign_in("code-1").await;
    assert_eq!(again.personal_tenant().await, tenant, "no second tenant");
    assert_eq!(app.store.user_of_login("octocat"), user, "no second user");
    assert_eq!(tenants(&again.get("/api/v1/session").await.json()).len(), 1);
    assert_ne!(first.session, again.session, "a new session each time");
}

#[tokio::test]
async fn a_renamed_github_login_keeps_the_tenant_id() {
    let app = TestApp::new();
    app.github.add_user("code-old", 7, "octocat", vec![]);
    let before = app.sign_in("code-old").await;
    let tenant = before.personal_tenant().await;
    assert_eq!(tenant, "octocat");

    // Same GitHub id, new login.
    app.github.add_user("code-new", 7, "monalisa", vec![]);
    let after = app.sign_in("code-new").await;
    assert_eq!(
        after.personal_tenant().await,
        "octocat",
        "the id never changes"
    );
    let session = after.get("/api/v1/session").await.json();
    assert_eq!(session["user"]["login"], "monalisa");
    assert_eq!(tenants(&session)[0]["account_login"], "monalisa");
    assert_eq!(tenants(&session).len(), 1, "still one tenant");
    // The same user: the old session is theirs too.
    let members = after
        .get(&format!("/api/v1/tenants/{tenant}/members"))
        .await
        .json();
    assert_eq!(members["members"][0]["login"], "monalisa");
}

#[tokio::test]
async fn a_taken_tenant_id_is_deduplicated() {
    let app = TestApp::new();
    app.github.add_user("code-1", 1, "octocat", vec![]);
    app.github.add_user("code-2", 2, "Octocat", vec![]);
    app.github.add_user("code-3", 3, "octo.cat", vec![]);
    let one = app.sign_in("code-1").await.personal_tenant().await;
    let two = app.sign_in("code-2").await.personal_tenant().await;
    let three = app.sign_in("code-3").await.personal_tenant().await;
    assert_eq!(one, "octocat");
    assert_eq!(two, "octocat-2");
    assert_eq!(three, "octo-cat");
}

#[tokio::test]
async fn racing_first_sign_ins_make_one_user_and_one_tenant() {
    let app = TestApp::new();
    app.github.add_user("code-1", 7, "octocat", vec![]);
    let (a, b, c, d) = tokio::join!(
        app.callback_for("github", "code-1"),
        app.callback_for("github", "code-1"),
        app.callback_for("github", "code-1"),
        app.callback_for("github", "code-1"),
    );
    for reply in [&a, &b, &c, &d] {
        assert_eq!(reply.status, StatusCode::FOUND, "{}", reply.text());
    }
    assert_eq!(app.store.user_count(), 1);
    assert_eq!(app.store.tenant_count(), 1);
    let again = app.sign_in("code-1").await;
    assert_eq!(again.personal_tenant().await, "octocat");
}

#[tokio::test]
async fn installations_are_added_beside_the_personal_tenant_and_do_not_drop_it() {
    let app = TestApp::new();
    app.github.add_user(
        "code-1",
        7,
        "octocat",
        vec![installation(100, "acme", "Organization", Role::Member)],
    );
    let octo = app.sign_in("code-1").await;
    let personal = octo.personal_tenant().await;
    let acme = octo.tenant_for("acme").await;
    assert_ne!(personal, acme);
    assert_eq!(tenants(&octo.get("/api/v1/session").await.json()).len(), 2);

    // Access GitHub no longer grants is dropped; the personal tenant is theirs.
    app.github.set_installations("code-1", vec![]);
    let later = app.sign_in("code-1").await;
    let session = later.get("/api/v1/session").await.json();
    assert_eq!(tenants(&session).len(), 1);
    assert_eq!(tenants(&session)[0]["id"], personal.as_str());
    assert_eq!(tenants(&session)[0]["role"], "owner");
}

#[tokio::test]
async fn a_member_of_an_installation_is_still_owner_of_their_own_tenant() {
    let app = TestApp::new();
    app.github.add_user(
        "code-1",
        7,
        "octocat",
        vec![installation(100, "acme", "Organization", Role::Member)],
    );
    let octo = app.sign_in("code-1").await;
    let roles: Vec<_> = tenants(&octo.get("/api/v1/session").await.json())
        .iter()
        .map(|t| {
            (
                t["account_type"].as_str().unwrap().to_string(),
                t["role"].as_str().unwrap().to_string(),
            )
        })
        .collect();
    assert!(
        roles.contains(&("Personal".into(), "owner".into())),
        "{roles:?}"
    );
    assert!(
        roles.contains(&("Organization".into(), "member".into())),
        "{roles:?}"
    );
}

#[tokio::test]
async fn no_token_or_secret_reaches_the_logs_or_an_error() {
    let (logs, _guard) = capture_logs(false);
    let app = TestApp::new();
    app.github.add_user("code-secret", 7, "octocat", vec![]);
    let ok = app.sign_in("code-secret").await;
    let _ = app.sign_in("code-secret").await;
    let failed = app.callback_for("github", "code-unknown").await;
    assert_eq!(failed.status, StatusCode::BAD_GATEWAY);
    let text = logs.text();
    for forbidden in [
        "gho_fake_",
        "code-secret",
        CLIENT_SECRET,
        WEBHOOK_SECRET,
        ok.session.as_str(),
        ok.csrf.as_str(),
    ] {
        assert!(
            !text.contains(forbidden),
            "logs contain {forbidden}: {text}"
        );
        assert!(
            !failed.text().contains(forbidden),
            "error contains {forbidden}"
        );
    }
    assert!(text.contains("signed in"), "{text}");
}

#[tokio::test]
async fn there_is_no_password_or_email_login_and_unknown_providers_are_not_found() {
    let app = TestApp::new();
    for path in [
        "/api/v1/auth/password/login",
        "/api/v1/auth/email/login",
        "/api/v1/auth/password/callback?code=x&state=y",
        "/api/v1/auth/login",
        "/api/v1/login",
        "/api/v1/register",
        "/api/v1/auth/register",
    ] {
        let reply = app.get(path).await;
        assert_eq!(reply.status, StatusCode::NOT_FOUND, "{path}");
    }
    for method in [axum::http::Method::POST, axum::http::Method::PUT] {
        let reply = app
            .call(
                axum::http::Request::builder()
                    .method(method)
                    .uri("/api/v1/auth/password/login")
                    .body(axum::body::Body::from(r#"{"password":"x"}"#))
                    .unwrap(),
            )
            .await;
        assert!(
            reply.status == StatusCode::NOT_FOUND || reply.status == StatusCode::METHOD_NOT_ALLOWED,
            "{}",
            reply.status
        );
    }
}

#[tokio::test]
async fn a_profile_the_provider_left_incomplete_registers_nobody() {
    let app = TestApp::new();
    app.github.add_user("code-blank", 7, "   ", vec![]);
    let reply = app.callback_for("github", "code-blank").await;
    assert_eq!(reply.status, StatusCode::BAD_GATEWAY);
    assert_eq!(app.store.user_count(), 0);
    assert_eq!(app.store.tenant_count(), 0);
}

/// A second OIDC-style provider, to prove the callback is not GitHub's alone.
struct Example;

#[async_trait]
impl IdentityProvider for Example {
    fn id(&self) -> &'static str {
        "example"
    }

    fn authorize_url(&self, redirect_uri: &str, state: &str, code_challenge: &str) -> String {
        format!("https://idp.example/authorize?redirect_uri={redirect_uri}&state={state}&code_challenge={code_challenge}")
    }

    async fn exchange_code(
        &self,
        code: &str,
        _redirect_uri: &str,
        _code_verifier: &str,
    ) -> Result<Secret, IdentityError> {
        Ok(Secret::new(format!("tok_{code}")))
    }

    async fn profile(&self, token: &Secret) -> Result<IdentityProfile, IdentityError> {
        Ok(IdentityProfile {
            provider: "example".into(),
            subject: "sub-123".into(),
            login: format!("Pat {}", token.expose().len()),
            display_name: None,
            avatar_url: None,
        })
    }
}

#[tokio::test]
async fn another_provider_registers_through_the_same_callback() {
    let app = TestApp::with_identity_provider(Arc::new(Example));
    let login = app.get("/api/v1/auth/example/login").await;
    assert_eq!(login.status, StatusCode::FOUND);
    assert!(login
        .location()
        .starts_with("https://idp.example/authorize?"));
    assert!(login
        .location()
        .contains("http://swarm.test/api/v1/auth/example/callback"));

    let pat = app.sign_in_with("example", "abc").await;
    let tenant = pat.personal_tenant().await;
    assert_eq!(tenant, "pat-7");
    let session = pat.get("/api/v1/session").await.json();
    assert_eq!(session["user"]["login"], "Pat 7");
    // GitHub is still there, and an unconfigured name still is not.
    assert_eq!(
        app.get("/api/v1/auth/github/login").await.status,
        StatusCode::FOUND
    );
    assert_eq!(
        app.get("/api/v1/auth/nope/login").await.status,
        StatusCode::NOT_FOUND
    );
}
