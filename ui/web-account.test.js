"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const api = require("./api.js");
const account = require("./web-account.js");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "style.css"), "utf8");
const app = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");

// ----- A mocked HTTP transport (the same shape api.test.js uses) --------------

function mockHttp(routes = {}) {
  const requests = [];
  const fetch = async (url, init) => {
    requests.push({ url, init });
    const key = `${init.method} ${url.split("?")[0]}`;
    const route = routes[key];
    if (route === undefined) return { ok: false, status: 404, text: async () => "" };
    const reply = typeof route === "function" ? route(init) : route;
    const status = reply.status || 200;
    return { ok: status >= 200 && status < 300, status, text: async () => (reply.body === undefined ? "" : JSON.stringify(reply.body)) };
  };
  return { requests, fetch };
}

const OWNER = { id: "t0000000000000001", account_login: "acme", account_type: "Organization", status: "active", role: "owner" };
const MEMBER = { id: "t0000000000000002", account_login: "octo", account_type: "User", status: "active", role: "member" };
const SUSPENDED = { id: "t0000000000000003", account_login: "gone", account_type: "User", status: "suspended", role: "owner" };

function signedIn(tenants = [OWNER, MEMBER], extra = {}) {
  return { authenticated: true, user: { login: "mona" }, csrf_token: "csrf", tenants, install_url: "https://github.com/apps/swarm/installations/new", ...extra };
}

function tenantRoutes(id, overrides = {}) {
  return {
    [`GET /api/v1/tenants/${id}/members`]: { body: { members: [{ login: "zed", role: "member" }, { login: "mona", role: "owner" }] } },
    [`GET /api/v1/tenants/${id}/provider-keys`]: { body: { keys: [{ provider: "claude", configured: true, updated_at: 1767225600, updated_by: "mona" }] } },
    [`GET /api/v1/tenants/${id}/quotas`]: { body: { plan: { max_concurrent_jobs: 3, monthly_spend_cap_usd: null }, budgets: { minimum_remaining_percent: 10, provider_budgets_usd: { claude: 50 } } } },
    [`GET /api/v1/tenants/${id}/usage`]: {
      body: {
        period: "2026-10",
        total_spend_usd: 12.5,
        plan: { max_concurrent_jobs: 3, monthly_spend_cap_usd: null },
        budgets: { minimum_remaining_percent: 10, provider_budgets_usd: { claude: 50 } },
        active_jobs: 1,
        providers: [
          { provider: "claude", status: 0, remaining_percent: 75, source: "budget", detail: null, spend_usd: 12.5, budget_usd: 50, priced_invocations: 4, unpriced_invocations: 1 },
          { provider: "codex", status: 2, remaining_percent: null, source: "unavailable", detail: null, spend_usd: 0, budget_usd: null, priced_invocations: 0, unpriced_invocations: 0 },
        ],
      },
    },
    ...overrides,
  };
}

function controllerFor(routes, options = {}) {
  const http = mockHttp(routes);
  const web = api.createApi({ fetch: http.fetch, csrfToken: () => "csrf", onUnauthorized: options.onUnauthorized });
  const chosen = [];
  const controller = account.createAccountController({ invoke: web.invoke, setTenant: (id) => { chosen.push(id); web.setTenant(id); } });
  return { http, web, controller, chosen };
}

// ----- Session ----------------------------------------------------------------

test("a signed-out or malformed session never opens the signed-in views", () => {
  for (const body of [null, undefined, {}, "x", { authenticated: "yes" }, { authenticated: false, tenants: [OWNER] }]) {
    const session = account.interpretSession(body);
    assert.equal(session.authenticated, false);
    assert.deepEqual(session.tenants, []);
    assert.equal(session.login, "");
  }
  assert.equal(account.interpretSession({ login_url: "https://evil.example/x" }).loginUrl, "/api/v1/auth/github/login", "only a same-site login path is used");
});

test("the session is read into a user, tenants and a safe install link", () => {
  const session = account.interpretSession(signedIn([OWNER, { id: "" }, null, MEMBER]));
  assert.equal(session.authenticated, true);
  assert.equal(session.login, "mona");
  assert.deepEqual(session.tenants.map((tenant) => tenant.id), [OWNER.id, MEMBER.id]);
  assert.deepEqual(session.tenants[0], { id: OWNER.id, account: "acme", accountType: "Organization", status: "active", role: "owner" });
  assert.equal(session.installUrl, "https://github.com/apps/swarm/installations/new");
  assert.equal(account.interpretSession(signedIn([], { install_url: "javascript:alert(1)" })).installUrl, "", "a non-https install link is dropped");
  assert.equal(account.interpretSession(signedIn([], { install_url: "http://github.com/apps/x" })).installUrl, "");
  assert.equal(account.interpretSession(signedIn([], { install_url: null })).installUrl, "");
});

test("an unknown role or status degrades to the least privilege, not to owner", () => {
  const [tenant] = account.interpretSession(signedIn([{ id: "t1", role: "root", status: "weird" }])).tenants;
  assert.equal(tenant.role, "member");
  assert.equal(tenant.status, "active", "an unknown status is shown, but writes still need an owner");
  assert.equal(account.canManage(tenant), false);
});

test("the tenant switcher keeps a still-granted choice and otherwise falls back to the first", () => {
  const tenants = account.interpretSession(signedIn()).tenants;
  assert.equal(account.pickTenant(tenants, MEMBER.id).id, MEMBER.id);
  assert.equal(account.pickTenant(tenants, "t-gone").id, OWNER.id);
  assert.equal(account.pickTenant(tenants, "").id, OWNER.id);
  assert.equal(account.pickTenant([], "x"), null);
});

test("only an owner of an active tenant can manage keys and budgets", () => {
  const [owner, member, suspended] = account.interpretSession(signedIn([OWNER, MEMBER, SUSPENDED])).tenants;
  assert.equal(account.canManage(owner), true);
  assert.equal(account.canManage(member), false);
  assert.equal(account.canManage(suspended), false);
  assert.equal(account.canManage(null), false);
  assert.equal(account.readOnlyReason(owner), "");
  assert.match(account.readOnlyReason(member), /Only a tenant owner/);
  assert.match(account.readOnlyReason(suspended), /not active/);
  assert.match(account.readOnlyReason(null), /Select a tenant/);
  assert.deepEqual(account.tenantStatus(owner), { label: "Active", tone: "running" });
  assert.deepEqual(account.tenantStatus(suspended), { label: "Suspended", tone: "paused" });
  assert.deepEqual(account.tenantStatus({ status: "deleted" }), { label: "Removed", tone: "error" });
});

test("members list owners first, then alphabetically, and ignores junk rows", () => {
  const rows = account.memberRows([{ login: "zed", role: "member" }, null, { login: "amy", role: "member" }, { login: "mona", role: "owner" }, { role: "owner" }]);
  assert.deepEqual(rows.map((row) => `${row.login}:${row.role}`), ["mona:owner", "amy:member", "zed:member"]);
  assert.equal(rows[0].roleLabel, "Owner");
  assert.deepEqual(account.memberRows(undefined), []);
});

// ----- Provider keys are write-only -----------------------------------------------

test("every provider has a row, configured or not, and no row carries a key", () => {
  const rows = account.keyRows({ keys: [{ provider: "claude", configured: true, updated_at: 1767225600, updated_by: "mona", key: "sk-must-never-appear" }, { provider: "grok", configured: false }] });
  assert.deepEqual(rows.map((row) => row.id), ["claude", "codex", "grok", "model-data"]);
  assert.deepEqual(rows.map((row) => row.configured), [true, false, false, false]);
  assert.match(rows[0].detail, /Updated 2026-01-01 00:00 UTC by mona/);
  assert.equal(rows[0].tone, "running");
  assert.equal(rows[1].status, "Not set");
  assert.doesNotMatch(JSON.stringify(rows), /sk-must-never-appear/);
  assert.equal(account.keyRows(null).every((row) => !row.configured), true);
});

test("key validation mirrors the server's shape rules and never echoes the value", () => {
  assert.equal(account.validateKey("  sk-ant-1234567890  ").key, "sk-ant-1234567890", "surrounding whitespace is trimmed");
  for (const bad of ["", "   ", null, undefined, "k3y-xz", "has space inside-key", "line\nbreak-key-123", "x".repeat(account.MAX_KEY_LENGTH + 1), "näive-key-123456"]) {
    const result = account.validateKey(bad);
    assert.equal(result.ok, false, JSON.stringify(bad));
    assert.equal(result.key, "");
    if (typeof bad === "string" && bad.trim().length > 4) assert.ok(!result.message.includes(bad), "the message never contains the value");
  }
  assert.equal(account.validateKey("12345678").ok, true);
  assert.equal(account.validateKey("1234567").ok, false);
});

// ----- Quota and budget ----------------------------------------------------------------

test("unreported quota is unavailable, never zero or a full bar", () => {
  const view = account.quotaView(null, null);
  assert.equal(view.totalSpendText, "—");
  assert.equal(view.activeJobsText, "—");
  assert.equal(view.maxJobsText, "—");
  assert.equal(view.monthlyCapText, "—");
  assert.equal(view.minimumPercent, null);
  assert.deepEqual(view.rows.map((row) => row.id), ["claude", "codex", "grok"], "the model-data key has no budget");
  for (const row of view.rows) {
    assert.equal(row.remainingText, "—");
    assert.equal(row.remainingPercent, null);
    assert.equal(row.spendText, "—");
    assert.equal(row.stateLabel, "Unavailable");
  }
});

test("quota rows show budget, spend and the provider state from the usage report", () => {
  const routes = tenantRoutes("t1");
  const view = account.quotaView(routes["GET /api/v1/tenants/t1/quotas"].body, routes["GET /api/v1/tenants/t1/usage"].body);
  const [claude, codex, grok] = view.rows;
  assert.equal(view.period, "2026-10");
  assert.equal(view.totalSpendText, "$12.50");
  assert.equal(view.activeJobsText, "1");
  assert.equal(view.maxJobsText, "3");
  assert.equal(view.monthlyCapText, "No cap");
  assert.equal(view.minimumPercent, 10);
  assert.equal(claude.remainingText, "75%");
  assert.equal(claude.remainingPercent, 75);
  assert.equal(claude.budgetText, "$50.00");
  assert.equal(claude.stateLabel, "Usable");
  assert.equal(claude.invocationsText, "4 priced, 1 unpriced");
  assert.equal(codex.remainingText, "—", "no budget and no provider report -> unavailable");
  assert.equal(codex.remainingPercent, null);
  assert.equal(codex.budgetText, "No budget");
  assert.equal(grok.invocationsText, "—", "a provider the report omits is unavailable, not 0 invocations");
  assert.deepEqual(view.budgets, { claude: "50", codex: "", grok: "" });
  assert.equal(account.usageState(1).tone, "paused");
});

test("a monthly cap is shown in dollars and a bar is clamped to 0-100", () => {
  const view = account.quotaView({ plan: { max_concurrent_jobs: 2, monthly_spend_cap_usd: 1200 }, budgets: { minimum_remaining_percent: 5, provider_budgets_usd: {} } }, {
    period: "2026-10", total_spend_usd: 0, active_jobs: 0,
    providers: [{ provider: "claude", status: 1, remaining_percent: 140, source: "provider", spend_usd: 0, budget_usd: null, priced_invocations: 0, unpriced_invocations: 0 }],
  });
  assert.equal(view.monthlyCapText, "$1,200.00");
  assert.equal(view.totalSpendText, "$0.00", "a reported zero is shown as zero");
  assert.equal(view.rows[0].remainingPercent, 100);
  assert.equal(view.rows[0].stateLabel, "Below minimum");
});

test("the budget form becomes the backend's body, with its limits checked first", () => {
  assert.deepEqual(account.budgetPayload({ minimum_remaining_percent: "15", provider_budgets_usd: { claude: "50", codex: "", grok: " 7.5 " } }), {
    ok: true, payload: { minimum_remaining_percent: 15, provider_budgets_usd: { claude: 50, grok: 7.5 } },
  });
  assert.deepEqual(account.budgetPayload({ minimum_remaining_percent: "0", provider_budgets_usd: {} }).payload, { minimum_remaining_percent: 0, provider_budgets_usd: {} }, "0% is a valid reserve");
  for (const minimum of ["", "abc", "-1", "101", "NaN", "Infinity"]) {
    const result = account.budgetPayload({ minimum_remaining_percent: minimum, provider_budgets_usd: {} });
    assert.equal(result.ok, false, minimum);
    assert.match(result.message, /0 to 100/);
  }
  for (const budget of ["0", "-5", "abc", "Infinity"]) {
    const result = account.budgetPayload({ minimum_remaining_percent: "10", provider_budgets_usd: { codex: budget } });
    assert.equal(result.ok, false, budget);
    assert.match(result.message, /Codex budget must be a positive number/);
  }
  assert.equal(account.budgetPayload({ minimum_remaining_percent: "10", provider_budgets_usd: { "model-data": "5" } }).payload.provider_budgets_usd["model-data"], undefined, "only job providers carry a budget");
});

// ----- The controller over the web transport ------------------------------------------------

test("sign-in loads the session, picks the first tenant and points the adapter at it", async () => {
  const { http, controller, chosen } = controllerFor({ "GET /api/v1/session": { body: signedIn() }, ...tenantRoutes(OWNER.id) });
  await controller.loadSession();
  assert.equal(controller.state.session.authenticated, true);
  assert.equal(controller.state.tenant.id, OWNER.id);
  assert.deepEqual(chosen, [OWNER.id]);
  await controller.loadTenantData();
  assert.deepEqual(controller.state.members.map((member) => member.login), ["mona", "zed"]);
  assert.equal(controller.state.keys[0].configured, true);
  assert.equal(controller.state.quota.totalSpendText, "$12.50");
  assert.deepEqual(controller.state.errors, {});
  assert.ok(http.requests.every((request) => request.init.method === "GET"), "loading is read-only");
});

test("a signed-out session leaves no tenant and no data", async () => {
  const { controller, http } = controllerFor({ "GET /api/v1/session": { body: { authenticated: false, login_url: "/api/v1/auth/github/login", install_url: null } } });
  await controller.loadSession();
  assert.equal(controller.state.session.authenticated, false);
  assert.equal(controller.state.tenant, null);
  assert.equal(controller.state.expired, false, "never having signed in is not an expiry");
  await controller.loadTenantData();
  assert.equal(http.requests.length, 1, "no tenant call without a tenant");
});

test("a session that disappears after sign-in is reported as expired", async () => {
  let answer = signedIn();
  const { controller } = controllerFor({ "GET /api/v1/session": () => ({ body: answer }) });
  await controller.loadSession();
  assert.equal(controller.state.expired, false);
  answer = { authenticated: false };
  await controller.loadSession();
  assert.equal(controller.state.expired, true);
  assert.equal(controller.state.session.authenticated, false);
  answer = signedIn();
  await controller.loadSession();
  assert.equal(controller.state.expired, false, "signing in again clears it");
});

test("a 401 from any command marks the session expired and keeps the error readable", async () => {
  const seen = [];
  const { controller } = controllerFor({
    "GET /api/v1/session": { body: signedIn([OWNER]) },
    ...tenantRoutes(OWNER.id, { [`PUT /api/v1/tenants/${OWNER.id}/provider-keys/claude`]: { status: 401, body: { error: "Sign in to continue." } } }),
  }, { onUnauthorized: (name) => seen.push(name) });
  await controller.loadSession();
  const result = await controller.saveKey("claude", "sk-ant-1234567890");
  assert.deepEqual(result, { ok: false, expired: true, message: "Sign in to continue." });
  assert.equal(controller.state.expired, true);
  assert.deepEqual(seen, ["web_set_provider_key"], "the adapter told the page");
  controller.markExpired();
  assert.equal(controller.state.session.authenticated, false);
});

test("the session read itself never reports an expiry through the adapter hook", async () => {
  const seen = [];
  const { web } = controllerFor({ "GET /api/v1/session": { status: 401, body: { error: "no" } } }, { onUnauthorized: (name) => seen.push(name) });
  await assert.rejects(web.invoke("web_session"));
  assert.deepEqual(seen, []);
});

test("switching tenant re-points the adapter and reloads that tenant's data only", async () => {
  const { http, controller, chosen } = controllerFor({
    "GET /api/v1/session": { body: signedIn() },
    ...tenantRoutes(OWNER.id),
    ...tenantRoutes(MEMBER.id, { [`GET /api/v1/tenants/${MEMBER.id}/members`]: { body: { members: [{ login: "octo", role: "owner" }] } } }),
  });
  await controller.loadSession();
  await controller.loadTenantData();
  await controller.selectTenant(MEMBER.id);
  assert.equal(controller.state.tenant.id, MEMBER.id);
  assert.deepEqual(chosen, [OWNER.id, MEMBER.id]);
  assert.deepEqual(controller.state.members.map((member) => member.login), ["octo"]);
  const afterSwitch = http.requests.filter((request) => request.url.includes(MEMBER.id));
  assert.equal(afterSwitch.length, 4);
  await controller.selectTenant("t-not-in-session");
  assert.equal(controller.state.tenant.id, MEMBER.id, "an unknown tenant is ignored, the server would answer 404 anyway");
});

test("one failing section is named without blanking the others", async () => {
  const { controller } = controllerFor({
    "GET /api/v1/session": { body: signedIn([OWNER]) },
    ...tenantRoutes(OWNER.id, {
      [`GET /api/v1/tenants/${OWNER.id}/members`]: { status: 500, body: { error: "database is down" } },
      [`GET /api/v1/tenants/${OWNER.id}/usage`]: { status: 501, body: { error: "not available yet" } },
    }),
  });
  await controller.loadSession();
  await controller.loadTenantData();
  assert.equal(controller.state.errors.members, "database is down");
  assert.equal(controller.state.errors.quota, "not available yet");
  assert.equal(controller.state.errors.keys, undefined);
  assert.deepEqual(controller.state.members, []);
  assert.equal(controller.state.keys[0].configured, true);
  assert.equal(controller.state.quota.totalSpendText, "—", "no usage report: unavailable, not $0.00");
  assert.equal(controller.state.quota.maxJobsText, "3", "the plan still came from /quotas");
});

test("saving a key sends it once, refreshes the metadata and keeps nothing", async () => {
  const secret = "sk-ant-super-secret-123456";
  let configured = false;
  const { http, controller } = controllerFor({
    "GET /api/v1/session": { body: signedIn([OWNER]) },
    [`PUT /api/v1/tenants/${OWNER.id}/provider-keys/codex`]: (init) => { configured = true; return { body: { provider: "codex", configured: true, key_echo: JSON.parse(init.body).key ? "n/a" : "" } }; },
    [`GET /api/v1/tenants/${OWNER.id}/provider-keys`]: () => ({ body: { keys: [{ provider: "codex", configured, updated_at: 1767225600, updated_by: "mona" }] } }),
  });
  await controller.loadSession();
  const result = await controller.saveKey("codex", ` ${secret} `);
  assert.deepEqual(result, { ok: true, message: "Codex key saved." });
  const put = http.requests.find((request) => request.init.method === "PUT");
  assert.equal(put.url, `/api/v1/tenants/${OWNER.id}/provider-keys/codex`, "the provider is a path segment");
  assert.equal(put.init.body, JSON.stringify({ key: secret }), "only the trimmed key is in the body");
  assert.equal(put.init.headers["X-CSRF-Token"], "csrf");
  assert.equal(controller.state.keys.find((row) => row.id === "codex").configured, true);
  assert.doesNotMatch(JSON.stringify(controller.state), new RegExp(secret), "the controller does not retain the key");
  assert.doesNotMatch(JSON.stringify(result), new RegExp(secret));
});

test("a rejected key shape never reaches the network, and the failure text omits the key", async () => {
  const { http, controller } = controllerFor({ "GET /api/v1/session": { body: signedIn([OWNER]) } });
  await controller.loadSession();
  const before = http.requests.length;
  for (const bad of ["", "tiny", "has a space in it"]) {
    const result = await controller.saveKey("claude", bad);
    assert.equal(result.ok, false);
    assert.ok(!result.message.includes("has a space in it"));
  }
  assert.equal((await controller.saveKey("not-a-provider", "sk-ant-1234567890")).message, "Unknown provider.");
  assert.equal(http.requests.length, before);

  const failing = controllerFor({
    "GET /api/v1/session": { body: signedIn([OWNER]) },
    [`PUT /api/v1/tenants/${OWNER.id}/provider-keys/claude`]: { status: 400, body: { error: "The key is too short." } },
  });
  await failing.controller.loadSession();
  const result = await failing.controller.saveKey("claude", "sk-ant-12345678");
  assert.equal(result.message, "The key is too short.");
  assert.equal(failing.controller.state.errors.keys, "The key is too short.");
});

test("a member or a suspended tenant cannot write, and no request is made", async () => {
  const { http, controller } = controllerFor({ "GET /api/v1/session": { body: signedIn([MEMBER, SUSPENDED]) } });
  await controller.loadSession();
  const before = http.requests.length;
  assert.match((await controller.saveKey("claude", "sk-ant-1234567890")).message, /Only a tenant owner/);
  assert.match((await controller.removeKey("claude")).message, /Only a tenant owner/);
  assert.match((await controller.saveBudgets({ minimum_remaining_percent: "10", provider_budgets_usd: {} })).message, /Only a tenant owner/);
  await controller.selectTenant(SUSPENDED.id).catch(() => {});
  assert.match((await controller.saveKey("claude", "sk-ant-1234567890")).message, /not active/);
  assert.equal(http.requests.filter((request) => request.init.method !== "GET").length, 0);
  assert.ok(http.requests.length >= before);
});

test("removing a key calls DELETE and refreshes", async () => {
  let configured = true;
  const { http, controller } = controllerFor({
    "GET /api/v1/session": { body: signedIn([OWNER]) },
    [`DELETE /api/v1/tenants/${OWNER.id}/provider-keys/claude`]: () => { configured = false; return { status: 204 }; },
    [`GET /api/v1/tenants/${OWNER.id}/provider-keys`]: () => ({ body: { keys: [{ provider: "claude", configured }] } }),
  });
  await controller.loadSession();
  assert.deepEqual(await controller.removeKey("claude"), { ok: true, message: "Claude key removed." });
  assert.equal(controller.state.keys[0].configured, false);
  assert.ok(http.requests.some((request) => request.init.method === "DELETE" && request.init.headers["X-CSRF-Token"] === "csrf"));
});

test("saving budgets validates, PUTs the body and reloads the numbers", async () => {
  let saved = null;
  const { http, controller } = controllerFor({
    "GET /api/v1/session": { body: signedIn([OWNER]) },
    ...tenantRoutes(OWNER.id, {
      [`PUT /api/v1/tenants/${OWNER.id}/budgets`]: (init) => { saved = JSON.parse(init.body); return { body: saved }; },
    }),
  });
  await controller.loadSession();
  const bad = await controller.saveBudgets({ minimum_remaining_percent: "200", provider_budgets_usd: {} });
  assert.equal(bad.ok, false);
  assert.equal(saved, null, "an invalid form is not sent");
  const ok = await controller.saveBudgets({ minimum_remaining_percent: "20", provider_budgets_usd: { claude: "80", codex: "" } });
  assert.deepEqual(ok, { ok: true, message: "Budgets saved." });
  assert.deepEqual(saved, { minimum_remaining_percent: 20, provider_budgets_usd: { claude: 80 } });
  assert.ok(http.requests.some((request) => request.url.endsWith("/usage")), "the usage figures are reloaded");
});

test("sign-out clears the state and tolerates an already-ended session", async () => {
  const { controller, http } = controllerFor({ "GET /api/v1/session": { body: signedIn() }, ...tenantRoutes(OWNER.id), "POST /api/v1/auth/logout": { status: 204 } });
  await controller.loadSession();
  await controller.loadTenantData();
  assert.deepEqual(await controller.signOut(), { ok: true, message: "Signed out." });
  assert.equal(controller.state.session.authenticated, false);
  assert.equal(controller.state.tenant, null);
  assert.deepEqual(controller.state.members, []);
  assert.equal(controller.state.keys.every((row) => !row.configured), true);
  assert.equal(controller.state.expired, false, "signing out is not an expiry");
  assert.equal(http.requests.at(-1).init.headers["X-CSRF-Token"], "csrf");

  const expired = controllerFor({ "GET /api/v1/session": { body: signedIn() }, "POST /api/v1/auth/logout": { status: 401, body: { error: "no session" } } });
  await expired.controller.loadSession();
  assert.equal((await expired.controller.signOut()).ok, true, "a 401 on logout still signs out locally");
  const down = controllerFor({ "GET /api/v1/session": { body: signedIn() }, "POST /api/v1/auth/logout": { status: 500, body: { error: "boom" } } });
  await down.controller.loadSession();
  assert.equal((await down.controller.signOut()).ok, false, "a real failure is reported and the session stays");
  assert.equal(down.controller.state.session.authenticated, true);
});

test("a network failure reading the session is shown, and treated as signed out", async () => {
  const web = api.createApi({ fetch: async () => { throw new TypeError("Failed to fetch"); } });
  const controller = account.createAccountController({ invoke: web.invoke });
  await controller.loadSession();
  assert.equal(controller.state.session.authenticated, false);
  assert.match(controller.state.errors.session, /Failed to fetch/);
});

// ----- The adapter reports a session that ended ------------------------------------------------

test("api.js tells the page about a 401 on any command but the session read", async () => {
  const seen = [];
  const http = mockHttp({
    "GET /api/v1/tenants/t1/usage": { status: 401, body: { error: "Sign in" } },
    "GET /api/v1/session": { status: 401, body: {} },
    "GET /api/v1/tenants/t1/members": { status: 403, body: { error: "no" } },
  });
  const web = api.createApi({ fetch: http.fetch, onUnauthorized: (name) => seen.push(name) });
  await assert.rejects(web.invoke("web_get_usage", { tenant: "t1" }), (error) => error.status === 401);
  await assert.rejects(web.invoke("web_session"));
  await assert.rejects(web.invoke("web_list_members", { tenant: "t1" }));
  assert.deepEqual(seen, ["web_get_usage"]);
});

test("the default instance fans a 401 out to every registered listener, even a throwing one", async () => {
  const previous = globalThis.window;
  const seen = [];
  globalThis.window = { fetch: async () => ({ ok: false, status: 401, text: async () => "" }) };
  try {
    api.onSessionExpired(() => { throw new Error("a listener must not mask the 401"); });
    api.onSessionExpired((name) => seen.push(name));
    api.onSessionExpired("not a function");
    await assert.rejects(api.invoke("web_get_usage", { tenant: "t1" }), (error) => error instanceof api.ApiError && error.status === 401);
    assert.deepEqual(seen, ["web_get_usage"]);
  } finally {
    if (previous === undefined) delete globalThis.window; else globalThis.window = previous;
  }
});

test("the desktop transport never reports an expiry and ignores the account hooks", async () => {
  const seen = [];
  const calls = [];
  const tauri = { core: { invoke: async (name) => { calls.push(name); throw { status: 401 }; } }, event: { listen: async () => () => {} } };
  const desktop = api.createApi({ tauri, onUnauthorized: (name) => seen.push(name) });
  await assert.rejects(desktop.invoke("get_config"));
  assert.deepEqual(seen, []);
  assert.deepEqual(calls, ["get_config"]);
});

// ----- Markup and design-system conformance ---------------------------------------------------

const VIEWS = ["signin", "account", "keys", "github", "quota"];

test("each new view follows the nav item + section + pageTitles + eyebrow/h2 pattern", () => {
  const titles = app.slice(app.indexOf("const pageTitles"), app.indexOf("const symbols"));
  for (const view of VIEWS) {
    assert.match(html, new RegExp(`<button class="nav-item" data-view-target="${view}"[^>]*data-web-only`), `${view} nav item is web-only`);
    const section = html.match(new RegExp(`<section id="view-${view}" class="view" data-web-only>([\\s\\S]*?)</section>`));
    assert.ok(section, `${view} section is web-only`);
    assert.match(section[1], /<div class="section-intro"><div><p class="eyebrow">[^<]+<\/p><h2>[^<]+<\/h2>/, `${view} opens with the shared header shape`);
    assert.match(titles, new RegExp(`\\b${view}: "`), `${view} has a pageTitles entry`);
  }
});

test("web-only markup is hidden on the desktop and desktop-only markup on the web", () => {
  assert.match(css, /body:not\(\[data-transport="web"\]\) \[data-web-only\] \{ display: none !important; \}/);
  assert.match(css, /body\[data-transport="web"\] \[data-desktop-only\] \{ display: none !important; \}/);
  assert.match(css, /body\[data-session="signed-out"\] \[data-signed-in-only\]/);
  assert.match(css, /body:not\(\[data-session="signed-out"\]\) \[data-signed-out-only\]/);
  // The tray, the local-machine footer, the workspace folder row and local bot setup are desktop-only.
  for (const marker of ['id="hide-button" class="quiet-button" data-desktop-only', 'class="machine-state" data-desktop-only', 'class="workspace-row" data-desktop-only', 'id="setup-bots" class="primary-button" data-desktop-only']) {
    assert.ok(html.includes(marker), marker);
  }
  assert.match(html, /data-desktop-only><span class="pulse-dot"><\/span><span>Runs on this Mac/);
  assert.match(html, /id="tenant-select"/);
  assert.match(html, /<div class="topbar-actions" data-signed-in-only>/);
});

test("the desktop never builds the account controller or sets the web markers", () => {
  assert.match(app, /const webMode = window\.SwarmApi\.transport\(\) === "web";/);
  assert.match(app, /const webAccount = webMode && window\.SwarmWebAccount/);
  assert.match(app, /if \(webAccount && !\(await bootstrapWeb\(\)\)\) return;/);
  assert.equal([...app.matchAll(/document\.body\.dataset\.transport/g)].length, 1);
  assert.match(app, /async function bootstrapWeb\(\) \{\s*document\.body\.dataset\.transport = "web";/);
  // Desktop-only local actions are skipped in web mode.
  assert.match(app, /!webMode && tool && !tool\.installed && tool\.installable/);
  assert.match(app, /!webMode && tool && tool\.installed && tool\.authenticated === false/);
  assert.match(app, /!webMode && \["node", "npm"\]/);
});

test("the account code reaches the backend only through the controller, never by command name", () => {
  assert.doesNotMatch(app, /invoke\("web_/);
  assert.match(fs.readFileSync(path.join(__dirname, "web-account.js"), "utf8"), /invoke\("web_session"\)/);
  assert.ok(html.indexOf('src="web-account.js"') > 0 && html.indexOf('src="web-account.js"') < html.indexOf('src="app.js"'));
  assert.ok(html.indexOf('src="api.js"') < html.indexOf('src="web-account.js"'));
});

test("web-account.js keeps no secret or token in the page, in storage or in a URL", () => {
  const source = fs.readFileSync(path.join(__dirname, "web-account.js"), "utf8");
  assert.doesNotMatch(source, /localStorage|sessionStorage|document\.cookie|\bconsole\.(log|info|warn|error|debug)\b/);
  assert.doesNotMatch(source, /\b(fetch|XMLHttpRequest|EventSource)\s*\(/);
  assert.match(app, /input\.value = "";/, "the key field is cleared after every attempt");
  assert.match(app, /input\.type = "password";/);
  assert.match(app, /input\.autocomplete = "off";/);
});

test("every new help dot has a topic and every new topic has a dot", () => {
  const topics = ["web-account", "web-keys", "web-github-app", "web-quota"];
  for (const topic of topics) {
    assert.match(html, new RegExp(`data-help="${topic}" aria-label="[^"]+"`), `${topic} has a labelled help dot`);
    assert.match(app, new RegExp(`"${topic}": \\{\\s*title: "[^"]+",\\s*html: "`), `${topic} is in HELP_TOPICS`);
  }
});

test("new markup uses the shared components, tokens and the accessibility conventions", () => {
  assert.doesNotMatch(html, /\sstyle="/, "no inline styles");
  const web = html.slice(html.indexOf('<section id="view-signin"'), html.indexOf('<section id="view-guides"'));
  for (const cls of ["panel", "panel-header", "status-pill", "banner", "usage-card", "usage-table", "primary-button", "secondary-button", "check-item"]) {
    assert.ok(web.includes(cls) || app.includes(cls), `${cls} is reused`);
  }
  assert.match(web, /role="alert"/);
  assert.match(web, /aria-live="polite"/);
  assert.match(web, /<caption class="sr-only">/, "tables are captioned");
  assert.match(html, /<div id="session-banner" class="banner warning hidden" role="alert" data-web-only>/);
  assert.match(html, /<select id="tenant-select" aria-label="Active tenant">/);
  const links = [...web.matchAll(/<a [^>]*target="_blank"[^>]*>/g)].map((match) => match[0]);
  assert.ok(links.length >= 2);
  for (const link of links) assert.match(link, /rel="noopener noreferrer"/);
});

test("the new CSS adds no color literal and collapses inside the two shared breakpoints", () => {
  const added = css.slice(css.indexOf("/* ----- Hosted web version"), css.indexOf("@media (max-width: 1060px)"));
  assert.ok(added.length > 200);
  assert.doesNotMatch(added, /#[0-9a-fA-F]{3,8}\b|rgba?\(/, "colors come from the :root tokens");
  const wide = css.slice(css.indexOf("@media (max-width: 1060px)"), css.indexOf("@media (max-width: 820px)"));
  const narrow = css.slice(css.indexOf("@media (max-width: 820px)"));
  assert.match(wide, /\.key-row \{ grid-template-columns/);
  assert.match(narrow, /\.key-row \{ grid-template-columns: minmax\(0, 1fr\)/);
  assert.equal((css.match(/@media \(max-width/g) || []).length, 2, "no third breakpoint");
  assert.ok(css.indexOf(".key-row {") < css.indexOf("@media (max-width: 1060px)"), "the base rule precedes its collapse so the media query wins");
});

test("the budget inputs bind through data attributes, not one-off listeners", () => {
  const form = html.match(/<form id="budget-form"[\s\S]*?<\/form>/)[0];
  for (const key of ["minimum", "claude", "codex", "grok"]) assert.match(form, new RegExp(`data-budget="${key}"`));
  assert.doesNotMatch(app, /byId\("budget-(minimum|claude|codex|grok)"\)\.addEventListener/);
  assert.match(app, /querySelectorAll\("\[data-budget\]"\)/);
});
