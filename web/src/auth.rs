//! Sign-in with GitHub, cookie sessions, CSRF protection and tenant access.
//!
//! * The session id is a 256-bit random value in an `HttpOnly`, `SameSite=Lax`
//!   (and, over HTTPS, `Secure`/`__Host-`) cookie. The store keeps only its
//!   SHA-256. Nothing credential-like is ever handed to JavaScript storage.
//! * Every state-changing request needs the session's CSRF token in
//!   `X-CSRF-Token` (constant-time compared) and, when the browser sends an
//!   `Origin`, it must be this site's. The check lives in the [`Authed`]
//!   extractor, so a handler cannot be signed-in-only without it.
//! * [`TenantAccess`] is the only door to tenant data: it proves the signed-in
//!   user belongs to the tenant in the path, and answers 404 (not 403) when they
//!   do not so tenant ids cannot be probed.

use axum::extract::{FromRequestParts, Path, Query, RawPathParams, State};
use axum::http::header::{COOKIE, LOCATION, SET_COOKIE};
use axum::http::request::Parts;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde::Deserialize;
use serde_json::json;
use subtle::ConstantTimeEq;

use crate::crypto::{random_token, sha256_hex};
use crate::error::ApiError;
use crate::github::pkce_challenge;
use crate::identity::{clean_profile, IdentityProvider};
use crate::model::*;
use crate::state::AppState;

pub const CSRF_HEADER: &str = "x-csrf-token";
const OAUTH_COOKIE_TTL_SECS: u64 = 600;
const AUTH_PATH: &str = "/api/v1/auth";

fn name(secure: bool, base: &str, host_prefix: bool) -> String {
    match (secure, host_prefix) {
        (true, true) => format!("__Host-{base}"),
        (true, false) => format!("__Secure-{base}"),
        (false, _) => base.to_string(),
    }
}

pub fn session_cookie_name(secure: bool) -> String {
    name(secure, "swarm_session", true)
}

pub fn csrf_cookie_name(secure: bool) -> String {
    name(secure, "swarm_csrf", true)
}

fn oauth_cookie_name(secure: bool) -> String {
    name(secure, "swarm_oauth", false)
}

fn set_cookie(
    name: &str,
    value: &str,
    max_age: u64,
    http_only: bool,
    path: &str,
    secure: bool,
) -> HeaderValue {
    let mut cookie = format!("{name}={value}; Path={path}; Max-Age={max_age}; SameSite=Lax");
    if http_only {
        cookie.push_str("; HttpOnly");
    }
    if secure {
        cookie.push_str("; Secure");
    }
    HeaderValue::from_str(&cookie).expect("cookie values are ASCII")
}

pub fn cookie_value(headers: &HeaderMap, wanted: &str) -> Option<String> {
    headers
        .get_all(COOKIE)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|line| line.split(';'))
        .filter_map(|pair| pair.trim().split_once('='))
        .find(|(key, _)| *key == wanted)
        .map(|(_, value)| value.to_string())
}

fn ct_eq(a: &str, b: &str) -> bool {
    a.len() == b.len() && a.as_bytes().ct_eq(b.as_bytes()).into()
}

/// A signed-in user with a live session whose CSRF requirements are met.
pub struct Authed {
    pub user: User,
    pub session: Session,
}

fn is_safe(method: &Method) -> bool {
    matches!(*method, Method::GET | Method::HEAD | Method::OPTIONS)
}

impl FromRequestParts<AppState> for Authed {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &AppState) -> Result<Self, ApiError> {
        let secure = state.config.cookie_secure();
        let token = cookie_value(&parts.headers, &session_cookie_name(secure))
            .ok_or(ApiError::Unauthorized)?;
        let hash = sha256_hex(token.as_bytes());
        let session = state
            .store
            .session(&hash)
            .await?
            .ok_or(ApiError::Unauthorized)?;
        if session.expires_at <= state.clock.now_secs() {
            state.store.delete_session(&hash).await?;
            return Err(ApiError::Unauthorized);
        }
        let user = state
            .store
            .user(&session.user_id)
            .await?
            .ok_or(ApiError::Unauthorized)?;
        if !is_safe(&parts.method) {
            if let Some(origin) = parts.headers.get("origin") {
                if origin
                    .to_str()
                    .map(|o| o != state.config.origin())
                    .unwrap_or(true)
                {
                    return Err(ApiError::forbidden(
                        "csrf_origin",
                        "The request did not come from this site.",
                    ));
                }
            }
            let presented = parts
                .headers
                .get(CSRF_HEADER)
                .and_then(|v| v.to_str().ok())
                .unwrap_or("");
            if presented.is_empty() || !ct_eq(presented, &session.csrf_token) {
                return Err(ApiError::forbidden(
                    "csrf_token",
                    "The CSRF token is missing or wrong. Reload the page.",
                ));
            }
        }
        Ok(Authed { user, session })
    }
}

/// Proof that the signed-in user is a platform administrator
/// (`users.is_platform_admin`). It is not a tenant [`Role::Owner`]: an owner
/// administers their own tenant, an admin the platform.
pub struct Admin {
    pub authed: Authed,
}

impl FromRequestParts<AppState> for Admin {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &AppState) -> Result<Self, ApiError> {
        // Anonymous -> 401 and a bad CSRF token -> 403 come from `Authed`; past
        // that, a non-admin must not learn the admin API exists.
        let authed = Authed::from_request_parts(parts, state).await?;
        if !authed.user.is_platform_admin {
            return Err(ApiError::NotFound);
        }
        Ok(Admin { authed })
    }
}

/// Proof that the signed-in user belongs to the tenant named by the `{tenant}`
/// path parameter, with the role they hold.
pub struct TenantAccess {
    pub authed: Authed,
    pub tenant: Tenant,
    pub role: Role,
}

impl TenantAccess {
    pub fn id(&self) -> &TenantId {
        &self.tenant.id
    }

    pub fn require_owner(&self) -> Result<(), ApiError> {
        if self.role == Role::Owner {
            Ok(())
        } else {
            Err(ApiError::forbidden(
                "owner_required",
                "Only a tenant owner can do this.",
            ))
        }
    }

    /// Writes are refused while the GitHub App installation is suspended or removed.
    pub fn require_active(&self) -> Result<(), ApiError> {
        if self.tenant.status == TenantStatus::Active {
            Ok(())
        } else {
            Err(ApiError::forbidden(
                "tenant_inactive",
                "This tenant's GitHub App installation is not active.",
            ))
        }
    }
}

impl FromRequestParts<AppState> for TenantAccess {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &AppState) -> Result<Self, ApiError> {
        let authed = Authed::from_request_parts(parts, state).await?;
        let params = RawPathParams::from_request_parts(parts, state)
            .await
            .map_err(|_| ApiError::NotFound)?;
        let raw = params
            .iter()
            .find(|(key, _)| *key == "tenant")
            .map(|(_, v)| v)
            .ok_or(ApiError::NotFound)?;
        let tenant_id = TenantId::parse(raw).ok_or(ApiError::NotFound)?;
        let role = state
            .store
            .role_in(&tenant_id, &authed.user.id)
            .await?
            .ok_or(ApiError::NotFound)?;
        let tenant = state
            .store
            .tenant(&tenant_id)
            .await?
            .ok_or(ApiError::NotFound)?;
        Ok(TenantAccess {
            authed,
            tenant,
            role,
        })
    }
}

fn redirect_uri(state: &AppState, provider: &str) -> String {
    format!("{}{AUTH_PATH}/{provider}/callback", state.config.origin())
}

/// The provider named by the path, or 404 (an unconfigured provider is not
/// distinguishable from a mistyped one).
fn provider_for(
    state: &AppState,
    provider: &str,
) -> Result<std::sync::Arc<dyn IdentityProvider>, ApiError> {
    state.identity.get(provider).ok_or(ApiError::NotFound)
}

pub async fn login(
    State(state): State<AppState>,
    Path(provider): Path<String>,
) -> Result<Response, ApiError> {
    let provider = provider_for(&state, &provider)?;
    let secure = state.config.cookie_secure();
    let oauth_state = random_token();
    let verifier = random_token();
    let location = provider.authorize_url(
        &redirect_uri(&state, provider.id()),
        &oauth_state,
        &pkce_challenge(&verifier),
    );
    let mut response = StatusCode::FOUND.into_response();
    let headers = response.headers_mut();
    headers.insert(
        LOCATION,
        HeaderValue::from_str(&location).expect("authorize URL is ASCII"),
    );
    headers.append(
        SET_COOKIE,
        set_cookie(
            &oauth_cookie_name(secure),
            &format!("{oauth_state}.{verifier}"),
            OAUTH_COOKIE_TTL_SECS,
            true,
            AUTH_PATH,
            secure,
        ),
    );
    Ok(response)
}

#[derive(Deserialize)]
pub struct CallbackQuery {
    code: Option<String>,
    state: Option<String>,
    error: Option<String>,
}

pub async fn callback(
    State(state): State<AppState>,
    Path(provider): Path<String>,
    headers: HeaderMap,
    Query(query): Query<CallbackQuery>,
) -> Result<Response, ApiError> {
    let provider = provider_for(&state, &provider)?;
    let secure = state.config.cookie_secure();
    if query.error.is_some() {
        return Err(ApiError::BadRequest(
            "GitHub sign-in was cancelled or refused.".into(),
        ));
    }
    let (Some(code), Some(returned_state)) = (query.code, query.state) else {
        return Err(ApiError::BadRequest(
            "The sign-in response is incomplete.".into(),
        ));
    };
    let cookie = cookie_value(&headers, &oauth_cookie_name(secure)).ok_or_else(|| {
        ApiError::BadRequest("The sign-in did not start in this browser. Start again.".into())
    })?;
    let (expected_state, verifier) = cookie.split_once('.').ok_or_else(|| {
        ApiError::BadRequest("The sign-in cookie is malformed. Start again.".into())
    })?;
    if code.len() > 512 || !ct_eq(&returned_state, expected_state) {
        return Err(ApiError::BadRequest(
            "The sign-in state did not match. Start again.".into(),
        ));
    }

    let token = provider
        .exchange_code(&code, &redirect_uri(&state, provider.id()), verifier)
        .await
        .map_err(|e| ApiError::BadGateway(e.to_string()))?;
    let profile = provider
        .profile(&token)
        .await
        .map_err(|e| ApiError::BadGateway(e.to_string()))?;
    let profile = clean_profile(profile).map_err(|e| ApiError::BadGateway(e.to_string()))?;
    let installations = provider
        .installations(&token, &profile)
        .await
        .map_err(|e| ApiError::BadGateway(e.to_string()))?;
    drop(token);

    // One transaction: a first sign-in registers the user, their identity, their
    // personal tenant and its Owner membership, or none of them.
    let registration = state.store.register_identity(&profile).await?;
    let mut user = registration.user;
    // The first-admin bootstrap: a listed account becomes the platform admin at
    // sign-in, but only while there is none (the store decides atomically).
    if state.config.is_bootstrap_admin(&profile)
        && state.store.bootstrap_platform_admin(&user.id).await?
    {
        user.is_platform_admin = true;
        tracing::warn!(user = %user.login, "first platform admin created from SWARM_WEB_BOOTSTRAP_ADMINS");
    }
    let mut kept = vec![registration.tenant.id];
    for installation in &installations {
        let tenant = state
            .store
            .upsert_installation_tenant(
                installation.id,
                &installation.account_login,
                &installation.account_type,
            )
            .await?;
        state
            .store
            .set_membership(&tenant.id, &user.id, installation.viewer_role)
            .await?;
        kept.push(tenant.id);
    }
    // The provider is the source of truth: access the user no longer has is
    // dropped. The personal tenant is theirs and always stays.
    state.store.retain_memberships(&user.id, &kept).await?;

    // A new session id on every sign-in, so a pre-login cookie is never promoted.
    let session_token = random_token();
    let csrf_token = random_token();
    let ttl = state.config.session_ttl_secs;
    state
        .store
        .create_session(Session {
            token_hash: sha256_hex(session_token.as_bytes()),
            user_id: user.id.clone(),
            csrf_token: csrf_token.clone(),
            expires_at: state.clock.now_secs() + ttl,
        })
        .await?;
    tracing::info!(
        user = %user.login,
        provider = provider.id(),
        first_sign_in = registration.first_sign_in,
        tenants = kept.len(),
        "signed in"
    );

    let mut response = StatusCode::FOUND.into_response();
    let out = response.headers_mut();
    out.insert(LOCATION, HeaderValue::from_static("/"));
    out.append(
        SET_COOKIE,
        set_cookie(
            &session_cookie_name(secure),
            &session_token,
            ttl,
            true,
            "/",
            secure,
        ),
    );
    // Readable by `ui/api.js`, which echoes it in `X-CSRF-Token`. The server
    // still compares against the copy stored with the session.
    out.append(
        SET_COOKIE,
        set_cookie(
            &csrf_cookie_name(secure),
            &csrf_token,
            ttl,
            false,
            "/",
            secure,
        ),
    );
    out.append(
        SET_COOKIE,
        set_cookie(&oauth_cookie_name(secure), "", 0, true, AUTH_PATH, secure),
    );
    Ok(response)
}

pub async fn logout(State(state): State<AppState>, authed: Authed) -> Result<Response, ApiError> {
    let secure = state.config.cookie_secure();
    state
        .store
        .delete_session(&authed.session.token_hash)
        .await?;
    let mut response = StatusCode::NO_CONTENT.into_response();
    let out = response.headers_mut();
    out.append(
        SET_COOKIE,
        set_cookie(&session_cookie_name(secure), "", 0, true, "/", secure),
    );
    out.append(
        SET_COOKIE,
        set_cookie(&csrf_cookie_name(secure), "", 0, false, "/", secure),
    );
    Ok(response)
}

fn install_url(state: &AppState) -> Option<String> {
    state.config.github.app_slug.as_ref().map(|slug| {
        format!(
            "{}/apps/{slug}/installations/new",
            state.config.github.web_base
        )
    })
}

pub fn tenant_json(tenant: &Tenant, role: Role) -> serde_json::Value {
    json!({
        "id": tenant.id,
        "account_login": tenant.account_login,
        "account_type": tenant.account_type,
        "status": tenant.status,
        "role": role,
    })
}

/// The signed-in user's profile (`GET /me`): the session decides who, nothing
/// from the client does. The tenant is the user's personal one (the first
/// tenant when they somehow have none); identities carry provider and login
/// only, never a subject, token or session hash.
pub async fn me(
    State(state): State<AppState>,
    authed: Authed,
) -> Result<Json<serde_json::Value>, ApiError> {
    let user = &authed.user;
    let tenants = state.store.tenants_for_user(&user.id).await?;
    let primary = tenants
        .iter()
        .find(|(tenant, _)| tenant.account_type == PERSONAL_ACCOUNT_TYPE)
        .or_else(|| tenants.first());
    let identities: Vec<_> = state
        .store
        .identities_for_user(&user.id)
        .await?
        .into_iter()
        .map(|(provider, login)| json!({ "provider": provider, "login": login }))
        .collect();
    Ok(Json(json!({
        "id": user.id,
        "login": user.login,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "tenant": primary.map(|(tenant, _)| tenant.id.clone()),
        "role": primary.map(|(_, role)| *role),
        "is_platform_admin": user.is_platform_admin,
        "identities": identities,
    })))
}

/// Who is signed in, their tenants, and the CSRF token for this session.
/// Answers 200 either way so the page can decide between the app and sign-in.
pub async fn session(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<serde_json::Value>, ApiError> {
    let secure = state.config.cookie_secure();
    let anonymous = || json!({ "authenticated": false, "login_url": format!("{AUTH_PATH}/github/login"), "install_url": install_url(&state) });
    let Some(token) = cookie_value(&headers, &session_cookie_name(secure)) else {
        return Ok(Json(anonymous()));
    };
    let Some(session) = state.store.session(&sha256_hex(token.as_bytes())).await? else {
        return Ok(Json(anonymous()));
    };
    if session.expires_at <= state.clock.now_secs() {
        return Ok(Json(anonymous()));
    }
    let Some(user) = state.store.user(&session.user_id).await? else {
        return Ok(Json(anonymous()));
    };
    let tenants: Vec<_> = state
        .store
        .tenants_for_user(&user.id)
        .await?
        .iter()
        .map(|(tenant, role)| tenant_json(tenant, *role))
        .collect();
    Ok(Json(json!({
        "authenticated": true,
        "user": { "login": user.login, "display_name": user.display_name, "avatar_url": user.avatar_url, "is_platform_admin": user.is_platform_admin },
        "csrf_token": session.csrf_token,
        "tenants": tenants,
        "install_url": install_url(&state),
    })))
}
