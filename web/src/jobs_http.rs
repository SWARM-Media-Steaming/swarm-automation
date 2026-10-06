//! Member-accessible controls for one repository's job. The tenant comes from
//! `TenantAccess`, never from the body. Missing orchestrator is a 404, the
//! same answer a tenant the caller does not belong to gets.

use std::collections::HashSet;
use std::convert::Infallible;

use axum::extract::{Path, State};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::Json;
use serde::Serialize;
use tokio_stream::wrappers::BroadcastStream;
use tokio_stream::StreamExt;

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
/// tenant parameter: `listen()` in `ui/api.js` does not substitute one.
pub async fn events(
    State(state): State<AppState>,
    authed: Authed,
) -> Result<Sse<impl tokio_stream::Stream<Item = Result<Event, Infallible>>>, ApiError> {
    let jobs = orchestrator(&state)?;
    let allowed: HashSet<String> = state
        .store
        .tenants_for_user(&authed.user.id)
        .await?
        .into_iter()
        .map(|(tenant, _)| tenant.id.to_string())
        .collect();
    let live = BroadcastStream::new(jobs.subscribe()).filter_map(move |item| {
        let event = item.ok()?;
        if !allowed.contains(&event.tenant) {
            return None;
        }
        let data = serde_json::to_string(&event).ok()?;
        Some(Ok(Event::default().event("job-log").data(data)))
    });
    let ready = tokio_stream::once(Ok(Event::default().comment("ready")));
    Ok(Sse::new(ready.chain(live)).keep_alive(KeepAlive::default()))
}
