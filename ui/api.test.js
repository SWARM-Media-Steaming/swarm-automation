"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const api = require("./api.js");

const app = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");

// ----- Mock transports -----------------------------------------------------

function mockTauri(handlers = {}) {
  const calls = { invoke: [], listen: [], unlisten: 0 };
  return {
    calls,
    core: {
      invoke: async (name, args) => {
        calls.invoke.push([name, args]);
        if (!(name in handlers)) throw `unknown command ${name}`;
        return handlers[name](args);
      },
    },
    event: {
      listen: async (event, callback) => {
        calls.listen.push([event, callback]);
        return () => { calls.unlisten += 1; };
      },
    },
  };
}

function mockHttp(responses = {}) {
  const requests = [];
  const sources = [];
  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.closed = false;
      this.listeners = {};
      sources.push(this);
    }
    addEventListener(type, handler) { this.listeners[type] = handler; }
    emit(type, data) { this.listeners[type]({ data }); }
    close() { this.closed = true; }
  }
  const fetch = async (url, init) => {
    requests.push({ url, init });
    const key = `${init.method} ${url.split("?")[0]}`;
    if (!(key in responses)) return { ok: false, status: 404, text: async () => "" };
    const reply = responses[key];
    const status = reply.status || 200;
    return {
      ok: status >= 200 && status < 300,
      status,
      text: async () => (reply.raw !== undefined ? reply.raw : reply.body === undefined ? "" : JSON.stringify(reply.body)),
    };
  };
  return { requests, sources, fetch, EventSource: FakeEventSource };
}

const COMMANDS = {
  app_version: { method: "GET", path: "/version" },
  get_recent_logs: { method: "GET", path: "/logs" },
  simulate_routing: { path: "/routing/simulate" },
  open_repo_folder: { method: "POST", path: "/repos/{repoId}/open" },
};
const EVENTS = { "automation-log": { path: "/events/logs", sse: "log" } };

// ----- Adapter behaviour ---------------------------------------------------

test("desktop delegates invoke and listen to window.__TAURI__ untouched", async () => {
  const tauri = mockTauri({ app_version: () => "0.9.1", start_issue_worker: (a) => a });
  const desktop = api.createApi({ tauri, commands: {}, events: {} });
  assert.equal(desktop.transport, "tauri");
  assert.equal(await desktop.invoke("app_version"), "0.9.1");
  assert.deepEqual(await desktop.invoke("start_issue_worker", { runOnce: true }), { runOnce: true });
  assert.deepEqual(tauri.calls.invoke, [["app_version", undefined], ["start_issue_worker", { runOnce: true }]]);
  const callback = () => {};
  const unlisten = await desktop.listen("automation-log", callback);
  assert.deepEqual(tauri.calls.listen, [["automation-log", callback]]);
  unlisten();
  assert.equal(tauri.calls.unlisten, 1);
});

test("desktop errors reach the caller exactly as Tauri raised them", async () => {
  const desktop = api.createApi({ tauri: mockTauri() });
  await assert.rejects(desktop.invoke("nope"), (error) => error === "unknown command nope");
});

test("web is selected when window.__TAURI__ is absent", () => {
  assert.equal(api.createApi({ commands: COMMANDS }).transport, "web");
  assert.equal(api.createApi({ tauri: {} }).transport, "web");
});

test("web invoke maps a GET command to fetch with args in the query string", async () => {
  const http = mockHttp({ "GET /api/v1/logs": { body: ["a", "b"] } });
  const web = api.createApi({ fetch: http.fetch, commands: COMMANDS });
  assert.deepEqual(await web.invoke("get_recent_logs", { limit: 5000, tag: "x y" }), ["a", "b"]);
  assert.equal(http.requests[0].url, "/api/v1/logs?limit=5000&tag=x+y");
  assert.equal(http.requests[0].init.method, "GET");
  assert.equal(http.requests[0].init.body, undefined);
});

test("web invoke sends a JSON body for POST and fills path placeholders", async () => {
  const http = mockHttp({
    "POST /api/v1/routing/simulate": { body: { results: [] } },
    "POST /api/v1/repos/octo%2Fcat/open": { status: 204 },
  });
  const web = api.createApi({ fetch: http.fetch, commands: COMMANDS });
  assert.deepEqual(await web.invoke("simulate_routing", { inputs: { complexity: 7 } }), { results: [] });
  assert.equal(http.requests[0].init.body, JSON.stringify({ inputs: { complexity: 7 } }));
  assert.equal(http.requests[0].init.headers["Content-Type"], "application/json");
  assert.equal(await web.invoke("open_repo_folder", { repoId: "octo/cat" }), null);
  assert.equal(http.requests[1].url, "/api/v1/repos/octo%2Fcat/open", "the placeholder is encoded");
  assert.equal(http.requests[1].init.body, undefined, "a consumed arg is not repeated in the body");
});

test("web invoke rejects with a readable ApiError on an HTTP failure", async () => {
  const http = mockHttp({
    "GET /api/v1/version": { status: 500, body: { error: "database is down" } },
    "GET /api/v1/logs": { status: 502, raw: "bad gateway" },
  });
  const web = api.createApi({ fetch: http.fetch, commands: COMMANDS });
  await assert.rejects(web.invoke("app_version"), (error) => {
    assert.ok(error instanceof api.ApiError);
    assert.equal(error.status, 500);
    assert.equal(error.message, "database is down");
    return true;
  });
  await assert.rejects(web.invoke("get_recent_logs"), /bad gateway/);
  await assert.rejects(web.invoke("simulate_routing", {}), (error) => error.status === 404 && /status 404/.test(error.message));
});

test("web invoke reports a network failure as an ApiError, not a raw TypeError", async () => {
  const web = api.createApi({ fetch: async () => { throw new TypeError("Failed to fetch"); }, commands: COMMANDS });
  await assert.rejects(web.invoke("app_version"), (error) => error instanceof api.ApiError && /Failed to fetch/.test(error.message));
});

test("an unmapped command fails with the typed not-available-on-web error", async () => {
  const http = mockHttp();
  const web = api.createApi({ fetch: http.fetch, commands: COMMANDS });
  await assert.rejects(web.invoke("hide_to_tray"), (error) => {
    assert.ok(error instanceof api.UnavailableOnWebError);
    assert.ok(api.isUnavailableOnWeb(error));
    assert.equal(error.code, "not_available_on_web");
    assert.equal(error.target, "hide_to_tray");
    assert.match(error.message, /hide_to_tray is not available in the web version/);
    return true;
  });
  assert.equal(http.requests.length, 0, "no request is made for an unmapped command");
  assert.equal(api.isUnavailableOnWeb(new Error("x")), false);
  assert.equal(api.isUnavailableOnWeb(null), false);
});

test("an inherited property name is not treated as a mapped command", async () => {
  const web = api.createApi({ fetch: mockHttp().fetch, commands: COMMANDS });
  await assert.rejects(web.invoke("constructor"), api.UnavailableOnWebError);
  await assert.rejects(web.invoke("toString"), api.UnavailableOnWebError);
});

test("web listen opens an EventSource and delivers { payload } like Tauri", async () => {
  const http = mockHttp();
  const web = api.createApi({ EventSource: http.EventSource, events: EVENTS });
  const seen = [];
  const unlisten = await web.listen("automation-log", (event) => seen.push(event));
  assert.equal(http.sources.length, 1);
  assert.equal(http.sources[0].url, "/api/v1/events/logs");
  http.sources[0].emit("log", JSON.stringify({ line: "hello" }));
  http.sources[0].emit("log", "plain text");
  assert.deepEqual(seen, [
    { event: "automation-log", payload: { line: "hello" } },
    { event: "automation-log", payload: "plain text" },
  ]);
  unlisten();
  assert.equal(http.sources[0].closed, true);
});

test("web listen on an unmapped event resolves a harmless no-op", async () => {
  const http = mockHttp();
  const web = api.createApi({ EventSource: http.EventSource, events: EVENTS });
  const unlisten = await web.listen("system-permission-primed", () => {});
  assert.equal(typeof unlisten, "function");
  unlisten();
  assert.equal(http.sources.length, 0);
});

test("the default instance follows window.__TAURI__ at call time", async () => {
  const previous = globalThis.window;
  try {
    const tauri = mockTauri({ app_version: () => "1.2.3" });
    globalThis.window = { __TAURI__: tauri };
    assert.equal(api.transport(), "tauri");
    assert.equal(await api.invoke("app_version"), "1.2.3");
    globalThis.window = {};
    assert.equal(api.transport(), "web");
    await assert.rejects(api.invoke("hide_to_tray"), api.UnavailableOnWebError, "a desktop-only command has no web row");
  } finally {
    if (previous === undefined) delete globalThis.window; else globalThis.window = previous;
  }
});

test("the shipped tables are well-formed data", () => {
  const methods = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);
  for (const [name, route] of Object.entries(api.COMMANDS)) {
    assert.match(route.path, /^\//, `${name} path is absolute`);
    assert.ok(methods.has(String(route.method || "POST").toUpperCase()), `${name} method`);
  }
  for (const [name, route] of Object.entries(api.EVENTS)) assert.match(route.path, /^\//, `${name} path is absolute`);
});

// ----- Hosted-account endpoints and CSRF -----------------------------------

test("the web account commands map onto the backend's /api/v1 routes", async () => {
  const http = mockHttp({
    "GET /api/v1/session": { body: { authenticated: true } },
    "GET /api/v1/tenants/t1abc/provider-keys": { body: { keys: [] } },
    "PUT /api/v1/tenants/t1abc/provider-keys/model-data": { body: { configured: true } },
    "DELETE /api/v1/tenants/t1abc/provider-keys/claude": { status: 204 },
    "PUT /api/v1/tenants/t1abc/budgets": { body: {} },
    "GET /api/v1/tenants/t1abc/usage": { body: {} },
  });
  const web = api.createApi({ fetch: http.fetch, csrfToken: () => "tok" });
  assert.deepEqual(await web.invoke("web_session"), { authenticated: true });
  await web.invoke("web_list_provider_keys", { tenant: "t1abc" });
  await web.invoke("web_set_provider_key", { tenant: "t1abc", provider: "model-data", key: "sk-secret-value" });
  assert.equal(await web.invoke("web_delete_provider_key", { tenant: "t1abc", provider: "claude" }), null);
  await web.invoke("web_set_budgets", { tenant: "t1abc", minimum_remaining_percent: 10, provider_budgets_usd: { claude: 50 } });
  await web.invoke("web_get_usage", { tenant: "t1abc" });
  const put = http.requests[2];
  assert.equal(put.init.body, JSON.stringify({ key: "sk-secret-value" }), "path args are consumed; only the key is in the body");
  assert.equal(http.requests.length, 6);
});

test("the platform admin commands map onto /api/v1/admin and carry the CSRF token on changes", async () => {
  const http = mockHttp({
    "GET /api/v1/admin/users": { body: { users: [] } },
    "POST /api/v1/admin/users/u1%2Fx/promote": { body: { changed: true } },
    "POST /api/v1/admin/users/u2/demote": { body: { changed: true } },
  });
  const web = api.createApi({ fetch: http.fetch, csrfToken: () => "csrf-adm" });
  assert.deepEqual(await web.invoke("web_admin_list_users"), { users: [] });
  await web.invoke("web_admin_promote_user", { userId: "u1/x" });
  await web.invoke("web_admin_demote_user", { userId: "u2" });
  assert.deepEqual(
    http.requests.map((r) => r.init.headers["X-CSRF-Token"]),
    [undefined, "csrf-adm", "csrf-adm"],
  );
  assert.ok(http.requests.slice(1).every((r) => !r.init.body || r.init.body === "{}"), "the id is a path segment, not a body field");
  assert.ok(!/\{tenant\}/.test(JSON.stringify(api.COMMANDS.web_admin_list_users)), "admin routes are not tenant scoped");
});

test("web state-changing requests carry the CSRF token and reads do not", async () => {
  const http = mockHttp({
    "GET /api/v1/session": { body: {} },
    "PUT /api/v1/tenants/t1/budgets": { body: {} },
    "DELETE /api/v1/tenants/t1/provider-keys/grok": { status: 204 },
    "POST /api/v1/auth/logout": { status: 204 },
  });
  const web = api.createApi({ fetch: http.fetch, csrfToken: () => "csrf-123" });
  await web.invoke("web_session");
  await web.invoke("web_set_budgets", { tenant: "t1", minimum_remaining_percent: 5 });
  await web.invoke("web_delete_provider_key", { tenant: "t1", provider: "grok" });
  await web.invoke("web_logout");
  const headers = http.requests.map((r) => r.init.headers["X-CSRF-Token"]);
  assert.deepEqual(headers, [undefined, "csrf-123", "csrf-123", "csrf-123"]);
  assert.ok(http.requests.every((r) => r.init.credentials === "same-origin"), "cookies only go to this origin");

  const anonymous = mockHttp({ "POST /api/v1/auth/logout": { status: 204 } });
  await api.createApi({ fetch: anonymous.fetch }).invoke("web_logout");
  assert.equal(anonymous.requests[0].init.headers["X-CSRF-Token"], undefined, "no token, no header");
});

test("the CSRF token is read from the script-readable cookie, never from storage", () => {
  assert.equal(api.readCsrfToken("a=1; swarm_csrf=abc%2Bdef; b=2"), "abc+def");
  assert.equal(api.readCsrfToken("__Host-swarm_csrf=secure-one; swarm_csrf=plain"), "secure-one", "the __Host- cookie wins over HTTPS");
  assert.equal(api.readCsrfToken("swarm_session=not-readable-anyway"), "");
  assert.equal(api.readCsrfToken(""), "");
  assert.equal(api.readCsrfToken(undefined), "");
  for (const file of ["api.js", "app.js"]) {
    const source = fs.readFileSync(path.join(__dirname, file), "utf8");
    assert.doesNotMatch(source, /localStorage|sessionStorage/, `${file} keeps no token in web storage`);
  }
});

test("the default instance sends the CSRF cookie value from document.cookie", async () => {
  const previous = globalThis.window;
  const seen = [];
  globalThis.window = {
    document: { cookie: "swarm_csrf=from-cookie" },
    fetch: async (url, init) => { seen.push(init.headers["X-CSRF-Token"]); return { ok: true, status: 204, text: async () => "" }; },
  };
  try {
    await api.invoke("web_logout");
    assert.deepEqual(seen, ["from-cookie"]);
  } finally {
    if (previous === undefined) delete globalThis.window; else globalThis.window = previous;
  }
});

test("web-only account commands are never invoked by the desktop app code", () => {
  for (const name of Object.keys(api.COMMANDS).filter((key) => key.startsWith("web_"))) {
    assert.doesNotMatch(app, new RegExp(`invoke\\("${name}"`), `${name} is web-only`);
  }
});

// ----- app.js controllers run on both transports ---------------------------

// The same controller code that ships in app.js, bound to each transport's
// `invoke`, so a change that works on one and not the other fails here.
function transports() {
  const tauri = mockTauri({
    app_version: () => "0.9.1",
    simulate_routing: ({ inputs }) => ({ echo: inputs }),
  });
  const http = mockHttp({
    "GET /api/v1/version": { body: "0.9.1" },
    "POST /api/v1/routing/simulate": { body: { echo: { complexity: 7 } } },
  });
  return {
    tauri: api.createApi({ tauri, commands: {}, events: {} }),
    web: api.createApi({ fetch: http.fetch, commands: COMMANDS }),
    webUnmapped: api.createApi({ fetch: http.fetch, commands: {} }),
  };
}

function sliceApp(startMarker, endMarker) {
  const start = app.indexOf(startMarker);
  const end = app.indexOf(endMarker, start);
  assert.ok(start > 0 && end > start, `${startMarker.trim()} is extractable`);
  return app.slice(start, end);
}

function versionController(invoke) {
  const label = {
    textContent: "", title: "", attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
    removeAttribute(k) { delete this.attrs[k]; this.title = ""; },
  };
  const window = { SwarmVersion: require("./version.js") };
  const build = new Function("invoke", "window", "byId", `${sliceApp("  async function refreshAppVersion()", "\n  // -----", )}\nreturn refreshAppVersion;`);
  return { refresh: build(invoke, window, () => label), label };
}

function calculatorController(invoke) {
  const status = [];
  const rendered = [];
  const state = { routingCalculator: { request: 0, timer: null, payload: null } };
  const window = { clearTimeout: () => {} };
  const build = new Function(
    "invoke", "state", "window", "setCalcStatus", "readCalcForm", "renderCalcResults", "errorText",
    `${sliceApp("  async function runRoutingCalculator()", "  function calcPill")}\nreturn runRoutingCalculator;`,
  );
  const run = build(
    invoke, state, window,
    (text, isError) => status.push([text, Boolean(isError)]),
    () => ({ complexity: 7 }),
    (payload) => rendered.push(payload),
    (error) => (typeof error === "string" ? error : error.message),
  );
  return { run, status, rendered };
}

for (const name of ["tauri", "web"]) {
  test(`the version label works on the ${name} transport`, async () => {
    const { refresh, label } = versionController(transports()[name].invoke);
    await refresh();
    assert.equal(label.textContent, "v0.9.1");
    assert.equal(label.title, "Running build v0.9.1");
  });

  test(`the routing calculator works on the ${name} transport`, async () => {
    const { run, rendered, status } = calculatorController(transports()[name].invoke);
    await run();
    assert.deepEqual(rendered, [{ echo: { complexity: 7 } }]);
    assert.deepEqual(status.at(-1), ["", false]);
  });
}

test("an unmapped command degrades gracefully in the UI instead of throwing", async () => {
  const { invoke } = transports().webUnmapped;
  const version = versionController(invoke);
  await version.refresh();
  assert.equal(version.label.textContent, "", "the version label simply stays blank");

  const calculator = calculatorController(invoke);
  await calculator.run();
  assert.deepEqual(calculator.rendered, []);
  assert.deepEqual(calculator.status.at(-1), ["simulate_routing is not available in the web version yet.", true]);
});

// ----- One seam, nothing around it -----------------------------------------

test("api.js loads before every module that uses it, and app.js takes the adapter from it", () => {
  assert.match(html, /<script src="api\.js"><\/script>/);
  assert.ok(html.indexOf('src="api.js"') < html.indexOf('src="app.js"'));
  assert.match(app, /const \{ invoke, listen \} = window\.SwarmApi;/);
});

test("nothing in ui/ outside api.js talks to Tauri or the network directly", () => {
  const files = fs.readdirSync(__dirname).filter((f) => f.endsWith(".js") && !f.endsWith(".test.js") && f !== "api.js");
  assert.ok(files.includes("app.js"));
  for (const file of files) {
    const source = fs.readFileSync(path.join(__dirname, file), "utf8");
    assert.doesNotMatch(source, /__TAURI__/, `${file} must use SwarmApi, not window.__TAURI__`);
    assert.doesNotMatch(source, /\b(fetch|XMLHttpRequest|EventSource)\s*\(|new\s+(XMLHttpRequest|EventSource)\b/, `${file} must not open its own transport`);
  }
  assert.doesNotMatch(html, /\sstyle="|<script>(?!\s*<\/script>)/, "no inline styles or scripts");
});

test("every command app.js invokes fails typed (never silently) while it has no web route", async () => {
  const names = [...new Set([...app.matchAll(/\binvoke\("([a-z_0-9]+)"/g)].map((m) => m[1]))];
  assert.ok(names.length > 20, "the command list was found");
  const web = api.createApi({ fetch: mockHttp().fetch, commands: {} });
  for (const name of names) {
    await assert.rejects(web.invoke(name), api.UnavailableOnWebError, `${name} fails typed while unmapped`);
  }
});

// ----- The desktop commands over REST/SSE (issue #419) -----------------------

// The hosted web app lives in its own repository (Chomp); its command table is
// documented there, so the documentation cross-checks below run only if the
// document is present.
const docsPath = path.join(__dirname, "..", "docs", "web-architecture.md");
const docs = fs.existsSync(docsPath) ? fs.readFileSync(docsPath, "utf8") : "";

test("every command app.js invokes has a web row or is listed as intentionally removed", () => {
  const names = [...new Set([...app.matchAll(/\binvoke\("([a-z_0-9]+)"/g)].map((m) => m[1]))];
  for (const name of names) {
    if (Object.prototype.hasOwnProperty.call(api.COMMANDS, name)) continue;
    if (docs) assert.match(docs, new RegExp("`" + name + "`"), `${name} has no web row and is not documented as removed`);
  }
});

test("every desktop event app.js listens to has a stream or is documented as removed", () => {
  const events = [...new Set([...app.matchAll(/\blisten\("([a-z_\-]+)"/g)].map((m) => m[1]))];
  assert.ok(events.includes("automation-log"));
  for (const event of events) {
    if (Object.prototype.hasOwnProperty.call(api.EVENTS, event)) continue;
    if (docs) assert.match(docs, new RegExp("`" + event + "`"), `${event} has no stream and is not documented as removed`);
  }
});

test("desktop commands fill the active tenant from the session, and an explicit tenant wins", async () => {
  const http = mockHttp({
    "GET /api/v1/session": { body: { authenticated: true, tenants: [{ id: "t1first" }, { id: "t2second" }] } },
    "GET /api/v1/tenants/t1first/history": { body: { records: [] } },
    "PUT /api/v1/tenants/t1first/config": { body: { repositories: [] } },
    "GET /api/v1/tenants/t2second/logs": { body: ["x"] },
  });
  const web = api.createApi({ fetch: http.fetch });
  assert.deepEqual(await web.invoke("get_execution_history_background", { repoIds: ["a__b"], offset: 10 }), { records: [] });
  assert.deepEqual(await web.invoke("save_config", { config: { repositories: [] } }), { repositories: [] });
  assert.deepEqual(await web.invoke("get_recent_logs", { tenant: "t2second", limit: 5 }), ["x"]);
  const urls = http.requests.map((request) => request.url);
  assert.equal(urls.filter((url) => url.endsWith("/session")).length, 1, "the session is read once");
  assert.ok(urls.includes("/api/v1/tenants/t1first/history?repoIds=%5B%22a__b%22%5D&offset=10"), urls.join("\n"));
  const save = http.requests.find((request) => request.init.method === "PUT");
  assert.equal(save.init.body, JSON.stringify({ config: { repositories: [] } }), "the tenant is a path segment, not a body field");
});

test("setTenant chooses the tenant without asking the session", async () => {
  const http = mockHttp({ "GET /api/v1/tenants/t9chosen/provider-usage": { body: [] } });
  const web = api.createApi({ fetch: http.fetch });
  web.setTenant("t9chosen");
  await web.invoke("check_provider_usage_background");
  assert.deepEqual(http.requests.map((request) => request.url), ["/api/v1/tenants/t9chosen/provider-usage"]);
});

test("the version and tenant-free commands need no tenant", async () => {
  const http = mockHttp({ "GET /api/v1/version": { body: "0.1.0" } });
  const web = api.createApi({ fetch: http.fetch });
  assert.equal(await web.invoke("app_version"), "0.1.0");
  assert.equal(http.requests.length, 1);
});

test("the log and calibration streams deliver the desktop's payload shape", async () => {
  const http = mockHttp();
  const web = api.createApi({ EventSource: http.EventSource });
  const lines = [];
  await web.listen("automation-log", (event) => lines.push(event.payload));
  await web.listen("model-calibration-refreshed", () => {});
  assert.deepEqual(http.sources.map((source) => source.url), ["/api/v1/events/automation-log", "/api/v1/events/model-calibration"]);
  http.sources[0].emit("automation-log", JSON.stringify({ source: "Issue worker", stream: "stdout", line: "Adversarial UAT for issue #12: round 1 of 3.", timestamp: 1 }));
  assert.equal(lines[0].line, "Adversarial UAT for issue #12: round 1 of 3.");
});

// ----- Avatar menu: the profile command on both transports -------------------

test("web_me is a plain session GET on the web and a Tauri command on the desktop", async () => {
  const profile = { login: "octo", display_name: "Octo Cat", is_platform_admin: false };
  const http = mockHttp({ "GET /api/v1/me": { body: profile } });
  const web = api.createApi({ fetch: http.fetch, csrfToken: () => "tok" });
  assert.deepEqual(await web.invoke("web_me"), profile);
  assert.equal(http.requests[0].init.headers["X-CSRF-Token"], undefined, "a read carries no CSRF token");
  assert.ok(!/\{tenant\}/.test(JSON.stringify(api.COMMANDS.web_me)), "the profile is session-scoped, not tenant-scoped");
  const desktop = api.createApi({ tauri: mockTauri({ web_me: () => profile }) });
  assert.equal(desktop.transport, "tauri");
  assert.deepEqual(await desktop.invoke("web_me"), profile, "the desktop adapter delegates unchanged");
});

// ----- Admin audit log: both transports (#444) --------------------------------

test("web_admin_audit_log is a plain admin GET on the web and delegates unchanged on the desktop", async () => {
  const body = { entries: [{ id: 1, action: "admin.promote", actor_login: "a", target_login: "b", created_at: 1 }] };
  const http = mockHttp({ "GET /api/v1/admin/audit-log": { body } });
  const web = api.createApi({ fetch: http.fetch, csrfToken: () => "tok" });
  assert.deepEqual(await web.invoke("web_admin_audit_log"), body);
  assert.ok(!/\{tenant\}/.test(JSON.stringify(api.COMMANDS.web_admin_audit_log)), "platform-wide, not tenant-scoped");
  const tauri = mockTauri({ web_admin_audit_log: () => body });
  const desktop = api.createApi({ tauri });
  assert.deepEqual(await desktop.invoke("web_admin_audit_log"), body);
  assert.equal(tauri.calls.invoke[0][0], "web_admin_audit_log");
});
