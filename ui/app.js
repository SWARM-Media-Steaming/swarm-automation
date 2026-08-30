(() => {
  "use strict";

  const { invoke } = window.__TAURI__.core;
  const { listen } = window.__TAURI__.event;

  const state = {
    config: null,
    status: null,
    tools: [],
    logs: [],
    logFilter: "all",
    activityFilter: "all",
    activityPaused: false,
    activitySnapshot: null,
    dirty: false,
    busy: new Set(),
    refreshing: { status: false, tools: false, branches: false },
    activeRepoId: "",
    branchOverview: null,
  };

  const pageTitles = {
    overview: "Automation overview",
    repository: "Repository",
    ai: "AI Configuration",
    scheduler: "Test Scheduler",
    advanced: "Advanced",
    debug: "Info & Debug",
    help: "Help",
  };
  const symbols = { git: "G", gh: "GH", python: "Py", node: "N", npm: "npm", claude: "C", codex: "X", grok: "Gk" };

  // Everything provider-specific the UI needs. Order here is the display /
  // rotation order and must match config.rs KNOWN_PROVIDERS.
  const PROVIDER_META = {
    claude: {
      label: "Claude", cli: "Claude Code", help: "provider-claude",
      efforts: ["low", "medium", "high", "max"],
      docs: "https://docs.anthropic.com/en/docs/claude-code/overview",
    },
    codex: {
      label: "Codex", cli: "Codex CLI", help: "provider-codex",
      efforts: ["low", "medium", "high", "xhigh", "max"],
      docs: "https://developers.openai.com/codex/cli/",
    },
    grok: {
      label: "Grok", cli: "Grok Build", help: "provider-grok",
      efforts: ["low", "medium", "high", "xhigh"],
      docs: "https://docs.x.ai/build/overview",
    },
  };
  const PROVIDER_ORDER = Object.keys(PROVIDER_META);
  const providerLabel = (id) => id === "xai" ? "xAI" : (PROVIDER_META[id]?.label || id);

  // Click-to-open help. `html` is a trusted local constant (no user input),
  // so innerHTML is safe here. Links open through open_external_url (HTTPS only).
  const HELP_TOPICS = {
    "provider-claude": {
      title: "Claude Code",
      html: "<p>Claude can write code for an issue or review work from another provider.</p><ul><li><strong>Switch</strong> — includes or removes Claude from new work.</li><li><strong>Model</strong> — the Claude model to use. The filled-in default is recommended.</li><li><strong>Effort</strong> — how much time Claude may spend reasoning.</li><li><strong>Install / Sign in</strong> — prepares Claude on this Mac.</li></ul>",
      links: [{ label: "Claude Code docs", url: "https://docs.anthropic.com/en/docs/claude-code/overview" }],
    },
    "provider-codex": {
      title: "Codex CLI",
      html: "<p>Codex can write code for an issue or review work from another provider.</p><ul><li><strong>Switch</strong> — includes or removes Codex from new work.</li><li><strong>Model</strong> — the Codex model to use. The filled-in default is recommended.</li><li><strong>Effort</strong> — how much time Codex may spend reasoning.</li><li><strong>Install / Sign in</strong> — prepares Codex on this Mac.</li></ul>",
      links: [{ label: "Codex CLI docs", url: "https://developers.openai.com/codex/cli/" }],
    },
    "provider-grok": {
      title: "Grok Build",
      html: "<p>Grok can write code for an issue or review work from another provider.</p><ul><li><strong>Switch</strong> — includes or removes Grok from new work.</li><li><strong>Model</strong> — the Grok model to use. The filled-in default is recommended.</li><li><strong>Effort</strong> — how much time Grok may spend reasoning.</li><li><strong>Install / Sign in</strong> — prepares Grok on this Mac.</li></ul>",
      links: [{ label: "Grok Build docs", url: "https://docs.x.ai/build/overview" }],
    },
    "provider-rotation": {
      title: "Provider rotation",
      html: "<p>This box controls which AI gets the first chance to take an issue.</p><ul><li><strong>Preferred first provider</strong> — tried first for new issues. Another enabled provider is used when it is unavailable.</li><li><strong>Minimum quota remaining</strong> — stops new work before a provider’s usage allowance gets too low.</li></ul><p>Follow-up work prefers a different provider when one is available.</p>",
      links: [],
    },
    "provider-include-exclude": {
      title: "Enabled AI tools",
      html: "<p>Each card represents an AI provider. Turn its switch on to allow it to receive new work.</p><p>At least one provider must remain enabled. Turning one off does not erase work it already completed.</p>",
      links: [],
    },
    "bot-identities": {
      title: "GitHub App bot identities",
      html: "<p>Each AI provider gets its own GitHub identity, making it clear which one wrote, reviewed, or merged work.</p><ul><li><strong>Set up GitHub Apps</strong> — starts GitHub’s approval process for identities that are missing.</li><li><strong>Verify bots</strong> — checks that each enabled provider can use its identity.</li><li><strong>Status</strong> — shows whether local bot settings were found.</li></ul>",
      links: [{ label: "About GitHub Apps", url: "https://docs.github.com/apps" }],
    },
    "quota-threshold": {
      title: "Minimum quota remaining",
      html: "<p>This keeps a small part of an AI provider’s usage allowance in reserve. If the provider falls below the chosen percentage, the app waits or tries another enabled provider.</p>",
      links: [],
    },
    "delivery-mode": {
      title: "Protected AI branch flow",
      html: "<p>This box keeps AI work separate from the branch people use.</p><ul><li><strong>Human-owned branch</strong> — the protected branch, usually <code>main</code>. AI never commits to it.</li><li><strong>AI integration branch</strong> — collects finished AI work, usually <code>ai-main</code>.</li><li><strong>Git remote</strong> — the saved connection to GitHub, usually <code>origin</code>.</li><li><strong>Issue branch prefix</strong> — the first part of AI branch names, usually <code>ai</code>.</li><li><strong>Automatically approve issue PRs</strong> — uses a bot identity to approve an AI pull request. A different provider is used when available.</li><li><strong>Automatically merge closed issue PRs</strong> — after the linked issue is closed, combines its commits into one, merges it into the AI integration branch, and deletes the issue branch.</li></ul><p>Both automation options start off. Moving <code>ai-main</code> into <code>main</code> always requires a person.</p>",
      links: [],
    },
    "auto-approve-merge": {
      title: "Approve & merge automatically",
      html: "<p><strong>Automatically approve issue PRs</strong> uses a bot identity to approve each AI pull request. A different provider is used when available.</p><p><strong>Automatically merge closed issue PRs</strong> merges only after the linked issue is closed. It creates one tidy commit in the AI integration branch and removes the issue branch.</p><p>Both settings are off by default. Neither setting merges AI work into <code>main</code>.</p>",
      links: [],
    },
    "schedule-modes": {
      title: "Pickup schedule",
      html: "<ul><li><strong>Continuous</strong> — checks repeatedly and handles ready issues one after another.</li><li><strong>Daily</strong> — checks once each day.</li><li><strong>Weekdays</strong> — checks Monday through Friday.</li><li><strong>Custom</strong> — checks on the days you choose.</li><li><strong>Manual</strong> — checks only when you select <em>Run now</em>.</li></ul>",
      links: [],
    },
    "uat-suite": {
      title: "Test scheduler",
      html: "<p>Runs the repository’s full test set on a schedule when that repository includes one. It avoids overlapping runs and reports real failures without changing the tests.</p>",
      links: [],
    },
    "repo-profile": {
      title: "Repository & workspace",
      html: "<p>This box chooses what the app monitors and where its copy is stored.</p><ul><li><strong>GitHub repository</strong> — the project in <code>owner/name</code> form.</li><li><strong>Assignee</strong> — the GitHub user whose assigned issues may be picked up.</li><li><strong>Enabled</strong> — includes this repository in monitoring.</li><li><strong>Clone / update</strong> — creates the app’s local copy or brings it up to date.</li><li><strong>Reveal in Finder</strong> — opens that local copy.</li><li><strong>Remove</strong> — stops listing the repository here; it does not delete GitHub branches or the local copy.</li></ul>",
      links: [],
    },
    "repo-queue": {
      title: "GitHub issue selection",
      html: "<p>These settings decide which comments and labels the worker trusts.</p><ul><li><strong>Trusted follow-up authors</strong> — GitHub users allowed to ask the AI for another pass. Separate names with commas.</li><li><strong>Completion authors</strong> — accounts whose completion messages the app accepts as proof that a pass finished. Separate names with commas.</li><li><strong>Ready label</strong> — the label added when AI work is ready for testing.</li></ul><p>Blank author lists use the repository assignee.</p>",
      links: [],
    },
    "repo-branches": {
      title: "Branches and promotion",
      html: "<p>The tree shows the protected human branch, the shared AI branch, and each active issue branch.</p><ul><li><strong>Refresh</strong> — reloads branch and pull-request information from GitHub.</li><li><strong>Squash into AI integration</strong> — combines a closed issue’s work into one commit and removes its issue branch.</li><li><strong>Create or open promotion PR</strong> — prepares the final move from the AI branch to the human branch.</li><li><strong>Raw Git graph</strong> — shows the same history in Git’s compact text format.</li></ul>",
      links: [],
    },
    "data-location": {
      title: "Where your data lives",
      html: "<p>Settings are stored in a private local file. Email passwords use macOS Keychain. GitHub and AI sign-in details stay with their own tools and are not copied into logs.</p>",
      links: [],
    },
    "email-notifications": {
      title: "Email completion notices",
      html: "<p>This box can send an email when issue work finishes.</p><ul><li><strong>Send email notifications</strong> — turns completion email on or off.</li><li><strong>Recipient</strong> — the address that receives the message.</li><li><strong>SMTP credentials file</strong> — a local file containing the mail server settings and username.</li><li><strong>SMTP password</strong> — the mail account password.</li><li><strong>Save password to Keychain</strong> — stores that password securely in macOS.</li></ul>",
      links: [],
    },
    "advanced-paths": {
      title: "Local path overrides",
      html: "<p>Leave these values alone unless the defaults do not fit your Mac.</p><ul><li><strong>Workspace folder</strong> — parent folder for app-managed repository copies.</li><li><strong>Working-copy override</strong> — uses a specific existing copy instead of an app-managed one.</li><li><strong>Worker state directory</strong> — stores progress needed to resume work.</li><li><strong>UAT run data directory</strong> — stores test-run state and results.</li><li><strong>GitHub Apps configuration</strong> — uses a specific bot-settings file.</li><li><strong>GitHub CLI / Python 3 binary</strong> — uses a specific program file when automatic detection fails.</li></ul>",
      links: [],
    },
    "provider-bins": {
      title: "AI program locations",
      html: "<p>The app normally finds Claude, Codex, and Grok automatically. Enter a full program path only when an installed provider is not detected or when you want to use a specific copy.</p>",
      links: [],
    },
  };
  const HELP_CONCEPTS = [
    ["Providers & rotation", "provider-rotation"],
    ["Including / excluding a provider", "provider-include-exclude"],
    ["GitHub App bot identities", "bot-identities"],
    ["Protected branch flow", "delivery-mode"],
    ["Minimum quota remaining", "quota-threshold"],
    ["Test scheduler", "uat-suite"],
    ["Where your data lives", "data-location"],
  ];

  function byId(id) {
    return document.getElementById(id);
  }

  function showToast(message, kind = "") {
    const toast = document.createElement("div");
    toast.className = `toast ${kind}`.trim();
    toast.textContent = String(message || "Unknown error");
    byId("toast-stack").appendChild(toast);
    window.setTimeout(() => toast.remove(), 5000);
  }

  function errorText(error) {
    if (typeof error === "string") return error;
    if (error && typeof error.message === "string") return error.message;
    try { return JSON.stringify(error); } catch (_) { return String(error); }
  }

  function setDirty(dirty = true) {
    state.dirty = dirty;
    const label = byId("save-state");
    label.textContent = dirty ? "Unsaved changes" : "All changes saved";
    label.style.color = dirty ? "var(--amber)" : "";
  }

  function navigate(view) {
    document.querySelectorAll(".nav-item").forEach((button) => {
      button.classList.toggle("active", button.dataset.viewTarget === view);
    });
    document.querySelectorAll(".view").forEach((section) => {
      section.classList.toggle("active", section.id === `view-${view}`);
    });
    byId("page-title").textContent = pageTitles[view] || pageTitles.overview;
    if (view === "repository") void refreshBranches({ quiet: true });
    if (view === "debug") void refreshTools({ quiet: true });
  }

  function populateHours() {
    const select = byId("uat-hour");
    select.replaceChildren();
    for (let hour = 0; hour < 24; hour += 1) {
      const option = document.createElement("option");
      option.value = String(hour);
      option.textContent = `${String(hour).padStart(2, "0")}:00`;
      select.appendChild(option);
    }
  }

  function bindConfig(config) {
    // Provider cards + the preferred-provider <option>s must exist before the
    // generic [data-config] pass sets #preferred-provider-select's value.
    renderProviderCards(config);
    providerList(config).forEach((provider) => {
      const input = document.querySelector(`[data-provider-bin="${provider.id}"]`);
      if (input) input.value = provider.bin;
    });
    renderPreferredOptions(config);
    ensureRepository(config);
    renderRepositorySelector();
    document.querySelectorAll("[data-config]").forEach((input) => {
      const key = input.dataset.config;
      const value = config[key];
      if (input.type === "checkbox") input.checked = Boolean(value);
      else if (input.dataset.list !== undefined) input.value = Array.isArray(value) ? value.join(", ") : "";
      else input.value = value ?? "";
    });
    document.querySelectorAll("#days-field input").forEach((input) => {
      input.checked = config.schedule_days.includes(input.value);
    });
    selectSchedule(config.schedule_mode, false);
    bindRepositoryForm();
    byId("profile-kicker").textContent = (currentRepo()?.github_repository || "REPOSITORY").toUpperCase();
    renderSummaries();
  }

  function defaultRepository() {
    return {
      id: `draft-${Date.now()}`,
      enabled: true,
      github_repository: "",
      assignee: "",
      base_branch: "main",
      integration_branch: "ai-main",
      branch_prefix: "ai",
      remote_name: "origin",
      github_host: "github.com",
      github_apps_config: "",
      require_bot_auth: true,
      ready_label: "Ready For Testing",
      trusted_followup_authors: [],
      completion_authors: [],
      preferred_provider: "",
      auto_approve: false,
      auto_merge: false,
      repo_dir: "",
      uat_enabled: false,
      uat_hour: 3,
      uat_issue_label: "Testing",
      uat_batocera_host: "batocera.local",
      uat_triage_enabled: true,
      run_dir: "",
    };
  }

  function ensureRepository(config = state.config) {
    if (!Array.isArray(config.repositories)) config.repositories = [];
    if (!config.repositories.length) config.repositories.push(defaultRepository());
    if (!config.repositories.some((repo) => repo.id === state.activeRepoId)) {
      state.activeRepoId = config.repositories[0].id;
    }
  }

  function currentRepo() {
    if (!state.config) return null;
    ensureRepository(state.config);
    return state.config.repositories.find((repo) => repo.id === state.activeRepoId) || state.config.repositories[0];
  }

  function renderRepositorySelector() {
    const select = byId("active-repo-select");
    if (!select || !state.config) return;
    ensureRepository(state.config);
    select.replaceChildren();
    state.config.repositories.forEach((repo, index) => {
      const option = document.createElement("option");
      option.value = repo.id;
      option.textContent = repo.github_repository || `New repository ${index + 1}`;
      if (!repo.enabled) option.textContent += " · paused";
      select.appendChild(option);
    });
    select.value = state.activeRepoId;
  }

  function bindRepositoryForm() {
    const repo = currentRepo();
    if (!repo) return;
    document.querySelectorAll("[data-repo-config]").forEach((input) => {
      const value = repo[input.dataset.repoConfig];
      if (input.type === "checkbox") input.checked = Boolean(value);
      else if (input.dataset.list !== undefined) input.value = Array.isArray(value) ? value.join(", ") : "";
      else input.value = value ?? "";
    });
    byId("profile-kicker").textContent = (repo.github_repository || "NEW REPOSITORY").toUpperCase();
  }

  function stashRepositoryForm() {
    const repo = currentRepo();
    if (!repo) return;
    document.querySelectorAll("[data-repo-config]").forEach((input) => {
      const key = input.dataset.repoConfig;
      if (input.type === "checkbox") repo[key] = input.checked;
      else if (input.dataset.list !== undefined) repo[key] = input.value.split(",").map((value) => value.trim()).filter(Boolean);
      else if (input.type === "number" || key === "uat_hour") repo[key] = Number(input.value);
      else repo[key] = input.value.trim();
    });
  }

  function providerList(config) {
    // Guarantee one card per known provider, in canonical order, even if a
    // hand-edited config dropped one.
    const stored = new Map((config.providers || []).map((p) => [p.id, p]));
    return PROVIDER_ORDER.map((id) => {
      const entry = stored.get(id) || {};
      return {
        id,
        enabled: entry.enabled !== false,
        model: entry.model || "",
        effort: entry.effort || "high",
        bin: entry.bin || "",
      };
    });
  }

  function renderProviderCards(config) {
    const grid = document.getElementById("provider-cards");
    if (!grid) return;
    grid.replaceChildren();
    providerList(config).forEach((provider) => {
      const meta = PROVIDER_META[provider.id];
      const tool = state.tools.find((entry) => entry.id === provider.id);
      const card = document.createElement("article");
      card.className = `provider-card${provider.enabled ? "" : " excluded"}`;
      card.dataset.provider = provider.id;

      const head = document.createElement("div");
      head.className = "provider-card-head";
      const ident = document.createElement("div");
      ident.className = "provider-ident";
      const sym = document.createElement("span");
      sym.className = "provider-sym";
      sym.textContent = symbols[provider.id] || meta.label.slice(0, 2);
      const name = document.createElement("strong");
      name.textContent = meta.label;
      const dot = document.createElement("button");
      dot.className = "help-dot";
      dot.type = "button";
      dot.dataset.help = meta.help;
      dot.setAttribute("aria-label", `About ${meta.label}`);
      dot.textContent = "?";
      ident.append(sym, name, dot);
      const toggle = document.createElement("label");
      toggle.className = "toggle";
      toggle.title = provider.enabled ? "In the flow" : "Excluded from the flow";
      const toggleInput = document.createElement("input");
      toggleInput.type = "checkbox";
      toggleInput.className = "provider-enabled";
      toggleInput.checked = provider.enabled;
      toggleInput.addEventListener("change", () => {
        card.classList.toggle("excluded", !toggleInput.checked);
        // Model is only required while the provider is in the flow.
        modelReq.hidden = !toggleInput.checked;
        onProvidersChanged();
      });
      const toggleTrack = document.createElement("span");
      toggle.append(toggleInput, toggleTrack);
      head.append(ident, toggle);

      const badge = document.createElement("span");
      badge.className = "provider-badge";
      badge.textContent = tool ? tool.status : "Checking…";

      const fields = document.createElement("div");
      fields.className = "provider-fields";
      const modelLabel = document.createElement("label");
      modelLabel.append("Model ");
      const modelReq = document.createElement("span");
      modelReq.className = "req";
      modelReq.title = "Required while this provider is in the flow";
      modelReq.textContent = "*";
      modelReq.hidden = !provider.enabled;
      modelLabel.appendChild(modelReq);
      const modelInput = document.createElement("input");
      modelInput.className = "provider-model";
      modelInput.value = provider.model;
      modelInput.addEventListener("input", setDirty);
      modelLabel.appendChild(modelInput);
      const effortLabel = document.createElement("label");
      effortLabel.textContent = "Effort ";
      const effortSelect = document.createElement("select");
      effortSelect.className = "provider-effort";
      meta.efforts.forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        effortSelect.appendChild(option);
      });
      effortSelect.value = meta.efforts.includes(provider.effort) ? provider.effort : "high";
      effortSelect.addEventListener("change", setDirty);
      effortLabel.appendChild(effortSelect);
      fields.append(modelLabel, effortLabel);

      const actions = document.createElement("div");
      actions.className = "provider-actions";
      if (tool && !tool.installed && tool.installable) {
        actions.appendChild(button(provider.id === "grok" ? "Install (Terminal)" : "Install", "primary-button", () => installProvider(provider.id)));
      }
      if (tool && tool.installed && tool.authenticated === false) {
        actions.appendChild(button("Sign in", "secondary-button", () => signIn(provider.id)));
      }
      actions.appendChild(button("Docs", "secondary-button", () => openUrl(meta.docs)));

      card.append(head, badge, fields, actions);
      grid.appendChild(card);
    });
  }

  function collectProviders() {
    return Array.from(document.querySelectorAll("#provider-cards .provider-card"), (card) => ({
      id: card.dataset.provider,
      enabled: card.querySelector(".provider-enabled").checked,
      model: card.querySelector(".provider-model").value.trim(),
      effort: card.querySelector(".provider-effort").value,
      bin: document.querySelector(`[data-provider-bin="${card.dataset.provider}"]`)?.value.trim() || "",
    }));
  }

  function renderPreferredOptions(config) {
    const select = document.getElementById("preferred-provider-select");
    if (!select) return;
    const current = select.value || config.preferred_provider;
    const enabled = providerList(config).filter((p) => p.enabled);
    select.replaceChildren();
    (enabled.length ? enabled : providerList(config)).forEach((provider) => {
      const option = document.createElement("option");
      option.value = provider.id;
      option.textContent = providerLabel(provider.id);
      select.appendChild(option);
    });
    const ids = Array.from(select.options, (option) => option.value);
    select.value = ids.includes(current) ? current : ids[0] || "claude";
  }

  function renderWorkerProviderSummary() {
    const box = document.getElementById("worker-provider-summary");
    if (!box || !state.config) return;
    box.replaceChildren();
    providerList(state.dirty ? collectConfig() : state.config).forEach((provider) => {
      const tool = state.tools.find((entry) => entry.id === provider.id);
      const cell = document.createElement("div");
      if (!provider.enabled) cell.className = "excluded";
      const name = document.createElement("span");
      name.textContent = providerLabel(provider.id) + (provider.enabled ? "" : " · excluded");
      const status = document.createElement("strong");
      status.textContent = provider.enabled ? tool?.status || "Not detected" : "—";
      cell.append(name, status);
      box.appendChild(cell);
    });
  }

  function collectConfig() {
    stashRepositoryForm();
    const next = { ...state.config };
    document.querySelectorAll("[data-config]").forEach((input) => {
      const key = input.dataset.config;
      if (input.type === "checkbox") next[key] = input.checked;
      else if (input.dataset.list !== undefined) {
        next[key] = input.value.split(",").map((value) => value.trim()).filter(Boolean);
      } else if (input.type === "number" || key === "uat_hour") {
        next[key] = Number(input.value);
      } else next[key] = input.value.trim();
    });
    next.schedule_mode = byId("schedule-mode-select")?.value || "continuous";
    next.schedule_days = Array.from(document.querySelectorAll("#days-field input:checked"), (input) => input.value);
    const providers = collectProviders();
    if (providers.length) next.providers = providers;
    return next;
  }

  function selectSchedule(mode, dirty = true) {
    if (byId("schedule-mode-select")) byId("schedule-mode-select").value = mode;
    byId("poll-field").classList.toggle("hidden", mode !== "continuous");
    byId("time-field").classList.toggle("hidden", !["daily", "weekdays", "custom"].includes(mode));
    byId("days-field").classList.toggle("hidden", mode !== "custom");
    if (dirty) setDirty();
    renderSummaries();
  }

  function renderSummaries() {
    if (!state.config) return;
    const config = state.dirty ? collectConfig() : state.config;
    const labels = {
      continuous: `Every ${config.poll_interval_seconds || 0}s while idle`,
      daily: `Daily at ${config.schedule_time}`,
      weekdays: `Weekdays at ${config.schedule_time}`,
      custom: `${(config.schedule_days || []).map((day) => day.toUpperCase()).join(", ") || "No days"} at ${config.schedule_time}`,
      manual: "Manual only",
    };
    byId("worker-schedule-summary").textContent = labels[config.schedule_mode] || "Not configured";
    byId("uat-schedule-summary").textContent = `${String(currentRepo()?.uat_hour ?? 3).padStart(2, "0")}:00 local`;
  }

  async function saveConfig({ quiet = false } = {}) {
    const next = collectConfig();
    const selectedRepository = currentRepo()?.github_repository;
    const saved = await invoke("save_config", { config: next });
    state.config = saved;
    state.activeRepoId = saved.repositories.find((repo) => repo.github_repository === selectedRepository)?.id
      || saved.repositories[0]?.id || "";
    bindConfig(saved);
    setDirty(false);
    if (!quiet) showToast("Configuration saved.", "success");
    void refreshStatus({ quiet: true });
    if (document.querySelector("#view-repository.active")) void refreshBranches({ quiet: true });
    return saved;
  }

  async function saveBeforeAction() {
    if (state.dirty) await saveConfig({ quiet: true });
  }

  async function withBusy(key, callback) {
    if (state.busy.has(key)) return;
    state.busy.add(key);
    renderControls();
    try {
      await callback();
    } catch (error) {
      showToast(errorText(error), "error");
    } finally {
      state.busy.delete(key);
      renderControls();
    }
  }

  async function runAction(action) {
    await withBusy(action, async () => {
      const isIssue = action.endsWith("issue");
      const repo = currentRepo();
      if (!isIssue && !repo) throw new Error("Choose a repository first.");
      const process = isIssue ? "issue" : `uat:${repo.id}`;
      if (action.startsWith("start-") || action.startsWith("run-")) {
        await saveBeforeAction();
        const command = isIssue ? "start_issue_worker" : "start_uat_scheduler";
        const args = { runOnce: action.startsWith("run-") };
        if (!isIssue) args.repoId = currentRepo().id;
        await invoke(command, args);
        showToast(`${isIssue ? "Issue worker" : "UAT scheduler"} started.`, "success");
      } else if (action.startsWith("pause-")) {
        const current = isIssue ? state.status?.issue?.state : currentRepoStatus()?.uat?.state;
        const command = current === "paused" ? "resume_process" : "pause_process";
        await invoke(command, { process });
        showToast(current === "paused" ? "Process resumed." : "Process paused.", "success");
      } else if (action.startsWith("stop-")) {
        await invoke("stop_process", { process });
        showToast("Process stopped.", "success");
      }
      await refreshStatus();
    });
  }

  function processLabel(status, kind) {
    if (!status) return "Stopped";
    if (status.state === "stopped" && status.exitCode !== null && status.exitCode !== undefined && status.exitCode !== 0) {
      if (kind === "issue" && status.exitCode === 10) return "Work completed";
      if (kind === "issue" && status.exitCode === 11) return "Progress saved";
      if (kind === "issue" && status.exitCode === 12) return "Waiting for AI";
      return `Exited ${status.exitCode}`;
    }
    return status.state.charAt(0).toUpperCase() + status.state.slice(1);
  }

  function renderProcess(kind, status) {
    const label = processLabel(status, kind);
    const expectedWorkerExit = kind === "issue" && [10, 11, 12].includes(status?.exitCode);
    const visualState = status?.state === "stopped" && status?.exitCode && !expectedWorkerExit ? "error" : (status?.state || "stopped");
    for (const id of [`${kind}-status-pill`, `${kind}-page-status`]) {
      const pill = byId(id);
      if (!pill) continue;
      pill.textContent = label;
      pill.className = `status-pill ${visualState}`;
    }
    const card = byId(`${kind}-service-card`);
    if (card) {
      card.classList.remove("running", "paused");
      if (["running", "paused"].includes(status?.state)) card.classList.add(status.state);
    }
    const copy = byId(`${kind}-status-copy`);
    if (!copy) return;
    if (status?.state === "running") copy.textContent = `Running as process ${status.pid}. Output is streaming to Info & Debug.`;
    else if (status?.state === "paused") copy.textContent = `Paused with its child processes preserved. Resume to continue exactly where it stopped.`;
    else if (kind === "issue" && status?.exitCode === 10) copy.textContent = "The latest issue pass completed successfully.";
    else if (kind === "issue" && status?.exitCode === 11) copy.textContent = "Work was safely saved until the selected AI provider has capacity again.";
    else if (kind === "issue" && status?.exitCode === 12) copy.textContent = "An issue is queued, but the enabled AI providers cannot start it yet. The worker will retry on schedule.";
    else if (status?.exitCode !== null && status?.exitCode !== undefined) copy.textContent = `Last run exited with status ${status.exitCode}. Review Info & Debug for details.`;
    else copy.textContent = kind === "issue" ? "Ready when your repository and AI providers are configured." : "Runs the repository’s frozen backend, Fire TV E2E, and TV UAT suites.";
  }

  function renderControls() {
    document.querySelectorAll("[data-action]").forEach((button) => {
      const action = button.dataset.action;
      const isIssue = action.endsWith("issue");
      const processState = isIssue ? (state.status?.issue?.state || "stopped") : (currentRepoStatus()?.uat?.state || "stopped");
      const busy = state.busy.has(action);
      if (action.startsWith("start-") || action.startsWith("run-")) {
        button.disabled = busy || processState !== "stopped" || (!isIssue && (!currentRepoStatus()?.uatAvailable || !currentRepo()?.uat_enabled));
      } else {
        button.disabled = busy || processState === "stopped";
      }
      if (action.startsWith("pause-")) {
        const long = button.textContent.trim().length > 2;
        button.textContent = processState === "paused" ? (long ? "Resume" : "▶") : (long ? "Pause" : "Ⅱ");
        button.title = processState === "paused" ? "Resume" : "Pause";
      }
    });
  }

  function addFact(container, label, value) {
    const fact = document.createElement("div");
    fact.className = "repo-fact";
    const name = document.createElement("span");
    name.textContent = label;
    const content = document.createElement("strong");
    content.textContent = value || "—";
    fact.append(name, content);
    container.appendChild(fact);
  }

  function currentRepoStatus() {
    return state.status?.repos?.find((repo) => repo.id === state.activeRepoId) || null;
  }

  function renderRepository(repo) {
    const status = currentRepoStatus() || {};
    const pill = byId("repo-valid-pill");
    pill.textContent = repo?.valid ? "Ready" : (status.workspaceManaged ? "Not cloned" : "Needs attention");
    pill.className = `status-pill ${repo?.valid ? "running" : "error"}`;
    const pathEl = byId("workspace-path");
    if (pathEl) pathEl.textContent = status.workspacePath || "—";
    const container = byId("repo-inspection");
    container.replaceChildren();
    addFact(container, "Branch", repo?.branch || "Not available");
    addFact(container, "Working tree", repo?.valid ? (repo.dirty ? "Uncommitted changes" : "Clean") : "Unknown");
    addFact(container, "GitHub remote", repo?.githubRepository || "Not inferred");
    if (repo?.error) addFact(container, "Problem", repo.error);
    const availability = byId("uat-availability");
    availability.textContent = repo?.uatAvailable ? "UAT runner found" : "UAT runner not present";
    availability.classList.toggle("ready", Boolean(repo?.uatAvailable));
  }

  // Accepts "owner/name", a full github.com URL, or an SSH remote; returns
  // "owner/name" or the trimmed input unchanged when it doesn't look like one.
  function normalizeRepoRef(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";
    const m = raw.match(/github\.com[/:]([^/\s]+)\/([^/\s]+?)(?:\.git)?\/?$/i);
    if (m) return `${m[1]}/${m[2]}`;
    return raw.replace(/\.git$/i, "").replace(/^\/+|\/+$/g, "");
  }

  async function prepareWorkspace() {
    await withBusy("prepare-workspace", async () => {
      await saveBeforeAction();
      showToast("Cloning / updating the workspace… watch Info & Debug if it's a large repo.", "success");
      const inspection = await invoke("prepare_workspace", { repoId: currentRepo().id });
      renderRepository(inspection);
      await refreshStatus();
      showToast("Workspace ready.", "success");
    });
  }

  function revealWorkspace() {
    invoke("open_workspace_folder", { repoId: currentRepo().id }).catch((error) => showToast(errorText(error), "error"));
  }

  function toolReady(tool) {
    return tool.installed && (tool.authenticated === null || tool.authenticated === undefined || tool.authenticated === true);
  }

  function button(text, className, handler) {
    const control = document.createElement("button");
    control.type = "button";
    control.className = className;
    control.textContent = text;
    control.addEventListener("click", handler);
    return control;
  }

  function renderTools() {
    const grid = byId("tool-grid");
    grid.replaceChildren();
    // AI providers get their own richer cards; this grid is the supporting
    // toolchain (git, gh, python, node, npm).
    state.tools.filter((tool) => !PROVIDER_META[tool.id]).forEach((tool) => {
      const ready = toolReady(tool);
      const card = document.createElement("article");
      card.className = `tool-card ${tool.required ? "required" : ""} ${ready ? "ready" : ""}`;
      const head = document.createElement("div");
      head.className = "tool-head";
      const symbol = document.createElement("div");
      symbol.className = "tool-symbol";
      symbol.textContent = symbols[tool.id] || tool.label.slice(0, 2);
      const badge = document.createElement("span");
      badge.className = "tool-badge";
      badge.textContent = tool.status;
      head.append(symbol, badge);
      const title = document.createElement("h3");
      title.textContent = tool.label;
      const version = document.createElement("div");
      version.className = "tool-version";
      version.textContent = tool.installed ? `${tool.version || "Installed"}\n${tool.path}` : "Not found in login-shell PATH";
      const actions = document.createElement("div");
      actions.className = "tool-actions";
      if (tool.id === "gh" && tool.installed && tool.authenticated === false) {
        actions.appendChild(button("Sign in", "secondary-button", () => signIn(tool.id)));
      }
      if (["node", "npm"].includes(tool.id) && !tool.installed) {
        actions.appendChild(button("Install Node.js", "secondary-button", () => openUrl("https://nodejs.org/en/download")));
      }
      card.append(head, title, version, actions);
      grid.appendChild(card);
    });
    if (state.config) renderProviderCards(state.dirty ? collectConfig() : state.config);
    renderWorkerProviderSummary();
  }

  async function refreshTools({ quiet = false } = {}) {
    if (state.refreshing.tools) return;
    state.refreshing.tools = true;
    try {
      state.tools = await invoke("detect_tools_background");
      renderTools();
      renderReadiness();
    } catch (error) {
      if (!quiet) showToast(errorText(error), "error");
    } finally {
      state.refreshing.tools = false;
    }
  }

  async function installProvider(provider) {
    await withBusy(`install-${provider}`, async () => {
      await invoke("install_ai_cli", { provider });
      const cli = PROVIDER_META[provider]?.cli || provider;
      showToast(
        provider === "grok"
          ? "A Terminal window opened running the Grok Build installer."
          : `${cli} installation started. Watch Info & Debug.`,
        "success",
      );
      if (provider !== "grok") navigate("debug");
    });
  }

  async function signIn(provider) {
    try {
      await invoke("open_provider_login", { provider });
      showToast("A Terminal window was opened for sign-in.", "success");
    } catch (error) { showToast(errorText(error), "error"); }
  }

  async function openUrl(url) {
    try { await invoke("open_external_url", { url }); } catch (error) { showToast(errorText(error), "error"); }
  }

  function renderReadiness() {
    if (!state.status) return;
    const gh = state.tools.find((tool) => tool.id === "gh");
    const enabledIds = new Set(
      providerList(state.dirty ? collectConfig() : state.config).filter((p) => p.enabled).map((p) => p.id),
    );
    const ais = state.tools.filter((tool) => enabledIds.has(tool.id));
    const repoStatus = currentRepoStatus();
    const checks = [
      ["GitHub CLI", Boolean(gh && toolReady(gh)), gh?.status || "Not detected"],
      ["AI provider", ais.some(toolReady), ais.some(toolReady) ? "Signed in" : "Sign in required"],
      ["Bot identities", !currentRepo()?.require_bot_auth || repoStatus?.botConfigExists, currentRepo()?.require_bot_auth ? (repoStatus?.botConfigExists ? "Configured" : "Setup needed") : "Optional"],
      ["Worker runtime", repoStatus?.workerAvailable, repoStatus?.workerAvailable ? "Available" : "Unavailable"],
    ];
    const list = byId("readiness-list");
    list.replaceChildren();
    checks.forEach(([label, ready, detail]) => {
      const row = document.createElement("div");
      row.className = `check-item ${ready ? "ready" : ""}`;
      const dot = document.createElement("span");
      dot.className = "check-dot";
      dot.textContent = "✓";
      const text = document.createElement("strong");
      text.textContent = label;
      const stateText = document.createElement("span");
      stateText.textContent = detail;
      row.append(dot, text, stateText);
      list.appendChild(row);
    });
    byId("readiness-score").textContent = `${checks.filter((entry) => entry[1]).length} / ${checks.length}`;
  }

  function renderStatus() {
    if (!state.status) return;
    renderProcess("issue", state.status.issue);
    const repoStatus = currentRepoStatus();
    renderProcess("uat", repoStatus?.uat);
    renderRepository(repoStatus?.repository);
    const warning = byId("config-warning");
    warning.replaceChildren();
    if (state.status.configError) {
      warning.append(`${state.status.configError} `);
      warning.appendChild(button("Open Repository →", "text-button", () => navigate("repository")));
    }
    warning.classList.toggle("hidden", !state.status.configError);
    byId("bot-config-pill").textContent = repoStatus?.botConfigExists ? "Configuration found" : "Not configured";
    byId("bot-config-pill").className = `status-pill ${repoStatus?.botConfigExists ? "running" : "stopped"}`;
    renderControls();
    renderReadiness();
  }

  async function refreshStatus({ quiet = false } = {}) {
    if (state.refreshing.status) return;
    state.refreshing.status = true;
    try {
      state.status = await invoke("get_automation_status_background");
      renderStatus();
    } catch (error) {
      if (!quiet) showToast(errorText(error), "error");
    } finally {
      state.refreshing.status = false;
    }
  }

  function formatLog(event) {
    const time = new Date(event.timestamp * 1000).toLocaleTimeString([], { hour12: false });
    return `[${time}] [${event.source}/${event.stream}] ${event.line}`;
  }

  function parseAutomationLog(raw) {
    const match = String(raw).match(/^\[([^\]]+)\] \[(.*)\/([^/\]]+)\] (.*)$/);
    if (!match) return null;
    const rawTime = match[1];
    const time = /^\d{9,}$/.test(rawTime)
      ? new Date(Number(rawTime) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : rawTime.slice(0, 5);
    return {
      raw: String(raw),
      time,
      source: match[2],
      stream: match[3],
      message: match[4].replace(/^\[[^\]]+\]\s*/, "").trim(),
    };
  }

  function activityCategory(source) {
    const value = source.toLowerCase();
    if (value.includes("uat") || value.includes("test")) return "tests";
    if (value.includes("setup") || value.includes("install") || value.includes("github bot")) return "setup";
    return "work";
  }

  function activitySourceLabel(source, category) {
    if (category === "tests") return "Tests";
    if (category === "setup") return source.toLowerCase().includes("github bot") ? "GitHub setup" : "Setup";
    return source.toLowerCase().includes("issue") ? "Issue worker" : "Automation";
  }

  function makeActivity(log, summary, description, tone = "info", category = activityCategory(log.source)) {
    return {
      ...log,
      summary,
      description,
      tone,
      category,
      sourceLabel: activitySourceLabel(log.source, category),
    };
  }

  // Overview is intentionally an allowlist of meaningful milestones. Every
  // raw line still goes to Info & Debug, while routine command output, paths,
  // HTTP requests, usage text, and process details stay out of this view.
  function importantActivity(raw) {
    const log = parseAutomationLog(raw);
    if (!log) return null;
    const message = log.message;
    const lower = message.toLowerCase();
    const category = activityCategory(log.source);
    const source = activitySourceLabel(log.source, category);
    const issueNumber = message.match(/\bissue\s*:?\s*#(\d+)/i)?.[1]
      || message.match(/\bissue-(\d+)/i)?.[1]
      || message.match(/\bissue\s+(\d+)/i)?.[1];

    if (
      !message
      || /^usage:/i.test(message)
      || /^\[--/.test(message)
      || /^GitHub App setup: "(?:GET|POST) /i.test(message)
      || lower.includes("live output is also appended")
      || /^=== repo:/i.test(message)
    ) return null;

    if (/unrecognized arguments/i.test(message)) {
      return makeActivity(log, "The issue worker could not start", "Its settings did not match the installed worker. Open Info & Debug for the exact options that failed.", "error", "work");
    }
    const nonzeroExit = message.match(/exited with status\s+(-?\d+)/i);
    if (nonzeroExit && Number(nonzeroExit[1]) !== 0) {
      const exitCode = Number(nonzeroExit[1]);
      const expectedWorkerExit = log.source.toLowerCase().includes("issue worker");
      if (expectedWorkerExit && exitCode === 10) return makeActivity(log, "Issue work completed", "The latest issue pass finished successfully.", "success", "work");
      if (expectedWorkerExit && exitCode === 11) return makeActivity(log, "Issue progress was saved", "The provider reached its usage limit. Work is safely saved and will resume when capacity returns.", "waiting", "work");
      if (expectedWorkerExit && exitCode === 12) return makeActivity(log, "Queued work is waiting for AI capacity", "The issue was found, but none of the enabled AI providers can start it yet. The worker will retry on schedule.", "waiting", "work");
      return makeActivity(log, `${source} stopped with an error`, "Open Info & Debug for the exact error. The app will not mark any issue complete because this run failed.", "error", category);
    }
    if (/quota unavailable|usage .* below|capacity unavailable/i.test(message)) {
      const name = message.match(/^(Claude|Codex|Grok)/i)?.[1] || "An AI provider";
      return makeActivity(log, `${name} is temporarily unavailable`, "The app will try another enabled provider or wait until capacity returns.", "waiting", "work");
    }
    if (/no enabled provider|an issue is queued.*no enabled ai provider|queued issue work is waiting for ai capacity/i.test(message)) {
      return makeActivity(log, "Queued work is waiting for AI capacity", "The issue was found, but none of the enabled AI providers can start it yet. The worker will retry on schedule.", "waiting", "work");
    }
    if (/traceback|permission denied|authentication failed|\berror:|\bfailed\b|\bcould not\b/i.test(message)) {
      return makeActivity(log, `${source} needs attention`, "Something prevented this step from finishing. Open Info & Debug for the exact error and command output.", "error", category);
    }
    if (/^Started .* as pid \d+/i.test(message)) {
      return makeActivity(log, `${source} started`, category === "tests" ? "The configured test run is now active." : category === "setup" ? "The requested setup task is now running." : "The app is now watching the configured repositories for ready issues.", "info", category);
    }
    if (/exited with status 0/i.test(message)) {
      return makeActivity(log, `${source} finished`, "The process completed normally.", "success", category);
    }
    if (/Ctrl\+C received|process stopped|scheduler stopped/i.test(message)) {
      return makeActivity(log, `${source} stopped`, "It will remain stopped until you start it again.", "waiting", category);
    }
    if (/Starting (?:a worker run|a cycle over)/i.test(message)) {
      return makeActivity(log, "Checking for ready issues", "The worker is reviewing the configured GitHub queues in order.", "info", "work");
    }
    if (/Local .* (?:is synchronized with|mirrors) origin/i.test(message)) {
      return makeActivity(log, "Repository is up to date", "The app confirmed that the local human-owned branch matches GitHub before starting new work.", "success", "work");
    }
    // The multi-repository scheduler used to append this generic line after
    // every zero exit, including a real issue blocked on provider capacity.
    // The worker's own selection result is the authoritative activity event.
    if (/^[^:]+:\s+no issue to work right now\.?$/i.test(message)) return null;
    if (/no new issue or follow-up comment assigned/i.test(message)) return null;
    if (/cycle complete: no ready issues|no (?:ready|eligible) issue|nothing to work/i.test(message)) {
      return makeActivity(log, "No ready issues found", "Nothing needs action right now. The worker will check again on the configured schedule.", "waiting", "work");
    }
    if (/Selected oldest unprocessed assigned issue/i.test(message) && issueNumber) {
      return makeActivity(log, `Picked up issue #${issueNumber}`, "The worker selected this issue from the configured GitHub queue.", "info", "work");
    }
    const provider = message.match(/^Selected (Claude|Codex|Grok) model/i)?.[1];
    if (provider) {
      return makeActivity(log, `${provider} is starting work`, "This provider has enough capacity and was selected for the current issue.", "info", "work");
    }
    if (/Created issue branch|Continuing issue .* existing branch|Recreated interrupted issue branch/i.test(message) && issueNumber) {
      return makeActivity(log, `Prepared a safe branch for issue #${issueNumber}`, "AI changes stay on this issue branch. They are not written directly to the human-owned branch.", "success", "work");
    }
    if (/Committed completed issue/i.test(message) && issueNumber) {
      return makeActivity(log, `Saved completed changes for issue #${issueNumber}`, "The verified changes were committed to the issue branch with the AI provider identified in the commit message.", "success", "work");
    }
    if (/Returned the clean local checkout/i.test(message)) {
      return makeActivity(log, "Workspace is ready for the next issue", "The app returned its managed copy to a clean state.", "success", "work");
    }
    if (/Posted .* start notice to issue/i.test(message) && issueNumber) {
      return makeActivity(log, `Posted a start update on issue #${issueNumber}`, "GitHub now shows which AI provider began this pass.", "success", "work");
    }
    if (/ Bot approved https?:\/\//i.test(message)) {
      return makeActivity(log, "A second AI approved the pull request", "The reviewing provider used a different GitHub identity from the provider that wrote the changes.", "success", "work");
    }
    if (/squash-merged/i.test(message) && issueNumber) {
      return makeActivity(log, `Merged closed issue #${issueNumber} into the AI branch`, "The issue branch was combined into one commit and removed. The human-owned branch was not changed.", "success", "work");
    }
    if (/Adding the .* label to GitHub issue/i.test(message) && issueNumber) {
      return makeActivity(log, `Marked issue #${issueNumber} ready for testing`, "The configured ready label was added on GitHub.", "success", "work");
    }
    if (/Shelved quota-paused issue/i.test(message) && issueNumber) {
      return makeActivity(log, `Paused issue #${issueNumber} until capacity returns`, "Its progress was saved so other ready issues can continue.", "waiting", "work");
    }
    if (/closed while quota-paused/i.test(message) && issueNumber) {
      return makeActivity(log, `Removed closed issue #${issueNumber} from waiting work`, "The saved attempt was archived without running another AI pass.", "success", "work");
    }
    if (/Authenticated .* successfully/i.test(message)) {
      const bot = message.match(/Authenticated (.+?) successfully/i)?.[1] || "GitHub bot";
      return makeActivity(log, `${bot} connected to GitHub`, "This provider can now identify its own commits, comments, reviews, and merges.", "success", "setup");
    }
    if (/Configuration (?:saved|already complete)/i.test(message)) {
      return makeActivity(log, "GitHub bot setup is complete", "The local bot identities are ready for the enabled AI providers.", "success", "setup");
    }
    if (/exists, but it cannot access|must be owned by/i.test(message)) {
      return makeActivity(log, "A GitHub bot needs setup", "The existing bot is not installed for this repository or belongs to the wrong GitHub owner.", "waiting", "setup");
    }
    if (category === "tests" && /skip(?:ped|ping).*unchanged/i.test(message)) {
      return makeActivity(log, "Tests skipped because the code has not changed", "There is no new commit to verify.", "waiting", "tests");
    }
    if (category === "tests" && /(?:all tests|test suite|uat).*(?:passed|completed|succeeded)|(?:passed|completed|succeeded).*(?:tests|uat)/i.test(message)) {
      return makeActivity(log, "Tests passed", "The configured repository checks completed successfully.", "success", "tests");
    }
    if (category === "tests" && /(?:starting|running).*(?:test|uat|backend|fire tv)/i.test(message)) {
      return makeActivity(log, "Test run started", "The app is running the repository’s configured checks.", "info", "tests");
    }
    if (/notification|email/i.test(message) && /sent|delivered/i.test(message)) {
      return makeActivity(log, "Completion email sent", "The configured recipient was notified.", "success", category);
    }
    return null;
  }

  function renderActivity() {
    const feed = byId("overview-activity");
    if (!feed) return;
    const sourceLogs = state.activityPaused ? (state.activitySnapshot || []) : state.logs;
    const events = [];
    sourceLogs.forEach((raw) => {
      const event = importantActivity(raw);
      if (!event) return;
      const previous = events[events.length - 1];
      if (previous && previous.summary === event.summary && previous.category === event.category) {
        events[events.length - 1] = event;
      } else {
        events.push(event);
      }
    });
    const filtered = events.filter((event) => {
      if (state.activityFilter === "all") return true;
      if (state.activityFilter === "problems") return event.tone === "error";
      return event.category === state.activityFilter;
    }).slice(-12).reverse();

    feed.replaceChildren();
    if (!filtered.length) {
      const empty = document.createElement("div");
      empty.className = "activity-empty";
      const title = document.createElement("strong");
      title.textContent = state.activityFilter === "all" ? "No important activity yet" : "Nothing in this category";
      const detail = document.createElement("span");
      detail.textContent = state.activityFilter === "all" ? "Start a service to see meaningful progress here." : "Try another filter or open Info & Debug for every log line.";
      empty.append(title, detail);
      feed.appendChild(empty);
    }

    const icons = { success: "✓", error: "!", waiting: "…", info: "•" };
    filtered.forEach((event) => {
      const item = document.createElement("details");
      item.className = `activity-item ${event.tone}`;
      const summary = document.createElement("summary");
      const icon = document.createElement("span");
      icon.className = "activity-icon";
      icon.textContent = icons[event.tone] || "•";
      const words = document.createElement("span");
      words.className = "activity-words";
      const title = document.createElement("strong");
      title.textContent = event.summary;
      const meta = document.createElement("span");
      meta.textContent = `${event.time} · ${event.sourceLabel}`;
      words.append(title, meta);
      const expand = document.createElement("span");
      expand.className = "activity-expand";
      expand.textContent = "Details";
      summary.append(icon, words, expand);
      const description = document.createElement("p");
      description.textContent = event.description;
      item.append(summary, description);
      feed.appendChild(item);
    });

    document.querySelectorAll("[data-activity-filter]").forEach((control) => {
      control.classList.toggle("active", control.dataset.activityFilter === state.activityFilter);
    });
    const live = byId("activity-live-state");
    live.classList.toggle("paused", state.activityPaused);
    live.lastChild.textContent = state.activityPaused ? "PAUSED" : "LIVE";
    byId("toggle-activity-live").textContent = state.activityPaused ? "Resume updates" : "Pause updates";
  }

  function renderLogs() {
    const filtered = state.logFilter === "all" ? state.logs : state.logs.filter((line) => line.includes(`[${state.logFilter}/`));
    const text = filtered.length ? filtered.join("\n") : "Waiting for output…";
    const full = byId("full-log");
    const stayAtBottom = full.scrollTop + full.clientHeight >= full.scrollHeight - 28;
    full.textContent = text;
    if (stayAtBottom) full.scrollTop = full.scrollHeight;
    renderActivity();
    byId("log-count").textContent = String(Math.min(state.logs.length, 999));
  }

  async function chooseRepository() {
    await withBusy("browse", async () => {
      const inspection = await invoke("choose_repository");
      if (!inspection) return;
      // The picker sets the advanced working-copy override.
      document.querySelector('[data-repo-config="repo_dir"]').value = inspection.path;
      const repoField = byId("github-repository-input");
      if (inspection.githubRepository && repoField && !repoField.value.trim()) {
        repoField.value = inspection.githubRepository;
      }
      renderRepository(inspection);
      setDirty();
      renderReadiness();
    });
  }

  async function savePassword() {
    const password = byId("smtp-password").value;
    try {
      const configured = await invoke("set_smtp_password", { password });
      byId("smtp-password").value = "";
      showToast(configured ? "SMTP password saved to macOS Keychain." : "Stored SMTP password removed.", "success");
      await refreshStatus();
    } catch (error) { showToast(errorText(error), "error"); }
  }

  async function setupBots() {
    await withBusy("bots", async () => {
      await saveBeforeAction();
      await invoke("launch_bot_setup", { repoId: currentRepo().id });
      showToast("Bot setup checked. A browser opens only for enabled bots that still need local credentials or installation.", "success");
      navigate("debug");
    });
  }

  async function verifyBots() {
    await withBusy("verify-bots", async () => {
      await saveBeforeAction();
      const results = await invoke("verify_github_bots", { repoId: currentRepo().id });
      const container = byId("bot-results");
      container.replaceChildren();
      if (!results.length) {
        const row = document.createElement("div");
        row.className = "verification-result";
        row.textContent = "No providers are enabled in the flow.";
        container.appendChild(row);
      }
      results.forEach((result) => {
        const row = document.createElement("div");
        row.className = `verification-result ${result.valid ? "valid" : "invalid"}`;
        row.textContent = `${providerLabel(result.provider)} Bot: ${result.message}`;
        container.appendChild(row);
      });
      await refreshStatus();
    });
  }

  function addRepository() {
    stashRepositoryForm();
    const repo = defaultRepository();
    state.config.repositories.push(repo);
    state.activeRepoId = repo.id;
    state.branchOverview = null;
    renderRepositorySelector();
    bindRepositoryForm();
    renderStatus();
    renderBranchOverview(null);
    setDirty();
    navigate("repository");
    byId("github-repository-input").focus();
  }

  function removeRepository() {
    const repo = currentRepo();
    if (!repo) return;
    const label = repo.github_repository || "this unsaved repository";
    if (!window.confirm(`Remove ${label} from monitoring? This does not delete its local clone or any GitHub branches.`)) return;
    state.config.repositories = state.config.repositories.filter((entry) => entry.id !== repo.id);
    if (!state.config.repositories.length) state.config.repositories.push(defaultRepository());
    state.activeRepoId = state.config.repositories[0].id;
    state.branchOverview = null;
    renderRepositorySelector();
    bindRepositoryForm();
    renderStatus();
    renderBranchOverview(null);
    setDirty();
  }

  function selectRepository(repoId) {
    stashRepositoryForm();
    state.activeRepoId = repoId;
    state.branchOverview = null;
    bindRepositoryForm();
    renderRepositorySelector();
    renderSummaries();
    renderStatus();
    if (document.querySelector("#view-repository.active")) void refreshBranches({ quiet: true });
  }

  function branchNode(label, name, tip, meta = "") {
    const row = document.createElement("div");
    row.className = "branch-node";
    const rail = document.createElement("span");
    rail.className = "branch-rail";
    const body = document.createElement("div");
    body.className = "branch-node-body";
    const kicker = document.createElement("span");
    kicker.className = "branch-kind";
    kicker.textContent = label;
    const title = document.createElement("strong");
    title.textContent = name;
    const detail = document.createElement("span");
    detail.className = "branch-detail";
    detail.textContent = tip?.sha ? `${tip.sha.slice(0, 8)} · ${tip.subject || "No subject"}${meta ? ` · ${meta}` : ""}` : (meta || "No commit available");
    body.append(kicker, title, detail);
    row.append(rail, body);
    return { row, body };
  }

  function renderBranchOverview(overview) {
    const tree = byId("branch-tree");
    const graph = byId("raw-git-graph");
    const warning = byId("branch-warning");
    tree.replaceChildren();
    graph.textContent = overview?.graph || "No branch data loaded.";
    warning.classList.add("hidden");
    if (!overview) {
      const empty = document.createElement("article");
      empty.className = "panel";
      empty.textContent = "Save and clone this repository to inspect its branches.";
      tree.appendChild(empty);
      return;
    }
    if (overview.error) {
      warning.textContent = overview.error;
      warning.classList.remove("hidden");
    }

    const panel = document.createElement("article");
    panel.className = "panel branch-map";
    const base = branchNode("HUMAN-OWNED", overview.baseBranch, overview.baseTip);
    panel.appendChild(base.row);

    const relation = overview.integrationExists
      ? `${overview.integrationVsBase.ahead} ahead · ${overview.integrationVsBase.behind} behind ${overview.baseBranch}`
      : "Created automatically before the next issue";
    const integration = branchNode("AI INTEGRATION", overview.integrationBranch, overview.integrationTip, relation);
    integration.row.classList.add("integration-node");
    const integrationActions = document.createElement("div");
    integrationActions.className = "branch-actions";
    if (overview.integrationVsBase.behind > 0) {
      const badge = document.createElement("span");
      badge.className = "branch-alert";
      badge.textContent = `Behind ${overview.baseBranch} — next issue run will attempt parity merge`;
      integrationActions.appendChild(badge);
    }
    if (overview.integrationVsBase.ahead > 0) {
      integrationActions.appendChild(button(
        overview.integrationPrUrl ? "Open promotion PR" : "Create promotion PR",
        "secondary-button compact",
        () => openIntegrationPullRequest(),
      ));
      if (overview.integrationPrNumber) {
        integrationActions.appendChild(button(
          `Merge into ${overview.baseBranch}`,
          "primary-button compact",
          () => mergeIntegrationPullRequest(overview.integrationPrNumber),
        ));
      }
    }
    integration.body.appendChild(integrationActions);
    panel.appendChild(integration.row);

    const issueList = document.createElement("div");
    issueList.className = "issue-branch-list";
    if (!overview.issueBranches.length) {
      const empty = document.createElement("p");
      empty.className = "panel-copy branch-empty";
      empty.textContent = "No active issue branches. Squash-merged branches disappear from this tree.";
      issueList.appendChild(empty);
    }
    overview.issueBranches.forEach((branch) => {
      const issueClosed = branch.issueState === "CLOSED";
      const issueStatus = issueClosed ? "issue closed" : branch.issueState === "OPEN" ? "issue open" : "issue state unknown";
      const meta = `${providerLabel(branch.aiTool)} · ${issueStatus} · ${branch.aheadOfIntegration} ahead · ${branch.behindIntegration} behind`;
      const node = branchNode(`ISSUE #${branch.issueNumber}`, branch.name.replace(/^origin\//, ""), branch.lastCommit, meta);
      node.row.classList.add("issue-node");
      const actions = document.createElement("div");
      actions.className = "branch-actions";
      if (branch.prUrl) {
        actions.appendChild(button(`Open PR #${branch.prNumber}`, "secondary-button compact", () => openUrl(branch.prUrl)));
        const canMerge = issueClosed && branch.mergeable !== "CONFLICTING";
        const merge = button("Squash into AI integration", "primary-button compact", () => mergeIssuePullRequest(branch));
        merge.disabled = !canMerge;
        merge.title = canMerge
          ? "Squash-merge this closed issue branch"
          : !issueClosed
            ? `Close issue #${branch.issueNumber} before merging`
            : "GitHub reports merge conflicts";
        actions.appendChild(merge);
        if (!issueClosed) {
          const status = document.createElement("span");
          status.className = "branch-alert";
          status.textContent = `Close issue #${branch.issueNumber} to unlock merge`;
          actions.appendChild(status);
        }
      } else {
        const status = document.createElement("span");
        status.className = "branch-alert";
        status.textContent = "Waiting for pull request";
        actions.appendChild(status);
      }
      node.body.appendChild(actions);
      issueList.appendChild(node.row);
    });
    panel.appendChild(issueList);
    tree.appendChild(panel);
  }

  async function refreshBranches({ quiet = false } = {}) {
    const repo = currentRepo();
    if (!repo || repo.id.startsWith("draft-")) {
      renderBranchOverview(null);
      return;
    }
    if (state.refreshing.branches) return;
    state.refreshing.branches = true;
    const requestedRepoId = repo.id;
    try {
      const overview = await invoke("git_overview_background", { repoId: requestedRepoId });
      if (currentRepo()?.id !== requestedRepoId) return;
      state.branchOverview = overview;
      renderBranchOverview(state.branchOverview);
    } catch (error) {
      if (currentRepo()?.id === requestedRepoId && !state.branchOverview) renderBranchOverview(null);
      if (!quiet) showToast(errorText(error), "error");
    } finally {
      state.refreshing.branches = false;
      if (currentRepo()?.id !== requestedRepoId && document.querySelector("#view-repository.active")) {
        void refreshBranches({ quiet: true });
      }
    }
  }

  async function mergeIssuePullRequest(branch) {
    if (!window.confirm(`Issue #${branch.issueNumber} is closed. Squash PR #${branch.prNumber} into ${currentRepo().integration_branch} and delete ${branch.name.replace(/^origin\//, "")}?`)) return;
    await withBusy(`merge-${branch.prNumber}`, async () => {
      state.branchOverview = await invoke("merge_issue_branch", {
        repoId: currentRepo().id,
        prNumber: branch.prNumber,
        issueNumber: branch.issueNumber,
      });
      renderBranchOverview(state.branchOverview);
      showToast(`Closed issue #${branch.issueNumber}'s PR was squash-merged.`, "success");
    });
  }

  async function openIntegrationPullRequest() {
    await withBusy("integration-pr", async () => {
      const url = await invoke("open_integration_pr", { repoId: currentRepo().id });
      showToast(`Promotion pull request ready: ${url}`, "success");
      await refreshBranches({ quiet: true });
    });
  }

  async function mergeIntegrationPullRequest(prNumber) {
    const repo = currentRepo();
    if (!window.confirm(`Merge ${repo.integration_branch} into ${repo.base_branch} via PR #${prNumber}? This is the explicit human promotion gate.`)) return;
    await withBusy("merge-integration", async () => {
      state.branchOverview = await invoke("merge_integration_branch", { repoId: repo.id, prNumber });
      renderBranchOverview(state.branchOverview);
      showToast(`${repo.integration_branch} was merged into ${repo.base_branch}.`, "success");
    });
  }

  // ----- Interactive help modal -------------------------------------------
  let helpReturnFocus = null;

  function openHelp(topic) {
    const entry = HELP_TOPICS[topic];
    if (!entry) return;
    byId("help-modal-title").textContent = entry.title;
    byId("help-modal-body").innerHTML = entry.html;
    const links = byId("help-modal-links");
    links.replaceChildren();
    (entry.links || []).forEach((link) => {
      links.appendChild(button(link.label, "secondary-button", () => openUrl(link.url)));
    });
    helpReturnFocus = document.activeElement;
    byId("help-modal").hidden = false;
    byId("help-modal-close").focus();
  }

  function closeHelp() {
    byId("help-modal").hidden = true;
    if (helpReturnFocus && helpReturnFocus.focus) helpReturnFocus.focus();
    helpReturnFocus = null;
  }

  function renderHelpConcepts() {
    const box = byId("help-concepts");
    if (!box) return;
    box.replaceChildren();
    HELP_CONCEPTS.forEach(([label, topic]) => {
      const row = document.createElement("div");
      row.className = "concept-row";
      const name = document.createElement("span");
      name.textContent = label;
      row.append(name, button("Learn more", "text-button", () => openHelp(topic)));
      box.appendChild(row);
    });
  }

  function onProvidersChanged() {
    setDirty();
    renderPreferredOptions(collectConfig());
    renderWorkerProviderSummary();
    renderReadiness();
  }

  function bindEvents() {
    document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.viewTarget)));
    // Delegated: help dots, "further reading" links, and in-app jump links —
    // covers elements rendered after init (provider cards, help concepts).
    document.addEventListener("click", (event) => {
      const help = event.target.closest("[data-help]");
      if (help) { openHelp(help.dataset.help); return; }
      const external = event.target.closest("[data-external]");
      if (external) { event.preventDefault(); openUrl(external.dataset.external); return; }
      const jump = event.target.closest("[data-view-jump]");
      if (jump) { event.preventDefault(); navigate(jump.dataset.viewJump); }
    });
    byId("help-modal-close").addEventListener("click", closeHelp);
    byId("help-modal").addEventListener("click", (event) => {
      if (event.target === byId("help-modal")) closeHelp();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !byId("help-modal").hidden) closeHelp();
    });
    byId("schedule-mode-select").addEventListener("change", (event) => selectSchedule(event.target.value));
    document.querySelectorAll("[data-config], [data-repo-config], [data-provider-bin], #days-field input").forEach((input) => {
      input.addEventListener("input", () => { setDirty(); renderSummaries(); });
      input.addEventListener("change", () => { setDirty(); renderSummaries(); });
    });
    document.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => runAction(button.dataset.action)));
    document.querySelectorAll("[data-log-filter]").forEach((button) => button.addEventListener("click", () => {
      state.logFilter = button.dataset.logFilter;
      document.querySelectorAll("[data-log-filter]").forEach((candidate) => candidate.classList.toggle("active", candidate === button));
      renderLogs();
    }));
    document.querySelectorAll("[data-activity-filter]").forEach((button) => button.addEventListener("click", () => {
      state.activityFilter = button.dataset.activityFilter;
      renderActivity();
    }));
    byId("toggle-activity-live").addEventListener("click", () => {
      state.activityPaused = !state.activityPaused;
      state.activitySnapshot = state.activityPaused ? [...state.logs] : null;
      renderActivity();
    });
    byId("save-button").addEventListener("click", () => withBusy("save", () => saveConfig()));
    byId("hide-button").addEventListener("click", () => invoke("hide_to_tray").catch((error) => showToast(errorText(error), "error")));
    byId("refresh-tools").addEventListener("click", () => refreshTools());
    byId("refresh-branches").addEventListener("click", () => refreshBranches());
    byId("active-repo-select").addEventListener("change", (event) => selectRepository(event.target.value));
    byId("add-repo").addEventListener("click", addRepository);
    byId("remove-repo").addEventListener("click", removeRepository);
    byId("browse-repo").addEventListener("click", chooseRepository);
    byId("prepare-workspace").addEventListener("click", prepareWorkspace);
    byId("reveal-workspace").addEventListener("click", revealWorkspace);
    byId("github-repository-input").addEventListener("blur", (event) => {
      const normalized = normalizeRepoRef(event.target.value);
      if (normalized !== event.target.value) event.target.value = normalized;
      stashRepositoryForm();
      renderRepositorySelector();
      setDirty();
    });
    byId("save-password").addEventListener("click", savePassword);
    byId("setup-bots").addEventListener("click", setupBots);
    byId("verify-bots").addEventListener("click", verifyBots);
    byId("open-log-folder").addEventListener("click", () => invoke("open_automation_folder").catch((error) => showToast(errorText(error), "error")));
    byId("clear-log").addEventListener("click", () => {
      state.logs = [];
      if (state.activityPaused) state.activitySnapshot = [];
      renderLogs();
    });
  }

  async function initialize() {
    populateHours();
    renderHelpConcepts();
    bindEvents();
    try {
      state.config = await invoke("get_config");
      bindConfig(state.config);
      renderWorkerProviderSummary();
      setDirty(false);
      state.logs = await invoke("get_recent_logs");
      renderLogs();
      await listen("automation-log", (event) => {
        state.logs.push(formatLog(event.payload));
        if (state.logs.length > 1000) state.logs.splice(0, state.logs.length - 1000);
        renderLogs();
      });
      // Tool detection and repository inspection run independently. Keeping
      // them out of the startup await path prevents slow CLIs or network-backed
      // Git checks from freezing navigation and configuration editing.
      void refreshStatus();
      void refreshTools();
      window.setInterval(() => void refreshStatus({ quiet: true }), 2000);
      window.setInterval(() => {
        if (state.busy.size === 0) void refreshTools({ quiet: true });
      }, 30000);
    } catch (error) {
      showToast(`Could not initialize the application: ${errorText(error)}`, "error");
    }
  }

  window.addEventListener("DOMContentLoaded", initialize);
})();
