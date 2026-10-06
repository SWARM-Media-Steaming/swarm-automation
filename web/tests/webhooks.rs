//! GitHub webhooks: signature verification and idempotent delivery.

mod common;

use axum::body::Body;
use axum::http::{header, Method, Request, StatusCode};
use common::*;
use serde_json::json;
use swarm_web::model::TenantStatus;
use swarm_web::store::Store;

fn installation_event(action: &str, id: u64, login: &str) -> String {
    json!({ "action": action, "installation": { "id": id, "account": { "login": login, "type": "Organization" } } }).to_string()
}

fn request_with(
    signature: Option<String>,
    event: Option<&str>,
    delivery: Option<&str>,
    body: &str,
) -> Request<Body> {
    let mut builder = Request::builder()
        .method(Method::POST)
        .uri("/api/v1/webhooks/github")
        .header(header::CONTENT_TYPE, "application/json");
    if let Some(signature) = signature {
        builder = builder.header("x-hub-signature-256", signature);
    }
    if let Some(event) = event {
        builder = builder.header("x-github-event", event);
    }
    if let Some(delivery) = delivery {
        builder = builder.header("x-github-delivery", delivery);
    }
    builder.body(Body::from(body.to_string())).unwrap()
}

fn sign(body: &str) -> String {
    swarm_web::github::sign_webhook(WEBHOOK_SECRET.as_bytes(), body.as_bytes())
}

#[tokio::test]
async fn a_correctly_signed_event_is_processed() {
    let app = TestApp::new();
    let ping = app
        .call(signed_webhook(
            "ping",
            "d-0000-ping",
            r#"{"zen":"Keep it logically awesome."}"#,
        ))
        .await;
    assert_eq!(ping.status, StatusCode::OK);
    assert_eq!(ping.json()["status"], "processed");

    let created = app
        .call(signed_webhook(
            "installation",
            "d-0001-created",
            &installation_event("created", 555, "acme"),
        ))
        .await;
    assert_eq!(created.json()["detail"], "installation_created");
    let tenant = app
        .store
        .tenant_by_installation(555)
        .await
        .unwrap()
        .expect("tenant created for the installation");
    assert_eq!(
        (tenant.account_login.as_str(), tenant.status),
        ("acme", TenantStatus::Active)
    );
}

#[tokio::test]
async fn bad_signatures_are_refused_before_anything_is_stored() {
    let app = TestApp::new();
    let body = installation_event("created", 777, "evil-corp");
    let other_body = installation_event("created", 778, "other");
    let wrong_secret = swarm_web::github::sign_webhook(b"not-the-secret", body.as_bytes());
    let attempts = [
        ("missing", None),
        ("empty", Some(String::new())),
        ("not hex", Some("sha256=zzzz".to_string())),
        ("no prefix", Some(sign(&body).replace("sha256=", ""))),
        ("sha1 prefix", Some(sign(&body).replace("sha256=", "sha1="))),
        ("wrong secret", Some(wrong_secret)),
        ("signature of another body", Some(sign(&other_body))),
        ("truncated", Some(sign(&body)[..40].to_string())),
    ];
    for (label, signature) in attempts {
        let reply = app
            .call(request_with(
                signature,
                Some("installation"),
                Some("d-bad-0001"),
                &body,
            ))
            .await;
        assert_eq!(reply.status, StatusCode::UNAUTHORIZED, "{label}");
        assert_eq!(reply.json()["code"], "invalid_signature", "{label}");
    }
    assert!(
        app.store
            .tenant_by_installation(777)
            .await
            .unwrap()
            .is_none(),
        "nothing was processed"
    );
    // The rejected attempts did not burn the delivery id: the genuine one still goes through.
    let genuine = app
        .call(signed_webhook("installation", "d-bad-0001", &body))
        .await;
    assert_eq!(genuine.json()["status"], "processed");
}

#[tokio::test]
async fn a_tampered_body_does_not_verify() {
    let app = TestApp::new();
    let body = installation_event("created", 801, "acme");
    let signature = sign(&body);
    let tampered = body.replace("acme", "evil");
    let reply = app
        .call(request_with(
            Some(signature),
            Some("installation"),
            Some("d-tamper-01"),
            &tampered,
        ))
        .await;
    assert_eq!(reply.status, StatusCode::UNAUTHORIZED);
    assert!(app
        .store
        .tenant_by_installation(801)
        .await
        .unwrap()
        .is_none());
}

#[tokio::test]
async fn the_same_delivery_is_processed_exactly_once() {
    let app = TestApp::new();
    let suspend = installation_event("suspend", 900, "acme");
    app.call(signed_webhook(
        "installation",
        "d-create-900",
        &installation_event("created", 900, "acme"),
    ))
    .await;
    let first = app
        .call(signed_webhook("installation", "d-suspend-900", &suspend))
        .await;
    assert_eq!(first.json()["detail"], "installation_status");
    let tenant = app
        .store
        .tenant_by_installation(900)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(tenant.status, TenantStatus::Suspended);

    // Someone reinstates the tenant; GitHub redelivers the old suspend (same id).
    app.store
        .set_tenant_status(&tenant.id, TenantStatus::Active)
        .await
        .unwrap();
    let again = app
        .call(signed_webhook("installation", "d-suspend-900", &suspend))
        .await;
    assert_eq!(
        again.status,
        StatusCode::OK,
        "a duplicate is acknowledged so GitHub stops retrying"
    );
    assert_eq!(again.json()["status"], "duplicate");
    assert_eq!(
        app.store.tenant(&tenant.id).await.unwrap().unwrap().status,
        TenantStatus::Active,
        "and not re-applied"
    );
}

#[tokio::test]
async fn a_captured_payload_replayed_under_a_new_delivery_id_is_refused() {
    let app = TestApp::new();
    app.call(signed_webhook(
        "installation",
        "d-create-910",
        &installation_event("created", 910, "acme"),
    ))
    .await;
    let suspend = installation_event("suspend", 910, "acme");
    assert_eq!(
        app.call(signed_webhook("installation", "d-suspend-910", &suspend))
            .await
            .status,
        StatusCode::OK
    );
    let tenant = app
        .store
        .tenant_by_installation(910)
        .await
        .unwrap()
        .unwrap();
    app.store
        .set_tenant_status(&tenant.id, TenantStatus::Active)
        .await
        .unwrap();

    // The attacker holds a valid signed body but cannot re-sign it; they can
    // only change the delivery id header.
    let replay = app
        .call(signed_webhook("installation", "d-attacker-910", &suspend))
        .await;
    assert_eq!(replay.status, StatusCode::CONFLICT);
    assert_eq!(replay.json()["code"], "replay");
    assert_eq!(
        app.store.tenant(&tenant.id).await.unwrap().unwrap().status,
        TenantStatus::Active
    );

    // Genuinely different events from GitHub still flow.
    let unsuspend = installation_event("unsuspend", 910, "acme");
    assert_eq!(
        app.call(signed_webhook(
            "installation",
            "d-unsuspend-910",
            &unsuspend
        ))
        .await
        .json()["status"],
        "processed"
    );
}

#[tokio::test]
async fn a_released_claim_lets_a_retry_run() {
    // The handler releases the claim when processing fails so GitHub's retry is
    // not swallowed as a duplicate; the store contract it relies on:
    let app = TestApp::new();
    let store = &app.store;
    assert_eq!(
        store.claim_delivery("d-1", "hash-1").await.unwrap(),
        swarm_web::model::DeliveryClaim::New
    );
    assert_eq!(
        store.claim_delivery("d-1", "hash-1").await.unwrap(),
        swarm_web::model::DeliveryClaim::Duplicate
    );
    assert_eq!(
        store.claim_delivery("d-2", "hash-1").await.unwrap(),
        swarm_web::model::DeliveryClaim::Replay
    );
    store.release_delivery("d-1").await.unwrap();
    assert_eq!(
        store.claim_delivery("d-1", "hash-1").await.unwrap(),
        swarm_web::model::DeliveryClaim::New
    );
}

#[tokio::test]
async fn malformed_requests_are_rejected_after_the_signature_check() {
    let app = TestApp::new();
    let body = installation_event("created", 920, "acme");
    let cases = [
        request_with(Some(sign(&body)), Some("installation"), None, &body),
        request_with(Some(sign(&body)), None, Some("d-ok-0001"), &body),
        request_with(
            Some(sign(&body)),
            Some("install ation!"),
            Some("d-ok-0002"),
            &body,
        ),
        request_with(
            Some(sign(&body)),
            Some("installation"),
            Some("../../etc"),
            &body,
        ),
        request_with(
            Some(sign("not json")),
            Some("ping"),
            Some("d-ok-0003"),
            "not json",
        ),
    ];
    for request in cases {
        assert_eq!(app.call(request).await.status, StatusCode::BAD_REQUEST);
    }
    assert!(app
        .store
        .tenant_by_installation(920)
        .await
        .unwrap()
        .is_none());
    // A malformed attempt never consumed the delivery id.
    assert_eq!(
        app.call(signed_webhook("installation", "d-ok-0002", &body))
            .await
            .status,
        StatusCode::OK
    );
}

#[tokio::test]
async fn events_for_unknown_or_inactive_tenants_are_acknowledged_and_ignored() {
    let app = TestApp::new();
    let issue =
        json!({ "action": "opened", "installation": { "id": 4242 }, "issue": { "number": 1 } })
            .to_string();
    let unknown = app
        .call(signed_webhook("issues", "d-issue-0001", &issue))
        .await;
    assert_eq!(
        (unknown.status, unknown.json()["detail"].clone()),
        (StatusCode::OK, json!("unknown_installation"))
    );

    app.call(signed_webhook(
        "installation",
        "d-inst-4242c",
        &installation_event("created", 4242, "acme"),
    ))
    .await;
    let known = app
        .call(signed_webhook(
            "issues",
            "d-issue-0002",
            &json!({ "action": "edited", "installation": { "id": 4242 } }).to_string(),
        ))
        .await;
    assert_eq!(known.json()["detail"], "recorded");
    app.call(signed_webhook(
        "installation",
        "d-inst-4242d",
        &installation_event("deleted", 4242, "acme"),
    ))
    .await;
    let after = app
        .call(signed_webhook(
            "issues",
            "d-issue-0003",
            &json!({ "action": "labeled", "installation": { "id": 4242 } }).to_string(),
        ))
        .await;
    assert_eq!(after.json()["detail"], "tenant_inactive");
    assert_eq!(
        app.store
            .tenant_by_installation(4242)
            .await
            .unwrap()
            .unwrap()
            .status,
        TenantStatus::Deleted
    );
    let no_installation = app
        .call(signed_webhook(
            "issues",
            "d-issue-0004",
            r#"{"action":"opened"}"#,
        ))
        .await;
    assert_eq!(no_installation.json()["detail"], "no_installation");
}

#[tokio::test]
async fn the_endpoint_is_post_only_and_bounded() {
    let app = TestApp::new();
    assert_eq!(
        app.get("/api/v1/webhooks/github").await.status,
        StatusCode::METHOD_NOT_ALLOWED
    );
    let big = format!("{{\"pad\":\"{}\"}}", "a".repeat(5 * 1024 * 1024));
    let reply = app.call(signed_webhook("ping", "d-big-0001", &big)).await;
    assert_eq!(reply.status, StatusCode::PAYLOAD_TOO_LARGE);
}

#[tokio::test]
async fn webhook_bodies_and_signatures_are_not_logged() {
    let app = TestApp::new();
    let (logs, _guard) = capture_logs(false);
    let body = json!({ "action": "created", "marker": "WEBHOOK-BODY-MARKER", "installation": { "id": 930, "account": { "login": "acme", "type": "User" } } }).to_string();
    app.call(signed_webhook("installation", "d-log-0001", &body))
        .await;
    app.call(request_with(
        Some(sign("x")),
        Some("installation"),
        Some("d-log-0002"),
        &body,
    ))
    .await;
    let output = logs.text();
    assert!(output.contains("webhook processed") && output.contains("invalid signature"));
    assert!(!output.contains("WEBHOOK-BODY-MARKER"));
    assert!(!output.contains(&sign(&body)) && !output.contains(WEBHOOK_SECRET));
}
