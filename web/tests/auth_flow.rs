//! Sign-in with GitHub, cookie sessions and CSRF.

mod common;

use axum::body::Body;
use axum::http::{header, Method, Request, StatusCode};
use common::*;
use serde_json::json;
use swarm_web::github::pkce_challenge;
use swarm_web::model::Role;

fn alice(app: &TestApp) {
    app.github.add_user(
        "code-alice",
        1,
        "alice",
        vec![installation(100, "alice", "User", Role::Owner)],
    );
}

#[tokio::test]
async fn a_user_signs_in_with_github_and_sees_their_tenant() {
    let app = TestApp::new();
    alice(&app);
    let alice = app.sign_in("code-alice").await;

    let session = alice.get("/api/v1/session").await;
    assert_eq!(session.status, StatusCode::OK);
    let body = session.json();
    assert_eq!(body["authenticated"], true);
    assert_eq!(body["user"]["login"], "alice");
    assert_eq!(body["csrf_token"], alice.csrf.as_str());
    let tenants = body["tenants"].as_array().unwrap();
    assert_eq!(tenants.len(), 1);
    assert_eq!(tenants[0]["account_login"], "alice");
    assert_eq!(tenants[0]["role"], "owner");
    assert_eq!(tenants[0]["status"], "active");

    let id = alice.tenant_for("alice").await;
    let tenant = alice.get(&format!("/api/v1/tenants/{id}")).await;
    assert_eq!(tenant.status, StatusCode::OK);
    assert_eq!(tenant.json()["id"], id.as_str());
}

#[tokio::test]
async fn the_login_redirect_binds_state_and_pkce_to_the_browser() {
    let app = TestApp::new();
    alice(&app);
    let login = app.get("/api/v1/auth/github/login").await;
    assert_eq!(login.status, StatusCode::FOUND);
    let url = url::Url::parse(&login.location()).unwrap();
    assert_eq!(url.host_str(), Some("github.com"));
    assert_eq!(url.path(), "/login/oauth/authorize");
    let q: std::collections::HashMap<_, _> = url.query_pairs().into_owned().collect();
    assert_eq!(q["client_id"], "Iv1.testclient");
    assert_eq!(
        q["redirect_uri"],
        "http://swarm.test/api/v1/auth/github/callback"
    );
    assert_eq!(q["code_challenge_method"], "S256");
    assert!(q["state"].len() >= 40, "state is 256 bits");

    let cookie = login.cookie_header("swarm_oauth").unwrap();
    assert!(
        cookie.contains("HttpOnly")
            && cookie.contains("SameSite=Lax")
            && cookie.contains("Path=/api/v1/auth")
    );
    assert!(cookie.contains("Max-Age=600"));

    let oauth = login.cookie("swarm_oauth").unwrap();
    let done = app
        .call(
            Request::builder()
                .uri(format!(
                    "/api/v1/auth/github/callback?code=code-alice&state={}",
                    q["state"]
                ))
                .header(header::COOKIE, format!("swarm_oauth={oauth}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await;
    assert_eq!(done.status, StatusCode::FOUND);
    let (_, redirect_uri, verifier) = app
        .github
        .exchanges
        .lock()
        .unwrap()
        .last()
        .cloned()
        .unwrap();
    assert_eq!(
        pkce_challenge(&verifier),
        q["code_challenge"],
        "the verifier proves the challenge"
    );
    assert_eq!(redirect_uri, q["redirect_uri"]);
    assert!(
        done.cookie_header("swarm_oauth")
            .unwrap()
            .contains("Max-Age=0"),
        "the one-shot cookie is cleared"
    );
}

#[tokio::test]
async fn a_callback_that_does_not_match_the_browser_is_refused() {
    let app = TestApp::new();
    alice(&app);
    let login = app.get("/api/v1/auth/github/login").await;
    let oauth = login.cookie("swarm_oauth").unwrap();
    let callback = |query: &str, cookie: Option<String>| {
        let mut builder = Request::builder().uri(format!("/api/v1/auth/github/callback{query}"));
        if let Some(cookie) = cookie {
            builder = builder.header(header::COOKIE, cookie);
        }
        builder.body(Body::empty()).unwrap()
    };

    // A forged state (the classic login-CSRF), a missing cookie, a denied
    // authorization and a missing code each fail without creating a session.
    let forged = app
        .call(callback(
            "?code=code-alice&state=forged",
            Some(format!("swarm_oauth={oauth}")),
        ))
        .await;
    assert_eq!(forged.status, StatusCode::BAD_REQUEST);
    let no_cookie = app
        .call(callback("?code=code-alice&state=whatever", None))
        .await;
    assert_eq!(no_cookie.status, StatusCode::BAD_REQUEST);
    let denied = app
        .call(callback(
            "?error=access_denied",
            Some(format!("swarm_oauth={oauth}")),
        ))
        .await;
    assert_eq!(denied.status, StatusCode::BAD_REQUEST);
    let incomplete = app
        .call(callback("?state=x", Some(format!("swarm_oauth={oauth}"))))
        .await;
    assert_eq!(incomplete.status, StatusCode::BAD_REQUEST);
    for reply in [&forged, &no_cookie, &denied, &incomplete] {
        assert!(reply.cookie("swarm_session").is_none());
    }
    assert!(
        app.github.exchanges.lock().unwrap().is_empty(),
        "GitHub was never asked to exchange a code"
    );
}

#[tokio::test]
async fn a_rejected_code_is_a_gateway_error_that_leaks_no_detail() {
    let app = TestApp::new();
    let login = app.get("/api/v1/auth/github/login").await;
    let state = url::Url::parse(&login.location())
        .unwrap()
        .query_pairs()
        .find(|(k, _)| k == "state")
        .unwrap()
        .1
        .to_string();
    let oauth = login.cookie("swarm_oauth").unwrap();
    let reply = app
        .call(
            Request::builder()
                .uri(format!(
                    "/api/v1/auth/github/callback?code=unknown&state={state}"
                ))
                .header(header::COOKIE, format!("swarm_oauth={oauth}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await;
    assert_eq!(reply.status, StatusCode::BAD_GATEWAY);
    assert!(!reply.text().contains("bad_verification_code"));
}

#[tokio::test]
async fn session_cookies_are_httponly_and_no_token_reaches_the_page() {
    let app = TestApp::new();
    alice(&app);
    let login = app.get("/api/v1/auth/github/login").await;
    let state = url::Url::parse(&login.location())
        .unwrap()
        .query_pairs()
        .find(|(k, _)| k == "state")
        .unwrap()
        .1
        .to_string();
    let oauth = login.cookie("swarm_oauth").unwrap();
    let done = app
        .call(
            Request::builder()
                .uri(format!(
                    "/api/v1/auth/github/callback?code=code-alice&state={state}"
                ))
                .header(header::COOKIE, format!("swarm_oauth={oauth}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await;
    let session = done.cookie_header("swarm_session").unwrap();
    assert!(
        session.contains("HttpOnly")
            && session.contains("SameSite=Lax")
            && session.contains("Path=/")
    );
    assert!(!session.contains("Domain"));
    // Only the CSRF token is readable by script; the session id never is.
    let csrf = done.cookie_header("swarm_csrf").unwrap();
    assert!(!csrf.contains("HttpOnly"));
    let everything = format!("{:?}{}", done.headers, done.text());
    assert!(
        !everything.contains("gho_"),
        "the user's GitHub token is never forwarded"
    );
    assert_eq!(done.location(), "/");
}

#[tokio::test]
async fn over_https_the_cookies_are_secure_and_host_prefixed() {
    let app = TestApp::with_url("https://swarm.example.com");
    alice(&app);
    let login = app.get("/api/v1/auth/github/login").await;
    assert!(login
        .cookie_header("__Secure-swarm_oauth")
        .unwrap()
        .contains("Secure"));
    let alice = app.sign_in("code-alice").await;
    let reply = alice.get("/api/v1/tenants").await;
    assert_eq!(reply.status, StatusCode::OK);
    let me = app.get("/api/v1/health").await;
    assert!(me.headers.get("strict-transport-security").is_some());
}

#[tokio::test]
async fn state_changing_requests_need_the_csrf_token_and_a_same_site_origin() {
    let app = TestApp::new();
    alice(&app);
    let alice = app.sign_in("code-alice").await;
    let id = alice.tenant_for("alice").await;
    let path = format!("/api/v1/tenants/{id}/budgets");
    let body = json!({ "minimum_remaining_percent": 20.0, "provider_budgets_usd": {} });
    let cookie = format!("swarm_session={}", alice.session);
    let attempt = |token: Option<&str>, origin: Option<&str>| {
        let mut builder = Request::builder()
            .method(Method::PUT)
            .uri(&path)
            .header(header::COOKIE, &cookie)
            .header(header::CONTENT_TYPE, "application/json");
        if let Some(token) = token {
            builder = builder.header("x-csrf-token", token);
        }
        if let Some(origin) = origin {
            builder = builder.header(header::ORIGIN, origin);
        }
        builder.body(Body::from(body.to_string())).unwrap()
    };

    let missing = app.call(attempt(None, Some("http://swarm.test"))).await;
    assert_eq!(
        (missing.status, missing.json()["code"].clone()),
        (StatusCode::FORBIDDEN, json!("csrf_token"))
    );
    let wrong = app
        .call(attempt(Some("not-the-token"), Some("http://swarm.test")))
        .await;
    assert_eq!(wrong.status, StatusCode::FORBIDDEN);
    let cross_site = app
        .call(attempt(Some(&alice.csrf), Some("https://evil.example")))
        .await;
    assert_eq!(
        (cross_site.status, cross_site.json()["code"].clone()),
        (StatusCode::FORBIDDEN, json!("csrf_origin"))
    );
    let same_length = "x".repeat(alice.csrf.len());
    assert_eq!(
        app.call(attempt(Some(&same_length), None)).await.status,
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        app.call(attempt(Some(&alice.csrf), Some("http://swarm.test")))
            .await
            .status,
        StatusCode::OK
    );
    assert_eq!(
        app.call(attempt(Some(&alice.csrf), None)).await.status,
        StatusCode::OK,
        "non-browser clients send no Origin"
    );

    // Safe methods need no token, and the rejected requests changed nothing.
    assert_eq!(
        alice
            .get(&format!("/api/v1/tenants/{id}/quotas"))
            .await
            .json()["budgets"]["minimum_remaining_percent"],
        20.0
    );
}

#[tokio::test]
async fn requests_without_a_valid_session_are_unauthorized() {
    let app = TestApp::new();
    alice(&app);
    assert_eq!(
        app.get("/api/v1/tenants").await.status,
        StatusCode::UNAUTHORIZED
    );
    let forged = app
        .call(
            Request::builder()
                .uri("/api/v1/tenants")
                .header(header::COOKIE, "swarm_session=forged")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
    assert_eq!(forged.status, StatusCode::UNAUTHORIZED);
    let anonymous = app.get("/api/v1/session").await.json();
    assert_eq!(anonymous["authenticated"], false);
    assert_eq!(anonymous["login_url"], "/api/v1/auth/github/login");
    assert_eq!(
        anonymous["install_url"],
        "https://github.com/apps/swarm-test/installations/new"
    );
    assert!(anonymous.get("csrf_token").is_none());
}

#[tokio::test]
async fn logout_ends_the_session_everywhere() {
    let app = TestApp::new();
    alice(&app);
    let alice = app.sign_in("code-alice").await;
    let no_csrf = app
        .call(
            Request::builder()
                .method(Method::POST)
                .uri("/api/v1/auth/logout")
                .header(header::COOKIE, format!("swarm_session={}", alice.session))
                .body(Body::empty())
                .unwrap(),
        )
        .await;
    assert_eq!(
        no_csrf.status,
        StatusCode::FORBIDDEN,
        "logout is CSRF-protected too"
    );
    let out = alice.send(Method::POST, "/api/v1/auth/logout", None).await;
    assert_eq!(out.status, StatusCode::NO_CONTENT);
    assert!(out
        .cookie_header("swarm_session")
        .unwrap()
        .contains("Max-Age=0"));
    assert_eq!(
        alice.get("/api/v1/tenants").await.status,
        StatusCode::UNAUTHORIZED,
        "the old cookie is dead server-side"
    );
}

#[tokio::test]
async fn sessions_expire_and_each_sign_in_mints_a_new_one() {
    let app = TestApp::new();
    alice(&app);
    let first = app.sign_in("code-alice").await;
    let second = app.sign_in("code-alice").await;
    assert_ne!(first.session, second.session);
    assert_ne!(first.csrf, second.csrf);
    assert!(
        !app.store.debug_dump().contains(&first.session),
        "only a hash of the session id is stored"
    );

    app.clock.advance(app.state.config.session_ttl_secs - 1);
    assert_eq!(first.get("/api/v1/tenants").await.status, StatusCode::OK);
    app.clock.advance(1);
    assert_eq!(
        first.get("/api/v1/tenants").await.status,
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        first.get("/api/v1/session").await.json()["authenticated"],
        false
    );
}
