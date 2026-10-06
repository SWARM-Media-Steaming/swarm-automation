//! Provider keys are write-only, encrypted at rest, never logged and reach a job
//! only for that job's provider. Every test seeds canary secrets and then looks
//! for them everywhere they could leak.

mod common;

use axum::body::Body;
use axum::http::{header, Method, Request, StatusCode};
use base64::Engine;
use common::*;
use serde_json::json;
use swarm_web::model::{Provider, Role, TenantId};
use swarm_web::secret::Secret;

// One shaped like a real key (the scrubber knows it), one with no recognizable
// shape at all (only "never logged by construction" can keep it out).
const KEY_CLAUDE: &str = "sk-ant-api03-CANARY-claude-0123456789";
const KEY_PLAIN: &str = "CANARYPLAIN0123456789abcdefghij";

fn encodings(secret: &str) -> Vec<String> {
    vec![
        secret.to_string(),
        base64::engine::general_purpose::STANDARD.encode(secret),
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(secret),
        hex::encode(secret),
    ]
}

fn assert_absent(haystack: &str, secret: &str, where_: &str) {
    for form in encodings(secret) {
        assert!(
            !haystack.contains(&form),
            "{where_} contains the secret ({} chars of it as {form:.12}…)",
            secret.len()
        );
    }
}

async fn owner(app: &TestApp) -> (Client<'_>, String) {
    app.github.add_user(
        "code-alice",
        1,
        "alice",
        vec![installation(100, "alice", "User", Role::Owner)],
    );
    let alice = app.sign_in("code-alice").await;
    let tenant = alice.tenant_for("alice").await;
    (alice, tenant)
}

#[tokio::test]
async fn adding_a_key_returns_metadata_and_never_the_key() {
    let app = TestApp::new();
    let (alice, tenant) = owner(&app).await;
    let base = format!("/api/v1/tenants/{tenant}/provider-keys");

    let put = alice
        .put(&format!("{base}/claude"), json!({ "key": KEY_CLAUDE }))
        .await;
    assert_eq!(put.status, StatusCode::OK);
    let meta = put.json();
    assert_eq!(meta["provider"], "claude");
    assert_eq!(meta["configured"], true);
    assert_eq!(meta["updated_by"], "alice");
    assert_eq!(meta["updated_at"], START);
    let fields: std::collections::BTreeSet<_> = meta.as_object().unwrap().keys().cloned().collect();
    assert_eq!(
        fields,
        ["configured", "provider", "updated_at", "updated_by"]
            .iter()
            .map(|s| s.to_string())
            .collect()
    );

    let list = alice.get(&base).await;
    let configured: Vec<_> = list.json()["keys"]
        .as_array()
        .unwrap()
        .iter()
        .map(|k| {
            (
                k["provider"].as_str().unwrap().to_string(),
                k["configured"].as_bool().unwrap(),
            )
        })
        .collect();
    assert_eq!(
        configured,
        vec![
            ("claude".into(), true),
            ("codex".into(), false),
            ("grok".into(), false),
            ("model-data".into(), false)
        ]
    );
    assert_absent(&list.text(), KEY_CLAUDE, "the key list");
    // There is no read route for a single key.
    assert_eq!(
        alice.get(&format!("{base}/claude")).await.status,
        StatusCode::METHOD_NOT_ALLOWED
    );

    // Replacing updates in place; deleting removes it; neither echoes it.
    let again = alice
        .put(
            &format!("{base}/claude"),
            json!({ "key": "  sk-ant-api03-SECOND-0123456789  " }),
        )
        .await;
    assert_eq!(
        again.status,
        StatusCode::OK,
        "surrounding whitespace from a paste is trimmed"
    );
    let gone = alice.delete(&format!("{base}/claude")).await;
    assert_eq!(gone.status, StatusCode::NO_CONTENT);
    assert_eq!(
        alice.delete(&format!("{base}/claude")).await.status,
        StatusCode::NOT_FOUND
    );
    assert!(!app
        .state
        .vault
        .is_configured(&TenantId::parse(&tenant).unwrap(), Provider::Claude)
        .await
        .unwrap());
}

#[tokio::test]
async fn invalid_keys_and_bodies_are_refused_without_echoing_them() {
    let app = TestApp::new();
    let (alice, tenant) = owner(&app).await;
    let base = format!("/api/v1/tenants/{tenant}/provider-keys");
    let bad_bodies = [
        json!({ "key": "has a space CANARYPLAIN-echo-1" }),
        json!({ "key": "short" }),
        json!({ "key": "line\nbreak-CANARYPLAIN-echo-2" }),
        json!({ "key": "k".repeat(5000) }),
        json!({ "key": ["CANARYPLAIN-echo-3-in-an-array"] }),
        json!({ "wrong": "CANARYPLAIN-echo-4" }),
    ];
    for body in bad_bodies {
        let reply = alice.put(&format!("{base}/claude"), body).await;
        assert_eq!(reply.status, StatusCode::BAD_REQUEST, "{}", reply.text());
        assert!(!reply.text().contains("CANARYPLAIN"), "{}", reply.text());
    }
    let malformed = alice
        .app
        .call(
            Request::builder()
                .method(Method::PUT)
                .uri(format!("{base}/claude"))
                .header(header::COOKIE, format!("swarm_session={}", alice.session))
                .header("x-csrf-token", &alice.csrf)
                .body(Body::from("{\"key\": \"CANARYPLAIN-echo-5\""))
                .unwrap(),
        )
        .await;
    assert_eq!(malformed.status, StatusCode::BAD_REQUEST);
    assert!(!malformed.text().contains("CANARYPLAIN"));
    let huge = alice
        .put(
            &format!("{base}/claude"),
            json!({ "key": "x".repeat(200_000) }),
        )
        .await;
    assert_eq!(
        huge.status,
        StatusCode::BAD_REQUEST,
        "an oversized body is cut off"
    );
    assert_eq!(
        alice
            .put(&format!("{base}/gemini"), json!({ "key": KEY_CLAUDE }))
            .await
            .status,
        StatusCode::BAD_REQUEST
    );
    assert!(!app
        .state
        .vault
        .is_configured(&TenantId::parse(&tenant).unwrap(), Provider::Claude)
        .await
        .unwrap());
}

#[tokio::test]
async fn the_store_holds_ciphertext_only() {
    let app = TestApp::new();
    let (alice, tenant) = owner(&app).await;
    for (provider, key) in [("claude", KEY_CLAUDE), ("codex", KEY_PLAIN)] {
        let reply = alice
            .put(
                &format!("/api/v1/tenants/{tenant}/provider-keys/{provider}"),
                json!({ "key": key }),
            )
            .await;
        assert_eq!(reply.status, StatusCode::OK);
    }
    let dump = app.store.debug_dump();
    for key in [KEY_CLAUDE, KEY_PLAIN] {
        assert_absent(&dump, key, "the store");
    }
    // What is stored is an envelope: a wrapped data key plus authenticated ciphertext.
    let stored = swarm_web::store::Store::provider_key(
        app.store.as_ref(),
        &TenantId::parse(&tenant).unwrap(),
        Provider::Claude,
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(stored.sealed.version, 1);
    assert!(stored.sealed.wrapper_key_id.starts_with("local:"));
    assert!(
        stored.sealed.wrapped_data_key.len() > 32
            && stored.sealed.ciphertext.len() > KEY_CLAUDE.len()
    );
}

#[tokio::test]
async fn keys_and_credentials_never_reach_the_logs() {
    for redacting in [false, true] {
        let app = TestApp::new();
        // The bare subscriber shows what the code tries to log; the real one
        // shows what survives the scrubber. Neither may contain a secret.
        let (logs, _guard) = capture_logs(redacting);
        let (alice, tenant) = owner(&app).await;
        let base = format!("/api/v1/tenants/{tenant}/provider-keys");
        let mut bodies = String::new();
        for (provider, key) in [("claude", KEY_CLAUDE), ("grok", KEY_PLAIN)] {
            bodies += &alice
                .put(&format!("{base}/{provider}"), json!({ "key": key }))
                .await
                .text();
        }
        bodies += &alice
            .put(
                &format!("{base}/codex"),
                json!({ "key": ["CANARYPLAIN-bad-shape"] }),
            )
            .await
            .text();
        bodies += &alice.get(&base).await.text();
        bodies += &alice
            .get(&format!("/api/v1/tenants/{tenant}/usage"))
            .await
            .text();
        alice.delete(&format!("{base}/grok")).await;
        let output = logs.text();

        assert!(
            output.contains("provider key stored"),
            "the action itself is logged: {output}"
        );
        assert!(output.contains(&tenant) && output.contains("claude"));
        for secret in [
            KEY_CLAUDE,
            KEY_PLAIN,
            "CANARYPLAIN-bad-shape",
            CLIENT_SECRET,
            WEBHOOK_SECRET,
            &alice.session,
            &alice.csrf,
            "gho_fake_code-alice",
        ] {
            assert_absent(&output, secret, &format!("the log (redacting={redacting})"));
        }
        assert!(
            !output.contains("code=code-alice"),
            "OAuth query strings are not logged"
        );
        assert_absent(&bodies, KEY_CLAUDE, "response bodies");
        assert_absent(&bodies, KEY_PLAIN, "response bodies");
    }
}

#[tokio::test]
async fn the_log_scrubber_is_a_safety_net_for_anything_that_slips_through() {
    let (logs, _guard) = capture_logs(true);
    tracing::info!(
        note = "authorization: Bearer abcdefghijklmnop0123",
        key = KEY_CLAUDE,
        "debugging a request"
    );
    tracing::error!(
        detail = "https://user:hunter2password@example.com/path?client_secret=topsecretvalue"
    );
    let output = logs.text();
    assert!(output.contains("debugging a request"));
    for leaked in [
        KEY_CLAUDE,
        "abcdefghijklmnop0123",
        "hunter2password",
        "topsecretvalue",
    ] {
        assert!(!output.contains(leaked), "{leaked} survived: {output}");
    }
}

#[tokio::test]
async fn a_job_receives_only_its_own_providers_key() {
    let app = TestApp::new();
    let (alice, tenant) = owner(&app).await;
    let t = TenantId::parse(&tenant).unwrap();
    let keys = [
        ("claude", "sk-ant-api03-JOB-CLAUDE-0123456789"),
        ("codex", "sk-proj-JOB-CODEX-0123456789"),
        ("grok", "xai-JOB-GROK-0123456789abcdef"),
        ("model-data", "aa-JOB-MODELDATA-0123456789"),
    ];
    for (provider, key) in keys {
        assert_eq!(
            alice
                .put(
                    &format!("/api/v1/tenants/{tenant}/provider-keys/{provider}"),
                    json!({ "key": key })
                )
                .await
                .status,
            StatusCode::OK
        );
    }
    let vault = &app.state.vault;

    let claude = vault
        .job_environment(&t, Provider::Claude, false)
        .await
        .unwrap();
    assert_eq!(claude.expose(), vec![("ANTHROPIC_API_KEY", keys[0].1)]);
    let codex = vault
        .job_environment(&t, Provider::Codex, false)
        .await
        .unwrap();
    assert_eq!(codex.expose(), vec![("OPENAI_API_KEY", keys[1].1)]);
    let grok = vault
        .job_environment(&t, Provider::Grok, true)
        .await
        .unwrap();
    assert_eq!(
        grok.expose(),
        vec![
            ("XAI_API_KEY", keys[2].1),
            ("ARTIFICIAL_ANALYSIS_API_KEY", keys[3].1)
        ]
    );
    for (env, others) in [
        (&claude, [1, 2, 3]),
        (&codex, [0, 2, 3]),
        (&grok, [0, 1, 3]),
    ] {
        let text = format!("{:?}", env.expose());
        for other in others
            .iter()
            .filter(|i| !(*i == &3 && env.names().len() == 2))
        {
            assert!(
                !text.contains(keys[*other].1),
                "another provider's key is in the environment"
            );
        }
        assert!(
            !format!("{env:?}").contains("sk-"),
            "the environment's Debug output hides values"
        );
    }

    // A tenant without the provider's key gets a refusal, not another key.
    app.state.vault.delete(&t, Provider::Codex).await.unwrap();
    let missing = vault
        .job_environment(&t, Provider::Codex, false)
        .await
        .expect_err("no codex key");
    assert!(!format!("{missing:?}").contains("sk-"));
    // The model-data key is optional for a job that does not need it.
    vault.delete(&t, Provider::ModelData).await.unwrap();
    assert_eq!(
        vault
            .job_environment(&t, Provider::Claude, true)
            .await
            .unwrap()
            .names(),
        vec!["ANTHROPIC_API_KEY"]
    );
}

#[tokio::test]
async fn debug_output_of_secret_bearing_values_is_redacted() {
    let secret = Secret::new(KEY_CLAUDE);
    assert!(!format!("{secret:?} {secret}").contains("CANARY"));
}
