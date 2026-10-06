//! `POST /api/v1/webhooks/github`: signature-verified, idempotent deliveries.
//!
//! Order matters. The signature is checked over the raw body before anything
//! else touches the store, so an unsigned request cannot cost a write. Then the
//! delivery is *claimed* (by `X-GitHub-Delivery`, and by payload hash so a
//! captured body replayed under a fresh id is refused too). A failure while
//! processing releases the claim and answers 5xx, so GitHub's retry or manual
//! redelivery runs again instead of being swallowed as a duplicate.

use axum::body::Bytes;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};

use crate::crypto::sha256_hex;
use crate::error::ApiError;
use crate::github::verify_webhook_signature;
use crate::model::{DeliveryClaim, TenantStatus};
use crate::state::AppState;

fn header<'a>(headers: &'a HeaderMap, name: &str) -> Option<&'a str> {
    headers.get(name).and_then(|v| v.to_str().ok())
}

fn valid_token(value: &str, extra: &[char], max: usize) -> bool {
    !value.is_empty()
        && value.len() <= max
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || extra.contains(&c))
}

pub async fn github_webhook(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response, ApiError> {
    let signature = header(&headers, "x-hub-signature-256").unwrap_or("");
    if !verify_webhook_signature(
        state.config.github.webhook_secret.expose().as_bytes(),
        &body,
        signature,
    ) {
        tracing::warn!("webhook rejected: invalid signature");
        return Err(ApiError::InvalidSignature);
    }
    let delivery = header(&headers, "x-github-delivery").filter(|v| valid_token(v, &['-'], 64));
    let event = header(&headers, "x-github-event").filter(|v| valid_token(v, &['_'], 64));
    let (Some(delivery), Some(event)) = (delivery, event) else {
        return Err(ApiError::BadRequest(
            "Missing or malformed webhook headers.".into(),
        ));
    };
    let payload: Value = serde_json::from_slice(&body)
        .map_err(|_| ApiError::BadRequest("The webhook body is not JSON.".into()))?;

    match state
        .store
        .claim_delivery(delivery, &sha256_hex(&body))
        .await?
    {
        DeliveryClaim::Duplicate => {
            tracing::info!(delivery, event, "webhook duplicate ignored");
            return Ok(Json(json!({ "status": "duplicate" })).into_response());
        }
        DeliveryClaim::Replay => {
            tracing::warn!(delivery, event, "webhook replay refused");
            return Ok((StatusCode::CONFLICT, Json(json!({ "error": "This payload was already delivered.", "code": "replay", "status": "replay" }))).into_response());
        }
        DeliveryClaim::New => {}
    }

    match process(&state, event, &payload).await {
        Ok(outcome) => {
            tracing::info!(delivery, event, outcome = outcome.0, "webhook processed");
            Ok(Json(json!({ "status": outcome.0, "detail": outcome.1 })).into_response())
        }
        Err(error) => {
            if let Err(release) = state.store.release_delivery(delivery).await {
                tracing::error!(delivery, error = %release, "could not release a failed webhook claim");
            }
            Err(error)
        }
    }
}

/// `(status, detail)`: `processed`, or `ignored` with the reason.
async fn process(
    state: &AppState,
    event: &str,
    payload: &Value,
) -> Result<(&'static str, &'static str), ApiError> {
    let installation = &payload["installation"];
    let installation_id = installation["id"].as_u64();
    match event {
        "ping" => Ok(("processed", "ping")),
        "installation" => {
            let action = payload["action"].as_str().unwrap_or("");
            let Some(id) = installation_id else {
                return Ok(("ignored", "no_installation"));
            };
            if action == "created" {
                let account = &installation["account"];
                let (Some(login), Some(kind)) =
                    (account["login"].as_str(), account["type"].as_str())
                else {
                    return Ok(("ignored", "no_account"));
                };
                state
                    .store
                    .upsert_installation_tenant(id, login, kind)
                    .await?;
                return Ok(("processed", "installation_created"));
            }
            let status = match action {
                "deleted" => TenantStatus::Deleted,
                "suspend" => TenantStatus::Suspended,
                "unsuspend" => TenantStatus::Active,
                _ => return Ok(("ignored", "installation_action")),
            };
            match state.store.tenant_by_installation(id).await? {
                Some(tenant) => {
                    state.store.set_tenant_status(&tenant.id, status).await?;
                    Ok(("processed", "installation_status"))
                }
                None => Ok(("ignored", "unknown_installation")),
            }
        }
        _ => {
            let Some(id) = installation_id else {
                return Ok(("ignored", "no_installation"));
            };
            match state.store.tenant_by_installation(id).await? {
                None => Ok(("ignored", "unknown_installation")),
                Some(tenant) if tenant.status != TenantStatus::Active => {
                    Ok(("ignored", "tenant_inactive"))
                }
                // Scheduling work from an issue event is the job runner's job.
                Some(_) => Ok(("processed", "recorded")),
            }
        }
    }
}
