//! The REST endpoints for the desktop's commands (`catalog::ROUTES`).
//!
//! Every route is registered from the catalog, so the router, `ui/api.js`, the
//! docs and the desktop command list cannot disagree. Tenancy is structural:
//! each handler takes [`TenantAccess`] (404 for a tenant the caller is not in)
//! and the tenant is never read from a body, header or query string. Owner
//! routes need the owner role, mutating routes need an active tenant, and the
//! CSRF token and `Origin` are enforced by the extractor.

use std::collections::HashMap;

use axum::body::Bytes;
use axum::extract::{Path, RawQuery, State};
use axum::routing::{on, MethodFilter, MethodRouter};
use axum::{Json, Router};
use serde_json::{json, Map, Value};

use crate::auth::TenantAccess;
use crate::bridge::{BridgeError, BridgeRequest};
use crate::catalog::{self, Access, Handler, Native, Route, Scope};
use crate::error::ApiError;
use crate::model::{Provider, TenantStatus};
use crate::orchestrator::JobError;
use crate::settings;
use crate::state::AppState;

const DEFAULT_LOG_LINES: usize = 300;
const MAX_LOG_LINES: usize = 20_000;
const PROCESS: &str = "issue";

fn filter(method: &str) -> MethodFilter {
    match method {
        "GET" => MethodFilter::GET,
        "PUT" => MethodFilter::PUT,
        "DELETE" => MethodFilter::DELETE,
        "PATCH" => MethodFilter::PATCH,
        _ => MethodFilter::POST,
    }
}

fn method_router(route: &'static Route) -> MethodRouter<AppState> {
    match route.scope {
        Scope::Public => on(
            filter(route.method),
            move |State(state): State<AppState>| async move {
                Json(Value::String(state.config.app_version.clone()))
            },
        ),
        Scope::Tenant => on(
            filter(route.method),
            move |State(state): State<AppState>,
                  access: TenantAccess,
                  Path(params): Path<HashMap<String, String>>,
                  RawQuery(query): RawQuery,
                  body: Bytes| async move {
                dispatch(state, access, route, params, query, body).await
            },
        ),
    }
}

/// Add every catalog route that is not already an account route.
pub fn mount(mut router: Router<AppState>) -> Router<AppState> {
    for route in catalog::ROUTES {
        if route.handler == Handler::Native(Native::Existing) {
            continue;
        }
        router = router.route(&catalog::full_path(route), method_router(route));
    }
    router
}

fn bad(message: &str) -> ApiError {
    ApiError::BadRequest(message.into())
}

/// Query values are strings except where `ui/api.js` JSON-encoded an object or list.
fn parse_query(raw: Option<String>) -> Map<String, Value> {
    let mut out = Map::new();
    let Some(raw) = raw else { return out };
    for (key, value) in url::form_urlencoded::parse(raw.as_bytes()) {
        let parsed = if value.starts_with('[') || value.starts_with('{') {
            serde_json::from_str(&value).unwrap_or_else(|_| Value::String(value.to_string()))
        } else {
            Value::String(value.to_string())
        };
        out.insert(key.to_string(), parsed);
    }
    out
}

fn parse_body(body: &Bytes) -> Result<Map<String, Value>, ApiError> {
    if body.iter().all(u8::is_ascii_whitespace) {
        return Ok(Map::new());
    }
    match serde_json::from_slice::<Value>(body) {
        Ok(Value::Object(map)) => Ok(map),
        _ => Err(bad("The request body is not valid for this endpoint.")),
    }
}

async fn dispatch(
    state: AppState,
    access: TenantAccess,
    route: &'static Route,
    mut params: HashMap<String, String>,
    query: Option<String>,
    body: Bytes,
) -> Result<Json<Value>, ApiError> {
    if route.access == Access::Owner {
        access.require_owner()?;
    }
    if route.method != "GET" {
        access.require_active()?;
    }
    params.remove("tenant");
    let mut args = if route.method == "GET" || route.method == "DELETE" {
        parse_query(query)
    } else {
        parse_body(&body)?
    };
    // Path parameters win over anything the caller also put in the query/body.
    for (key, value) in params {
        args.insert(key, Value::String(value));
    }
    match route.handler {
        Handler::Native(native) => run_native(&state, &access, native, args).await,
        Handler::Bridge(op) => run_bridge(&state, &access, op, args).await,
    }
}

// ---- the worker bridge --------------------------------------------------------

fn repository_name(config: &Value, id: &str) -> Option<String> {
    config["repositories"]
        .as_array()?
        .iter()
        .find(|repo| repo["id"] == id)
        .and_then(|repo| repo["github_repository"].as_str())
        .map(str::to_string)
}

fn valid_full_name(value: &str) -> bool {
    let mut parts = value.split('/');
    matches!((parts.next(), parts.next(), parts.next()), (Some(o), Some(n), None)
        if !o.is_empty() && !n.is_empty())
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "_-./".contains(c))
        && value.len() <= 140
}

/// Resolve repository ids to `owner/name` from the tenant's own settings, so a
/// caller can only ever ask about repositories the tenant has configured.
fn resolve_repositories(config: &Value, args: &mut Map<String, Value>) -> Result<(), ApiError> {
    if let Some(ids) = args.get("repoIds") {
        let ids = ids
            .as_array()
            .ok_or_else(|| bad("repoIds must be a list of repository ids."))?
            .clone();
        let names = if ids.is_empty() {
            config["repositories"]
                .as_array()
                .map(|repos| {
                    repos
                        .iter()
                        .filter_map(|r| r["github_repository"].as_str())
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default()
        } else {
            ids.iter()
                .map(|id| {
                    id.as_str()
                        .and_then(|id| repository_name(config, id))
                        .ok_or_else(|| bad("Unknown repository."))
                })
                .collect::<Result<Vec<String>, ApiError>>()?
        };
        args.insert("repositories".into(), json!(names));
    }
    if let Some(id) = args
        .get("repoId")
        .and_then(Value::as_str)
        .map(str::to_string)
    {
        let name = repository_name(config, &id).ok_or(ApiError::NotFound)?;
        args.insert("repository".into(), Value::String(name));
    } else if let Some(name) = args.get("repository").and_then(Value::as_str) {
        let known = config["repositories"]
            .as_array()
            .is_some_and(|repos| repos.iter().any(|r| r["github_repository"] == name));
        if !valid_full_name(name) || !known {
            return Err(ApiError::NotFound);
        }
    }
    Ok(())
}

async fn run_bridge(
    state: &AppState,
    access: &TenantAccess,
    op: &'static str,
    mut args: Map<String, Value>,
) -> Result<Json<Value>, ApiError> {
    let config = settings::load(state.store.as_ref(), access.id()).await?;
    resolve_repositories(&config, &mut args)?;
    let request = BridgeRequest {
        tenant: access.id().clone(),
        op,
        args: Value::Object(args),
        config,
    };
    let result = state
        .bridge()
        .call(request)
        .await
        .map_err(|error| match error {
            BridgeError::NotAvailable(message) => ApiError::NotAvailable {
                code: "not_available_yet",
                message,
            },
            BridgeError::BadRequest(message) => ApiError::BadRequest(message),
            BridgeError::Unconfigured(message) => ApiError::Unconfigured {
                code: "bridge_unconfigured",
                message,
            },
            BridgeError::Failed(message) => ApiError::Internal(message),
        })?;
    tracing::info!(tenant = %access.id(), op, "worker operation served");
    if op == "calibration_refresh" {
        state
            .events
            .publish_calibration(access.id().as_str(), result.clone());
    }
    Ok(Json(result))
}

// ---- native endpoints -----------------------------------------------------------

fn orchestrator(
    state: &AppState,
) -> Result<std::sync::Arc<crate::orchestrator::Orchestrator>, ApiError> {
    state.orchestrator().ok_or(ApiError::Unconfigured {
        code: "jobs_unconfigured",
        message: "This deployment has no job runner configured.".into(),
    })
}

fn job_error(error: JobError) -> ApiError {
    match error {
        JobError::Denied(denied) => {
            ApiError::Conflict(crate::orchestrator::denial_message(&denied))
        }
        JobError::Inactive => ApiError::forbidden(
            "tenant_inactive",
            "This tenant's GitHub App installation is not active.",
        ),
        JobError::NotFound => ApiError::NotFound,
        JobError::Conflict(message) => ApiError::Conflict(message),
        JobError::Store(error) => ApiError::Internal(error.to_string()),
        JobError::Runner(message) => ApiError::Internal(message),
    }
}

fn label(provider: Provider) -> &'static str {
    match provider {
        Provider::Claude => "Claude",
        Provider::Codex => "Codex",
        Provider::Grok => "Grok",
        Provider::ModelData => "Model data",
    }
}

async fn key_configured(
    state: &AppState,
    access: &TenantAccess,
) -> Result<HashMap<Provider, bool>, ApiError> {
    Ok(state
        .vault
        .list(access.id())
        .await?
        .into_iter()
        .map(|meta| (meta.provider, meta.configured))
        .collect())
}

fn process_status(state: &str, detail: String, exit_code: Option<i32>) -> Value {
    json!({
        "state": state,
        "pid": null,
        "startedAt": null,
        "exitCode": exit_code,
        "detail": detail,
    })
}

async fn run_native(
    state: &AppState,
    access: &TenantAccess,
    native: Native,
    args: Map<String, Value>,
) -> Result<Json<Value>, ApiError> {
    let store = state.store.as_ref();
    let tenant = access.id();
    match native {
        Native::Existing | Native::Version => Err(ApiError::NotFound),
        Native::GetConfig => Ok(Json(settings::load(store, tenant).await?)),
        Native::SaveConfig => {
            let config = args
                .get("config")
                .ok_or_else(|| bad("Send the configuration as { \"config\": { ... } }."))?;
            let saved = settings::save(store, tenant, config).await?;
            tracing::info!(tenant = %tenant, actor = %access.authed.user.login, credentials_dropped = saved.dropped.len(), "settings saved");
            Ok(Json(saved.config))
        }
        Native::FeedbackFilter => {
            let ids = args
                .get("repoIds")
                .ok_or_else(|| bad("repoIds is required."))?;
            Ok(Json(
                settings::set_feedback_filter(store, tenant, ids).await?,
            ))
        }
        Native::Repositories => {
            let config = settings::load(store, tenant).await?;
            let repos: Vec<Value> = config["repositories"]
                .as_array()
                .map(|items| {
                    items
                        .iter()
                        .map(|repo| {
                            let github = repo["github_repository"].as_str().unwrap_or("");
                            json!({
                                "id": repo["id"],
                                "label": repo.get("label").and_then(Value::as_str).unwrap_or(github),
                                "githubRepository": github,
                                "enabled": repo.get("enabled").and_then(Value::as_bool).unwrap_or(true),
                            })
                        })
                        .collect()
                })
                .unwrap_or_default();
            Ok(Json(json!({ "repositories": repos })))
        }
        Native::GetRepoConfig => {
            let id = path_arg(&args, "repoId")?;
            settings::load_repo(store, tenant, id)
                .await?
                .map(Json)
                .ok_or(ApiError::NotFound)
        }
        Native::SaveRepoConfig => {
            let id = path_arg(&args, "repoId")?.to_string();
            let mut body = args.clone();
            body.remove("repoId");
            let saved = settings::save_repo(store, tenant, &id, &Value::Object(body)).await?;
            tracing::info!(tenant = %tenant, actor = %access.authed.user.login, credentials_dropped = saved.dropped.len(), "repository settings saved");
            Ok(Json(saved.config))
        }
        Native::Tools => {
            let keys = key_configured(state, access).await?;
            let tools: Vec<Value> = Provider::ALL
                .into_iter()
                .filter(|p| p.runs_jobs())
                .map(|provider| {
                    let configured = keys.get(&provider).copied().unwrap_or(false);
                    json!({
                        "id": provider.as_str(),
                        "label": label(provider),
                        "required": false,
                        "installed": true,
                        "path": "",
                        "version": "",
                        "authenticated": configured,
                        "status": if configured { "API key saved" } else { "No API key saved" },
                        "installable": false,
                        "models": [],
                        "modelsDetected": false,
                    })
                })
                .collect();
            Ok(Json(Value::Array(tools)))
        }
        Native::ProviderUsage => {
            let usage = state.accounting.usage(tenant).await?;
            let rows: Vec<Value> = usage
                .providers
                .iter()
                .map(|p| {
                    json!({
                        "provider": p.provider.as_str(),
                        "status": p.status,
                        "usable": p.status == 0,
                        "remainingPercent": p.remaining_percent,
                        "detail": p.detail,
                    })
                })
                .collect();
            Ok(Json(Value::Array(rows)))
        }
        Native::ModelDataKeyStatus => {
            let keys = key_configured(state, access).await?;
            Ok(Json(Value::Bool(
                keys.get(&Provider::ModelData).copied().unwrap_or(false),
            )))
        }
        Native::Readiness => {
            let id = path_arg(&args, "repoId")?;
            let config = settings::load(store, tenant).await?;
            repository_name(&config, id).ok_or(ApiError::NotFound)?;
            let keys = key_configured(state, access).await?;
            let active = access.tenant.status == TenantStatus::Active;
            let rows: Vec<Value> = Provider::ALL
                .into_iter()
                .filter(|p| p.runs_jobs())
                .map(|provider| {
                    let configured = keys.get(&provider).copied().unwrap_or(false);
                    let name = label(provider);
                    let message = if !active {
                        "The GitHub App installation is suspended or removed.".to_string()
                    } else if !configured {
                        format!("No {name} API key is saved for this tenant.")
                    } else {
                        format!("{name} is ready: the GitHub App is installed and a key is saved.")
                    };
                    json!({
                        "provider": provider.as_str(),
                        "configured": configured,
                        "valid": configured && active,
                        "message": message,
                    })
                })
                .collect();
            Ok(Json(Value::Array(rows)))
        }
        Native::Status => status(state, access).await,
        Native::Scan => {
            let jobs = orchestrator(state)?;
            jobs.tick().await.map_err(job_error)?;
            let active = jobs.tenant_jobs(tenant).await.len();
            Ok(Json(Value::String(format!(
                "Scan requested. The hosted scheduler is always on; {active} job(s) active."
            ))))
        }
        Native::Pause | Native::Resume | Native::Stop => {
            control(state, access, native, &args).await
        }
        Native::RecentLogs => {
            let limit = match args.get("limit") {
                None | Some(Value::Null) => DEFAULT_LOG_LINES,
                Some(Value::String(text)) => {
                    text.parse().map_err(|_| bad("limit must be a number."))?
                }
                Some(Value::Number(number)) => number.as_u64().unwrap_or(0) as usize,
                Some(_) => return Err(bad("limit must be a number.")),
            }
            .clamp(1, MAX_LOG_LINES);
            Ok(Json(json!(state
                .events
                .recent_logs(tenant.as_str(), limit))))
        }
    }
}

fn path_arg<'a>(args: &'a Map<String, Value>, name: &str) -> Result<&'a str, ApiError> {
    args.get(name)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| bad("A path parameter is missing."))
}

async fn status(state: &AppState, access: &TenantAccess) -> Result<Json<Value>, ApiError> {
    let tenant = access.id();
    let config = settings::load(state.store.as_ref(), tenant).await?;
    let jobs = match state.orchestrator() {
        Some(jobs) => jobs.tenant_jobs(tenant).await,
        None => Vec::new(),
    };
    let running = jobs.iter().filter(|j| j.status == "running").count();
    let paused = jobs.iter().filter(|j| j.status == "paused").count();
    let (state_name, detail) = if running > 0 {
        ("running", format!("{running} job(s) running"))
    } else if paused > 0 {
        ("paused", format!("{paused} job(s) paused"))
    } else {
        ("stopped", "No job is running.".to_string())
    };
    let repos: Vec<Value> = config["repositories"]
        .as_array()
        .map(|items| {
            items
                .iter()
                .map(|repo| {
                    let github = repo["github_repository"].as_str().unwrap_or("");
                    json!({
                        "id": repo["id"],
                        "label": repo.get("label").and_then(Value::as_str).unwrap_or(github),
                        "githubRepository": github,
                        "enabled": repo.get("enabled").and_then(Value::as_bool).unwrap_or(true),
                        "workspacePath": "",
                        "workspaceReady": true,
                        "workspaceManaged": true,
                        "workerAvailable": true,
                        "botConfigExists": true,
                        "repoConfigError": "",
                        "deferredReason": null,
                        "repository": {},
                    })
                })
                .collect()
        })
        .unwrap_or_default();
    Ok(Json(json!({
        "issue": process_status(state_name, detail, None),
        "task": process_status("stopped", String::new(), None),
        "schedulerRepoCount": repos.len(),
        "repos": repos,
        "botConfigExists": true,
        "configError": "",
        "logPath": "",
        "jobs": jobs,
    })))
}

/// Pause, resume or stop the tenant's jobs. `process` is `issue`, the hosted
/// scheduler's one slot per repository (the desktop's `uat:<repo>` slots have no
/// web counterpart: UAT runs inside the job).
async fn control(
    state: &AppState,
    access: &TenantAccess,
    native: Native,
    args: &Map<String, Value>,
) -> Result<Json<Value>, ApiError> {
    if path_arg(args, "process")? != PROCESS {
        return Err(ApiError::NotFound);
    }
    let jobs = orchestrator(state)?;
    let tenant = access.id();
    let wanted = match native {
        Native::Pause => "running",
        Native::Resume => "paused",
        _ => "",
    };
    let active = jobs.tenant_jobs(tenant).await;
    let targets: Vec<_> = active
        .iter()
        .filter(|job| match native {
            Native::Stop => matches!(job.status.as_str(), "running" | "paused" | "quota" | "held"),
            _ => job.status == wanted,
        })
        .collect();
    if targets.is_empty() {
        let what = match native {
            Native::Pause => "running job to pause",
            Native::Resume => "paused job to resume",
            _ => "job to stop",
        };
        return Err(ApiError::Conflict(format!("There is no {what}.")));
    }
    for job in &targets {
        let Some((owner, repo)) = job.repository.split_once('/') else {
            continue;
        };
        match native {
            Native::Pause => jobs.pause(tenant, owner, repo, job.issue).await,
            Native::Resume => jobs.resume_job(tenant, owner, repo, job.issue).await,
            _ => jobs.stop(tenant, owner, repo, job.issue).await,
        }
        .map_err(job_error)?;
    }
    let (name, detail) = match native {
        Native::Pause => ("paused", format!("{} job(s) paused", targets.len())),
        Native::Resume => ("running", format!("{} job(s) resumed", targets.len())),
        _ => ("stopped", format!("{} job(s) stopped", targets.len())),
    };
    Ok(Json(process_status(name, detail, None)))
}
