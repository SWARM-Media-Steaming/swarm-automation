//! Platform administration: list users, promote, demote (issue #441).
//!
//! A platform admin (`users.is_platform_admin`) runs the hosted service. That is
//! not a tenant Owner, who administers one tenant. These handlers take the
//! [`Admin`] extractor, which is signed-in-with-CSRF plus the flag, so a handler
//! cannot be reached without both. The routes come from
//! `catalog::ADMIN_ROUTES`, the same table `ui/api.js` and the docs are checked
//! against. Every change is one store step that also writes the audit row, and
//! the store refuses to demote the last admin.

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::routing::{delete, get, post, put};
use axum::{Json, Router};
use serde_json::{json, Value};

use crate::auth::Admin;
use crate::catalog::{self, AdminAction};
use crate::error::ApiError;
use crate::model::{AdminChange, BlacklistEntry, KeyPurpose, Provider, User};
use crate::routes::ApiJson;
use crate::secret::Secret;
use crate::state::AppState;
use crate::vault::validate_key;
use serde::Deserialize;

pub fn user_json(user: &User) -> Value {
    json!({
        "id": user.id,
        "login": user.login,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "last_login_at": user.last_login_at,
        "is_platform_admin": user.is_platform_admin,
    })
}

/// Register every route of `catalog::ADMIN_ROUTES`.
pub fn mount(mut router: Router<AppState>) -> Router<AppState> {
    for route in catalog::ADMIN_ROUTES {
        let handler = match route.action {
            AdminAction::ListUsers => get(list_users),
            AdminAction::AuditLog => get(audit_log),
            AdminAction::Promote => post(promote),
            AdminAction::Demote => post(demote),
            AdminAction::ListBlacklist => get(list_blacklist),
            AdminAction::PutBlacklist => put(put_blacklist),
            AdminAction::DeleteBlacklist => delete(delete_blacklist),
            AdminAction::ListPlatformKeys => get(list_platform_keys),
            AdminAction::PutPlatformKey => put(put_platform_key),
            AdminAction::DeletePlatformKey => delete(delete_platform_key),
        };
        router = router.route(&catalog::admin_full_path(route), handler);
    }
    router
}

async fn list_users(State(state): State<AppState>, _admin: Admin) -> Result<Json<Value>, ApiError> {
    let users: Vec<Value> = state
        .store
        .platform_users()
        .await?
        .iter()
        .map(user_json)
        .collect();
    Ok(Json(json!({ "users": users })))
}

/// How many audit rows the Admin view gets (newest first).
const AUDIT_LIMIT: usize = 50;

/// The recent audit trail: action, who and when. Logins are resolved from the
/// user list (`null` once the user is gone); the free-form `detail` stays server side.
async fn audit_log(State(state): State<AppState>, _admin: Admin) -> Result<Json<Value>, ApiError> {
    let logins: std::collections::HashMap<String, String> = state
        .store
        .platform_users()
        .await?
        .into_iter()
        .map(|user| (user.id, user.login))
        .collect();
    let login = |id: &Option<String>| id.as_ref().and_then(|id| logins.get(id)).cloned();
    let entries: Vec<Value> = state
        .store
        .admin_audit_log(AUDIT_LIMIT)
        .await?
        .iter()
        .map(|row| {
            json!({
                "id": row.id,
                "action": row.action,
                "actor_login": login(&row.actor_user_id),
                "target_login": login(&row.target_user_id),
                "subject": audit_subject(&row.detail),
                "created_at": row.created_at,
            })
        })
        .collect();
    Ok(Json(json!({ "entries": entries })))
}

async fn promote(
    state: State<AppState>,
    admin: Admin,
    Path(user_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    change(state, admin, user_id, true).await
}

async fn demote(
    state: State<AppState>,
    admin: Admin,
    Path(user_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    change(state, admin, user_id, false).await
}

async fn change(
    State(state): State<AppState>,
    admin: Admin,
    user_id: String,
    make_admin: bool,
) -> Result<Json<Value>, ApiError> {
    let actor = &admin.authed.user;
    match state
        .store
        .set_platform_admin(&actor.id, &user_id, make_admin)
        .await?
    {
        AdminChange::Changed(user) => {
            tracing::warn!(
                actor = %actor.login,
                target = %user.login,
                admin = make_admin,
                "platform admin changed"
            );
            Ok(Json(json!({ "user": user_json(&user), "changed": true })))
        }
        // Asking for the state the user is already in is not an error and not
        // a change, so it writes no audit row.
        AdminChange::Unchanged(user) => {
            Ok(Json(json!({ "user": user_json(&user), "changed": false })))
        }
        AdminChange::UnknownUser => Err(ApiError::NotFound),
        AdminChange::LastAdmin => Err(ApiError::Conflict(
            "This is the last platform admin. Promote another user first.".into(),
        )),
    }
}

/// What a configuration audit row was about, from its non-secret detail: a model
/// name, or `purpose/provider` for a platform key. `null` for user changes.
fn audit_subject(detail: &Value) -> Option<String> {
    if let Some(model) = detail.get("model").and_then(Value::as_str) {
        return Some(model.to_string());
    }
    let purpose = detail.get("purpose").and_then(Value::as_str)?;
    let provider = detail.get("provider").and_then(Value::as_str)?;
    Some(format!("{purpose}/{provider}"))
}

// ---- model blacklist (issue: admin-managed, database driven) ----------------

const MAX_MODEL_LEN: usize = 100;
const MAX_REASON_LEN: usize = 300;

/// A model slug: letters, digits and `. _ : -`, as every provider names them.
fn model_slug(raw: &str, what: &str) -> Result<String, ApiError> {
    let slug = raw.trim().to_ascii_lowercase();
    let valid = !slug.is_empty()
        && slug.len() <= MAX_MODEL_LEN
        && slug
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b':' | b'-'));
    if valid {
        Ok(slug)
    } else {
        Err(ApiError::BadRequest(format!(
            "{what} must be 1-{MAX_MODEL_LEN} letters, digits, dots, dashes, colons or underscores."
        )))
    }
}

/// The blacklist as the worker reads it (`model-blacklist.json`'s shape). A job
/// gets this in `SWARM_MODEL_BLACKLIST_JSON`.
pub fn blacklist_document(entries: &[BlacklistEntry]) -> Value {
    json!({
        "models": entries.iter().map(|e| json!({
            "model": e.model,
            "superseded_by": e.superseded_by,
            "reason": e.reason,
        })).collect::<Vec<_>>(),
    })
}

fn entry_json(entry: &BlacklistEntry) -> Value {
    json!({
        "model": entry.model,
        "superseded_by": entry.superseded_by,
        "reason": entry.reason,
        "updated_at": entry.updated_at,
        "updated_by": entry.updated_by,
    })
}

async fn list_blacklist(
    State(state): State<AppState>,
    _admin: Admin,
) -> Result<Json<Value>, ApiError> {
    let entries = state.store.model_blacklist().await?;
    Ok(Json(
        json!({ "models": entries.iter().map(entry_json).collect::<Vec<_>>() }),
    ))
}

#[derive(Deserialize)]
struct BlacklistBody {
    #[serde(default)]
    superseded_by: String,
    #[serde(default)]
    reason: String,
}

async fn put_blacklist(
    State(state): State<AppState>,
    admin: Admin,
    Path(model): Path<String>,
    ApiJson(body): ApiJson<BlacklistBody>,
) -> Result<Json<Value>, ApiError> {
    let model = model_slug(&model, "The model")?;
    let superseded_by = if body.superseded_by.trim().is_empty() {
        String::new()
    } else {
        model_slug(&body.superseded_by, "The successor")?
    };
    if superseded_by == model {
        return Err(ApiError::BadRequest(
            "A model cannot be its own successor.".into(),
        ));
    }
    let reason = body.reason.trim().to_string();
    if reason.len() > MAX_REASON_LEN || reason.chars().any(char::is_control) {
        return Err(ApiError::BadRequest(format!(
            "The reason must be plain text of at most {MAX_REASON_LEN} characters."
        )));
    }
    let actor = &admin.authed.user;
    let entry = BlacklistEntry {
        model,
        superseded_by,
        reason,
        updated_at: state.clock.now_secs(),
        updated_by: actor.login.clone(),
    };
    let created = state
        .store
        .put_blacklist_entry(&actor.id, entry.clone())
        .await?;
    tracing::warn!(actor = %actor.login, model = %entry.model, "model blacklist changed");
    Ok(Json(
        json!({ "entry": entry_json(&entry), "created": created }),
    ))
}

async fn delete_blacklist(
    State(state): State<AppState>,
    admin: Admin,
    Path(model): Path<String>,
) -> Result<StatusCode, ApiError> {
    let model = model_slug(&model, "The model")?;
    let actor = &admin.authed.user;
    if !state
        .store
        .delete_blacklist_entry(&actor.id, &model)
        .await?
    {
        return Err(ApiError::NotFound);
    }
    tracing::warn!(actor = %actor.login, model = %model, "model blacklist entry removed");
    Ok(StatusCode::NO_CONTENT)
}

// ---- platform provider keys (write-only) -----------------------------------

fn parse_purpose_and_provider(
    purpose: &str,
    provider: &str,
) -> Result<(KeyPurpose, Provider), ApiError> {
    let purpose = KeyPurpose::parse(purpose).ok_or_else(|| {
        ApiError::BadRequest("Unknown purpose. Use platform or automation.".into())
    })?;
    let provider = Provider::parse(provider).ok_or_else(|| {
        ApiError::BadRequest("Unknown provider. Use claude, codex, grok or model-data.".into())
    })?;
    Ok((purpose, provider))
}

async fn list_platform_keys(
    State(state): State<AppState>,
    _admin: Admin,
) -> Result<Json<Value>, ApiError> {
    Ok(Json(json!({ "keys": state.vault.list_platform().await? })))
}

#[derive(Deserialize)]
struct PlatformKeyBody {
    key: Secret,
}

/// Write-only: the response carries metadata, never the key.
async fn put_platform_key(
    State(state): State<AppState>,
    admin: Admin,
    Path((purpose, provider)): Path<(String, String)>,
    ApiJson(body): ApiJson<PlatformKeyBody>,
) -> Result<Json<Value>, ApiError> {
    let (purpose, provider) = parse_purpose_and_provider(&purpose, &provider)?;
    let key = Secret::new(body.key.expose().trim());
    validate_key(key.expose())?;
    let actor = &admin.authed.user;
    state
        .vault
        .put_platform(&actor.id, purpose, provider, &key, &actor.login)
        .await?;
    tracing::warn!(actor = %actor.login, purpose = purpose.as_str(), provider = provider.as_str(), "platform provider key stored");
    let meta = state
        .vault
        .list_platform()
        .await?
        .into_iter()
        .find(|m| m.purpose == purpose && m.provider == provider);
    Ok(Json(json!(meta)))
}

async fn delete_platform_key(
    State(state): State<AppState>,
    admin: Admin,
    Path((purpose, provider)): Path<(String, String)>,
) -> Result<StatusCode, ApiError> {
    let (purpose, provider) = parse_purpose_and_provider(&purpose, &provider)?;
    let actor = &admin.authed.user;
    if !state
        .vault
        .delete_platform(&actor.id, purpose, provider)
        .await?
    {
        return Err(ApiError::NotFound);
    }
    tracing::warn!(actor = %actor.login, purpose = purpose.as_str(), provider = provider.as_str(), "platform provider key removed");
    Ok(StatusCode::NO_CONTENT)
}
