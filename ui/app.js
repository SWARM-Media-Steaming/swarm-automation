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
    dirty: false,
    busy: new Set(),
  };

  const pageTitles = {
    overview: "Automation overview",
    worker: "Issue worker",
    providers: "AI & GitHub",
    uat: "UAT scheduler",
    settings: "Repository profile",
    logs: "Live logs",
  };
  const symbols = { git: "G", gh: "GH", python: "Py", node: "N", npm: "npm", claude: "C", codex: "X" };

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
    byId("profile-kicker").textContent = `${config.profile_name || "REPOSITORY"} PROFILE`.toUpperCase();
    renderSummaries();
  }

  function collectConfig() {
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
    next.schedule_mode = document.querySelector("#schedule-segmented .active")?.dataset.schedule || "continuous";
    next.schedule_days = Array.from(document.querySelectorAll("#days-field input:checked"), (input) => input.value);
    return next;
  }

  function selectSchedule(mode, dirty = true) {
    document.querySelectorAll("#schedule-segmented button").forEach((button) => {
      button.classList.toggle("active", button.dataset.schedule === mode);
    });
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
    byId("uat-schedule-summary").textContent = `${String(config.uat_hour).padStart(2, "0")}:00 local`;
  }

  async function saveConfig({ quiet = false } = {}) {
    const next = collectConfig();
    const saved = await invoke("save_config", { config: next });
    state.config = saved;
    bindConfig(saved);
    setDirty(false);
    if (!quiet) showToast("Configuration saved.", "success");
    await refreshStatus();
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
      const process = isIssue ? "issue" : "uat";
      if (action.startsWith("start-") || action.startsWith("run-")) {
        await saveBeforeAction();
        const command = isIssue ? "start_issue_worker" : "start_uat_scheduler";
        await invoke(command, { runOnce: action.startsWith("run-") });
        showToast(`${isIssue ? "Issue worker" : "UAT scheduler"} started.`, "success");
      } else if (action.startsWith("pause-")) {
        const current = state.status?.[process]?.state;
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

  function processLabel(status) {
    if (!status) return "Stopped";
    if (status.state === "stopped" && status.exitCode !== null && status.exitCode !== undefined && status.exitCode !== 0) {
      return `Exited ${status.exitCode}`;
    }
    return status.state.charAt(0).toUpperCase() + status.state.slice(1);
  }

  function renderProcess(kind, status) {
    const label = processLabel(status);
    const visualState = status?.state === "stopped" && status?.exitCode ? "error" : (status?.state || "stopped");
    for (const id of [`${kind}-status-pill`, `${kind}-page-status`]) {
      const pill = byId(id);
      pill.textContent = label;
      pill.className = `status-pill ${visualState}`;
    }
    const card = byId(`${kind}-service-card`);
    card.classList.remove("running", "paused");
    if (["running", "paused"].includes(status?.state)) card.classList.add(status.state);
    const copy = byId(`${kind}-status-copy`);
    if (status?.state === "running") copy.textContent = `Running as process ${status.pid}. Output is streaming to Live logs.`;
    else if (status?.state === "paused") copy.textContent = `Paused with its child processes preserved. Resume to continue exactly where it stopped.`;
    else if (status?.exitCode !== null && status?.exitCode !== undefined) copy.textContent = `Last run exited with status ${status.exitCode}. Review Live logs for details.`;
    else copy.textContent = kind === "issue" ? "Ready when your repository and AI providers are configured." : "Runs the repository’s frozen backend, Fire TV E2E, and TV UAT suites.";
  }

  function renderControls() {
    document.querySelectorAll("[data-action]").forEach((button) => {
      const action = button.dataset.action;
      const isIssue = action.endsWith("issue");
      const process = isIssue ? "issue" : "uat";
      const processState = state.status?.[process]?.state || "stopped";
      const busy = state.busy.has(action);
      if (action.startsWith("start-") || action.startsWith("run-")) {
        button.disabled = busy || processState !== "stopped" || (!isIssue && (!state.status?.uatAvailable || !state.config?.uat_enabled));
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

  function renderRepository(repo) {
    const pill = byId("repo-valid-pill");
    pill.textContent = repo?.valid ? "Git checkout" : "Needs attention";
    pill.className = `status-pill ${repo?.valid ? "running" : "error"}`;
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
    state.tools.forEach((tool) => {
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
      if (["claude", "codex"].includes(tool.id) && !tool.installed && tool.installable) {
        actions.appendChild(button("Install", "primary-button", () => installProvider(tool.id)));
      }
      if (["claude", "codex", "gh"].includes(tool.id) && tool.installed && tool.authenticated === false) {
        actions.appendChild(button("Sign in", "secondary-button", () => signIn(tool.id)));
      }
      if (["node", "npm"].includes(tool.id) && !tool.installed) {
        actions.appendChild(button("Install Node.js", "secondary-button", () => openUrl("https://nodejs.org/en/download")));
      }
      card.append(head, title, version, actions);
      grid.appendChild(card);
    });
    for (const provider of ["claude", "codex"]) {
      const tool = state.tools.find((entry) => entry.id === provider);
      document.querySelector(`[data-tool-mini="${provider}"]`).textContent = tool?.status || "Not detected";
    }
  }

  async function refreshTools() {
    await withBusy("tools", async () => {
      state.tools = await invoke("detect_tools");
      renderTools();
      renderReadiness();
    });
  }

  async function installProvider(provider) {
    await withBusy(`install-${provider}`, async () => {
      await invoke("install_ai_cli", { provider });
      showToast(`${provider === "claude" ? "Claude Code" : "Codex CLI"} installation started. Watch Live logs.`, "success");
      navigate("logs");
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
    const ais = state.tools.filter((tool) => ["claude", "codex"].includes(tool.id));
    const checks = [
      ["Repository", state.status.repository.valid && !state.status.configError, state.status.repository.valid ? "Configured" : "Choose checkout"],
      ["GitHub CLI", Boolean(gh && toolReady(gh)), gh?.status || "Not detected"],
      ["AI provider", ais.some(toolReady), ais.some(toolReady) ? "Signed in" : "Sign in required"],
      ["Bot identities", !state.config.require_bot_auth || state.status.botConfigExists, state.config.require_bot_auth ? (state.status.botConfigExists ? "Configured" : "Setup needed") : "Optional"],
      ["Worker runtime", state.status.workerAvailable, state.status.workerAvailable ? "Available" : "Unavailable"],
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
    renderProcess("uat", state.status.uat);
    renderRepository(state.status.repository);
    const warning = byId("config-warning");
    warning.textContent = state.status.configError;
    warning.classList.toggle("hidden", !state.status.configError);
    byId("bot-config-pill").textContent = state.status.botConfigExists ? "Configuration found" : "Not configured";
    byId("bot-config-pill").className = `status-pill ${state.status.botConfigExists ? "running" : "stopped"}`;
    renderControls();
    renderReadiness();
  }

  async function refreshStatus() {
    try {
      state.status = await invoke("get_automation_status");
      renderStatus();
    } catch (error) {
      showToast(errorText(error), "error");
    }
  }

  function formatLog(event) {
    const time = new Date(event.timestamp * 1000).toLocaleTimeString([], { hour12: false });
    return `[${time}] [${event.source}/${event.stream}] ${event.line}`;
  }

  function renderLogs() {
    const filtered = state.logFilter === "all" ? state.logs : state.logs.filter((line) => line.includes(`[${state.logFilter}/`));
    const text = filtered.length ? filtered.join("\n") : "Waiting for output…";
    const full = byId("full-log");
    const stayAtBottom = full.scrollTop + full.clientHeight >= full.scrollHeight - 28;
    full.textContent = text;
    if (stayAtBottom) full.scrollTop = full.scrollHeight;
    byId("overview-log").textContent = state.logs.length ? state.logs.slice(-8).join("\n") : "No automation output yet.";
    byId("log-count").textContent = String(Math.min(state.logs.length, 999));
  }

  async function inspectRepository(path, applyRemote = false) {
    const inspection = await invoke("inspect_repository", { path });
    renderRepository(inspection);
    if (applyRemote && inspection.githubRepository) {
      document.querySelector('[data-config="github_repository"]').value = inspection.githubRepository;
    }
    return inspection;
  }

  async function chooseRepository() {
    await withBusy("browse", async () => {
      const inspection = await invoke("choose_repository");
      if (!inspection) return;
      document.querySelector('[data-config="repo_dir"]').value = inspection.path;
      if (inspection.githubRepository) document.querySelector('[data-config="github_repository"]').value = inspection.githubRepository;
      const runDir = document.querySelector('[data-config="run_dir"]');
      if (!runDir.value) runDir.value = `${inspection.path}/.run`;
      renderRepository(inspection);
      setDirty();
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
      await invoke("launch_bot_setup");
      showToast("GitHub App setup started. Follow the browser assistant.", "success");
      navigate("logs");
    });
  }

  async function verifyBots() {
    await withBusy("verify-bots", async () => {
      await saveBeforeAction();
      const results = await invoke("verify_github_bots");
      const container = byId("bot-results");
      container.replaceChildren();
      results.forEach((result) => {
        const row = document.createElement("div");
        row.className = `verification-result ${result.valid ? "valid" : "invalid"}`;
        row.textContent = `${result.provider === "claude" ? "Claude Bot" : "Codex Bot"}: ${result.message}`;
        container.appendChild(row);
      });
      await refreshStatus();
    });
  }

  function bindEvents() {
    document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.viewTarget)));
    document.querySelectorAll("[data-view-jump]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.viewJump)));
    document.querySelectorAll("#schedule-segmented button").forEach((button) => button.addEventListener("click", () => selectSchedule(button.dataset.schedule)));
    document.querySelectorAll("[data-config], #days-field input").forEach((input) => {
      input.addEventListener("input", () => { setDirty(); renderSummaries(); });
      input.addEventListener("change", () => { setDirty(); renderSummaries(); });
    });
    document.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => runAction(button.dataset.action)));
    document.querySelectorAll("[data-log-filter]").forEach((button) => button.addEventListener("click", () => {
      state.logFilter = button.dataset.logFilter;
      document.querySelectorAll("[data-log-filter]").forEach((candidate) => candidate.classList.toggle("active", candidate === button));
      renderLogs();
    }));
    byId("save-button").addEventListener("click", () => withBusy("save", () => saveConfig()));
    byId("hide-button").addEventListener("click", () => invoke("hide_to_tray").catch((error) => showToast(errorText(error), "error")));
    byId("refresh-tools").addEventListener("click", refreshTools);
    byId("browse-repo").addEventListener("click", chooseRepository);
    byId("save-password").addEventListener("click", savePassword);
    byId("setup-bots").addEventListener("click", setupBots);
    byId("verify-bots").addEventListener("click", verifyBots);
    byId("open-log-folder").addEventListener("click", () => invoke("open_automation_folder").catch((error) => showToast(errorText(error), "error")));
    byId("clear-log").addEventListener("click", () => { state.logs = []; renderLogs(); });
  }

  async function initialize() {
    populateHours();
    bindEvents();
    try {
      state.config = await invoke("get_config");
      bindConfig(state.config);
      setDirty(false);
      state.logs = await invoke("get_recent_logs");
      renderLogs();
      await listen("automation-log", (event) => {
        state.logs.push(formatLog(event.payload));
        if (state.logs.length > 1000) state.logs.splice(0, state.logs.length - 1000);
        renderLogs();
      });
      await Promise.all([refreshStatus(), refreshTools()]);
      if (state.config.repo_dir) await inspectRepository(state.config.repo_dir);
      window.setInterval(refreshStatus, 2000);
      window.setInterval(() => {
        if (state.status?.task?.state === "stopped" && state.busy.size === 0) refreshTools();
      }, 30000);
    } catch (error) {
      showToast(`Could not initialize the application: ${errorText(error)}`, "error");
    }
  }

  window.addEventListener("DOMContentLoaded", initialize);
})();
