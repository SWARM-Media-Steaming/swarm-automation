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
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{json, Value};

use crate::auth::Admin;
use crate::catalog::{self, AdminAction};
use crate::error::ApiError;
use crate::model::{AdminChange, User};
use crate::state::AppState;

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
