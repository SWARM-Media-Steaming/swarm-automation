//! The one table that says what happens to every desktop command and event on
//! the web.
//!
//! Each `#[tauri::command]` in `src/main.rs` is either an endpoint here or in
//! [`REMOVED`] with the reason. The router registers its routes from
//! [`ROUTES`], `ui/api.js`'s `COMMANDS` / `EVENTS` rows are checked against it,
//! and `docs/web-architecture.md` prints it (`tests/api_catalog.rs` fails when
//! any of the four drifts), so a command cannot be half-ported.
//!
//! Paths are relative to `/api/v1/tenants/{tenant}` unless [`Scope::Public`].
//! `{name}` segments are path parameters; every other argument is a query
//! parameter on `GET` and a JSON body field otherwise (`ui/api.js`).

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Access {
    /// Any member of the tenant. Mutating routes still need an active tenant.
    Member,
    /// The tenant's owner (changes settings, merges, promotes, files issues).
    Owner,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Scope {
    Tenant,
    Public,
}

/// Endpoints the backend answers itself.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Native {
    GetConfig,
    SaveConfig,
    FeedbackFilter,
    Repositories,
    GetRepoConfig,
    SaveRepoConfig,
    Tools,
    Status,
    Scan,
    ProviderUsage,
    ModelDataKeyStatus,
    Readiness,
    Pause,
    Resume,
    Stop,
    RecentLogs,
    Version,
    /// Already served by an account route (`provider-keys`).
    Existing,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Handler {
    Native(Native),
    /// An operation of `issue_worker/web_bridge.py`.
    Bridge(&'static str),
}

pub struct Route {
    /// The desktop command names this serves (a `*_background` twin shares it).
    pub commands: &'static [&'static str],
    pub method: &'static str,
    pub path: &'static str,
    pub access: Access,
    pub scope: Scope,
    pub handler: Handler,
}

pub struct Removed {
    pub command: &'static str,
    pub reason: &'static str,
}

pub struct EventRoute {
    pub event: &'static str,
    pub path: &'static str,
}

const fn route(
    commands: &'static [&'static str],
    method: &'static str,
    path: &'static str,
    access: Access,
    handler: Handler,
) -> Route {
    Route {
        commands,
        method,
        path,
        access,
        scope: Scope::Tenant,
        handler,
    }
}

use Access::{Member, Owner};
use Handler::{Bridge, Native as N};

pub const ROUTES: &[Route] = &[
    // ---- settings -------------------------------------------------------
    route(
        &["get_config"],
        "GET",
        "/config",
        Member,
        N(Native::GetConfig),
    ),
    route(
        &["save_config"],
        "PUT",
        "/config",
        Owner,
        N(Native::SaveConfig),
    ),
    route(
        &["save_feedback_repo_filter"],
        "PUT",
        "/feedback-repo-filter",
        Member,
        N(Native::FeedbackFilter),
    ),
    route(
        &["web_list_repositories"],
        "GET",
        "/repos",
        Member,
        N(Native::Repositories),
    ),
    route(
        &["web_get_repo_config"],
        "GET",
        "/repos/{repoId}/config",
        Member,
        N(Native::GetRepoConfig),
    ),
    route(
        &["web_save_repo_config"],
        "PUT",
        "/repos/{repoId}/config",
        Owner,
        N(Native::SaveRepoConfig),
    ),
    // ---- providers, tools, status -----------------------------------------
    route(
        &["detect_tools", "detect_tools_background"],
        "GET",
        "/tools",
        Member,
        N(Native::Tools),
    ),
    route(
        &["check_provider_usage", "check_provider_usage_background"],
        "GET",
        "/provider-usage",
        Member,
        N(Native::ProviderUsage),
    ),
    route(
        &["get_model_data_key_status"],
        "GET",
        "/model-data-key",
        Member,
        N(Native::ModelDataKeyStatus),
    ),
    route(
        &["save_model_data_key"],
        "PUT",
        "/provider-keys/model-data",
        Owner,
        N(Native::Existing),
    ),
    route(
        &["clear_model_data_key"],
        "DELETE",
        "/provider-keys/model-data",
        Owner,
        N(Native::Existing),
    ),
    route(
        &["verify_github_bots"],
        "GET",
        "/repos/{repoId}/bots",
        Member,
        N(Native::Readiness),
    ),
    route(
        &["check_repo_bot_readiness"],
        "GET",
        "/repos/{repoId}/readiness",
        Member,
        N(Native::Readiness),
    ),
    // ---- process controls and logs ----------------------------------------
    route(
        &["get_automation_status", "get_automation_status_background"],
        "GET",
        "/status",
        Member,
        N(Native::Status),
    ),
    route(
        &["start_issue_worker", "request_issue_scan"],
        "POST",
        "/scan",
        Member,
        N(Native::Scan),
    ),
    route(
        &["pause_process"],
        "POST",
        "/processes/{process}/pause",
        Member,
        N(Native::Pause),
    ),
    route(
        &["resume_process"],
        "POST",
        "/processes/{process}/resume",
        Member,
        N(Native::Resume),
    ),
    route(
        &["stop_process"],
        "POST",
        "/processes/{process}/stop",
        Member,
        N(Native::Stop),
    ),
    route(
        &["get_recent_logs"],
        "GET",
        "/logs",
        Member,
        N(Native::RecentLogs),
    ),
    // ---- execution history, usage, grades, Jev -----------------------------
    route(
        &["get_execution_history", "get_execution_history_background"],
        "GET",
        "/history",
        Member,
        Bridge("execution_history"),
    ),
    route(
        &["get_jev_feedback", "get_jev_feedback_background"],
        "GET",
        "/jev-feedback",
        Member,
        Bridge("jev_feedback"),
    ),
    route(
        &["get_usage_report", "get_usage_report_background"],
        "GET",
        "/usage-report",
        Member,
        Bridge("usage_report"),
    ),
    route(
        &["get_prompt_grades", "get_prompt_grades_background"],
        "GET",
        "/prompt-grades",
        Member,
        Bridge("prompt_grades"),
    ),
    route(
        &[
            "import_execution_history",
            "import_execution_history_background",
        ],
        "POST",
        "/history/import",
        Owner,
        Bridge("import_execution_history"),
    ),
    // ---- engineering knowledge and architecture ----------------------------
    route(
        &["get_knowledge_status", "get_knowledge_status_background"],
        "GET",
        "/knowledge",
        Member,
        Bridge("knowledge_status"),
    ),
    route(
        &["refresh_knowledge", "refresh_knowledge_background"],
        "POST",
        "/knowledge/refresh",
        Member,
        Bridge("knowledge_refresh"),
    ),
    route(
        &["ask_swarm", "ask_swarm_background"],
        "POST",
        "/knowledge/ask",
        Member,
        Bridge("ask_swarm"),
    ),
    route(
        &["get_architecture_docs"],
        "GET",
        "/architecture-docs",
        Member,
        Bridge("architecture_docs"),
    ),
    // ---- model calibration, routing ----------------------------------------
    route(
        &[
            "get_model_calibration_status",
            "get_model_calibration_status_background",
        ],
        "GET",
        "/calibration",
        Member,
        Bridge("calibration_status"),
    ),
    route(
        &["refresh_model_data", "refresh_model_data_background"],
        "POST",
        "/calibration/refresh",
        Member,
        Bridge("calibration_refresh"),
    ),
    route(
        &[
            "analyze_model_calibration_update",
            "analyze_model_calibration_update_background",
        ],
        "POST",
        "/calibration/analyze",
        Member,
        Bridge("calibration_analyze"),
    ),
    route(
        &[
            "activate_model_calibration",
            "activate_model_calibration_background",
        ],
        "POST",
        "/calibration/activate",
        Owner,
        Bridge("calibration_activate"),
    ),
    route(
        &["describe_routing_calculator"],
        "GET",
        "/routing/calculator",
        Member,
        Bridge("routing_describe"),
    ),
    route(
        &["simulate_routing"],
        "POST",
        "/routing/simulate",
        Member,
        Bridge("routing_simulate"),
    ),
    // ---- diagnostics --------------------------------------------------------
    route(
        &["run_diagnostics", "run_diagnostics_background"],
        "POST",
        "/diagnostics/run",
        Member,
        Bridge("diagnostics_run"),
    ),
    route(
        &["file_diagnostic_issue", "file_diagnostic_issue_background"],
        "POST",
        "/diagnostics/issue",
        Owner,
        Bridge("diagnostics_file_issue"),
    ),
    // ---- git, branches, promotion --------------------------------------------
    route(
        &["git_overview", "git_overview_background"],
        "GET",
        "/repos/{repoId}/git",
        Member,
        Bridge("git_overview"),
    ),
    route(
        &["refresh_repo"],
        "POST",
        "/repos/{repoId}/refresh",
        Member,
        Bridge("git_overview"),
    ),
    route(
        &["merge_issue_branch"],
        "POST",
        "/repos/{repoId}/merge-issue",
        Owner,
        Bridge("merge_issue_branch"),
    ),
    route(
        &["merge_integration_branch"],
        "POST",
        "/repos/{repoId}/merge-integration",
        Owner,
        Bridge("merge_integration_branch"),
    ),
    route(
        &["branch_push_access"],
        "GET",
        "/repos/{repoId}/push-access",
        Member,
        Bridge("branch_push_access"),
    ),
    route(
        &["grant_bot_branch_push"],
        "POST",
        "/repos/{repoId}/push-access",
        Owner,
        Bridge("grant_bot_branch_push"),
    ),
    route(
        &["promotion_overview", "promotion_overview_background"],
        "GET",
        "/promotions",
        Member,
        Bridge("promotion_overview"),
    ),
    route(
        &["open_integration_pr"],
        "POST",
        "/repos/{repoId}/integration-pr",
        Owner,
        Bridge("open_integration_pr"),
    ),
    route(
        &[
            "promote_integration_branch",
            "promote_integration_branch_background",
        ],
        "POST",
        "/repos/{repoId}/promote",
        Owner,
        Bridge("promote_integration_branch"),
    ),
    // ---- public ---------------------------------------------------------------
    Route {
        commands: &["app_version"],
        method: "GET",
        path: "/version",
        access: Access::Member,
        scope: Scope::Public,
        handler: N(Native::Version),
    },
];

/// What an [`AdminRoute`] does (`admin.rs` has one handler for each).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum AdminAction {
    ListUsers,
    AuditLog,
    Promote,
    Demote,
}

/// A platform-administration endpoint. Not a desktop command and not tenant
/// data: it sits at `/api/v1{path}`, behind the `Admin` extractor (signed in,
/// CSRF on every non-`GET`, and `users.is_platform_admin`; anyone else is
/// answered 404). `command` is its `ui/api.js` `COMMANDS` row.
pub struct AdminRoute {
    pub command: &'static str,
    pub method: &'static str,
    pub path: &'static str,
    pub action: AdminAction,
}

pub const ADMIN_ROUTES: &[AdminRoute] = &[
    AdminRoute {
        command: "web_admin_list_users",
        method: "GET",
        path: "/admin/users",
        action: AdminAction::ListUsers,
    },
    AdminRoute {
        command: "web_admin_audit_log",
        method: "GET",
        path: "/admin/audit-log",
        action: AdminAction::AuditLog,
    },
    AdminRoute {
        command: "web_admin_promote_user",
        method: "POST",
        path: "/admin/users/{userId}/promote",
        action: AdminAction::Promote,
    },
    AdminRoute {
        command: "web_admin_demote_user",
        method: "POST",
        path: "/admin/users/{userId}/demote",
        action: AdminAction::Demote,
    },
];

pub fn admin_full_path(route: &AdminRoute) -> String {
    format!("/api/v1{}", route.path)
}

/// Desktop commands with no web equivalent, and why.
pub const REMOVED: &[Removed] = &[
    Removed { command: "choose_repository", reason: "A browser has no native folder picker. Repositories are the GitHub App installation's repositories, saved with the tenant's settings (GET /repos)." },
    Removed { command: "inspect_repository", reason: "It inspects a path on the user's machine. A hosted job clones the repository fresh for each run." },
    Removed { command: "prepare_workspace", reason: "A hosted job prepares its own clean workspace (job_launch.py); there is no managed checkout to prepare." },
    Removed { command: "open_workspace_folder", reason: "There is no local folder to open; the work is on the issue branch and its pull request." },
    Removed { command: "open_automation_folder", reason: "There is no local log file. Logs are GET /logs and the automation-log stream." },
    Removed { command: "open_external_url", reason: "The browser opens links itself." },
    Removed { command: "hide_to_tray", reason: "It hides the desktop window; the web page has no tray." },
    Removed { command: "launch_bot_setup", reason: "The GitHub App is installed from GitHub (sign-in creates the tenant); there is no local bot setup terminal." },
    Removed { command: "install_ai_cli", reason: "The provider CLIs are baked into the worker image; nothing is installed on the user's machine." },
    Removed { command: "open_provider_login", reason: "Hosted jobs use the tenant's own provider API keys (provider-keys), not an interactive CLI login." },
];

/// Desktop events and the stream that carries each on the web.
pub const EVENTS: &[EventRoute] = &[
    EventRoute {
        event: "automation-log",
        path: "/events/automation-log",
    },
    EventRoute {
        event: "job-log",
        path: "/events/jobs",
    },
    EventRoute {
        event: "model-calibration-refreshed",
        path: "/events/model-calibration",
    },
];

/// Desktop events that are intentionally not streamed, and why.
pub const REMOVED_EVENTS: &[(&str, &str)] = &[(
    "system-permission-primed",
    "It reports a macOS Automation permission prompt on the desktop; the web has no such permission.",
)];

pub fn full_path(route: &Route) -> String {
    match route.scope {
        Scope::Tenant => format!("/api/v1/tenants/{{tenant}}{}", route.path),
        Scope::Public => format!("/api/v1{}", route.path),
    }
}
