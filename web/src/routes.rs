//! The router: `/api/v1` REST, the webhook, the operator-only internal API and
//! the shared `ui/` as static assets, all behind strict security headers.

use axum::extract::{
    DefaultBodyLimit, FromRequest, FromRequestParts, Path, RawPathParams, Request, State,
};
use axum::http::header::{AUTHORIZATION, CACHE_CONTROL, CONTENT_SECURITY_POLICY};
use axum::http::request::Parts;
use axum::http::{HeaderName, HeaderValue, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{any, delete, get, post, put};
use axum::{Json, Router};
use serde::de::DeserializeOwned;
use serde::Deserialize;
use serde_json::json;
use subtle::ConstantTimeEq;
use tower_http::services::ServeDir;

use crate::auth::{self, tenant_json, Authed, TenantAccess};
use crate::error::ApiError;
use crate::jobs_http;
use crate::model::*;
use crate::secret::Secret;
use crate::state::AppState;
use crate::usage::{validate_budgets, validate_plan, IngestBody};
use crate::vault::validate_key;
use crate::webhook;

/// Everything the page needs is same-origin: no inline script or style, no
/// third-party origin, nothing framing it. `ui/` honors this by construction
/// (`.claude/rules/ui-design-system.md`).
pub const CONTENT_SECURITY_POLICY_VALUE: &str = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'";
const JSON_BODY_LIMIT: usize = 64 * 1024;
const WEBHOOK_BODY_LIMIT: usize = 5 * 1024 * 1024;

/// JSON body extractor whose failures never echo the body: a rejected request
/// may have carried a provider key.
pub struct ApiJson<T>(pub T);

impl<S, T> FromRequest<S> for ApiJson<T>
where
    S: Send + Sync,
    T: DeserializeOwned,
{
    type Rejection = ApiError;

    async fn from_request(req: Request, state: &S) -> Result<Self, ApiError> {
        let bytes = axum::body::Bytes::from_request(req, state)
            .await
            .map_err(|_| {
                ApiError::BadRequest("The request body is too large or unreadable.".into())
            })?;
        serde_json::from_slice(&bytes).map(ApiJson).map_err(|_| {
            ApiError::BadRequest("The request body is not valid for this endpoint.".into())
        })
    }
}

/// The operator-only internal API (the job runner and billing pipeline): a
/// bearer token, compared in constant time. Disabled (404) when no token is
/// configured. Carries the tenant from the path; it has no cookies, so it needs
/// no CSRF and cannot be driven from a browser session.
pub struct Internal {
    pub tenant: TenantId,
}

impl FromRequestParts<AppState> for Internal {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &AppState) -> Result<Self, ApiError> {
        let Some(expected) = &state.config.internal_token else {
            return Err(ApiError::NotFound);
        };
        let presented = parts
            .headers
            .get(AUTHORIZATION)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "))
            .unwrap_or("");
        let matches: bool = presented
            .as_bytes()
            .ct_eq(expected.expose().as_bytes())
            .into();
        if presented.is_empty() || !matches {
            return Err(ApiError::Unauthorized);
        }
        let params = RawPathParams::from_request_parts(parts, state)
            .await
            .map_err(|_| ApiError::NotFound)?;
        let raw = params
            .iter()
            .find(|(key, _)| *key == "tenant")
            .map(|(_, v)| v)
            .ok_or(ApiError::NotFound)?;
        let tenant = TenantId::parse(raw).ok_or(ApiError::NotFound)?;
        if state.store.tenant(&tenant).await?.is_none() {
            return Err(ApiError::NotFound);
        }
        Ok(Internal { tenant })
    }
}

async fn health() -> Json<serde_json::Value> {
    Json(json!({ "status": "ok", "service": "swarm-web", "version": env!("CARGO_PKG_VERSION") }))
}

async fn list_tenants(
    State(state): State<AppState>,
    authed: Authed,
) -> Result<Json<serde_json::Value>, ApiError> {
    let tenants: Vec<_> = state
        .store
        .tenants_for_user(&authed.user.id)
        .await?
        .iter()
        .map(|(tenant, role)| tenant_json(tenant, *role))
        .collect();
    Ok(Json(json!({ "tenants": tenants })))
}

async fn get_tenant(access: TenantAccess) -> Json<serde_json::Value> {
    Json(tenant_json(&access.tenant, access.role))
}

async fn list_members(
    State(state): State<AppState>,
    access: TenantAccess,
) -> Result<Json<serde_json::Value>, ApiError> {
    Ok(Json(
        json!({ "members": state.store.members(access.id()).await? }),
    ))
}

async fn list_keys(
    State(state): State<AppState>,
    access: TenantAccess,
) -> Result<Json<serde_json::Value>, ApiError> {
    Ok(Json(
        json!({ "keys": state.vault.list(access.id()).await? }),
    ))
}

fn parse_provider(raw: &str) -> Result<Provider, ApiError> {
    Provider::parse(raw).ok_or_else(|| {
        ApiError::BadRequest("Unknown provider. Use claude, codex, grok or model-data.".into())
    })
}

#[derive(Deserialize)]
struct PutKeyBody {
    key: Secret,
}

/// Write-only: the response carries metadata, never the key.
async fn put_key(
    State(state): State<AppState>,
    access: TenantAccess,
    Path((_, provider)): Path<(String, String)>,
    ApiJson(body): ApiJson<PutKeyBody>,
) -> Result<Json<serde_json::Value>, ApiError> {
    access.require_owner()?;
    access.require_active()?;
    let provider = parse_provider(&provider)?;
    let key = Secret::new(body.key.expose().trim());
    validate_key(key.expose())?;
    state
        .vault
        .put(access.id(), provider, &key, &access.authed.user.login)
        .await?;
    tracing::info!(tenant = %access.id(), provider = provider.as_str(), actor = %access.authed.user.login, "provider key stored");
    let meta = state
        .vault
        .list(access.id())
        .await?
        .into_iter()
        .find(|m| m.provider == provider);
    Ok(Json(json!(meta)))
}

async fn delete_key(
    State(state): State<AppState>,
    access: TenantAccess,
    Path((_, provider)): Path<(String, String)>,
) -> Result<StatusCode, ApiError> {
    access.require_owner()?;
    access.require_active()?;
    let provider = parse_provider(&provider)?;
    if !state.vault.delete(access.id(), provider).await? {
        return Err(ApiError::NotFound);
    }
    tracing::info!(tenant = %access.id(), provider = provider.as_str(), actor = %access.authed.user.login, "provider key deleted");
    Ok(StatusCode::NO_CONTENT)
}

async fn get_quotas(
    State(state): State<AppState>,
    access: TenantAccess,
) -> Result<Json<serde_json::Value>, ApiError> {
    Ok(Json(json!({
        "plan": state.accounting.plan(access.id()).await?,
        "budgets": state.accounting.budgets(access.id()).await?,
    })))
}

async fn put_budgets(
    State(state): State<AppState>,
    access: TenantAccess,
    ApiJson(budgets): ApiJson<Budgets>,
) -> Result<Json<Budgets>, ApiError> {
    access.require_owner()?;
    access.require_active()?;
    validate_budgets(&budgets)?;
    state
        .store
        .set_budgets(access.id(), budgets.clone())
        .await?;
    Ok(Json(budgets))
}

async fn get_usage(
    State(state): State<AppState>,
    access: TenantAccess,
) -> Result<Json<crate::usage::UsageView>, ApiError> {
    Ok(Json(state.accounting.usage(access.id()).await?))
}

// ---- operator-only internal API --------------------------------------------

async fn internal_ingest(
    State(state): State<AppState>,
    internal: Internal,
    ApiJson(body): ApiJson<IngestBody>,
) -> Result<Json<crate::usage::IngestResult>, ApiError> {
    Ok(Json(state.accounting.ingest(&internal.tenant, body).await?))
}

async fn internal_set_plan(
    State(state): State<AppState>,
    internal: Internal,
    ApiJson(plan): ApiJson<PlanQuotas>,
) -> Result<Json<PlanQuotas>, ApiError> {
    validate_plan(&plan)?;
    state.store.set_plan_quotas(&internal.tenant, plan).await?;
    Ok(Json(plan))
}

#[derive(Deserialize)]
struct AdmitBody {
    job_id: String,
    provider: String,
}

async fn internal_admit(
    State(state): State<AppState>,
    internal: Internal,
    ApiJson(body): ApiJson<AdmitBody>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let provider = parse_provider(&body.provider).and_then(|p| {
        p.runs_jobs()
            .then_some(p)
            .ok_or_else(|| ApiError::BadRequest("Jobs run on claude, codex or grok.".into()))
    })?;
    if body.job_id.is_empty()
        || body.job_id.len() > 128
        || !body
            .job_id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "-_.".contains(c))
    {
        return Err(ApiError::BadRequest(
            "job_id must be 1-128 letters, digits, '-', '_' or '.'.".into(),
        ));
    }
    match state
        .accounting
        .admit(&internal.tenant, &body.job_id, provider)
        .await?
    {
        Ok(()) => Ok(Json(json!({ "admitted": true, "job_id": body.job_id }))),
        Err(denied) => Err(denied.into_error()),
    }
}

async fn internal_release(
    State(state): State<AppState>,
    internal: Internal,
    Path((_, job_id)): Path<(String, String)>,
) -> Result<StatusCode, ApiError> {
    if state.accounting.release(&internal.tenant, &job_id).await? {
        Ok(StatusCode::NO_CONTENT)
    } else {
        Err(ApiError::NotFound)
    }
}

async fn api_not_found() -> ApiError {
    ApiError::NotFound
}

// ---- middleware ----------------------------------------------------------------

async fn security_headers(State(state): State<AppState>, request: Request, next: Next) -> Response {
    let api = request.uri().path().starts_with("/api/");
    let mut response = next.run(request).await;
    let headers = response.headers_mut();
    let fixed: [(&str, &'static str); 5] = [
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("referrer-policy", "no-referrer"),
        ("cross-origin-opener-policy", "same-origin"),
        (
            "permissions-policy",
            "camera=(), microphone=(), geolocation=()",
        ),
    ];
    for (name, value) in fixed {
        headers.insert(
            HeaderName::from_static(name),
            HeaderValue::from_static(value),
        );
    }
    headers.insert(
        CONTENT_SECURITY_POLICY,
        HeaderValue::from_static(CONTENT_SECURITY_POLICY_VALUE),
    );
    if state.config.cookie_secure() {
        headers.insert(
            HeaderName::from_static("strict-transport-security"),
            HeaderValue::from_static("max-age=31536000; includeSubDomains"),
        );
    }
    if api {
        headers.insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    }
    response
}

/// One structured line per request. The path only, never the query string: the
/// OAuth callback carries a code and state there.
async fn request_log(request: Request, next: Next) -> Response {
    let method = request.method().clone();
    let path = request.uri().path().to_string();
    let started = std::time::Instant::now();
    let response = next.run(request).await;
    tracing::info!(
        method = %method,
        path = %path,
        status = response.status().as_u16(),
        latency_ms = started.elapsed().as_millis() as u64,
        "request"
    );
    response
}

/// The UI is public, but not everything under `ui/` is for browsers: tests,
/// dotfiles and notes stay unserved.
async fn static_guard(request: Request, next: Next) -> Response {
    let path = request.uri().path();
    let hidden = path.split('/').any(|segment| segment.starts_with('.'))
        || path.ends_with(".test.js")
        || path.ends_with(".md");
    if hidden {
        return StatusCode::NOT_FOUND.into_response();
    }
    next.run(request).await
}

pub fn router(state: AppState) -> Router {
    let tenant = "/api/v1/tenants/{tenant}";
    let internal = "/api/v1/internal/tenants/{tenant}";
    let api = Router::new();
    let api = crate::api::mount(api);
    let api = api
        .route("/api/v1/health", get(health))
        .route("/api/v1/session", get(auth::session))
        .route("/api/v1/auth/{provider}/login", get(auth::login))
        .route("/api/v1/auth/{provider}/callback", get(auth::callback))
        .route("/api/v1/auth/logout", post(auth::logout))
        .route("/api/v1/tenants", get(list_tenants))
        .route(tenant, get(get_tenant))
        .route(&format!("{tenant}/members"), get(list_members))
        .route(&format!("{tenant}/provider-keys"), get(list_keys))
        .route(
            &format!("{tenant}/provider-keys/{{provider}}"),
            put(put_key).delete(delete_key),
        )
        .route(&format!("{tenant}/quotas"), get(get_quotas))
        .route(&format!("{tenant}/budgets"), put(put_budgets))
        .route(&format!("{tenant}/usage"), get(get_usage))
        .route(
            &format!("{tenant}/work/{{owner}}/{{repo}}/issues/{{issue}}"),
            get(jobs_http::status),
        )
        .route(
            &format!("{tenant}/work/{{owner}}/{{repo}}/issues/{{issue}}/run"),
            post(jobs_http::run),
        )
        .route(
            &format!("{tenant}/work/{{owner}}/{{repo}}/issues/{{issue}}/pause"),
            post(jobs_http::pause),
        )
        .route(
            &format!("{tenant}/work/{{owner}}/{{repo}}/issues/{{issue}}/resume"),
            post(jobs_http::resume),
        )
        .route(
            &format!("{tenant}/work/{{owner}}/{{repo}}/issues/{{issue}}/stop"),
            post(jobs_http::stop),
        )
        .route(
            &format!("{tenant}/work/{{owner}}/{{repo}}/issues/{{issue}}/logs"),
            get(jobs_http::logs),
        )
        .route("/api/v1/events/jobs", get(jobs_http::events))
        .route(
            "/api/v1/events/automation-log",
            get(jobs_http::automation_log),
        )
        .route(
            "/api/v1/events/model-calibration",
            get(jobs_http::calibration_events),
        )
        .route(&format!("{internal}/usage"), post(internal_ingest))
        .route(&format!("{internal}/quotas"), put(internal_set_plan))
        .route(&format!("{internal}/jobs"), post(internal_admit))
        .route(
            &format!("{internal}/jobs/{{job_id}}"),
            delete(internal_release),
        )
        .layer(DefaultBodyLimit::max(JSON_BODY_LIMIT))
        .route(
            "/api/v1/webhooks/github",
            post(webhook::github_webhook).layer(DefaultBodyLimit::max(WEBHOOK_BODY_LIMIT)),
        )
        .route("/api/v1", any(api_not_found))
        .route("/api/v1/", any(api_not_found))
        .route("/api/v1/{*rest}", any(api_not_found));

    let ui = Router::new()
        .fallback_service(
            ServeDir::new(&state.config.ui_dir).append_index_html_on_directories(true),
        )
        .layer(middleware::from_fn(static_guard));

    api.merge(ui)
        .layer(middleware::from_fn_with_state(
            state.clone(),
            security_headers,
        ))
        .layer(middleware::from_fn(request_log))
        .with_state(state)
}
