(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmWebAccount = api;
})(typeof window === "undefined" ? globalThis : window, () => {
  "use strict";

  // The hosted web version's account views: sign-in and session expiry, the
  // tenant switcher, members, provider API keys, the GitHub App install and the
  // quota/budget display. Everything here is state and arithmetic with no DOM,
  // so `app.js` only renders what these functions return and the same code is
  // tested against a mocked HTTP transport. Nothing in this file is used by the
  // desktop: `app.js` builds the controller only when the transport is "web".
  //
  // Two rules every helper shares. A secret never comes back: the key field is
  // write-only, `keyRows` carries metadata, and `validateKey` never echoes the
  // value in a message. A number nobody reported is UNAVAILABLE, never zero
  // (remaining quota is `null` when there is neither a budget nor a provider
  // report; the usage report's rule, `usage_report.py`).
  const UNAVAILABLE = "—";

  // Provider keys the backend stores, in display order. `jobs` marks the three
  // that run worker jobs and can carry a budget; `model-data` is the optional
  // Artificial Analysis key. Mirrors `web/src/model.rs::Provider`.
  const PROVIDERS = [
    { id: "claude", label: "Claude", jobs: true, hint: "Anthropic API key (sk-ant-…)", docs: "https://console.anthropic.com/settings/keys" },
    { id: "codex", label: "Codex", jobs: true, hint: "OpenAI API key (sk-…)", docs: "https://platform.openai.com/api-keys" },
    { id: "grok", label: "Grok", jobs: true, hint: "xAI API key (xai-…)", docs: "https://console.x.ai/" },
    { id: "model-data", label: "Model data", jobs: false, hint: "Artificial Analysis key, optional", docs: "https://artificialanalysis.ai/" },
  ];
  const JOB_PROVIDERS = PROVIDERS.filter((provider) => provider.jobs);

  // Mirrors `web/src/vault.rs::validate_key`; the server remains the authority.
  const MIN_KEY_LENGTH = 8;
  const MAX_KEY_LENGTH = 4096;

  function providerById(id) {
    return PROVIDERS.find((provider) => provider.id === id) || null;
  }

  // ----- Avatar menu ---------------------------------------------------------

  // The top-right menu. Every entry opens a view of the view-switcher (`view` is
  // the `data-view-target`/`pageTitles` key); Admin is shown to platform admins
  // only and there is deliberately no Billing entry. `signout` is an action, not a view.
  const MENU_ITEMS = [
    { id: "profile", label: "Profile", view: "profile" },
    { id: "settings", label: "Settings", view: "repository" },
    { id: "account", label: "Account", view: "account" },
    { id: "admin", label: "Admin", view: "admin", adminOnly: true },
    { id: "signout", label: "Sign out", action: "signout" },
  ];

  // `GET /me`, reduced to what the menu shows. Anything unexpected is "no
  // profile", so the avatar stays hidden rather than showing a half-built one.
  function profileView(body) {
    const value = body && typeof body === "object" ? body : {};
    if (typeof value.login !== "string" || !value.login) return null;
    const login = value.login;
    const displayName = typeof value.display_name === "string" && value.display_name.trim() ? value.display_name.trim() : login;
    return {
      login,
      displayName,
      avatarUrl: safeHttpsUrl(value.avatar_url),
      initials: initialsOf(displayName, login),
      tenant: typeof value.tenant === "string" ? value.tenant : "",
      role: value.role === "owner" ? "owner" : "member",
      isAdmin: value.is_platform_admin === true,
    };
  }

  // Up to two letters: first letters of the first two words of the name, or the
  // first letters of the login when the name is empty.
  function initialsOf(name, login) {
    const words = String(name || "").split(/\s+/).filter(Boolean);
    const letters = words.length > 1 ? [words[0], words[1]] : [String(name || login || "?")];
    const text = words.length > 1 ? letters.map((word) => Array.from(word)[0]).join("") : Array.from(letters[0]).slice(0, 2).join("");
    return text.toUpperCase();
  }

  function menuItems(profile) {
    return MENU_ITEMS.filter((item) => !item.adminOnly || Boolean(profile && profile.isAdmin));
  }

  // Keyboard navigation inside the open menu (wraps around). Returns the new
  // index, or -1 when the key is not a navigation key.
  function nextMenuIndex(current, key, count) {
    if (!count) return -1;
    if (key === "Home") return 0;
    if (key === "End") return count - 1;
    if (key === "ArrowDown") return current < 0 ? 0 : (current + 1) % count;
    if (key === "ArrowUp") return current < 0 ? count - 1 : (current - 1 + count) % count;
    return -1;
  }

  // ----- Session -------------------------------------------------------------

  // `GET /session` answers 200 either way. Anything unexpected is treated as
  // signed out, so a malformed reply can never open the signed-in views.
  function interpretSession(body) {
    const value = body && typeof body === "object" ? body : {};
    const authenticated = value.authenticated === true;
    const tenants = authenticated && Array.isArray(value.tenants)
      ? value.tenants.filter((tenant) => tenant && typeof tenant.id === "string" && tenant.id).map(normalizeTenant)
      : [];
    return {
      authenticated,
      login: authenticated && value.user && typeof value.user.login === "string" ? value.user.login : "",
      tenants,
      installUrl: safeHttpsUrl(value.install_url),
      loginUrl: typeof value.login_url === "string" && value.login_url.startsWith("/") ? value.login_url : "/api/v1/auth/github/login",
    };
  }

  function normalizeTenant(tenant) {
    return {
      id: String(tenant.id),
      account: String(tenant.account_login || tenant.id),
      accountType: String(tenant.account_type || ""),
      status: ["active", "suspended", "deleted"].includes(tenant.status) ? tenant.status : "active",
      role: tenant.role === "owner" ? "owner" : "member",
    };
  }

  // The install link is rendered as an anchor; only an https URL qualifies.
  function safeHttpsUrl(value) {
    if (typeof value !== "string") return "";
    try {
      const url = new URL(value);
      return url.protocol === "https:" ? url.href : "";
    } catch (_) {
      return "";
    }
  }

  // The chosen tenant survives a reload only if the session still grants it;
  // otherwise the first one is used. The server re-checks membership on every
  // request, so this is a convenience, never an authority.
  function pickTenant(tenants, preferredId) {
    return tenants.find((tenant) => tenant.id === preferredId) || tenants[0] || null;
  }

  function roleLabel(role) {
    return role === "owner" ? "Owner" : "Member";
  }

  // Pill classes come from the shared stopped/running/paused/error palette.
  function tenantStatus(tenant) {
    if (!tenant) return { label: "No tenant", tone: "stopped" };
    if (tenant.status === "active") return { label: "Active", tone: "running" };
    if (tenant.status === "suspended") return { label: "Suspended", tone: "paused" };
    return { label: "Removed", tone: "error" };
  }

  // Keys and budgets are owner-only and refused while the installation is not
  // active; members get the same views read-only.
  function canManage(tenant) {
    return Boolean(tenant) && tenant.role === "owner" && tenant.status === "active";
  }

  function readOnlyReason(tenant) {
    if (!tenant) return "Select a tenant first.";
    if (tenant.status !== "active") return "This GitHub App installation is not active, so settings are read-only.";
    if (tenant.role !== "owner") return "Only a tenant owner can change this. You can view it.";
    return "";
  }

  function memberRows(members) {
    const list = Array.isArray(members) ? members : [];
    return list
      .filter((member) => member && typeof member.login === "string")
      .map((member) => ({ login: member.login, role: member.role === "owner" ? "owner" : "member", roleLabel: roleLabel(member.role) }))
      .sort((a, b) => (a.role === b.role ? a.login.localeCompare(b.login) : a.role === "owner" ? -1 : 1));
  }

  // ----- Provider keys (write-only) -------------------------------------------

  function formatTimestamp(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value <= 0) return "";
    return new Date(value * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC";
  }

  // One row per provider, whether or not the server listed it. Never contains
  // a key: the endpoint does not return one.
  function keyRows(response) {
    const listed = response && Array.isArray(response.keys) ? response.keys : [];
    return PROVIDERS.map((provider) => {
      const meta = listed.find((entry) => entry && entry.provider === provider.id) || {};
      const configured = meta.configured === true;
      const updated = formatTimestamp(meta.updated_at);
      const by = typeof meta.updated_by === "string" ? meta.updated_by : "";
      return {
        ...provider,
        configured,
        status: configured ? "Configured" : "Not set",
        tone: configured ? "running" : "stopped",
        detail: configured ? [updated && `Updated ${updated}`, by && `by ${by}`].filter(Boolean).join(" ") : "",
      };
    });
  }

  // The message never contains the value, only what is wrong with its shape.
  function validateKey(value) {
    const key = String(value ?? "").trim();
    if (!key) return { ok: false, key: "", message: "Paste a key first." };
    if (key.length < MIN_KEY_LENGTH) return { ok: false, key: "", message: "That key is too short." };
    if (key.length > MAX_KEY_LENGTH) return { ok: false, key: "", message: "That key is too long." };
    if (!/^[\x21-\x7e]+$/.test(key)) return { ok: false, key: "", message: "A key is printable text without spaces or line breaks." };
    return { ok: true, key, message: "" };
  }

  // ----- Quota and budget -------------------------------------------------------

  function formatUsd(value) {
    const number = Number(value);
    if (value === null || value === undefined || value === "" || !Number.isFinite(number)) return UNAVAILABLE;
    return `$${number.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }

  function formatPercent(value) {
    const number = Number(value);
    if (value === null || value === undefined || value === "" || !Number.isFinite(number)) return UNAVAILABLE;
    return `${Math.round(number * 10) / 10}%`;
  }

  // The provider's own status code, as `ProviderUsage` reports it: 0 usable,
  // 1 below the configured minimum, 2 unavailable.
  function usageState(status) {
    if (status === 0) return { label: "Usable", tone: "running" };
    if (status === 1) return { label: "Below minimum", tone: "paused" };
    return { label: "Unavailable", tone: "stopped" };
  }

  function sourceLabel(source) {
    if (source === "budget") return "From your budget";
    if (source === "provider") return "Reported by the provider";
    return "No budget or provider report";
  }

  // `quotas` is GET /quotas, `usage` is GET /usage. Either may be missing (a
  // failed request): that section then shows UNAVAILABLE rather than a zero.
  function quotaView(quotas, usage) {
    const plan = (quotas && quotas.plan) || (usage && usage.plan) || null;
    const budgets = (quotas && quotas.budgets) || (usage && usage.budgets) || null;
    const providers = usage && Array.isArray(usage.providers) ? usage.providers : [];
    const rows = JOB_PROVIDERS.map((provider) => {
      const entry = providers.find((candidate) => candidate && candidate.provider === provider.id) || null;
      const budget = entry && entry.budget_usd !== null && entry.budget_usd !== undefined
        ? entry.budget_usd
        : budgets && budgets.provider_budgets_usd && budgets.provider_budgets_usd[provider.id] !== undefined
          ? budgets.provider_budgets_usd[provider.id]
          : null;
      const state = usageState(entry ? entry.status : 2);
      const remaining = entry && entry.remaining_percent !== undefined ? entry.remaining_percent : null;
      const priced = entry ? Number(entry.priced_invocations) || 0 : null;
      const unpriced = entry ? Number(entry.unpriced_invocations) || 0 : null;
      return {
        id: provider.id,
        label: provider.label,
        stateLabel: state.label,
        tone: state.tone,
        remainingText: formatPercent(remaining),
        // A meter only for a real number; unavailable renders no bar at all.
        remainingPercent: remaining === null || !Number.isFinite(Number(remaining)) ? null : Math.max(0, Math.min(100, Number(remaining))),
        source: sourceLabel(entry ? entry.source : "unavailable"),
        detail: entry && typeof entry.detail === "string" ? entry.detail : "",
        spendText: entry ? formatUsd(entry.spend_usd) : UNAVAILABLE,
        budgetText: budget === null ? "No budget" : formatUsd(budget),
        invocationsText: entry ? `${priced} priced, ${unpriced} unpriced` : UNAVAILABLE,
      };
    });
    return {
      period: usage && typeof usage.period === "string" ? usage.period : "",
      totalSpendText: usage ? formatUsd(usage.total_spend_usd) : UNAVAILABLE,
      activeJobsText: usage && Number.isFinite(Number(usage.active_jobs)) ? String(usage.active_jobs) : UNAVAILABLE,
      maxJobsText: plan && Number.isFinite(Number(plan.max_concurrent_jobs)) ? String(plan.max_concurrent_jobs) : UNAVAILABLE,
      monthlyCapText: !plan ? UNAVAILABLE : plan.monthly_spend_cap_usd === null || plan.monthly_spend_cap_usd === undefined ? "No cap" : formatUsd(plan.monthly_spend_cap_usd),
      minimumPercent: budgets && Number.isFinite(Number(budgets.minimum_remaining_percent)) ? Number(budgets.minimum_remaining_percent) : null,
      budgets: Object.fromEntries(JOB_PROVIDERS.map((provider) => {
        const value = budgets && budgets.provider_budgets_usd ? budgets.provider_budgets_usd[provider.id] : undefined;
        return [provider.id, value === undefined || value === null ? "" : String(value)];
      })),
      rows,
    };
  }

  // The budget form -> the PUT body, with the backend's own limits
  // (`validate_budgets`) checked first so the message names the field. A blank
  // provider budget means "no budget" and is omitted.
  function budgetPayload(form) {
    const minimumText = String((form && form.minimum_remaining_percent) ?? "").trim();
    const minimum = Number(minimumText);
    if (!minimumText || !Number.isFinite(minimum) || minimum < 0 || minimum > 100) {
      return { ok: false, message: "Minimum remaining must be a number from 0 to 100." };
    }
    const provider_budgets_usd = {};
    for (const provider of JOB_PROVIDERS) {
      const text = String(((form && form.provider_budgets_usd) || {})[provider.id] ?? "").trim();
      if (!text) continue;
      const amount = Number(text);
      if (!Number.isFinite(amount) || amount <= 0) {
        return { ok: false, message: `${provider.label} budget must be a positive number of dollars, or blank for none.` };
      }
      provider_budgets_usd[provider.id] = amount;
    }
    return { ok: true, payload: { minimum_remaining_percent: minimum, provider_budgets_usd } };
  }

  // ----- Controller --------------------------------------------------------------

  // The state machine behind the views. `invoke` is `SwarmApi.invoke`, so the
  // same code runs against the real fetch transport and a mocked one. Each
  // tenant section loads independently: one failing endpoint marks only its own
  // section as errored. A 401 anywhere means the session is gone.
  function isUnauthorized(error) {
    return Boolean(error) && error.code === "api_error" && error.status === 401;
  }

  function messageOf(error) {
    if (typeof error === "string") return error;
    if (error && typeof error.message === "string") return error.message;
    return "Something went wrong.";
  }

  function createAccountController({ invoke, setTenant = () => {}, storedTenant = () => "", rememberTenant = () => {} }) {
    const state = {
      session: interpretSession(null),
      tenant: null,
      members: [],
      keys: keyRows(null),
      quota: quotaView(null, null),
      errors: {},
      expired: false,
      loaded: false,
      profile: null,
      adminUsers: [],
    };

    function fail(section, error) {
      if (isUnauthorized(error)) state.expired = true;
      state.errors[section] = messageOf(error);
      return { ok: false, expired: state.expired, message: state.errors[section] };
    }

    async function loadSession() {
      try {
        state.session = interpretSession(await invoke("web_session"));
      } catch (error) {
        state.session = interpretSession(null);
        state.errors.session = messageOf(error);
        return state;
      }
      delete state.errors.session;
      if (!state.session.authenticated) {
        // Signed out. If we had been signed in, the session expired.
        state.expired = state.loaded ? true : state.expired;
        state.tenant = null;
        return state;
      }
      state.expired = false;
      state.loaded = true;
      state.tenant = pickTenant(state.session.tenants, state.tenant ? state.tenant.id : storedTenant());
      if (state.tenant) {
        setTenant(state.tenant.id);
        rememberTenant(state.tenant.id);
      }
      return state;
    }

    // The menu's profile. A failure leaves the avatar hidden; it never blocks
    // the rest of the page. A 401 is the session ending, like any other command.
    async function loadProfile() {
      try {
        state.profile = profileView(await invoke("web_me"));
      } catch (error) {
        state.profile = null;
        if (isUnauthorized(error)) state.expired = true;
      }
      return state.profile;
    }

    // Platform admin user list for the Admin view (the server answers 404 to
    // anyone who is not an admin, which shows as an error, never as data).
    async function loadAdminUsers() {
      if (!state.profile || !state.profile.isAdmin) { state.adminUsers = []; return state.adminUsers; }
      try {
        const body = await invoke("web_admin_list_users");
        state.adminUsers = (body && Array.isArray(body.users) ? body.users : [])
          .filter((user) => user && typeof user.login === "string")
          .map((user) => ({ login: user.login, displayName: String(user.display_name || user.login), isAdmin: user.is_platform_admin === true }));
        delete state.errors.admin;
      } catch (error) {
        state.adminUsers = [];
        state.errors.admin = messageOf(error);
      }
      return state.adminUsers;
    }

    async function loadTenantData() {
      state.errors = state.errors.session ? { session: state.errors.session } : {};
      if (!state.tenant) return state;
      const tenant = state.tenant.id;
      const [members, keys, quotas, usage] = await Promise.allSettled([
        invoke("web_list_members", { tenant }),
        invoke("web_list_provider_keys", { tenant }),
        invoke("web_get_quotas", { tenant }),
        invoke("web_get_usage", { tenant }),
      ]);
      if (members.status === "fulfilled") state.members = memberRows(members.value && members.value.members);
      else { state.members = []; fail("members", members.reason); }
      if (keys.status === "fulfilled") state.keys = keyRows(keys.value);
      else { state.keys = keyRows(null); fail("keys", keys.reason); }
      if (quotas.status === "rejected") fail("quota", quotas.reason);
      if (usage.status === "rejected") fail("quota", usage.reason);
      state.quota = quotaView(
        quotas.status === "fulfilled" ? quotas.value : null,
        usage.status === "fulfilled" ? usage.value : null,
      );
      return state;
    }

    async function selectTenant(id) {
      const next = state.session.tenants.find((tenant) => tenant.id === id);
      if (!next) return state;
      state.tenant = next;
      setTenant(next.id);
      rememberTenant(next.id);
      state.members = [];
      state.keys = keyRows(null);
      state.quota = quotaView(null, null);
      return loadTenantData();
    }

    // Write-only: the key goes out once and is not kept. The returned object
    // carries a message, never the value.
    async function saveKey(provider, value) {
      if (!providerById(provider)) return { ok: false, message: "Unknown provider." };
      if (!canManage(state.tenant)) return { ok: false, message: readOnlyReason(state.tenant) };
      const checked = validateKey(value);
      if (!checked.ok) return { ok: false, message: checked.message };
      try {
        await invoke("web_set_provider_key", { tenant: state.tenant.id, provider, key: checked.key });
      } catch (error) {
        return fail("keys", error);
      }
      delete state.errors.keys;
      const response = await invoke("web_list_provider_keys", { tenant: state.tenant.id }).catch(() => null);
      if (response) state.keys = keyRows(response);
      return { ok: true, message: `${providerById(provider).label} key saved.` };
    }

    async function removeKey(provider) {
      if (!providerById(provider)) return { ok: false, message: "Unknown provider." };
      if (!canManage(state.tenant)) return { ok: false, message: readOnlyReason(state.tenant) };
      try {
        await invoke("web_delete_provider_key", { tenant: state.tenant.id, provider });
      } catch (error) {
        return fail("keys", error);
      }
      delete state.errors.keys;
      const response = await invoke("web_list_provider_keys", { tenant: state.tenant.id }).catch(() => null);
      if (response) state.keys = keyRows(response);
      return { ok: true, message: `${providerById(provider).label} key removed.` };
    }

    async function saveBudgets(form) {
      if (!canManage(state.tenant)) return { ok: false, message: readOnlyReason(state.tenant) };
      const checked = budgetPayload(form);
      if (!checked.ok) return checked;
      try {
        await invoke("web_set_budgets", { tenant: state.tenant.id, ...checked.payload });
      } catch (error) {
        return fail("quota", error);
      }
      await loadTenantData();
      return { ok: true, message: "Budgets saved." };
    }

    async function signOut() {
      try {
        await invoke("web_logout");
      } catch (error) {
        if (!isUnauthorized(error)) return { ok: false, message: messageOf(error) };
      }
      state.session = interpretSession(null);
      state.tenant = null;
      state.expired = false;
      state.loaded = false;
      state.profile = null;
      state.adminUsers = [];
      state.members = [];
      state.keys = keyRows(null);
      state.quota = quotaView(null, null);
      state.errors = {};
      return { ok: true, message: "Signed out." };
    }

    // The API adapter reports any 401 from a non-session command.
    function markExpired() {
      state.expired = true;
      state.session = { ...state.session, authenticated: false };
    }

    return { state, loadSession, loadProfile, loadAdminUsers, loadTenantData, selectTenant, saveKey, removeKey, saveBudgets, signOut, markExpired };
  }

  return {
    UNAVAILABLE,
    PROVIDERS,
    JOB_PROVIDERS,
    MIN_KEY_LENGTH,
    MAX_KEY_LENGTH,
    providerById,
    MENU_ITEMS,
    profileView,
    initialsOf,
    menuItems,
    nextMenuIndex,
    interpretSession,
    pickTenant,
    roleLabel,
    tenantStatus,
    canManage,
    readOnlyReason,
    memberRows,
    keyRows,
    validateKey,
    formatUsd,
    formatPercent,
    usageState,
    quotaView,
    budgetPayload,
    createAccountController,
  };
});
