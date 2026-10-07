(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmApi = api;
})(typeof window === "undefined" ? globalThis : window, () => {
  "use strict";

  // The one transport seam for `ui/`. Everything else calls `invoke(name, args)`
  // and `listen(event, cb)`; this module decides how they reach the backend:
  //
  //   desktop  window.__TAURI__ is present -> delegate to Tauri, unchanged.
  //   web      window.__TAURI__ is absent  -> fetch() for commands and
  //            EventSource (Server-Sent Events) for events.
  //
  // The web side is driven entirely by the two data tables below. A command or
  // event that has no row is not available on web: `invoke` rejects with an
  // `UnavailableOnWebError` and `listen` resolves a no-op unlisten, so the rest
  // of the UI keeps working. The web backend fills the tables in; nothing else
  // in `ui/` changes when it does.

  const API_BASE = "/api/v1";

  // Command name -> endpoint. One row per command the web backend serves:
  //   { method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE", path: "/repos/{repoId}/status" }
  // `{name}` in the path is filled from `args[name]` (URL-encoded) and consumed;
  // the remaining args go in the query string for GET/DELETE and as a JSON body
  // otherwise. `method` defaults to "POST".
  //
  // The `web_*` rows are the account endpoints the hosted backend serves
  // (`web/`, `docs/web-architecture.md`): sign-in state, the signed-in user's
  // tenants, write-only provider keys, budgets and usage. They exist only on
  // the web, so the desktop never invokes them. `{tenant}` is a tenant id from
  // `web_session`; the server decides access from the session, never from args.
  const COMMANDS = {
    web_session: { method: "GET", path: "/session" },
    web_me: { method: "GET", path: "/me" },
    web_logout: { method: "POST", path: "/auth/logout" },
    web_list_tenants: { method: "GET", path: "/tenants" },
    web_get_tenant: { method: "GET", path: "/tenants/{tenant}" },
    web_list_members: { method: "GET", path: "/tenants/{tenant}/members" },
    web_list_provider_keys: { method: "GET", path: "/tenants/{tenant}/provider-keys" },
    web_set_provider_key: { method: "PUT", path: "/tenants/{tenant}/provider-keys/{provider}" },
    web_delete_provider_key: { method: "DELETE", path: "/tenants/{tenant}/provider-keys/{provider}" },
    web_get_quotas: { method: "GET", path: "/tenants/{tenant}/quotas" },
    web_set_budgets: { method: "PUT", path: "/tenants/{tenant}/budgets" },
    web_get_usage: { method: "GET", path: "/tenants/{tenant}/usage" },
    web_job_status: { method: "GET", path: "/tenants/{tenant}/work/{owner}/{repo}/issues/{issue}" },
    web_job_logs: { method: "GET", path: "/tenants/{tenant}/work/{owner}/{repo}/issues/{issue}/logs" },
    web_run_job: { method: "POST", path: "/tenants/{tenant}/work/{owner}/{repo}/issues/{issue}/run" },
    web_pause_job: { method: "POST", path: "/tenants/{tenant}/work/{owner}/{repo}/issues/{issue}/pause" },
    web_resume_job: { method: "POST", path: "/tenants/{tenant}/work/{owner}/{repo}/issues/{issue}/resume" },
    web_stop_job: { method: "POST", path: "/tenants/{tenant}/work/{owner}/{repo}/issues/{issue}/stop" },
    web_list_repositories: { method: "GET", path: "/tenants/{tenant}/repos" },
    web_get_repo_config: { method: "GET", path: "/tenants/{tenant}/repos/{repoId}/config" },
    web_save_repo_config: { method: "PUT", path: "/tenants/{tenant}/repos/{repoId}/config" },
    // Platform administration (web/src/admin.rs): not tenant-scoped, and the
    // server answers 404 to anyone who is not a platform admin.
    web_admin_list_users: { method: "GET", path: "/admin/users" },
    web_admin_promote_user: { method: "POST", path: "/admin/users/{userId}/promote" },
    web_admin_demote_user: { method: "POST", path: "/admin/users/{userId}/demote" },
    // The desktop's own commands, by the name the UI already invokes. A
    // `*_background` twin shares its command's row. `{tenant}` is not passed by
    // those call sites: the adapter fills it from the active tenant (below).
    // Commands with no row here (`choose_repository`, `hide_to_tray`, ...) are
    // intentionally absent on the web; docs/web-architecture.md lists why, and
    // web/tests/api_catalog.rs fails if this table and the backend disagree.
    get_config: { method: "GET", path: "/tenants/{tenant}/config" },
    save_config: { method: "PUT", path: "/tenants/{tenant}/config" },
    save_feedback_repo_filter: { method: "PUT", path: "/tenants/{tenant}/feedback-repo-filter" },
    detect_tools: { method: "GET", path: "/tenants/{tenant}/tools" },
    detect_tools_background: { method: "GET", path: "/tenants/{tenant}/tools" },
    check_provider_usage: { method: "GET", path: "/tenants/{tenant}/provider-usage" },
    check_provider_usage_background: { method: "GET", path: "/tenants/{tenant}/provider-usage" },
    get_model_data_key_status: { method: "GET", path: "/tenants/{tenant}/model-data-key" },
    save_model_data_key: { method: "PUT", path: "/tenants/{tenant}/provider-keys/model-data" },
    clear_model_data_key: { method: "DELETE", path: "/tenants/{tenant}/provider-keys/model-data" },
    verify_github_bots: { method: "GET", path: "/tenants/{tenant}/repos/{repoId}/bots" },
    check_repo_bot_readiness: { method: "GET", path: "/tenants/{tenant}/repos/{repoId}/readiness" },
    get_automation_status: { method: "GET", path: "/tenants/{tenant}/status" },
    get_automation_status_background: { method: "GET", path: "/tenants/{tenant}/status" },
    start_issue_worker: { method: "POST", path: "/tenants/{tenant}/scan" },
    request_issue_scan: { method: "POST", path: "/tenants/{tenant}/scan" },
    pause_process: { method: "POST", path: "/tenants/{tenant}/processes/{process}/pause" },
    resume_process: { method: "POST", path: "/tenants/{tenant}/processes/{process}/resume" },
    stop_process: { method: "POST", path: "/tenants/{tenant}/processes/{process}/stop" },
    get_recent_logs: { method: "GET", path: "/tenants/{tenant}/logs" },
    get_execution_history: { method: "GET", path: "/tenants/{tenant}/history" },
    get_execution_history_background: { method: "GET", path: "/tenants/{tenant}/history" },
    get_jev_feedback: { method: "GET", path: "/tenants/{tenant}/jev-feedback" },
    get_jev_feedback_background: { method: "GET", path: "/tenants/{tenant}/jev-feedback" },
    get_usage_report: { method: "GET", path: "/tenants/{tenant}/usage-report" },
    get_usage_report_background: { method: "GET", path: "/tenants/{tenant}/usage-report" },
    get_prompt_grades: { method: "GET", path: "/tenants/{tenant}/prompt-grades" },
    get_prompt_grades_background: { method: "GET", path: "/tenants/{tenant}/prompt-grades" },
    import_execution_history: { method: "POST", path: "/tenants/{tenant}/history/import" },
    import_execution_history_background: { method: "POST", path: "/tenants/{tenant}/history/import" },
    get_knowledge_status: { method: "GET", path: "/tenants/{tenant}/knowledge" },
    get_knowledge_status_background: { method: "GET", path: "/tenants/{tenant}/knowledge" },
    refresh_knowledge: { method: "POST", path: "/tenants/{tenant}/knowledge/refresh" },
    refresh_knowledge_background: { method: "POST", path: "/tenants/{tenant}/knowledge/refresh" },
    ask_swarm: { method: "POST", path: "/tenants/{tenant}/knowledge/ask" },
    ask_swarm_background: { method: "POST", path: "/tenants/{tenant}/knowledge/ask" },
    get_architecture_docs: { method: "GET", path: "/tenants/{tenant}/architecture-docs" },
    get_model_calibration_status: { method: "GET", path: "/tenants/{tenant}/calibration" },
    get_model_calibration_status_background: { method: "GET", path: "/tenants/{tenant}/calibration" },
    refresh_model_data: { method: "POST", path: "/tenants/{tenant}/calibration/refresh" },
    refresh_model_data_background: { method: "POST", path: "/tenants/{tenant}/calibration/refresh" },
    analyze_model_calibration_update: { method: "POST", path: "/tenants/{tenant}/calibration/analyze" },
    analyze_model_calibration_update_background: { method: "POST", path: "/tenants/{tenant}/calibration/analyze" },
    activate_model_calibration: { method: "POST", path: "/tenants/{tenant}/calibration/activate" },
    activate_model_calibration_background: { method: "POST", path: "/tenants/{tenant}/calibration/activate" },
    describe_routing_calculator: { method: "GET", path: "/tenants/{tenant}/routing/calculator" },
    simulate_routing: { method: "POST", path: "/tenants/{tenant}/routing/simulate" },
    run_diagnostics: { method: "POST", path: "/tenants/{tenant}/diagnostics/run" },
    run_diagnostics_background: { method: "POST", path: "/tenants/{tenant}/diagnostics/run" },
    file_diagnostic_issue: { method: "POST", path: "/tenants/{tenant}/diagnostics/issue" },
    file_diagnostic_issue_background: { method: "POST", path: "/tenants/{tenant}/diagnostics/issue" },
    git_overview: { method: "GET", path: "/tenants/{tenant}/repos/{repoId}/git" },
    git_overview_background: { method: "GET", path: "/tenants/{tenant}/repos/{repoId}/git" },
    refresh_repo: { method: "POST", path: "/tenants/{tenant}/repos/{repoId}/refresh" },
    merge_issue_branch: { method: "POST", path: "/tenants/{tenant}/repos/{repoId}/merge-issue" },
    merge_integration_branch: { method: "POST", path: "/tenants/{tenant}/repos/{repoId}/merge-integration" },
    branch_push_access: { method: "GET", path: "/tenants/{tenant}/repos/{repoId}/push-access" },
    grant_bot_branch_push: { method: "POST", path: "/tenants/{tenant}/repos/{repoId}/push-access" },
    promotion_overview: { method: "GET", path: "/tenants/{tenant}/promotions" },
    promotion_overview_background: { method: "GET", path: "/tenants/{tenant}/promotions" },
    open_integration_pr: { method: "POST", path: "/tenants/{tenant}/repos/{repoId}/integration-pr" },
    promote_integration_branch: { method: "POST", path: "/tenants/{tenant}/repos/{repoId}/promote" },
    promote_integration_branch_background: { method: "POST", path: "/tenants/{tenant}/repos/{repoId}/promote" },
    app_version: { method: "GET", path: "/version" },
  };

  // Event name -> Server-Sent Events stream. One row per event the web backend
  // emits: { path: "/events/automation-log", sse: "automation-log" }. `sse` is
  // the SSE `event:` type to listen for and defaults to the event name. Each
  // message's `data` is JSON and becomes the callback's `payload`, the same
  // `{ payload }` shape Tauri delivers. `listen()` does not substitute path
  // parameters, so this stream is one static path and the payload names the tenant.
  //
  // Every stream is session-scoped (the tenants the signed-in user belongs to),
  // resumes from the browser's automatic `Last-Event-ID`, carries a heartbeat
  // and may send a `resync` event when the client fell behind: refetch the log
  // (`get_recent_logs`) when it arrives. `system-permission-primed` is a macOS
  // desktop prompt and is not streamed.
  const EVENTS = {
    "job-log": { path: "/events/jobs", sse: "job-log" },
    "automation-log": { path: "/events/automation-log", sse: "automation-log" },
    "model-calibration-refreshed": { path: "/events/model-calibration", sse: "model-calibration-refreshed" },
  };

  class UnavailableOnWebError extends Error {
    constructor(kind, name) {
      super(`${name} is not available in the web version yet.`);
      this.name = "UnavailableOnWebError";
      this.code = "not_available_on_web";
      this.kind = kind;
      this.target = name;
    }
  }

  class ApiError extends Error {
    constructor(message, status, body) {
      super(message);
      this.name = "ApiError";
      this.code = "api_error";
      this.status = status;
      this.body = body;
    }
  }

  function isUnavailableOnWeb(error) {
    return Boolean(error) && error.code === "not_available_on_web";
  }

  function has(table, key) {
    return Object.prototype.hasOwnProperty.call(table, key);
  }

  // The web backend sets a script-readable CSRF cookie at sign-in (the session
  // cookie itself is HttpOnly and never reaches JavaScript) and requires its
  // value in this header on every state-changing request. Over HTTPS the cookie
  // carries the `__Host-` prefix.
  const CSRF_HEADER = "X-CSRF-Token";
  const CSRF_COOKIES = ["__Host-swarm_csrf", "swarm_csrf"];

  function readCsrfToken(cookieString) {
    const pairs = String(cookieString || "").split(";").map((part) => part.trim());
    for (const name of CSRF_COOKIES) {
      const found = pairs.find((pair) => pair.startsWith(`${name}=`));
      if (found) return decodeURIComponent(found.slice(name.length + 1));
    }
    return "";
  }

  // The desktop's commands are not tenant-aware. On the web the active tenant is
  // chosen once (`setTenant`, or the first tenant of the session) and filled into
  // any `{tenant}` placeholder the caller did not name. It is a path segment the
  // server re-checks for membership, never an authority of its own.
  const needsTenant = (route) => String(route.path).includes("{tenant}");

  function buildRequest(base, route, args, csrfToken) {
    const method = String(route.method || "POST").toUpperCase();
    const rest = { ...(args || {}) };
    const path = String(route.path).replace(/\{(\w+)\}/g, (_, key) => {
      const value = rest[key];
      delete rest[key];
      return encodeURIComponent(value === undefined || value === null ? "" : String(value));
    });
    const init = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
    let url = `${base}${path}`;
    const keys = Object.keys(rest);
    if (method === "GET" || method === "DELETE") {
      const query = new URLSearchParams();
      keys.forEach((key) => {
        if (rest[key] === undefined || rest[key] === null) return;
        const value = rest[key];
        query.set(key, typeof value === "object" ? JSON.stringify(value) : String(value));
      });
      const text = query.toString();
      if (text) url += `${url.includes("?") ? "&" : "?"}${text}`;
    } else if (keys.length) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(rest);
    }
    if (csrfToken && method !== "GET") init.headers[CSRF_HEADER] = csrfToken;
    return { url, init };
  }

  async function readBody(response) {
    if (response.status === 204) return null;
    const text = await response.text();
    if (!text) return null;
    try { return JSON.parse(text); } catch (_) { return text; }
  }

  function failureMessage(response, body) {
    if (typeof body === "string" && body.trim()) return body.trim();
    if (body && typeof body.error === "string") return body.error;
    if (body && typeof body.message === "string") return body.message;
    return `Request failed with status ${response.status}.`;
  }

  // `env` is injectable so both transports can be tested without a browser:
  //   tauri        the `window.__TAURI__` object, or undefined for web
  //   fetch / EventSource   the web transport's primitives
  //   csrfToken             () => the CSRF token to send on state-changing requests
  //   commands / events     override the tables (tests, or a future backend)
  //   onUnauthorized        (name) => void, called when a command other than
  //                         `web_session` is answered 401: the session expired
  function createApi(env = {}) {
    const base = env.base === undefined ? API_BASE : env.base;
    const commands = env.commands || COMMANDS;
    const events = env.events || EVENTS;
    const tauri = env.tauri;
    const desktop = Boolean(tauri && tauri.core && tauri.event);
    let selectedTenant = "";

    async function request(name, args) {
      const fetchFn = env.fetch;
      const csrfToken = typeof env.csrfToken === "function" ? env.csrfToken() : "";
      const { url, init } = buildRequest(base, commands[name], args, csrfToken);
      const response = await fetchFn(url, init);
      return { response, body: await readBody(response) };
    }

    async function activeTenant() {
      const fromEnv = typeof env.tenant === "function" ? await env.tenant() : "";
      if (fromEnv) return fromEnv;
      if (selectedTenant) return selectedTenant;
      if (!has(commands, "web_session")) return "";
      try {
        const { response, body } = await request("web_session", {});
        const first = response.ok && body && Array.isArray(body.tenants) ? body.tenants[0] : null;
        if (first && first.id) {
          selectedTenant = String(first.id);
          if (typeof env.rememberTenant === "function") env.rememberTenant(selectedTenant);
        }
      } catch (_) { /* the real call below reports the failure */ }
      return selectedTenant;
    }

    async function invoke(name, args) {
      if (desktop) return tauri.core.invoke(name, args);
      if (!has(commands, name)) throw new UnavailableOnWebError("command", name);
      const fetchFn = env.fetch;
      if (typeof fetchFn !== "function") throw new ApiError("This browser cannot reach the SWARM Automation service.", 0, null);
      const csrfToken = typeof env.csrfToken === "function" ? env.csrfToken() : "";
      let callArgs = args;
      if (needsTenant(commands[name]) && !(callArgs && callArgs.tenant)) {
        callArgs = { ...(callArgs || {}), tenant: await activeTenant() };
      }
      const { url, init } = buildRequest(base, commands[name], callArgs, csrfToken);
      let response;
      try {
        response = await fetchFn(url, init);
      } catch (error) {
        throw new ApiError(`Could not reach the SWARM Automation service: ${error && error.message ? error.message : error}`, 0, null);
      }
      const body = await readBody(response);
      if (!response.ok) {
        if (response.status === 401 && name !== "web_session" && typeof env.onUnauthorized === "function") env.onUnauthorized(name);
        throw new ApiError(failureMessage(response, body), response.status, body);
      }
      return body;
    }

    async function listen(event, callback) {
      if (desktop) return tauri.event.listen(event, callback);
      const noop = () => {};
      if (!has(events, event) || typeof env.EventSource !== "function") return noop;
      const route = events[event];
      const source = new env.EventSource(`${base}${route.path}`);
      source.addEventListener(route.sse || event, (message) => {
        let payload = message.data;
        try { payload = JSON.parse(message.data); } catch (_) { /* keep the raw text */ }
        callback({ event, payload });
      });
      return () => source.close();
    }

    return { invoke, listen, transport: desktop ? "tauri" : "web", setTenant: (id) => { selectedTenant = String(id || ""); } };
  }

  // The default instance reads the browser globals at call time, so it works
  // whenever Tauri injects its bridge and needs nothing in Node.
  function current() {
    const win = typeof window === "undefined" ? undefined : window;
    return createApi({
      tauri: win && win.__TAURI__,
      fetch: win && typeof win.fetch === "function" ? win.fetch.bind(win) : undefined,
      EventSource: win && win.EventSource,
      csrfToken: () => readCsrfToken(win && win.document ? win.document.cookie : ""),
      tenant: () => defaultTenant,
      rememberTenant: (id) => { defaultTenant = id; },
      onUnauthorized: (name) => expiryListeners.slice().forEach((listener) => { try { listener(name); } catch (_) { /* a listener must not mask the 401 */ } }),
    });
  }

  // The default instance is rebuilt per call, so the chosen tenant lives here.
  let defaultTenant = "";
  // Callbacks told that the web session is gone (a 401 from any command but
  // `web_session`). The desktop never produces one.
  const expiryListeners = [];

  return {
    API_BASE,
    COMMANDS,
    EVENTS,
    UnavailableOnWebError,
    ApiError,
    isUnavailableOnWeb,
    readCsrfToken,
    createApi,
    invoke: (name, args) => current().invoke(name, args),
    listen: (event, callback) => current().listen(event, callback),
    transport: () => current().transport,
    setTenant: (id) => { defaultTenant = String(id || ""); },
    getTenant: () => defaultTenant,
    onSessionExpired: (listener) => { if (typeof listener === "function") expiryListeners.push(listener); },
  };
});
