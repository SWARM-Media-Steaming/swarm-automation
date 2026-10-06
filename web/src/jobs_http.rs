//! Member-accessible controls for one repository's job. The tenant comes from
//! `TenantAccess`, never from the body. Missing orchestrator is a 404, the
//! same answer a tenant the caller does not belong to gets.

use axum::extract::{Path, State};
use axum::http::HeaderMap;
use axum::response::IntoResponse;
use axum::Json;
use serde::Serialize;

use crate::auth::{Authed, TenantAccess};
use crate::error::ApiError;
use crate::orchestrator::{denial_message, JobError, JobView};
use crate::state::AppState;

fn orchestrator(
    state: &AppState,
) -> Result<std::sync::Arc<crate::orchestrator::Orchestrator>, ApiError> {
    state.orchestrator().ok_or(ApiError::NotFound)
}

fn into_api(error: JobError) -> ApiError {
    match error {
        JobError::Denied(denied) => ApiError::Conflict(denial_message(&denied)),
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

fn ready(access: &TenantAccess) -> Result<(), ApiError> {
    access.require_active()
}

pub async fn status(
    State(state): State<AppState>,
    access: TenantAccess,
    Path((_, owner, repo, issue)): Path<(String, String, String, u64)>,
) -> Result<Json<JobView>, ApiError> {
    ready(&access)?;
    let jobs = orchestrator(&state)?;
    jobs.status(access.id(), &owner, &repo, issue)
        .await
        .map(Json)
        .map_err(into_api)
}

pub async fn run(
    State(state): State<AppState>,
    access: TenantAccess,
    Path((_, owner, repo, issue)): Path<(String, String, String, u64)>,
) -> Result<Json<JobView>, ApiError> {
    ready(&access)?;
    let jobs = orchestrator(&state)?;
    jobs.run_now(access.id(), &owner, &repo, issue)
        .await
        .map(Json)
        .map_err(into_api)
}

pub async fn pause(
    State(state): State<AppState>,
    access: TenantAccess,
    Path((_, owner, repo, issue)): Path<(String, String, String, u64)>,
) -> Result<Json<JobView>, ApiError> {
    ready(&access)?;
    let jobs = orchestrator(&state)?;
    jobs.pause(access.id(), &owner, &repo, issue)
        .await
        .map(Json)
        .map_err(into_api)
}

pub async fn resume(
    State(state): State<AppState>,
    access: TenantAccess,
    Path((_, owner, repo, issue)): Path<(String, String, String, u64)>,
) -> Result<Json<JobView>, ApiError> {
    ready(&access)?;
    let jobs = orchestrator(&state)?;
    jobs.resume_job(access.id(), &owner, &repo, issue)
        .await
        .map(Json)
        .map_err(into_api)
}

pub async fn stop(
    State(state): State<AppState>,
    access: TenantAccess,
    Path((_, owner, repo, issue)): Path<(String, String, String, u64)>,
) -> Result<Json<JobView>, ApiError> {
    ready(&access)?;
    let jobs = orchestrator(&state)?;
    jobs.stop(access.id(), &owner, &repo, issue)
        .await
        .map(Json)
        .map_err(into_api)
}

#[derive(Serialize)]
pub struct LogsBody {
    pub lines: Vec<String>,
}

pub async fn logs(
    State(state): State<AppState>,
    access: TenantAccess,
    Path((_, owner, repo, issue)): Path<(String, String, String, u64)>,
) -> Result<Json<LogsBody>, ApiError> {
    ready(&access)?;
    let jobs = orchestrator(&state)?;
    let lines = jobs
        .logs(access.id(), &owner, &repo, issue)
        .await
        .map_err(into_api)?;
    Ok(Json(LogsBody { lines }))
}

/// One stream for every tenant the signed-in user belongs to. The path has no
/// tenant parameter: `listen()` in `ui/api.js` does not substitute one. It is
/// served from the event hub (`events.rs`): replay after `Last-Event-ID`,
/// heartbeat, bounded backpressure.
pub async fn events(
    State(state): State<AppState>,
    authed: Authed,
    headers: HeaderMap,
) -> Result<impl IntoResponse, ApiError> {
    crate::events::serve(state, authed, headers, "job-log").await
}

pub async fn automation_log(
    State(state): State<AppState>,
    authed: Authed,
    headers: HeaderMap,
) -> Result<impl IntoResponse, ApiError> {
    crate::events::serve(state, authed, headers, "automation-log").await
}

pub async fn calibration_events(
    State(state): State<AppState>,
    authed: Authed,
    headers: HeaderMap,
) -> Result<impl IntoResponse, ApiError> {
    crate::events::serve(state, authed, headers, "model-calibration-refreshed").await
}
