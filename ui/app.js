(() => {
  "use strict";

  const { invoke } = window.__TAURI__.core;
  const { listen } = window.__TAURI__.event;

  const state = {
    config: null,
    status: null,
    tools: [],
    logs: [],
    workerLogs: [],
    logFilter: "all",
    logSearch: "",
    activityFilter: "all",
    activityPaused: false,
    activitySnapshot: null,
    dirty: false,
    busy: new Set(),
    refreshing: { status: false, tools: false, botReadiness: false, promotions: false, branchPushAccess: false },
    activeRepoId: "",
    promotions: [],
    executionHistory: null,
    executionHistorySearch: "",
    executionHistorySort: "recent",
    executionHistoryOffset: 0,
    executionHistoryRequest: 0,
    executionHistorySearchTimer: null,
    promptGrades: null,
    promptGradesSearch: "",
    promptGradesGrade: "",
    // Provider key of the AI platform that graded/routed an issue. A different
    // axis from the search box, which matches the platform that worked it.
    promptGradesRouter: "",
    // Exact model used by the selected grading platform. Kept separate so a
    // model click can narrow the grades without flattening platform identity.
    promptGradesRouterModel: "",
    promptGradesOffset: 0,
    promptGradesRequest: 0,
    promptGradesSearchTimer: null,
    feedbackTab: "grades",
    repositoryTab: "source",
    // Feedback owns this filter. It never follows or changes activeRepoId.
    feedbackRepoFilter: [],
    feedbackRepoSavePromise: Promise.resolve(),
    // repoId -> array of BotReadiness from check_repo_bot_readiness.
    botReadiness: {},
    // repoId -> last BranchPushAccess from branch_push_access.
    branchPushAccess: {},
    botReadinessPoll: null,
    pendingUpdate: null,
  };

  const pageTitles = {
    overview: "Automation overview",
    repository: "Repository",
    ai: "AI Configuration",
    feedback: "Feedback",
    debug: "Info & Debug",
    help: "Help",
  };
  const symbols = { git: "G", gh: "GH", python: "Py", node: "N", npm: "npm", claude: "C", codex: "X", grok: "Gk" };

  // Everything provider-specific the UI needs. Order here is the display /
  // rotation order and must match config.rs KNOWN_PROVIDERS.
  const PROVIDER_META = {
    claude: {
      label: "Claude", cli: "Claude Code", help: "provider-claude",
      docs: "https://docs.anthropic.com/en/docs/claude-code/overview",
    },
    codex: {
      label: "Codex", cli: "Codex CLI", help: "provider-codex",
      docs: "https://developers.openai.com/codex/cli/",
    },
    grok: {
      label: "Grok", cli: "Grok Build", help: "provider-grok",
      docs: "https://docs.x.ai/build/overview",
    },
  };
  const PROVIDER_ORDER = Object.keys(PROVIDER_META);
  const PREFERRED_PROVIDER_AUTO = "auto";
  const providerLabel = (id) => id === "xai" ? "xAI" : (PROVIDER_META[id]?.label || id);

  // Click-to-open help. `html` is a trusted local constant (no user input),
  // so innerHTML is safe here. Links open through open_external_url (HTTPS only).
  const HELP_TOPICS = {
    "provider-claude": {
      title: "Claude Code",
      html: "<p>Claude can write code for an issue or review work from another provider.</p><ul><li><strong>Switch</strong> — includes or removes Claude from new work.</li><li><strong>Model</strong> — choose the Claude model that should handle work.</li><li><strong>Effort</strong> — choose how hard Claude should think before acting. The choices update for the selected model.</li><li><strong>Install / Sign in</strong> — prepares Claude on this Mac.</li></ul>",
      links: [{ label: "Claude Code docs", url: "https://docs.anthropic.com/en/docs/claude-code/overview" }],
    },
    "provider-codex": {
      title: "Codex CLI",
      html: "<p>Codex can write code for an issue or review work from another provider.</p><ul><li><strong>Switch</strong> — includes or removes Codex from new work.</li><li><strong>Model</strong> — choose the Codex model that should handle work.</li><li><strong>Effort</strong> — choose how hard Codex should think before acting. The choices update for the selected model.</li><li><strong>Install / Sign in</strong> — prepares Codex on this Mac.</li></ul>",
      links: [{ label: "Codex CLI docs", url: "https://developers.openai.com/codex/cli/" }],
    },
    "provider-grok": {
      title: "Grok Build",
      html: "<p>Grok can write code for an issue or review work from another provider.</p><ul><li><strong>Switch</strong> — includes or removes Grok from new work.</li><li><strong>Model</strong> — choose the Grok model that should handle work.</li><li><strong>Effort</strong> — choose how hard Grok should think before acting. The choices update for the selected model.</li><li><strong>Install / Sign in</strong> — prepares Grok on this Mac.</li></ul>",
      links: [{ label: "Grok Build docs", url: "https://docs.x.ai/build/overview" }],
    },
    "dynamic-model-routing": {
      title: "Dynamic Model Routing",
      html: "<p><strong>OFF</strong> keeps today’s behavior: you choose the worker model and reasoning effort on each provider card.</p><p><strong>ON</strong> disables those worker selectors and shows <strong>Router model</strong> and <strong>Router effort</strong>. Before an issue is implemented, that router grades the original prompt, then picks which enabled AI tool runs it — weighing each tool’s remaining usage and a built-in summary of what it tends to be good at — and chooses the worker model and reasoning effort itself, from the full catalog of models that tool offers. The saved complexity tiers stay as your reference, and are still what runs if the router ever names a model that does not exist. The original issue text is not rewritten.</p><p>The chosen tool, why it was chosen, the grade, and the routing confidence are posted on the issue when work starts and stored with the run in AI history. A rework goes to a different tool than the previous pass unless the router is clearly confident the same one is the better choice.</p><p><strong>Optimize routing for cost</strong> (off by default) decides how the router weighs price. On, it starts from the least expensive model that can actually do the work. A frontier model is a last resort, used only when complexity is 9 or 10. High risk may justify a capable mid-tier model. Off, it picks whichever model best fits the task and ignores cost — which is not the same as always picking the most capable model.</p><p><strong>Use models requiring usage credits</strong> (off by default) controls whether a model that draws on a separate usage-credit balance, rather than the account's normal plan allowance, can ever be picked as a worker, router, or tier model — manually or by SWARM's own catalog repair. Leave it off unless you know usage credits are provisioned for the account running this app.</p>",
      links: [],
    },
    "provider-include-exclude": {
      title: "Enabled AI tools",
      html: "<p>Each card represents an AI provider. Turn its switch on to allow it to receive new work, and set the shared minimum quota reserve.</p><p><strong>Dynamic Model Routing</strong> grades the original issue, picks which enabled provider handles it, and chooses that provider’s worker model and reasoning effort from the models it offers. The card’s router model performs the grading; to tell the providers apart it uses each one’s remaining usage and a built-in summary of what it tends to be good at. Turn routing off to choose the provider order and the worker model and effort yourself.</p><p><strong>No preference</strong> means you do not care who handles a new issue first. The enabled provider with the most usage left is selected, so one account is not used up before the others. If remaining usage is tied, the order is Claude, then Codex, then Grok.</p><p>Choosing a provider instead makes that provider the tie-breaker when remaining usage is equal. At least one provider must remain enabled. Turning one off does not erase work it already completed.</p>",
      links: [],
    },
    "software-update": {
      title: "Software updates",
      html: "<p>New versions are published automatically after each change passes tests. Updates install in place and restart the app — your configuration is untouched.</p><ul><li><strong>Notify me</strong> — a banner appears when a new version is available; you choose when to install.</li><li><strong>Automatically</strong> — a detected update waits until the issue worker is idle on its own (never stopped just to make room), then downloads, installs, and restarts.</li></ul><p><strong>Check now</strong> works in either mode and also lists the 3 most recent release builds and 3 most recent beta builds so you can pick a specific version — anything older than what's installed is shown for context but can't be selected.</p>",
      links: [],
    },
    "bot-identities": {
      title: "GitHub App bot identities",
      html: "<p>Each AI provider gets its own GitHub identity, making it clear which one wrote, reviewed, or merged work. This also lets one provider approve another's pull request, which GitHub blocks when the author and approver are the same account.</p><p>The checklist shows, per provider, whether its bot app is <strong>created</strong> and <strong>installed on this repository's GitHub account</strong>. A provider that is not ready has a button that opens the exact GitHub page to fix it.</p><ul><li><strong>Set up GitHub Apps</strong> — creates any missing bot apps (a one-time browser approval).</li><li><strong>Re-check</strong> — asks GitHub again after you finish an install.</li><li><strong>Verify sign-in</strong> — confirms each app can mint a working token.</li></ul><p>On every install screen choose <strong>All repositories</strong>. Then adding another repo in the same GitHub account needs no further setup.</p>",
      links: [{ label: "About GitHub Apps", url: "https://docs.github.com/apps" }],
    },
    "quota-threshold": {
      title: "Minimum quota remaining",
      html: "<p>This keeps a small part of an AI provider’s usage allowance in reserve. If the provider falls below the chosen percentage, the app waits or tries another enabled provider.</p>",
      links: [],
    },
    "delivery-mode": {
      title: "Protected AI branch flow",
      html: "<p>This box keeps AI work separate from the branch people use.</p><ul><li><strong>Human-owned branch</strong> — the protected branch, usually <code>main</code>. AI never commits to it.</li><li><strong>AI integration branch</strong> — collects finished AI work, usually <code>ai-main</code>.</li><li><strong>Git remote</strong> — the saved connection to GitHub, usually <code>origin</code>.</li><li><strong>Issue branch prefix</strong> — the first part of AI branch names, usually <code>ai</code>.</li></ul>",
      links: [],
    },
    "auto-approve-merge": {
      title: "Approve & merge automatically",
      html: "<p><strong>Automatically approve and merge issue PRs</strong> asks another AI provider’s bot to approve the pull request, then combines it into one tidy commit on the AI integration branch and removes the issue branch.</p><p>The GitHub issue does not need to be closed first. Merge conflicts remain open for attention.</p><p><strong>Automatically merge <code>ai-main</code> into <code>main</code></strong> is off by default. When on, the worker also opens (or reuses) the <code>ai-main</code> → <code>main</code> pull request after issue PRs land, has another provider’s bot approve it, and merges it — so everything the app has finished lands on <code>main</code> immediately. It needs issue PR merging on, and a promotion with conflicts stays open for a person to resolve. Leave it off to keep <code>main</code> a human decision.</p><p><strong>Allow bots to merge</strong> updates the human-owned branch’s existing push allow list so the worker’s GitHub Apps can perform that merge. People already on the list stay on it. If the branch does not restrict who can push, the button stays off.</p>",
      links: [],
    },
    "ci-monitoring": {
      title: "Monitor GitHub Actions",
      html: "<p><strong>Monitor GitHub Actions</strong> is off by default. When on, each worker run first checks the newest GitHub Actions run of every workflow on the AI integration branch (<code>ai-main</code>).</p><p>If a pipeline is failing and nothing tracks it yet, the worker files one issue — labelled <code>bug</code> and <code>ci-failure</code>, assigned to the configured assignee, with the failing runs and the tail of their logs — and works it in that same run, like any other issue.</p><p>That issue is handled once: the worker does not also pick it up from the regular queue in that run, and it files nothing new while a CI-failure issue for the branch is open or was already filed for the same commit. Runs still in progress and cancelled runs are ignored. If GitHub cannot be read, the worker logs it and carries on with the normal queue.</p>",
      links: [],
    },
    "parallel-repo-workers": {
      title: "One worker per repository",
      html: "<p>This chooses how the issue worker handles more than one repository.</p><ul><li><strong>Off (default)</strong> — a single worker visits each repository in turn and picks up one issue at a time.</li><li><strong>On</strong> — every repository with a ready issue gets its own independent worker. When one repository finishes an issue, it can pick up its next issue immediately without waiting for workers in other repositories.</li></ul><p>Turning this on works through a backlog faster, <strong>but it uses AI credits faster</strong> because several providers run at once. Each repository still keeps its own branch, state, and one-issue-at-a-time limit.</p>",
      links: [],
    },
    "schedule-modes": {
      title: "Pickup schedule",
      html: "<ul><li><strong>Continuous</strong> — checks repeatedly and handles ready issues one after another.</li><li><strong>Daily</strong> — checks once each day.</li><li><strong>Weekdays</strong> — checks Monday through Friday.</li><li><strong>Custom</strong> — checks on the days you choose.</li><li><strong>Manual</strong> — checks only when you select <em>Run now</em>.</li></ul><p><strong>Run now</strong> stays available whatever the schedule says, including while the worker is already running: it checks every enabled repository straight away, then the timer for the next check restarts from the end of that check.</p>",
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
    "promotion-queue": {
      title: "Repositories ready to promote",
      html: "<p>This list appears on the Overview page whenever one or more repositories have finished AI work waiting to reach the human-owned branch. It refreshes automatically while the Overview is visible.</p><ul><li>A repository shows up when its AI integration branch (usually <code>ai-main</code>) is <strong>ahead</strong> of the human-owned branch.</li><li><strong>Create PR</strong> creates or opens the promotion pull request in your browser.</li><li><strong>Merge to Main</strong> first merges the latest human-owned branch into the AI branch, favoring human-owned changes if the same lines conflict. It then creates the PR, approves it with a configured bot, and merges it.</li></ul><p>Promotion reconciles and merges in its own isolated clone, so it is safe to run while the issue worker keeps working — it never touches the worker's checkout.</p>",
      links: [],
    },
    "data-location": {
      title: "Where your data lives",
      html: "<p>Settings are stored in a private local file. GitHub and AI sign-in details stay with their own tools and are not copied into logs.</p>",
      links: [],
    },
    "async-refresh": {
      title: "Independent refresh",
      html: "<p>Tool detection and logs refresh independently so slow checks do not block the rest of the interface.</p>",
      links: [],
    },
    "work-policy": {
      title: "Issue instructions",
      html: "<p>These switches control issue implementation and verification.</p><ul><li><strong>Require issue tests</strong> — asks for UAT and integration test coverage with the change.</li><li><strong>Adversarial UAT</strong> — replaces the same-session instruction with independent tests and up to six fix/re-test rounds. A deadlock publishes the PR for human review with automatic merging disabled.</li><li><strong>Update Claude assets</strong> — asks the AI to update any Claude skill, agent, rule, workflow, or CLAUDE.md file in the repository that the issue makes relevant.</li><li><strong>Allow environment-only summary</strong> — lets the AI explain a non-code problem without changing files.</li></ul><p>These issue policies start off and apply only to this repository.</p>",
      links: [],
    },
    "execution-history": {
      title: "Execution history",
      html: "<p>Every AI issue execution across all repositories by default, newest first. The repository chips above the tabs independently filter all three reports; the repository dropdown in the header does not affect Feedback. The list loads ten at a time from the local database. Each row shows its repository, AI tool, model, effort and UAT round count. Sort by UAT rounds across all pages. The aggregate reports average fix/re-test rounds and clean-first-pass/cap-hit rates over the filtered repositories.</p><p>This view only reads what <strong>Store AI execution history</strong> already saved locally (see AI Configuration). It never changes issue processing, and nothing is uploaded unless <strong>Allow prompt feedback upload</strong> is also on and an uploader is configured.</p><p><strong>Import from GitHub</strong> scans every repository checked in the Feedback filter and adds a placeholder \"Imported\" entry for any issue with no execution history yet. It reports success or failure for each repository and never overwrites or duplicates a real execution.</p>",
      links: [],
    },
    "prompt-grades": {
      title: "Prompt grades",
      html: "<p>When <strong>Dynamic Model Routing</strong> is on, the router grades the original issue before any AI works on it — from <strong>A+</strong> down to <strong>F</strong> — and explains what the issue does well, what is missing, and what would raise the grade. This panel lists grades across all repositories by default, newest first; the repository chips above the tabs filter the list and its server-computed summary.</p><p>Search matches issue number, title, provider, model, branch, or status, and Previous and Next fetch another page. Click a bar in the chart, such as <strong>B-</strong>, to show only prompts with that grade. Click the same bar again to clear it. The chart keeps counting every grade in the current search, so another bar can be chosen without clearing the search.</p><p>The <strong>average grade</strong> uses a 4.0 scale across the filtered runs. Each row identifies its repository and shows <strong>Graded by</strong> — the AI platform and model that ran the pre-flight grading and routing pass.</p><p>The <strong>Router activity</strong> tab breaks the same filtered grades down by which platform graded them. Runs without routing, and runs where the router was unavailable, are not graded and do not appear here.</p>",
      links: [],
    },
    "router-activity": {
      title: "Router activity",
      html: "<p>When <strong>Dynamic Model Routing</strong> is on, one AI platform runs a pre-flight pass over a new issue: it grades the issue, scores its complexity, and picks which AI tool actually does the work. This panel keeps one card per grading platform, then breaks that platform down by the models it used and the tools it picked.</p><p>Read the model section as grading activity and the picked-platform section as router bias. For example, if Claude graded twelve issues using Opus eight times, the Opus model shows <strong>67%</strong>. If Claude handed nine of those issues to Claude, the picked-platform bar shows <strong>75%</strong>.</p><p>Select a platform to filter every prompt grade by who <strong>graded</strong> it. Select a grading model to narrow that platform further; selecting the same model again returns to the whole platform. The matrix always retains every platform and model in the current search so another can be chosen directly.</p><p>Historical platform or model details that were never recorded remain visible as <strong>Not recorded</strong> or <strong>Model not recorded</strong> and cannot be selected.</p>",
      links: [],
    },
    "provider-bins": {
      title: "AI program locations",
      html: "<p>The app normally finds Claude, Codex, and Grok automatically. Enter a full program path only when an installed provider is not detected or when you want to use a specific copy.</p>",
      links: [],
    },
  };
  const HELP_CONCEPTS = [
    ["Including / excluding a provider", "provider-include-exclude"],
    ["GitHub App bot identities", "bot-identities"],
    ["Protected branch flow", "delivery-mode"],
    ["Repositories ready to promote", "promotion-queue"],
    ["Pull request automation", "auto-approve-merge"],
    ["Monitor GitHub Actions", "ci-monitoring"],
    ["One worker per repository", "parallel-repo-workers"],
    ["Minimum quota remaining", "quota-threshold"],
    ["Prompt grades", "prompt-grades"],
    ["Router activity", "router-activity"],
    ["Execution history", "execution-history"],
    ["Where your data lives", "data-location"],
    ["Independent refresh", "async-refresh"],
  ];

  function byId(id) {
    return document.getElementById(id);
  }

  function showToast(message, kind = "") {
    const toast = document.createElement("div");
    toast.className = `toast ${kind}`.trim();
    if (kind === "progress") {
      // Persistent "something is happening" toast — the caller removes it.
      const spinner = document.createElement("span");
      spinner.className = "toast-spinner";
      spinner.setAttribute("aria-hidden", "true");
      const label = document.createElement("span");
      label.textContent = String(message || "Working…");
      toast.append(spinner, label);
    } else {
      toast.textContent = String(message || "Unknown error");
    }
    byId("toast-stack").appendChild(toast);
    if (kind !== "progress") window.setTimeout(() => toast.remove(), 5000);
    return toast;
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
    if (view === "debug") void refreshTools({ quiet: true });
    if (view === "repository") {
      void refreshPromotions({ quiet: true });
      void refreshBotReadiness({ quiet: true });
      void refreshBranchPushAccess({ quiet: true });
    }
    if (view === "feedback") {
      void refreshPromptGrades({ quiet: true });
      void refreshExecutionHistory({ quiet: true });
    }
  }

  // A checkbox carrying data-checked-value/data-unchecked-value stores a
  // two-value string instead of a boolean (routing_optimization). The mapping
  // lives in dynamic-routing-ui.js so it can be unit tested; this keeps the
  // generic [data-config] loops working even if that script has not loaded.
  function valuedToggle() {
    return (
      window.SwarmDynamicRouting || {
        valuedToggleChecked: (value, checkedValue) => String(value ?? "") === String(checkedValue ?? ""),
        valuedToggleValue: (checked, checkedValue, uncheckedValue) =>
          checked ? String(checkedValue ?? "") : String(uncheckedValue ?? ""),
      }
    );
  }

  function bindConfig(config) {
    renderProviderCards(config);
    providerList(config).forEach((provider) => {
      const input = document.querySelector(`[data-provider-bin="${provider.id}"]`);
      if (input) input.value = provider.bin;
    });
    ensureRepository(config);
    restoreFeedbackRepoFilter(config);
    renderRepositorySelector();
    renderFeedbackRepoFilter();
    document.querySelectorAll("[data-config]").forEach((input) => {
      const key = input.dataset.config;
      const value = config[key];
      if (input.type === "checkbox" && input.dataset.checkedValue !== undefined) {
        input.checked = valuedToggle().valuedToggleChecked(value, input.dataset.checkedValue);
      } else if (input.type === "checkbox") input.checked = Boolean(value);
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
    syncDynamicRoutingChrome();
  }

  function syncDynamicRoutingChrome() {
    const toggle = byId("dynamic-model-routing");
    const enabled = Boolean(toggle && toggle.checked);
    const label = byId("dynamic-routing-state");
    if (label && window.SwarmDynamicRouting) {
      label.textContent = window.SwarmDynamicRouting.routingControlState(enabled).statusLabel;
    }
    if (!window.SwarmDynamicRouting) return;
    document.querySelectorAll("#provider-cards .provider-card").forEach((card) => {
      window.SwarmDynamicRouting.applyRoutingControlState(card, enabled);
    });
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
      auto_approve: true,
      auto_merge: true,
      auto_promote: false,
      monitor_actions: false,
      require_issue_tests: false,
      adversarial_uat_enabled: false,
      update_claude_assets_enabled: false,
      allow_environment_only_summary: false,
      repo_dir: "",
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

  function feedbackRepositories() {
    return (state.config?.repositories || []).filter((repo) => repo.id && repo.github_repository);
  }

  function restoreFeedbackRepoFilter(config = state.config) {
    const repositories = (config?.repositories || []).filter((repo) => repo.id && repo.github_repository);
    const available = new Set(repositories.map((repo) => repo.id));
    const saved = Array.isArray(config?.feedback_repo_filter)
      ? [...new Set(config.feedback_repo_filter.filter((id) => available.has(id)))]
      : [];
    state.feedbackRepoFilter = saved.length ? saved : repositories.map((repo) => repo.id);
  }

  function feedbackRepoIdsForQuery() {
    const allIds = feedbackRepositories().map((repo) => repo.id);
    const selected = state.feedbackRepoFilter.filter((id) => allIds.includes(id));
    return selected.length === allIds.length ? [] : selected;
  }

  function feedbackRepoFilterSignature() {
    return [...state.feedbackRepoFilter].sort().join("\n");
  }

  function renderFeedbackRepoFilter() {
    const box = byId("feedback-repo-chips");
    if (!box) return;
    box.replaceChildren();
    const repositories = feedbackRepositories();
    const selected = new Set(state.feedbackRepoFilter);
    const allSelected = repositories.length > 0 && repositories.every((repo) => selected.has(repo.id));
    const chip = (label, checked, value, disabled = false) => {
      const wrapper = document.createElement("label");
      wrapper.className = "feedback-repo-chip";
      wrapper.title = value ? label : "Include every configured repository";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = checked;
      input.disabled = disabled;
      input.dataset.feedbackRepo = value;
      const text = document.createElement("span");
      text.textContent = label;
      wrapper.append(input, text);
      return wrapper;
    };
    box.appendChild(chip("All", allSelected, ""));
    repositories.forEach((repo) => {
      const checked = selected.has(repo.id);
      box.appendChild(chip(repo.github_repository, checked, repo.id, checked && selected.size === 1));
    });
  }

  function persistFeedbackRepoFilter() {
    const persisted = feedbackRepoIdsForQuery();
    state.feedbackRepoSavePromise = state.feedbackRepoSavePromise
      .catch(() => {})
      .then(async () => {
        const saved = await invoke("save_feedback_repo_filter", { repoIds: persisted });
        state.config.feedback_repo_filter = saved.feedback_repo_filter || [];
      });
    return state.feedbackRepoSavePromise;
  }

  function selectFeedbackRepositories(repoIds) {
    const allIds = feedbackRepositories().map((repo) => repo.id);
    const selected = [...new Set(repoIds.filter((id) => allIds.includes(id)))];
    state.feedbackRepoFilter = selected.length ? selected : allIds;
    state.executionHistoryOffset = 0;
    state.promptGradesOffset = 0;
    renderFeedbackRepoFilter();
    void persistFeedbackRepoFilter().catch((error) => showToast(errorText(error), "error"));
    void refreshPromptGrades({ quiet: true });
    void refreshExecutionHistory({ quiet: true });
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
      const bots = state.botReadiness[repo.id];
      if (repo.enabled && bots && bots.length && !bots.every((row) => row.ready)) {
        option.textContent += " · ⚠ bots";
      }
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
      else input.value = input.dataset.repoConfig === "github_repository" && value ? githubUrl(value) : (value ?? "");
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
      else if (input.type === "number") repo[key] = Number(input.value);
      else repo[key] = key === "github_repository" ? normalizeRepoRef(input.value) : input.value.trim();
    });
    // Approval and squash-merging are intentionally one repository setting.
    repo.auto_merge = repo.auto_approve;
  }

  function modelSpecs(providerId) {
    return state.tools.find((tool) => tool.id === providerId)?.models || [];
  }

  function defaultModel(providerId) {
    return modelSpecs(providerId)[0]?.value || "";
  }

  function selectedModelSpec(providerId, model) {
    const models = modelSpecs(providerId);
    return models.find((entry) => entry.value === model) || (!model ? models[0] : null) || null;
  }

  function effortOptions(providerId, model) {
    const spec = selectedModelSpec(providerId, model);
    // A saved model the CLI no longer lists still needs a usable effort list:
    // offer every level the provider's current models support.
    const efforts = spec ? spec.efforts : modelSpecs(providerId).flatMap((entry) => entry.efforts);
    return [...new Set(efforts)];
  }

  function defaultEffort(providerId, model) {
    const spec = selectedModelSpec(providerId, model);
    const efforts = effortOptions(providerId, model);
    return spec?.defaultEffort || (efforts.includes("high") ? "high" : efforts[0] || "high");
  }

  function populateEffortSelect(select, providerId, model, currentEffort) {
    const efforts = effortOptions(providerId, model);
    if (!efforts.length) efforts.push(currentEffort || defaultEffort(providerId, model));
    select.replaceChildren();
    efforts.forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      select.appendChild(option);
    });
    select.value = efforts.includes(currentEffort) ? currentEffort : defaultEffort(providerId, model);
  }

  function providerList(config) {
    // Guarantee one card per known provider, in canonical order, even if a
    // hand-edited config dropped one.
    const stored = new Map((config.providers || []).map((p) => [p.id, p]));
    return PROVIDER_ORDER.map((id) => {
      const entry = stored.get(id) || {};
      const routerDefault = window.SwarmDynamicRouting
        ? window.SwarmDynamicRouting.defaultRouter(id)
        : { model: "", effort: "low" };
      const routerModel = entry.router_model || routerDefault.model;
      return {
        id,
        enabled: entry.enabled !== false,
        model: entry.model || defaultModel(id),
        effort: entry.effort || defaultEffort(id, entry.model || defaultModel(id)),
        router_model: routerModel,
        router_effort: entry.router_effort || routerDefault.effort,
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
      modelLabel.className = "worker-model-label";
      modelLabel.append("Model ");
      const modelReq = document.createElement("span");
      modelReq.className = "req";
      modelReq.title = "Required while this provider is in the flow";
      modelReq.textContent = "*";
      modelReq.hidden = !provider.enabled;
      modelLabel.appendChild(modelReq);
      const knownModels = modelSpecs(provider.id);
      const modelInput = document.createElement("select");
      modelInput.className = "provider-model";
      knownModels.forEach((entry) => {
        const option = document.createElement("option");
        option.value = entry.value;
        option.textContent = entry.label;
        modelInput.appendChild(option);
      });
      if (provider.model && !knownModels.some((entry) => entry.value === provider.model)) {
        const option = document.createElement("option");
        option.value = provider.model;
        option.textContent = `${provider.model} (saved)`;
        modelInput.appendChild(option);
      }
      if (tool && !tool.modelsDetected) {
        modelInput.title = tool.installed
          ? "The CLI did not report a model list, so built-in defaults are shown."
          : "Install the CLI to load its current model list; built-in defaults are shown.";
      }
      modelInput.value = provider.model || defaultModel(provider.id);
      modelLabel.appendChild(modelInput);
      const effortLabel = document.createElement("label");
      effortLabel.className = "worker-effort-label";
      effortLabel.append("Reasoning/Effort ");
      const effortInput = document.createElement("select");
      effortInput.className = "provider-effort";
      populateEffortSelect(effortInput, provider.id, modelInput.value, provider.effort);
      modelInput.addEventListener("change", () => {
        populateEffortSelect(effortInput, provider.id, modelInput.value, effortInput.value);
        setDirty();
      });
      effortInput.addEventListener("change", setDirty);
      effortLabel.appendChild(effortInput);
      const routingNote = document.createElement("p");
      routingNote.className = "dynamic-routing-note fine-print";
      routingNote.textContent = "AI tool, worker model, and reasoning are chosen for each issue.";
      const routerModelLabel = document.createElement("label");
      routerModelLabel.className = "router-model-label";
      routerModelLabel.append("Router model ");
      const routerModelInput = document.createElement("select");
      routerModelInput.className = "provider-router-model";
      knownModels.forEach((entry) => {
        const option = document.createElement("option");
        option.value = entry.value;
        option.textContent = entry.label;
        routerModelInput.appendChild(option);
      });
      if (provider.router_model && !knownModels.some((entry) => entry.value === provider.router_model)) {
        const option = document.createElement("option");
        option.value = provider.router_model;
        option.textContent = `${provider.router_model} (saved)`;
        routerModelInput.appendChild(option);
      }
      routerModelInput.value = provider.router_model || "";
      routerModelLabel.appendChild(routerModelInput);
      const routerEffortLabel = document.createElement("label");
      routerEffortLabel.className = "router-effort-label";
      routerEffortLabel.append("Router effort ");
      const routerEffortInput = document.createElement("select");
      routerEffortInput.className = "provider-router-effort";
      populateEffortSelect(routerEffortInput, provider.id, routerModelInput.value, provider.router_effort);
      routerModelInput.addEventListener("change", () => {
        populateEffortSelect(routerEffortInput, provider.id, routerModelInput.value, routerEffortInput.value);
        setDirty();
      });
      routerEffortInput.addEventListener("change", setDirty);
      routerEffortLabel.appendChild(routerEffortInput);
      const quotaLabel = document.createElement("label");
      quotaLabel.append("Minimum quota remaining ");
      const quotaWrap = document.createElement("div");
      quotaWrap.className = "input-with-suffix";
      const quotaInput = document.createElement("input");
      quotaInput.type = "number";
      quotaInput.min = "0";
      quotaInput.max = "100";
      quotaInput.className = "provider-minimum-quota";
      quotaInput.value = String(config.minimum_remaining_percent ?? 10);
      quotaInput.addEventListener("input", () => {
        document.querySelectorAll(".provider-minimum-quota").forEach((input) => {
          if (input !== quotaInput) input.value = quotaInput.value;
        });
        setDirty();
      });
      const suffix = document.createElement("span");
      suffix.textContent = "%";
      quotaWrap.append(quotaInput, suffix);
      quotaLabel.appendChild(quotaWrap);
      fields.append(
        modelLabel,
        effortLabel,
        routingNote,
        routerModelLabel,
        routerEffortLabel,
        quotaLabel,
      );

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
      if (window.SwarmDynamicRouting) {
        window.SwarmDynamicRouting.applyRoutingControlState(card, Boolean(config.dynamic_model_routing));
      }
      grid.appendChild(card);
    });
    renderProviderPreference(config);
  }

  function preferenceChoice(value, title, detail, { checked = false, disabled = false, auto = false } = {}) {
    const label = document.createElement("label");
    label.className = "provider-preference-choice";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "preferred-provider";
    input.className = auto ? "provider-preferred provider-preference-auto" : "provider-preferred";
    input.value = value;
    input.checked = checked;
    input.disabled = disabled;
    input.addEventListener("change", setDirty);
    const copy = document.createElement("span");
    const heading = document.createElement("strong");
    heading.textContent = title;
    const note = document.createElement("small");
    note.textContent = detail;
    copy.append(heading, note);
    label.append(input, copy);
    return label;
  }

  function renderProviderPreference(config) {
    const host = document.getElementById("provider-preference");
    if (!host) return;
    host.replaceChildren();
    const legend = document.createElement("p");
    legend.className = "provider-preference-legend";
    legend.id = "provider-preference-legend";
    legend.textContent = "Who handles a new issue first";
    const options = document.createElement("div");
    options.className = "provider-preference-options";
    const auto = config.preferred_provider === PREFERRED_PROVIDER_AUTO;
    options.appendChild(preferenceChoice(
      PREFERRED_PROVIDER_AUTO,
      "No preference",
      "Whoever has the most usage left goes first, so one provider is not used up before the others.",
      { checked: auto, auto: true },
    ));
    providerList(config).forEach((provider) => {
      const meta = PROVIDER_META[provider.id];
      options.appendChild(preferenceChoice(
        provider.id,
        meta.label,
        provider.enabled ? "Preferred when remaining usage is tied." : "Turn this provider on to prefer it.",
        {
          checked: !auto && provider.id === config.preferred_provider && provider.enabled,
          disabled: !provider.enabled,
        },
      ));
    });
    host.append(legend, options);
    if (!host.querySelector('input[name="preferred-provider"]:checked')) {
      const fallback = host.querySelector(".provider-preferred:not(.provider-preference-auto):not(:disabled)");
      const autoInput = host.querySelector(".provider-preference-auto");
      if (fallback) fallback.checked = true;
      else if (autoInput) autoInput.checked = true;
    }
  }

  function collectProviders() {
    return Array.from(document.querySelectorAll("#provider-cards .provider-card"), (card) => ({
      id: card.dataset.provider,
      enabled: card.querySelector(".provider-enabled").checked,
      model: card.querySelector(".provider-model").value.trim(),
      effort: card.querySelector(".provider-effort").value,
      router_model: card.querySelector(".provider-router-model")?.value.trim() || "",
      router_effort: card.querySelector(".provider-router-effort")?.value || "",
      bin: document.querySelector(`[data-provider-bin="${card.dataset.provider}"]`)?.value.trim() || "",
    }));
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
    next.feedback_repo_filter = feedbackRepoIdsForQuery();
    document.querySelectorAll("[data-config]").forEach((input) => {
      const key = input.dataset.config;
      if (input.type === "checkbox" && input.dataset.checkedValue !== undefined) {
        next[key] = valuedToggle().valuedToggleValue(
          input.checked,
          input.dataset.checkedValue,
          input.dataset.uncheckedValue,
        );
      } else if (input.type === "checkbox") next[key] = input.checked;
      else if (input.dataset.list !== undefined) {
        next[key] = input.value.split(",").map((value) => value.trim()).filter(Boolean);
      } else if (input.type === "number") {
        next[key] = Number(input.value);
      } else next[key] = input.value.trim();
    });
    next.schedule_mode = byId("schedule-mode-select")?.value || "continuous";
    next.schedule_days = Array.from(document.querySelectorAll("#days-field input:checked"), (input) => input.value);
    const providers = collectProviders();
    if (providers.length) next.providers = providers;
    const selectedPreference = document.querySelector('input[name="preferred-provider"]:checked')?.value;
    if (selectedPreference) next.preferred_provider = selectedPreference;
    const minimumQuota = document.querySelector(".provider-minimum-quota")?.value;
    if (minimumQuota !== undefined) next.minimum_remaining_percent = Number(minimumQuota);
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
    void refreshPromotions({ quiet: true });
    return saved;
  }

  async function saveBeforeAction() {
    if (state.dirty) await saveConfig({ quiet: true });
  }

  async function withBusy(key, callback, { progress = "Working…" } = {}) {
    if (state.busy.has(key)) return;
    state.busy.add(key);
    // Disable the button that triggered the action (dynamically-built merge /
    // promote buttons aren't [data-action], so renderControls can't reach them).
    const trigger = document.activeElement;
    const lockedTrigger = trigger instanceof HTMLButtonElement && !trigger.disabled ? trigger : null;
    if (lockedTrigger) lockedTrigger.disabled = true;
    const progressToast = progress ? showToast(progress, "progress") : null;
    renderControls();
    try {
      await callback();
    } catch (error) {
      showToast(errorText(error), "error");
    } finally {
      state.busy.delete(key);
      if (progressToast) progressToast.remove();
      if (lockedTrigger) lockedTrigger.disabled = false;
      renderControls();
    }
  }

  // What "Run now" should do for a [data-action] in the current status.
  function runNowModeFor(action) {
    return window.SwarmRunNow.runNowMode({
      kind: "issue",
      processState: state.status?.issue?.state || "stopped",
      available: true,
      busy: state.busy.has(action),
    });
  }

  async function runAction(action) {
    const verb = action.startsWith("start-") ? "Starting" : action.startsWith("run-") ? "Running"
      : action.startsWith("pause-") ? "Updating" : "Stopping";
    // Resolved before withBusy marks the action busy, which would read as
    // "disabled" and lose the distinction between starting and interrupting.
    const runMode = action.startsWith("run-") ? runNowModeFor(action) : "";
    const progress = runMode === "request"
      ? "Asking the issue worker to scan now…"
      : `${verb} the issue worker…`;
    await withBusy(action, async () => {
      const process = "issue";
      if (runMode === "request") {
        // Already running: don't start a second worker, ask this one to
        // scan now and restart its timer.
        await saveBeforeAction();
        showToast(await invoke("request_issue_scan"), "success");
      } else if (action.startsWith("start-") || action.startsWith("run-")) {
        await saveBeforeAction();
        await invoke("start_issue_worker", { runOnce: action.startsWith("run-") });
        showToast("Issue worker started.", "success");
      } else if (action.startsWith("pause-")) {
        const current = state.status?.issue?.state;
        const command = current === "paused" ? "resume_process" : "pause_process";
        await invoke(command, { process });
        showToast(current === "paused" ? "Process resumed." : "Process paused.", "success");
      } else if (action.startsWith("stop-")) {
        await invoke("stop_process", { process });
        showToast("Process stopped.", "success");
      }
      await refreshStatus();
    }, { progress });
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
    else copy.textContent = "Ready when your repository and AI providers are configured.";
  }

  function renderControls() {
    document.querySelectorAll("[data-action]").forEach((button) => {
      const action = button.dataset.action;
      const processState = state.status?.issue?.state || "stopped";
      const busy = state.busy.has(action);
      if (action.startsWith("run-")) {
        // Stays available while the issue worker runs — see runNowMode.
        button.disabled = runNowModeFor(action) === "disabled";
      } else if (action.startsWith("start-")) {
        button.disabled = busy || processState !== "stopped";
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
    const deferredWarning = byId("repo-deferred-warning");
    deferredWarning.classList.toggle("hidden", !status.deferredReason);
    deferredWarning.textContent = status.deferredReason || "";
    const pathEl = byId("workspace-path");
    if (pathEl) pathEl.textContent = status.workspacePath || "—";
    const container = byId("repo-inspection");
    container.replaceChildren();
    addFact(container, "Branch", repo?.branch || "Not available");
    addFact(container, "Working tree", repo?.valid ? (repo.dirty ? "Uncommitted changes" : "Clean") : "Unknown");
    addFact(container, "GitHub remote", repo?.githubRepository || "Not inferred");
    if (repo?.error) addFact(container, "Problem", repo.error);
  }

  // ----- Feedback (AI execution history) ------------------------------

  const EXECUTION_STATUS_META = {
    accepted: { label: "Accepted", cls: "running" },
    preparing_repository: { label: "Preparing repository", cls: "running" },
    prompt_generated: { label: "Prompt generated", cls: "running" },
    running: { label: "Running", cls: "running" },
    ai_response_received: { label: "AI response received", cls: "running" },
    validated: { label: "Validated", cls: "running" },
    completed: { label: "Completed", cls: "passed" },
    environment_only: { label: "Environment only", cls: "passed" },
    quota_paused: { label: "Quota paused", cls: "waiting-for-input" },
    failed: { label: "Failed", cls: "failed" },
    imported: { label: "Imported", cls: "" },
  };

  function executionStatusMeta(status) {
    return EXECUTION_STATUS_META[status] || { label: status || "Unknown", cls: "" };
  }

  function formatIsoTimestamp(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
  }

  function formatDurationSeconds(totalSeconds) {
    const total = Math.round(totalSeconds);
    if (total < 60) return `${total}s`;
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
  }

  function rawTextPanel(label, text) {
    const details = document.createElement("details");
    details.className = "raw-graph-panel";
    const summary = document.createElement("summary");
    summary.textContent = label;
    const pre = document.createElement("pre");
    pre.className = "full-log branch-graph";
    pre.textContent = text || "Nothing recorded.";
    details.append(summary, pre);
    return details;
  }

  function executionNoteRow(kind, message, stateClass) {
    const row = document.createElement("div");
    row.className = "execution-note";
    const strong = document.createElement("strong");
    strong.textContent = message;
    const badge = document.createElement("span");
    badge.className = `suite-state ${stateClass}`.trim();
    badge.textContent = kind;
    row.append(strong, badge);
    return row;
  }

  function executionRepoFacts(record) {
    const facts = document.createElement("div");
    facts.className = "repo-inspection";
    const addFact = (label, value) => {
      if (!value) return;
      const fact = document.createElement("div");
      fact.className = "repo-fact";
      const span = document.createElement("span");
      span.textContent = label;
      const strong = document.createElement("strong");
      strong.textContent = value;
      fact.append(span, strong);
      facts.appendChild(fact);
    };
    addFact("Branch", record.branchName);
    addFact("Duration", record.durationSeconds != null ? formatDurationSeconds(record.durationSeconds) : "");
    addFact("Files changed", (record.filesChanged || []).length ? String(record.filesChanged.length) : "");
    addFact("Commits", (record.commitShas || []).length ? String(record.commitShas.length) : "");
    addFact("Application version", record.applicationVersion);
    addFact("Prompt template", record.promptTemplateVersion);
    return facts;
  }

  function repositoryBadge(repository) {
    const badge = document.createElement("span");
    badge.className = "repository-badge";
    badge.textContent = repository || "Repository not recorded";
    badge.title = repository || "Repository not recorded";
    return badge;
  }

  function buildExecutionRecordItem(record) {
    const item = document.createElement("details");
    item.className = "execution-record";

    const summary = document.createElement("summary");
    const head = document.createElement("div");
    head.className = "execution-head";
    const title = document.createElement("strong");
    title.textContent = `#${record.issueNumber} ${record.issueTitle || ""}`.trim();
    const titleRow = document.createElement("div");
    titleRow.className = "execution-title-row";
    titleRow.append(title, repositoryBadge(record.repository));
    const meta = document.createElement("small");
    meta.textContent = [
      record.attemptNumber ? `Attempt ${record.attemptNumber}` : "",
      record.startedAt ? `Started ${formatIsoTimestamp(record.startedAt)}` : "",
    ].filter(Boolean).join(" · ");
    head.append(titleRow, meta);
    const tagging = document.createElement("div");
    tagging.className = "execution-tagging";
    [["AI tool", record.aiProvider], ["Model", record.model], ["Effort", record.effort], ["UAT rounds", window.SwarmAdversarialUat.roundCount(record)]].forEach(([label, value]) => {
      const cell = document.createElement("div");
      cell.className = "execution-tag";
      const name = document.createElement("span");
      name.textContent = label;
      const text = document.createElement("strong");
      text.textContent = value || "—";
      text.title = value || "Not recorded";
      cell.append(name, text);
      tagging.appendChild(cell);
    });
    const tally = document.createElement("div");
    tally.className = "execution-tally";
    const statusMeta = executionStatusMeta(record.finalStatus);
    const badge = document.createElement("span");
    badge.className = `suite-state ${statusMeta.cls}`.trim();
    badge.textContent = statusMeta.label;
    tally.appendChild(badge);
    summary.append(head, tagging, tally);
    item.appendChild(summary);

    const body = document.createElement("div");
    body.className = "execution-body";

    const facts = executionRepoFacts(record);
    if (facts.children.length) body.appendChild(facts);

    const links = document.createElement("div");
    links.className = "control-row";
    if (record.issueUrl) links.appendChild(externalLink(`Open issue #${record.issueNumber} ↗`, record.issueUrl, "text-button"));
    if (record.pullRequestUrl) links.appendChild(externalLink("Open pull request ↗", record.pullRequestUrl, "text-button"));
    if (links.children.length) body.appendChild(links);

    const addSummaryParagraph = (label, text) => {
      if (!text) return;
      const p = document.createElement("p");
      p.className = "panel-copy";
      const strong = document.createElement("strong");
      strong.textContent = `${label}: `;
      p.append(strong, document.createTextNode(text));
      body.appendChild(p);
    };
    addSummaryParagraph("Requested work", record.requestedWorkSummary);
    addSummaryParagraph("Changes made", record.changesSummary);
    addSummaryParagraph("Adversarial UAT", record.adversarialOutcome?.replaceAll("_", " "));
    const filedFindings = record.adversarialFiledFindings || [];
    if (filedFindings.length) {
      addSummaryParagraph("Out-of-scope UAT findings", `${filedFindings.length} separately filed issue${filedFindings.length === 1 ? "" : "s"}.`);
      const findingLinks = document.createElement("div");
      findingLinks.className = "control-row";
      for (const finding of filedFindings) {
        if (finding?.url) findingLinks.appendChild(externalLink(finding.title || "Open separately filed issue ↗", finding.url, "text-button"));
      }
      if (findingLinks.children.length) body.appendChild(findingLinks);
    }
    if (record.capacityConsumedPercent != null) {
      addSummaryParagraph("Approximate quota consumed", window.SwarmAdversarialUat.capacity(record.capacityConsumedPercent));
    }
    for (const round of record.adversarialRounds || []) {
      addSummaryParagraph(`UAT ${round.round_number === 0 ? "initial assessment" : `round ${round.round_number}`}`,
        window.SwarmAdversarialUat.roundDetail(round));
    }
    const routing = record.routingDecision;
    if (routing && typeof routing === "object") {
      if (routing.fallback) {
        addSummaryParagraph("AI routing", routing.grade_reason || "Fell back to the configured worker model.");
      } else if (routing.prompt_grade) {
        const confidence = Number(routing.confidence);
        const percent = Number.isFinite(confidence) ? `${Math.round(confidence * 100)}% confidence` : "";
        addSummaryParagraph(
          "AI routing",
          [
            `Grade ${routing.prompt_grade}`,
            routing.complexity != null ? `complexity ${routing.complexity}/10` : "",
            routing.provider_name || routing.provider || "",
            routing.selected_model || "",
            routing.reasoning_effort || "",
            percent,
          ].filter(Boolean).join(" · "),
        );
        addSummaryParagraph(
          `Why ${routing.provider_name || routing.provider || "this tool"}`,
          [routing.provider_reason || "", routing.provider_override_reason || ""].filter(Boolean).join(" "),
        );
        addSummaryParagraph(`Why grade ${routing.prompt_grade}`, routing.grade_reason || "");
        addSummaryParagraph(
          routing.complexity != null ? `How complexity ${routing.complexity}/10 was determined` : "Complexity",
          [routing.complexity_reason || "", routing.tier_explanation || ""].filter(Boolean).join(" "),
        );
      }
    }

    body.appendChild(rawTextPanel("Original GitHub issue", record.originalIssueBody));
    body.appendChild(rawTextPanel("Effective AI prompt", record.effectivePrompt));

    const notes = document.createElement("div");
    notes.className = "execution-notes";
    (record.operationalNotes || []).forEach((note) => notes.appendChild(executionNoteRow("Note", note, "")));
    (record.warningsErrors || []).forEach((warning) => notes.appendChild(executionNoteRow("Warning", warning, "failed")));
    if (notes.children.length) body.appendChild(notes);

    if (record.reviewerFeedback) {
      const banner = document.createElement("div");
      banner.className = "banner policy";
      const strong = document.createElement("strong");
      strong.textContent = "Reviewer feedback.";
      const span = document.createElement("span");
      span.textContent = record.reviewerFeedback;
      banner.append(strong, span);
      body.appendChild(banner);
    }

    item.appendChild(body);
    return item;
  }

  function executionHistoryView() {
    const page = state.executionHistory;
    if (!page || !Array.isArray(page.records)) {
      return { records: [], total: 0, offset: 0, limit: 10 };
    }
    return page;
  }

  function executionCountNoun(total, searching) {
    if (searching) return total === 1 ? "match" : "matches";
    return total === 1 ? "execution" : "executions";
  }

  function renderExecutionHistory() {
    const box = byId("execution-history-list");
    if (!box) return;
    box.replaceChildren();
    const page = executionHistoryView();
    const aggregate = byId("adversarial-history-summary");
    const stats = page.adversarial;
    if (aggregate) aggregate.textContent = window.SwarmAdversarialUat.aggregate(stats);
    const searching = state.executionHistorySearch.trim().length > 0;
    const total = Number(page.total) || 0;
    const limit = Number(page.limit) || 10;
    const offset = Number(page.offset) || 0;
    const records = page.records;
    const count = byId("execution-history-count");
    if (count) {
      if (!total || (offset === 0 && records.length >= total)) {
        count.textContent = `${total} ${executionCountNoun(total, searching)}`;
      } else {
        const start = offset + 1;
        const end = offset + records.length;
        count.textContent = `Showing ${start}-${end} of ${total} ${executionCountNoun(total, searching)}`;
      }
    }
    const pager = byId("execution-history-pager");
    if (pager) {
      const pageCount = Math.max(1, Math.ceil(total / limit));
      const pageNumber = Math.floor(offset / limit) + 1;
      pager.classList.toggle("hidden", total <= limit);
      const label = byId("execution-history-page-label");
      if (label) label.textContent = `Page ${pageNumber} of ${pageCount}`;
      const prev = byId("execution-history-prev");
      const next = byId("execution-history-next");
      if (prev) prev.disabled = offset <= 0;
      if (next) next.disabled = offset + records.length >= total;
    }
    if (!records.length) {
      box.appendChild(Object.assign(document.createElement("p"), {
        className: "panel-copy",
        textContent: searching
          ? "No executions match this search."
          : "No AI executions recorded yet. Turn on “Store AI execution history” in AI Configuration, then run an issue.",
      }));
      return;
    }
    records.forEach((record) => box.appendChild(buildExecutionRecordItem(record)));
  }

  async function refreshExecutionHistory({ quiet = false } = {}) {
    const repoIds = feedbackRepoIdsForQuery();
    const filterSignature = feedbackRepoFilterSignature();
    const requestId = state.executionHistoryRequest + 1;
    state.executionHistoryRequest = requestId;
    if (!feedbackRepositories().length) {
      state.executionHistory = { records: [], total: 0, offset: 0, limit: 10 };
      state.executionHistoryOffset = 0;
      renderExecutionHistory();
      return;
    }
    const offset = Math.max(0, Number(state.executionHistoryOffset) || 0);
    const search = state.executionHistorySearch.trim();
    try {
      const page = await invoke("get_execution_history_background", {
        repoIds,
        offset,
        search,
        sort: state.executionHistorySort,
      });
      if (requestId !== state.executionHistoryRequest || filterSignature !== feedbackRepoFilterSignature()) return;
      state.executionHistory = page;
      state.executionHistoryOffset = Number(page.offset) || 0;
      renderExecutionHistory();
    } catch (error) {
      if (requestId !== state.executionHistoryRequest) return;
      if (!quiet) showToast(errorText(error), "error");
    }
  }

  function promptGradesView() {
    const page = state.promptGrades;
    if (!page || !Array.isArray(page.records)) {
      return { records: [], total: 0, offset: 0, limit: 10, summary: null, routerMatrix: [] };
    }
    return page;
  }

  // The AI platform, model, and effort that graded and routed an issue, from
  // the stored routing decision. Reported separately from the worker's own
  // model everywhere, so a grade can be attributed to the AI that gave it.
  function routerLabel(decision) {
    const provider = String((decision && decision.router_provider) || "").trim();
    return provider ? providerLabel(provider) : "";
  }

  function routerModelLabel(decision) {
    const model = String((decision && decision.router_model) || "").trim();
    const name = routerLabel(decision);
    if (name && model) return `${name} · ${model}`;
    return name || model;
  }

  function gradeBadge(grade, extraClass = "") {
    const badge = document.createElement("span");
    badge.className = `grade-badge ${window.SwarmPromptGrades.gradeTone(grade)} ${extraClass}`.trim();
    badge.textContent = grade || "—";
    return badge;
  }

  function buildPromptGradeItem(record) {
    const decision = record.routingDecision || {};
    const item = document.createElement("details");
    item.className = "execution-record grade-record";

    const summary = document.createElement("summary");
    const head = document.createElement("div");
    head.className = "execution-head";
    const title = document.createElement("strong");
    title.textContent = `#${record.issueNumber} ${record.issueTitle || ""}`.trim();
    const titleRow = document.createElement("div");
    titleRow.className = "execution-title-row";
    titleRow.append(title, repositoryBadge(record.repository));
    const meta = document.createElement("small");
    meta.textContent = [
      record.attemptNumber > 1 ? `Attempt ${record.attemptNumber}` : "",
      record.startedAt ? `Graded ${formatIsoTimestamp(record.startedAt)}` : "",
    ].filter(Boolean).join(" · ");
    head.append(titleRow, meta);
    const tagging = document.createElement("div");
    tagging.className = "execution-tagging";
    [
      ["AI tool", record.aiProvider],
      ["Model", record.model],
      ["Graded by", routerModelLabel(decision)],
      ["Complexity", decision.complexity != null ? `${decision.complexity}/10` : ""],
    ].forEach(([label, value]) => {
      const cell = document.createElement("div");
      cell.className = "execution-tag";
      const name = document.createElement("span");
      name.textContent = label;
      const text = document.createElement("strong");
      text.textContent = value || "—";
      text.title = value || "Not recorded";
      cell.append(name, text);
      tagging.appendChild(cell);
    });
    summary.append(gradeBadge(decision.prompt_grade), head, tagging);
    item.appendChild(summary);

    const body = document.createElement("div");
    body.className = "execution-body";
    const reason = document.createElement("p");
    reason.className = "panel-copy";
    const strong = document.createElement("strong");
    strong.textContent = `Why ${decision.prompt_grade}: `;
    reason.append(strong, document.createTextNode(decision.grade_reason || "The router gave no explanation."));
    body.appendChild(reason);
    const confidence = Number(decision.confidence);
    const facts = document.createElement("div");
    facts.className = "repo-inspection";
    [
      ["Effort", record.effort],
      ["Graded by", routerLabel(decision)],
      ["Grading model", decision.router_model || ""],
      ["Grading effort", decision.router_effort || ""],
      ["Router confidence", Number.isFinite(confidence) ? `${Math.round(confidence * 100)}%` : ""],
      ["Outcome", executionStatusMeta(record.finalStatus).label],
    ].forEach(([label, value]) => {
      if (!value) return;
      const fact = document.createElement("div");
      fact.className = "repo-fact";
      const span = document.createElement("span");
      span.textContent = label;
      const valueEl = document.createElement("strong");
      valueEl.textContent = value;
      fact.append(span, valueEl);
      facts.appendChild(fact);
    });
    if (facts.children.length) body.appendChild(facts);
    if (record.issueUrl) {
      const links = document.createElement("div");
      links.className = "control-row";
      links.appendChild(externalLink(`Open issue #${record.issueNumber} ↗`, record.issueUrl, "text-button"));
      body.appendChild(links);
    }
    item.appendChild(body);
    return item;
  }

  function renderPromptGradeSummary(summary) {
    const box = byId("prompt-grades-summary");
    if (!box) return;
    const graded = Number(summary && summary.graded) || 0;
    box.classList.toggle("hidden", graded === 0);
    if (!graded) return;
    const average = byId("prompt-grades-average");
    average.className = `grade-badge ${window.SwarmPromptGrades.gradeTone(summary.averageGrade)}`;
    average.textContent = summary.averageGrade || "—";
    byId("prompt-grades-average-detail").textContent = [
      Number.isFinite(Number(summary.averagePoints)) ? `${Number(summary.averagePoints).toFixed(2)} / 4.0` : "",
      window.SwarmPromptGrades.summaryLine(summary),
    ].filter(Boolean).join(" · ");
    const bars = byId("prompt-grades-bars");
    bars.replaceChildren();
    const selected = state.promptGradesGrade;
    window.SwarmPromptGrades.distributionBars(summary, selected).forEach((bar) => {
      const column = document.createElement("button");
      column.type = "button";
      column.className = `grade-bar ${bar.tone} ${bar.selected ? "selected" : ""}`.trim();
      column.dataset.grade = bar.grade;
      column.setAttribute("aria-pressed", bar.selected ? "true" : "false");
      const countLabel = `${bar.count} prompt${bar.count === 1 ? "" : "s"} graded ${bar.grade}`;
      column.title = bar.selected ? `${countLabel}. Click to show every grade.` : countLabel;
      column.setAttribute("aria-label", bar.selected ? `Clear ${bar.grade} filter` : `Show only ${bar.grade}`);
      const count = document.createElement("span");
      count.className = "grade-bar-count";
      count.textContent = bar.count ? String(bar.count) : "";
      const track = document.createElement("div");
      track.className = "grade-bar-track";
      const fill = document.createElement("div");
      fill.className = "grade-bar-fill";
      fill.style.setProperty("--fill", `${bar.percent}%`);
      track.appendChild(fill);
      const label = document.createElement("span");
      label.className = "grade-bar-label";
      label.textContent = bar.grade;
      column.append(count, track, label);
      bars.appendChild(column);
    });
  }

  // One row per grading platform, each showing the platforms it handed work to
  // as a proportional bar with counts and percentages. Selecting a row filters
  // every grade by who graded it, which is the axis the search box cannot
  // reach. The matrix itself ignores that filter so another row stays pickable.
  function renderPromptGradeRouters(matrix) {
    const box = byId("prompt-grades-router-matrix");
    if (!box) return;
    box.replaceChildren();
    const rows = window.SwarmPromptGrades.routerRows(
      matrix,
      state.promptGradesRouter,
      state.promptGradesRouterModel,
    );
    const count = byId("prompt-grades-router-count");
    if (count) {
      const summary = window.SwarmPromptGrades.routerSummary(matrix);
      const platforms = `${summary.platforms} platform${summary.platforms === 1 ? "" : "s"}`;
      const models = `${summary.models} model${summary.models === 1 ? "" : "s"}`;
      count.textContent = `${platforms} · ${models}`;
    }
    const legend = byId("prompt-grades-router-legend");
    if (legend) legend.replaceChildren();
    if (!rows.length) {
      box.appendChild(Object.assign(document.createElement("p"), {
        className: "panel-copy",
        textContent: state.promptGradesSearch.trim()
          ? "No graded prompts match this search."
          : "No graded prompts yet. Turn on Dynamic Model Routing and Store AI execution history, then run an issue.",
      }));
      return;
    }
    // One series colour per platform across every row, so a colour means the
    // same thing whichever router picked it.
    const seriesOrder = [];
    rows.forEach((row) => row.selections.forEach(({ provider }) => {
      if (provider && !seriesOrder.includes(provider)) seriesOrder.push(provider);
    }));
    const seriesClass = (provider) => {
      const index = seriesOrder.indexOf(provider);
      return index >= 0 && index < 4 ? `series-${index + 1}` : "";
    };
    rows.forEach((row) => {
      const element = document.createElement("div");
      element.className = `router-row router-${row.router || "unknown"} ${row.selected ? "selected" : ""}`.trim();
      const platform = document.createElement(row.interactive ? "button" : "div");
      platform.className = "router-platform-control";
      if (row.interactive) {
        platform.type = "button";
        platform.dataset.router = row.router;
        platform.setAttribute("aria-pressed", row.selected && !state.promptGradesRouterModel ? "true" : "false");
        platform.setAttribute(
          "aria-label",
          row.selected && !state.promptGradesRouterModel
            ? `Clear the ${providerLabel(row.router)} grading filter`
            : `Show only prompts graded by ${providerLabel(row.router)}`,
        );
      }
      const name = document.createElement("div");
      name.className = "router-row-name";
      const title = document.createElement("strong");
      title.textContent = row.interactive ? providerLabel(row.router) : "Not recorded";
      const subtitle = document.createElement("span");
      subtitle.textContent = `${row.graded} graded`;
      name.append(title, subtitle);
      platform.appendChild(name);

      const detail = document.createElement("div");
      detail.className = "router-row-detail";
      const modelHeading = document.createElement("span");
      modelHeading.className = "router-detail-heading";
      modelHeading.textContent = "Grading models";
      const models = document.createElement("div");
      models.className = "router-models";
      row.models.forEach((entry) => {
        const model = document.createElement(entry.interactive ? "button" : "div");
        model.className = `router-model ${entry.selected ? "selected" : ""}`.trim();
        model.style.setProperty("--model-share", `${entry.percent}%`);
        if (entry.interactive) {
          model.type = "button";
          model.dataset.router = row.router;
          model.dataset.routerModel = entry.model;
          model.setAttribute("aria-pressed", entry.selected ? "true" : "false");
          model.setAttribute(
            "aria-label",
            entry.selected
              ? `Show every ${providerLabel(row.router)} grading model`
              : `Show only prompts graded by ${providerLabel(row.router)} ${entry.model}`,
          );
        }
        const modelName = document.createElement("strong");
        modelName.textContent = window.SwarmPromptGrades.modelLabel(entry.model);
        modelName.title = entry.model || "Model not recorded";
        const modelCount = document.createElement("span");
        modelCount.textContent = `${entry.count} · ${Math.round(entry.percent)}%`;
        model.append(modelName, modelCount);
        models.appendChild(model);
      });

      const pickedHeading = document.createElement("span");
      pickedHeading.className = "router-detail-heading";
      pickedHeading.textContent = "Platforms picked";
      const shares = document.createElement("div");
      shares.className = "router-shares";
      const track = document.createElement("div");
      track.className = "router-share-track";
      const labels = document.createElement("div");
      labels.className = "router-share-labels";
      row.selections.forEach((entry) => {
        const share = document.createElement("div");
        share.className = `router-share ${seriesClass(entry.provider)}`.trim();
        share.style.setProperty("--share", `${entry.percent}%`);
        const text = `${providerLabel(entry.provider)} ${entry.count} (${Math.round(entry.percent)}%)`;
        share.title = text;
        track.appendChild(share);
        labels.appendChild(Object.assign(document.createElement("span"), { textContent: text }));
      });
      shares.append(track, labels);
      detail.append(modelHeading, models, pickedHeading, shares);
      element.append(platform, detail);
      box.appendChild(element);
    });
    if (legend) {
      seriesOrder.forEach((provider) => {
        const item = document.createElement("span");
        const swatch = document.createElement("i");
        swatch.className = seriesClass(provider);
        item.append(swatch, document.createTextNode(providerLabel(provider)));
        legend.appendChild(item);
      });
    }
  }

  // The banner shown on the grades tab while a router filter set on the
  // routing tab is narrowing the list, so the filter is never invisible.
  function renderPromptGradeRouterFilter() {
    const banner = byId("prompt-grades-router-filter");
    if (!banner) return;
    const router = state.promptGradesRouter;
    const routerModel = state.promptGradesRouterModel;
    banner.classList.toggle("hidden", !router);
    banner.replaceChildren();
    if (!router) return;
    const label = document.createElement("strong");
    label.textContent = `Graded by ${providerLabel(router)}${routerModel ? ` · ${window.SwarmPromptGrades.modelLabel(routerModel)}` : ""}`;
    if (routerModel) label.title = routerModel;
    const detail = document.createElement("span");
    detail.textContent = routerModel
      ? "Only issues this AI platform graded with this model are listed."
      : "Only issues this AI platform graded and routed are listed.";
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "text-button";
    clear.id = "prompt-grades-router-clear";
    clear.textContent = "Show every router";
    banner.append(label, detail, clear);
  }

  function promptGradesFiltering() {
    return state.promptGradesSearch.trim().length > 0
      || state.promptGradesGrade.length > 0
      || state.promptGradesRouter.length > 0
      || state.promptGradesRouterModel.length > 0;
  }

  function promptGradeCountNoun(total, filtering) {
    if (filtering) return total === 1 ? "match" : "matches";
    return "graded";
  }

  function renderPromptGrades() {
    const box = byId("prompt-grades-list");
    if (!box) return;
    box.replaceChildren();
    const page = promptGradesView();
    const filtering = promptGradesFiltering();
    const total = Number(page.total) || 0;
    const limit = Number(page.limit) || 10;
    const offset = Number(page.offset) || 0;
    const records = page.records;
    renderPromptGradeSummary(page.summary);
    renderPromptGradeRouters(page.routerMatrix);
    renderPromptGradeRouterFilter();
    const count = byId("prompt-grades-count");
    if (count) {
      const noun = promptGradeCountNoun(total, filtering);
      if (!total || (offset === 0 && records.length >= total)) {
        count.textContent = `${total} ${noun}`;
      } else {
        const start = offset + 1;
        const end = offset + records.length;
        count.textContent = `Showing ${start}-${end} of ${total} ${noun}`;
      }
    }
    const pager = byId("prompt-grades-pager");
    if (pager) {
      const pageCount = Math.max(1, Math.ceil(total / limit));
      const pageNumber = Math.floor(offset / limit) + 1;
      pager.classList.toggle("hidden", total <= limit);
      const label = byId("prompt-grades-page-label");
      if (label) label.textContent = `Page ${pageNumber} of ${pageCount}`;
      const prev = byId("prompt-grades-prev");
      const next = byId("prompt-grades-next");
      if (prev) prev.disabled = offset <= 0;
      if (next) next.disabled = offset + records.length >= total;
    }
    if (!records.length) {
      box.appendChild(Object.assign(document.createElement("p"), {
        className: "panel-copy",
        textContent: filtering
          ? "No graded prompts match this filter."
          : "No graded prompts yet. Turn on Dynamic Model Routing and Store AI execution history, then run an issue.",
      }));
      return;
    }
    records.forEach((record) => box.appendChild(buildPromptGradeItem(record)));
  }

  // The Feedback view is a tablist over three reports rather than one long
  // stack. Same shape as the view-switcher: a button per panel, one panel
  // visible at a time, no route of its own.
  function showFeedbackTab(tab, { focus = false } = {}) {
    state.feedbackTab = window.SwarmPromptGrades.activeTab(tab);
    document.querySelectorAll("[data-feedback-tab]").forEach((button) => {
      const active = button.dataset.feedbackTab === state.feedbackTab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
      // Roving tabindex: only the selected tab is in the tab order, so the
      // arrow keys below are the only way to reach the others by keyboard.
      button.tabIndex = active ? 0 : -1;
      if (active && focus) button.focus();
    });
    document.querySelectorAll("[data-feedback-panel]").forEach((panel) => {
      const active = panel.dataset.feedbackPanel === state.feedbackTab;
      panel.classList.toggle("active", active);
      panel.hidden = !active;
    });
  }

  // The Repository view is a tablist over repo-specific setting groups
  // instead of one long scroll, same shape as the Feedback view above.
  const REPOSITORY_TABS = ["source", "bots", "queue", "delivery"];

  function showRepositoryTab(tab, { focus = false } = {}) {
    state.repositoryTab = REPOSITORY_TABS.includes(String(tab || "")) ? tab : REPOSITORY_TABS[0];
    document.querySelectorAll("[data-repository-tab]").forEach((button) => {
      const active = button.dataset.repositoryTab === state.repositoryTab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
      button.tabIndex = active ? 0 : -1;
      if (active && focus) button.focus();
    });
    document.querySelectorAll("[data-repository-panel]").forEach((panel) => {
      const active = panel.dataset.repositoryPanel === state.repositoryTab;
      panel.classList.toggle("active", active);
      panel.hidden = !active;
    });
  }

  async function refreshPromptGrades({ quiet = false } = {}) {
    const repoIds = feedbackRepoIdsForQuery();
    const filterSignature = feedbackRepoFilterSignature();
    const requestId = state.promptGradesRequest + 1;
    state.promptGradesRequest = requestId;
    if (!feedbackRepositories().length) {
      state.promptGrades = null;
      state.promptGradesOffset = 0;
      renderPromptGrades();
      return;
    }
    const offset = Math.max(0, Number(state.promptGradesOffset) || 0);
    const search = state.promptGradesSearch.trim();
    const grade = state.promptGradesGrade;
    const router = state.promptGradesRouter;
    const routerModel = state.promptGradesRouterModel;
    try {
      const page = await invoke("get_prompt_grades_background", {
        repoIds,
        query: { offset, search, grade, router, routerModel },
      });
      if (requestId !== state.promptGradesRequest || filterSignature !== feedbackRepoFilterSignature()) return;
      state.promptGrades = page;
      state.promptGradesOffset = Number(page.offset) || 0;
      renderPromptGrades();
    } catch (error) {
      if (requestId !== state.promptGradesRequest) return;
      if (!quiet) showToast(errorText(error), "error");
    }
  }

  async function importExecutionHistory() {
    if (!feedbackRepositories().length) return;
    await withBusy("import-execution-history", async () => {
      const batch = await invoke("import_execution_history_background", { repoIds: feedbackRepoIdsForQuery() });
      await refreshExecutionHistory();
      await refreshPromptGrades({ quiet: true });
      const results = Array.isArray(batch.results) ? batch.results : [];
      const messages = results.map((result) => result.success
        ? `${result.repository}: ${result.imported} imported, ${result.skipped} already tracked`
        : `${result.repository}: failed — ${result.error || "unknown error"}`);
      const failed = results.some((result) => !result.success);
      showToast(messages.join(" · ") || "No repositories are configured for import.", failed ? "error" : "success");
    }, { progress: "Scanning the GitHub issue backlog…" });
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
      const inspection = await invoke("prepare_workspace", { repoId: currentRepo().id });
      renderRepository(inspection);
      await refreshStatus();
      showToast("Workspace ready.", "success");
    }, { progress: "Cloning / updating the workspace… watch Info & Debug if it's a large repo." });
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
      // Tool detection also repairs selections retired by a provider. Pull
      // the persisted result back into the renderer before rebuilding model
      // dropdowns; never overwrite a form the user is actively editing.
      if (!state.dirty) state.config = await invoke("get_config");
      renderTools();
      renderReadiness();
    } catch (error) {
      if (!quiet) showToast(errorText(error), "error");
    } finally {
      state.refreshing.tools = false;
    }
  }

  async function installProvider(provider) {
    const cliName = PROVIDER_META[provider]?.cli || provider;
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
    }, { progress: `Starting the ${cliName} install…` });
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

  // ----- GitHub bot readiness -------------------------------------------------

  function botReadinessFor(repoId) {
    return state.botReadiness[repoId || state.activeRepoId] || null;
  }

  function botReadinessSummary(list) {
    if (!list || !list.length) return { text: "Not checked", ok: false };
    if (list.every((row) => row.ready)) return { text: "All bots ready", ok: true };
    const pending = list.filter((row) => !row.ready).length;
    return { text: `${pending} bot${pending === 1 ? "" : "s"} need setup`, ok: false };
  }

  const PUSH_ACCESS_PILL = {
    allowed: ["Allowed", "running"],
    missing: ["Blocked", "error"],
    unrestricted: ["Open", "paused"],
    unprotected: ["Open", "paused"],
    unconfigured: ["No bots", "stopped"],
  };

  function renderBranchPushAccess(status) {
    if (status && state.activeRepoId) state.branchPushAccess[state.activeRepoId] = status;
    renderReadiness();
    const pill = byId("branch-push-access-pill");
    const copy = byId("branch-push-access-copy");
    const button = byId("grant-bot-push-access");
    const [text, tone] = PUSH_ACCESS_PILL[status?.state] || ["Not checked", "stopped"];
    if (pill) {
      pill.textContent = text;
      pill.className = `status-pill ${tone}`;
    }
    if (copy) copy.textContent = status?.message || "Checking whether the worker bots are allowed to merge promotion pull requests.";
    if (button) {
      const branch = status?.branch || currentRepo()?.base_branch || "main";
      button.textContent = `Allow bots to merge into ${branch}`;
      button.disabled = !status?.canGrant;
    }
  }

  async function refreshBranchPushAccess({ quiet = true } = {}) {
    const repo = currentRepo();
    if (!repo || !String(repo.github_repository || "").includes("/")) {
      renderBranchPushAccess({
        state: "unconfigured",
        branch: repo?.base_branch || "main",
        message: "Enter this repository's owner/name to check who can merge into its human-owned branch.",
        canGrant: false,
      });
      return;
    }
    if (state.refreshing.branchPushAccess) return;
    state.refreshing.branchPushAccess = true;
    const repoId = repo.id;
    try {
      const status = await invoke("branch_push_access", { repoId });
      if (currentRepo()?.id !== repoId) return;
      renderBranchPushAccess(status);
    } catch (error) {
      if (currentRepo()?.id !== repoId) return;
      renderBranchPushAccess({
        state: "error",
        branch: repo.base_branch || "main",
        message: errorText(error),
        canGrant: false,
      });
      if (!quiet) showToast(errorText(error), "error");
    } finally {
      state.refreshing.branchPushAccess = false;
    }
  }

  async function grantBotPushAccess() {
    const repo = currentRepo();
    if (!repo) return;
    if (state.dirty) {
      showToast("Save changes before updating branch protection.", "error");
      return;
    }
    const button = byId("grant-bot-push-access");
    if (button) button.disabled = true;
    try {
      const status = await invoke("grant_bot_branch_push", { repoId: repo.id });
      if (currentRepo()?.id !== repo.id) return;
      renderBranchPushAccess(status);
      showToast(status.message, status.state === "allowed" ? "success" : "");
    } catch (error) {
      showToast(errorText(error), "error");
      await refreshBranchPushAccess({ quiet: true });
    }
  }

  async function refreshBotReadiness({ quiet = true } = {}) {
    const repo = currentRepo();
    if (!repo || !String(repo.github_repository || "").includes("/")) return;
    if (state.refreshing.botReadiness) return;
    state.refreshing.botReadiness = true;
    const repoId = repo.id;
    try {
      const results = await invoke("check_repo_bot_readiness", { repoId });
      state.botReadiness[repoId] = results;
      renderBotPanel();
      renderStatus();
    } catch (error) {
      if (!quiet) showToast(errorText(error), "error");
    } finally {
      state.refreshing.botReadiness = false;
    }
  }

  // After a browser hand-off GitHub takes a few seconds to expose a new
  // installation. Poll briefly so the checklist flips to ✓ on its own.
  function pollBotReadiness() {
    if (state.botReadinessPoll) window.clearInterval(state.botReadinessPoll);
    let ticks = 0;
    state.botReadinessPoll = window.setInterval(async () => {
      ticks += 1;
      await refreshBotReadiness({ quiet: true });
      const list = botReadinessFor();
      if (ticks >= 12 || (list && list.length && list.every((row) => row.ready))) {
        window.clearInterval(state.botReadinessPoll);
        state.botReadinessPoll = null;
      }
    }, 5000);
  }

  async function botAction(row) {
    if (row.needsSetupFlow) {
      await setupBots();
      return;
    }
    if (row.actionUrl) {
      await openUrl(row.actionUrl);
      showToast("Finish the install on GitHub — choose “All repositories” — then return here.", "success");
      pollBotReadiness();
    }
  }

  const BOT_STATE_HINT = {
    ready: "Ready",
    not_installed_on_owner: "Not installed on this account",
    no_repo_access: "Installed, but this repository is not granted",
    unconfigured: "App not created yet",
    error: "Could not check",
  };

  function renderBotPanel() {
    const list = botReadinessFor();
    const pill = byId("bot-config-pill");
    const summary = botReadinessSummary(list);
    if (pill) {
      pill.textContent = summary.text;
      pill.className = `status-pill ${summary.ok ? "running" : list && list.length ? "stopped" : ""}`;
    }
    const container = byId("bot-checklist");
    if (!container) return;
    container.replaceChildren();
    const repo = currentRepo();
    if (!repo || !String(repo.github_repository || "").includes("/")) {
      const hint = document.createElement("p");
      hint.className = "fine-print";
      hint.textContent = "Enter this repository's owner/name to check its bots.";
      container.appendChild(hint);
      return;
    }
    if (!list) {
      const hint = document.createElement("p");
      hint.className = "fine-print";
      hint.textContent = state.refreshing.botReadiness ? "Checking GitHub…" : "Press Re-check to inspect the bots for this repository.";
      container.appendChild(hint);
      return;
    }
    list.forEach((row) => {
      const item = document.createElement("div");
      item.className = `check-item ${row.ready ? "ready" : "attention"}`;
      const dot = document.createElement("span");
      dot.className = "check-dot";
      dot.textContent = row.ready ? "✓" : "!";
      const text = document.createElement("div");
      text.className = "check-item-text";
      const title = document.createElement("strong");
      title.textContent = `${row.providerLabel} bot`;
      const detail = document.createElement("span");
      detail.textContent = row.message || BOT_STATE_HINT[row.state] || row.state;
      text.append(title, detail);
      item.append(dot, text);
      if (!row.ready) {
        const label = row.needsSetupFlow ? "Set up GitHub Apps" : (row.actionLabel || "Open GitHub");
        item.appendChild(button(label, "secondary-button compact", () => botAction(row)));
      }
      container.appendChild(item);
    });
  }

  function renderReadiness() {
    if (!state.status) return;
    const gh = state.tools.find((tool) => tool.id === "gh");
    const enabledIds = new Set(
      providerList(state.dirty ? collectConfig() : state.config).filter((p) => p.enabled).map((p) => p.id),
    );
    const ais = state.tools.filter((tool) => enabledIds.has(tool.id));
    const repoStatus = currentRepoStatus();
    const botList = botReadinessFor();
    const botSummary = botReadinessSummary(botList);
    const botsRequired = Boolean(currentRepo()?.require_bot_auth);
    const botsReady = !botsRequired
      || (botList && botList.length ? botSummary.ok : Boolean(repoStatus?.botConfigExists));
    const botsDetail = !botsRequired
      ? "Optional"
      : botList && botList.length
        ? botSummary.text
        : repoStatus?.botConfigExists ? "Credentials found" : "Setup needed";
    const promoteRequired = Boolean(currentRepo()?.auto_promote);
    const pushAccess = state.branchPushAccess[state.activeRepoId];
    // A branch that does not restrict pushes lets the bots merge already.
    const promoteBlocked = promoteRequired && pushAccess?.state === "missing";
    const promoteDetail = !promoteRequired
      ? "Not enabled"
      : promoteBlocked
        ? `Not configured — allow bots to merge into ${pushAccess.branch}`
        : pushAccess?.state === "unconfigured" ? "Set up GitHub Apps first"
        : pushAccess?.state === "error" ? "Could not check"
        : pushAccess ? "Configured" : "Checking…";
    const checks = [
      ["GitHub CLI", Boolean(gh && toolReady(gh)), gh?.status || "Not detected"],
      ["AI provider", ais.some(toolReady), ais.some(toolReady) ? "Signed in" : "Sign in required"],
      ["Bot identities", botsReady, botsDetail],
      ["Worker runtime", repoStatus?.workerAvailable, repoStatus?.workerAvailable ? "Available" : "Unavailable"],
    ];
    if (promoteRequired) {
      checks.splice(3, 0, [
        "Bots can merge into main",
        ["allowed", "unrestricted", "unprotected"].includes(pushAccess?.state),
        promoteDetail,
      ]);
    }
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
    renderRepository(repoStatus?.repository);
    const warning = byId("config-warning");
    warning.replaceChildren();
    if (state.status.configError) {
      warning.append(`${state.status.configError} `);
      warning.appendChild(button("Open Repository →", "text-button", () => navigate("repository")));
    }
    warning.classList.toggle("hidden", !state.status.configError);
    renderBotPanel();
    renderControls();
    renderReadiness();
    renderNowWorking();
  }

  const NOW_WORKING_KINDS = { issue: "Issue", ci: "CI/CD", adversarial: "Adversarial UAT" };
  const NOW_WORKING_PILLS = { running: "Running", paused: "Paused", error: "Failing", ok: "Passing", idle: "Idle" };

  function nowWorkingRepositories() {
    const configured = new Map((state.config?.repositories || []).map((repo) => [repo.id, repo]));
    return (state.status?.repos || []).filter((repo) => repo.enabled).map((repo) => ({
      id: repo.id,
      name: normalizeRepoRef(repo.githubRepository),
      monitorActions: Boolean(configured.get(repo.id)?.monitor_actions),
    }));
  }

  function renderNowWorking() {
    const list = byId("now-working-list");
    if (!list) return;
    const rows = window.SwarmNowWorking.deriveNowWorking({
      logs: state.workerLogs,
      workerState: state.status?.issue?.state || "stopped",
      repositories: nowWorkingRepositories(),
    });
    const active = rows.filter((row) => row.state === "running").length;
    const count = byId("now-working-count");
    count.lastChild.textContent = `${active} ACTIVE`;
    list.replaceChildren();
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "now-working-empty";
      empty.append(
        Object.assign(document.createElement("strong"), { textContent: "Nothing is being worked on" }),
        Object.assign(document.createElement("span"), { textContent: "Issues, test runs, and CI fixes appear here only while they are in progress." }),
      );
      list.appendChild(empty);
      return;
    }
    const multiple = (state.config?.repositories || []).length > 1;
    rows.forEach((row) => {
      const item = document.createElement("div");
      item.className = `now-working-row ${row.kind}`;
      const kind = document.createElement("span");
      kind.className = "now-working-kind";
      kind.textContent = NOW_WORKING_KINDS[row.kind] || row.kind;
      const words = document.createElement("div");
      words.className = "now-working-words";
      const title = document.createElement("strong");
      title.textContent = row.title;
      const meta = document.createElement("span");
      const startedAt = row.startedAt ? `since ${new Date(row.startedAt * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "";
      meta.textContent = [multiple ? row.repository : "", row.detail, startedAt || (row.since ? `since ${row.since}` : "")].filter(Boolean).join(" · ");
      words.append(title, meta);
      const pill = document.createElement("span");
      pill.className = `status-pill ${row.state}`;
      pill.textContent = NOW_WORKING_PILLS[row.state] || row.state;
      item.append(kind, words, pill);
      list.appendChild(item);
    });
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

  function isWorkerLog(raw) {
    return /^\[[^\]]*\] \[issue worker[^\]]*\/[^/\]]+\]/i.test(String(raw));
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
      ? new Date(Number(rawTime) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: true })
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
    if (value.includes("setup") || value.includes("install") || value.includes("github bot")) return "setup";
    return "work";
  }

  function activitySourceLabel(source, category) {
    if (category === "setup") return source.toLowerCase().includes("github bot") ? "GitHub setup" : "Setup";
    return source.toLowerCase().includes("issue") ? "Issue worker" : "Automation";
  }

  function makeActivity(log, summary, description, tone = "info", category = activityCategory(log.source)) {
    const issueNumber = log.message.match(/\bissue\s*:?\s*#(\d+)/i)?.[1]
      || log.message.match(/\bissue-(\d+)/i)?.[1]
      || log.message.match(/\bissue\s+(\d+)/i)?.[1]
      || "";
    const branchName = log.message.match(
      /\b([A-Za-z0-9._-]+\/(?:claude|codex|xai|grok)\/issue-\d+)\b/i,
    )?.[1] || "";
    return {
      ...log,
      summary,
      description,
      tone,
      category,
      issueNumber,
      branchName,
      sourceLabel: activitySourceLabel(log.source, category),
    };
  }

  function githubUrl(repository, ...parts) {
    const segments = String(repository || "").trim().split("/");
    if (segments.length !== 2 || segments.some((segment) => !segment)) return "";
    return `https://github.com/${segments.map(encodeURIComponent).join("/")}${
      parts.length ? `/${parts.flatMap((part) => String(part).split("/")).map(encodeURIComponent).join("/")}` : ""
    }`;
  }

  function externalLink(label, url, className = "") {
    const link = document.createElement("a");
    link.href = url;
    link.dataset.external = url;
    link.className = className;
    link.textContent = label;
    link.title = `Open ${label} on GitHub`;
    return link;
  }

  // Overview is intentionally an allowlist of meaningful milestones. Routine
  // command output, paths, HTTP requests, usage text, and process details stay
  // out of both the activity feed and the actionable log.
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
    // The scheduler skips a whole cycle (no GitHub polling, no AI) when the
    // machine is busy streaming media, or when the managed workspace is not in a
    // safe state to touch. Every skip should be visible here, not silent.
    if (/transcode is active; deferring/i.test(message)) {
      return makeActivity(log, "Issue work paused while media is streaming", "A SWARM transcode is running on this machine, so the worker skipped this cycle to keep playback smooth. It resumes automatically once streaming stops.", "waiting", "work");
    }
    if (/could not fetch .*; deferring/i.test(message)) {
      return makeActivity(log, "Could not reach GitHub; cycle skipped", "The worker could not fetch the latest branches this cycle and will retry on the next one. Open Info & Debug for the exact error.", "waiting", "work");
    }
    if (/; deferring (?:the worker|this run|synchronization)/i.test(message)) {
      return makeActivity(log, "Issue work skipped this cycle", "The managed workspace was not in a safe state to start (uncommitted work, missing checkout, or Git unavailable). The worker will retry on the next cycle — see Info & Debug for the reason.", "waiting", "work");
    }
    if (/Another foreground SWARM issue runner is already active/i.test(message)) {
      return makeActivity(log, "Another issue worker is already running", "This start was ignored so two workers never run against the same repositories at once.", "waiting", "work");
    }
    if (/cargo target exceeds .*cleanup deferred/i.test(message)) {
      return makeActivity(log, "Build cache cleanup postponed", "The Rust build cache is over its size limit, but a build is in progress so cleanup was deferred to a later cycle.", "waiting", "work");
    }
    if (/traceback|permission denied|authentication failed|\berror:|\bfailed\b|\bcould not\b/i.test(message)) {
      return makeActivity(log, `${source} needs attention`, "Something prevented this step from finishing. Open Info & Debug for the exact error and command output.", "error", category);
    }
    if (/^Started .* as pid \d+/i.test(message)) {
      return makeActivity(log, `${source} started`, category === "setup" ? "The requested setup task is now running." : "The app is now watching the configured repositories for ready issues.", "info", category);
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
    const workingProvider = message.match(/^(Claude|Codex|Grok) is working/i)?.[1];
    if (workingProvider) {
      return makeActivity(log, `${workingProvider} is working`, "Detailed AI output is hidden here. The final summary will appear when the issue pass finishes.", "info", "work");
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
      return makeActivity(log, `Merged issue #${issueNumber} into the AI branch`, "The approved issue branch was combined into one commit and removed. The human-owned branch was not changed.", "success", "work");
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
    return null;
  }

  function renderActivity() {
    const feed = byId("overview-activity");
    if (!feed) return;
    const sourceLogs = state.activityPaused ? (state.activitySnapshot || []) : state.logs;
    const events = [];
    const repositoryBySource = new Map();
    const configuredRepositories = (state.config?.repositories || [])
      .map((repo) => normalizeRepoRef(repo.github_repository))
      .filter((repo) => repo.split("/").length === 2);
    sourceLogs.forEach((raw) => {
      const parsed = parseAutomationLog(raw);
      if (!parsed) return;
      const repositoryMarker = parsed.message.match(/^=== repo:\s*([^\s]+\/[^\s=]+)\s*===$/i)?.[1];
      if (repositoryMarker) repositoryBySource.set(parsed.source, normalizeRepoRef(repositoryMarker));
      const event = importantActivity(raw);
      if (!event) return;
      const namedRepository = parsed.message.match(/^([^:\s]+\/[^:\s]+):/)?.[1];
      event.repository = normalizeRepoRef(namedRepository || repositoryBySource.get(parsed.source) || "");
      if (!event.repository && configuredRepositories.length === 1) {
        [event.repository] = configuredRepositories;
      }
      if (/^Picked up issue #/i.test(event.summary) && event.repository) {
        event.summary += ` · ${event.repository}`;
      }
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
    }).slice(-24).reverse();

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
      meta.textContent = `${event.time} · ${event.sourceLabel}${event.repository ? ` · ${event.repository}` : ""}`;
      words.append(title, meta);
      const issueUrl = event.issueNumber
        ? githubUrl(event.repository, "issues", event.issueNumber)
        : "";
      if (/^Picked up issue #/i.test(event.summary) && issueUrl) {
        // Appended inside `words`, not as a fourth child of `summary` — the
        // summary row is a fixed 3-column grid (icon/words/expand); a link
        // living there directly would shove "Details" onto its own row.
        words.appendChild(externalLink("Open issue ↗", issueUrl, "activity-reference activity-quick-link"));
      }
      const expand = document.createElement("span");
      expand.className = "activity-expand";
      expand.textContent = "Details";
      summary.append(icon, words, expand);
      const description = document.createElement("p");
      description.textContent = event.description;
      item.append(summary, description);
      const references = document.createElement("div");
      references.className = "activity-links";
      const branchUrl = event.branchName
        ? githubUrl(event.repository, "tree", event.branchName)
        : "";
      if (issueUrl) {
        references.appendChild(externalLink(`Open issue #${event.issueNumber} ↗`, issueUrl, "activity-reference"));
      }
      if (branchUrl) {
        references.appendChild(externalLink(`Open branch ${event.branchName} ↗`, branchUrl, "activity-reference"));
      }
      if (references.childElementCount) item.appendChild(references);
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
    const search = state.logSearch.trim().toLowerCase();
    const entries = window.SwarmLogging.actionableLogEntries(state.logs);
    const filtered = entries.filter((entry) => {
      if (state.logFilter === "worker" && !entry.worker) return false;
      if (state.logFilter === "errors" && entry.level !== "error") return false;
      if (search && !`${entry.time} ${entry.source} ${entry.message}`.toLowerCase().includes(search)) return false;
      return true;
    });
    const full = byId("full-log");
    const stayAtTop = full.scrollTop <= 28;
    full.replaceChildren();
    if (!filtered.length) {
      const empty = document.createElement("p");
      empty.className = "log-empty";
      empty.textContent = entries.length ? "No actionable logs match this filter." : "Waiting for actionable output…";
      full.appendChild(empty);
    }
    filtered.reverse().forEach((entry) => {
      const line = document.createElement("div");
      line.className = `log-line ${entry.level}`;
      const time = document.createElement("span");
      time.className = "log-line-time";
      time.textContent = entry.time;
      const level = document.createElement("strong");
      level.className = "log-line-level";
      level.textContent = entry.level === "error" ? "ERROR" : "INFO";
      const message = document.createElement("span");
      message.className = "log-line-message";
      message.textContent = entry.message;
      const source = document.createElement("span");
      source.className = "log-line-source";
      source.textContent = entry.source;
      line.append(time, level, message, source);
      full.appendChild(line);
    });
    if (stayAtTop) full.scrollTop = 0;
    renderActivity();
    renderNowWorking();
    byId("log-count").textContent = String(Math.min(entries.length, 999));
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
    }, { progress: "Opening the repository picker…" });
  }

  async function setupBots() {
    await withBusy("bots", async () => {
      await saveBeforeAction();
      await invoke("launch_bot_setup", { repoId: currentRepo().id });
      showToast("A browser opened for any bot that still needs creating or installing. Choose “All repositories” on the install screen.", "success");
      pollBotReadiness();
    }, { progress: "Opening GitHub for bot setup…" });
  }

  async function recheckBots() {
    await withBusy("recheck-bots", async () => {
      await saveBeforeAction();
      await refreshBotReadiness({ quiet: false });
    }, { progress: "Re-checking bot readiness…" });
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
    }, { progress: "Verifying bot sign-in…" });
  }

  // ----- Software update ---------------------------------------------------

  async function refreshAppVersion() {
    try {
      const version = window.SwarmVersion.formatBuildVersion(await invoke("app_version"));
      byId("update-version-pill").textContent = version || "v0.0.0";
      const label = byId("app-version-label");
      label.textContent = version;
      if (version) {
        label.title = `Running build ${version}`;
        label.setAttribute("aria-label", `Running build ${version}`);
      } else {
        label.removeAttribute("title");
        label.removeAttribute("aria-label");
      }
    } catch (_) { /* unavailable outside a Tauri window */ }
  }

  function renderUpdateDetail(summary) {
    const detail = byId("update-detail");
    detail.replaceChildren();
    if (!summary) return;
    const row = document.createElement("div");
    row.className = "verification-result";
    row.textContent = summary.notes ? summary.notes.trim().split("\n")[0] : `Version ${summary.version} is ready to install.`;
    detail.appendChild(row);
    detail.appendChild(button("Install & restart", "primary-button compact", applyUpdate));
  }

  // Only the newest release and newest beta (`directInstall`) can be
  // installed from here today — see install_update_candidate's doc comment
  // on the Rust side. The rest of the 3+3 list is shown for context so a
  // version pick is never silently hidden, just explained.
  function renderUpdateCandidates(candidates) {
    const container = byId("update-candidates");
    container.replaceChildren();
    if (!candidates || !candidates.length) {
      container.classList.add("hidden");
      return;
    }
    container.classList.remove("hidden");
    const heading = document.createElement("p");
    heading.className = "panel-copy";
    heading.textContent = "Check Now: 3 most recent release builds and 3 most recent beta builds.";
    container.appendChild(heading);
    candidates.forEach((candidate) => {
      const row = document.createElement("div");
      row.className = "workspace-row";
      const label = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = `${candidate.version} · ${candidate.channel === "beta" ? "Beta" : "Release"}`;
      label.appendChild(title);
      const meta = document.createElement("small");
      const published = candidate.publishedAt ? new Date(candidate.publishedAt).toLocaleDateString() : "";
      let reason = "";
      if (!candidate.installable) {
        reason = "Older than the installed version — downgrading isn't supported yet.";
      } else if (!candidate.directInstall) {
        reason = "Context only — only the newest release and newest beta install directly today.";
      }
      meta.textContent = [published, reason].filter(Boolean).join(" — ");
      label.appendChild(meta);
      row.appendChild(label);
      const actions = document.createElement("div");
      actions.className = "control-row";
      const canInstall = candidate.installable && candidate.directInstall;
      const install = button(canInstall ? "Install" : "Not available", "secondary-button compact", () => installUpdateCandidate(candidate));
      install.disabled = !canInstall;
      actions.appendChild(install);
      row.appendChild(actions);
      container.appendChild(row);
    });
  }

  async function checkForUpdate({ quiet = false } = {}) {
    await withBusy("check-update", async () => {
      byId("update-status").textContent = "Checking for updates…";
      const [summary, candidates] = await Promise.all([
        invoke("check_for_update"),
        invoke("list_update_candidates").catch(() => []),
      ]);
      state.pendingUpdate = summary;
      if (summary) {
        byId("update-status").textContent = `Version ${summary.version} is available (you have ${summary.currentVersion}).`;
        renderUpdateDetail(summary);
        showUpdateBanner(summary);
      } else {
        byId("update-status").textContent = "You're on the latest version.";
        renderUpdateDetail(null);
        if (!quiet) showToast("SWARM Automation is up to date.", "success");
      }
      renderUpdateCandidates(candidates);
    }, { progress: "Checking for updates…" });
  }

  async function applyUpdate() {
    await withBusy("apply-update", async () => {
      await invoke("install_update"); // the process restarts on success
    }, { progress: "Downloading update… the app will restart when it's ready." });
  }

  async function installUpdateCandidate(candidate) {
    await withBusy("apply-update", async () => {
      await invoke("install_update_candidate", { tag: candidate.tag, channel: candidate.channel }); // restarts on success
    }, { progress: `Downloading ${candidate.version}… the app will restart when it's ready.` });
  }

  function showUpdateBanner(summary) {
    if (!summary) return;
    state.pendingUpdate = summary;
    byId("update-banner-text").textContent = `SWARM Automation ${summary.version} is available.`;
    byId("update-banner").classList.remove("hidden");
  }

  function addRepository() {
    stashRepositoryForm();
    const repo = defaultRepository();
    state.config.repositories.push(repo);
    state.activeRepoId = repo.id;
    renderRepositorySelector();
    bindRepositoryForm();
    renderStatus();
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
    renderRepositorySelector();
    bindRepositoryForm();
    renderStatus();
    setDirty();
  }

  function selectRepository(repoId) {
    stashRepositoryForm();
    state.activeRepoId = repoId;
    bindRepositoryForm();
    renderRepositorySelector();
    renderSummaries();
    renderStatus();
    if (document.querySelector("#view-repository.active")) void refreshBotReadiness({ quiet: true });
    if (document.querySelector("#view-repository.active")) void refreshBranchPushAccess({ quiet: true });
  }

  // ----- Repository promotion queue -------------------------------------------

  function renderPromotions() {
    const panel = byId("promotion-panel");
    const list = byId("promotion-list");
    if (!panel || !list) return;
    const promotions = state.promotions || [];
    panel.classList.toggle("hidden", promotions.length === 0);
    list.replaceChildren();
    promotions.forEach((promotion) => {
      const row = document.createElement("div");
      row.className = "promotion-row";
      const name = document.createElement("strong");
      name.textContent = promotion.label || promotion.githubRepository || promotion.repoId;
      const actions = document.createElement("div");
      actions.className = "promotion-actions";
      actions.appendChild(button(
        promotion.integrationPrUrl ? "Open PR" : "Create PR",
        "secondary-button compact",
        () => openPromotionPr(promotion),
      ));
      actions.appendChild(button(
        "Merge to Main",
        "primary-button compact",
        () => mergePromotion(promotion),
      ));
      const commits = `${promotion.integrationBranch} is ${promotion.ahead} commit${promotion.ahead === 1 ? "" : "s"} ahead of ${promotion.baseBranch}`
        + (promotion.behind > 0 ? ` · ${promotion.behind} behind` : "");
      const meta = document.createElement("span");
      meta.className = "promotion-meta";
      meta.textContent = promotion.integrationPrNumber
        ? `${commits} · PR #${promotion.integrationPrNumber} open`
        : commits;
      row.append(name, actions, meta);
      if (promotion.error) {
        const alert = document.createElement("span");
        alert.className = "branch-alert";
        alert.textContent = promotion.error;
        row.appendChild(alert);
      }
      list.appendChild(row);
    });
  }

  async function refreshPromotions({ quiet = true } = {}) {
    if (state.refreshing.promotions) return;
    state.refreshing.promotions = true;
    try {
      state.promotions = await invoke("promotion_overview_background");
      renderPromotions();
    } catch (error) {
      if (!quiet) showToast(errorText(error), "error");
    } finally {
      state.refreshing.promotions = false;
    }
  }

  async function openPromotionPr(promotion) {
    await withBusy(`promotion-${promotion.repoId}`, async () => {
      const url = await invoke("open_integration_pr", { repoId: promotion.repoId });
      showToast(`Promotion pull request ready: ${url}`, "success");
      void refreshPromotions({ quiet: true });
    }, { progress: "Preparing the promotion pull request…" });
  }

  async function mergePromotion(promotion) {
    const name = promotion.label || promotion.githubRepository || promotion.repoId;
    if (!window.confirm(`Merge ${promotion.baseBranch} into ${promotion.integrationBranch}, approve the promotion PR, and merge ${promotion.integrationBranch} into ${promotion.baseBranch} for ${name}?`)) return;
    await withBusy(`promotion-merge-${promotion.repoId}`, async () => {
      await invoke("promote_integration_branch_background", { repoId: promotion.repoId });
      showToast(`${name} was promoted to ${promotion.baseBranch}.`, "success");
      await refreshPromotions({ quiet: true });
    }, { progress: `Promoting ${name} to ${promotion.baseBranch}…` });
  }

  // ----- "What's wrong?" diagnostic modal ---------------------------------
  let diagnoseReturnFocus = null;
  let lastDiagnosis = null;

  function openDiagnoseModal() {
    diagnoseReturnFocus = document.activeElement;
    byId("diagnose-modal").hidden = false;
    byId("diagnose-modal-close").focus();
  }

  function closeDiagnoseModal() {
    byId("diagnose-modal").hidden = true;
    if (diagnoseReturnFocus && diagnoseReturnFocus.focus) diagnoseReturnFocus.focus();
    diagnoseReturnFocus = null;
  }

  function setDiagnoseModalState({ loading = false, unavailable = "" } = {}) {
    byId("diagnose-modal-loading").hidden = !loading;
    byId("diagnose-modal-unavailable").hidden = !unavailable;
    byId("diagnose-modal-unavailable").textContent = unavailable;
    byId("diagnose-modal-empty").hidden = true;
    if (loading || unavailable) byId("diagnose-modal-problems").replaceChildren();
  }

  function diagnosticProblemCard(problem) {
    const card = document.createElement("article");
    card.className = "panel";
    const header = document.createElement("div");
    header.className = "panel-header";
    const title = document.createElement("h3");
    title.textContent = problem.repository;
    header.appendChild(title);
    card.appendChild(header);

    const explanation = document.createElement("p");
    explanation.className = "panel-copy";
    explanation.textContent = problem.explanation || "No explanation was returned.";
    card.appendChild(explanation);

    const confidence = document.createElement("p");
    confidence.className = "availability";
    confidence.textContent = window.SwarmDebugDiagnose.confidenceLabel(problem);
    card.appendChild(confidence);

    const items = window.SwarmDebugDiagnose.actionableList(problem);
    if (items.length) {
      const list = document.createElement("ul");
      items.forEach((item) => {
        const row = document.createElement("li");
        row.textContent = item;
        list.appendChild(row);
      });
      card.appendChild(list);
    }

    const evidence = window.SwarmDebugDiagnose.evidenceList(problem);
    if (evidence.length) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "Evidence";
      details.appendChild(summary);
      evidence.forEach((entry) => {
        const pre = document.createElement("pre");
        pre.className = "debug-log";
        pre.textContent = `[${entry.source}]\n${entry.excerpt}`;
        details.appendChild(pre);
      });
      card.appendChild(details);
    }

    if (problem.filedIssueUrl) {
      const filed = document.createElement("p");
      filed.className = "panel-copy";
      filed.append("Filed as a GitHub issue: ");
      filed.appendChild(button(problem.filedIssueUrl, "text-button", () => openUrl(problem.filedIssueUrl)));
      card.appendChild(filed);
    } else if (window.SwarmDebugDiagnose.canFileIssue(problem)) {
      card.appendChild(
        button("Post as a GitHub issue", "secondary-button", () => fileDiagnosticIssue(problem))
      );
    }

    return card;
  }

  function renderDiagnoseResult(result) {
    setDiagnoseModalState({ loading: false, unavailable: "" });
    if (!result.aiAvailable) {
      byId("diagnose-modal-unavailable").hidden = false;
      byId("diagnose-modal-unavailable").textContent = window.SwarmDebugDiagnose.unavailableMessage(result);
    }
    const box = byId("diagnose-modal-problems");
    box.replaceChildren();
    if (window.SwarmDebugDiagnose.hasNoActiveProblems(result)) {
      byId("diagnose-modal-empty").hidden = false;
      return;
    }
    result.problems.forEach((problem) => box.appendChild(diagnosticProblemCard(problem)));
  }

  async function runDiagnostics() {
    openDiagnoseModal();
    setDiagnoseModalState({ loading: true });
    try {
      const result = await invoke("run_diagnostics_background");
      lastDiagnosis = result;
      renderDiagnoseResult(result);
    } catch (error) {
      setDiagnoseModalState({ loading: false, unavailable: errorText(error) });
    }
  }

  async function fileDiagnosticIssue(problem) {
    if (!window.confirm(window.SwarmDebugDiagnose.confirmFileIssuePrompt(problem))) return;
    await withBusy("file-diagnostic-issue", async () => {
      const filed = await invoke("file_diagnostic_issue_background", { problemId: problem.problemId });
      showToast(`Filed: ${filed.filedIssueUrl}`, "success");
      problem.filedIssueUrl = filed.filedIssueUrl;
      if (lastDiagnosis) renderDiagnoseResult(lastDiagnosis);
    }, { progress: "Filing the GitHub issue…" });
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
    document.querySelectorAll("#provider-cards .provider-card").forEach((card) => {
      const enabled = card.querySelector(".provider-enabled").checked;
      const radio = document.querySelector(`.provider-preferred[value="${card.dataset.provider}"]`);
      if (!radio) return;
      radio.disabled = !enabled;
      const note = radio.parentElement?.querySelector("small");
      if (note) {
        note.textContent = enabled
          ? "Preferred when remaining usage is tied."
          : "Turn this provider on to prefer it.";
      }
    });
    const checked = document.querySelector('input[name="preferred-provider"]:checked');
    if (!checked || checked.disabled) {
      const fallback = document.querySelector(".provider-preferred:not(.provider-preference-auto):not(:disabled)");
      (fallback || document.querySelector(".provider-preference-auto"))?.click();
    }
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
      if (event.key === "Escape" && !byId("diagnose-modal").hidden) closeDiagnoseModal();
      // Clickable panel headings are plain elements with role="button", not
      // real <button>s, so they need Enter/Space activation spelled out.
      const heading = event.target.closest("[data-help][role=\"button\"]");
      if (heading && (event.key === "Enter" || event.key === " ")) {
        event.preventDefault();
        openHelp(heading.dataset.help);
      }
    });
    byId("schedule-mode-select").addEventListener("change", (event) => selectSchedule(event.target.value));
    document.querySelectorAll("[data-config], [data-repo-config], [data-provider-bin], #days-field input").forEach((input) => {
      input.addEventListener("input", () => { setDirty(); renderSummaries(); });
      input.addEventListener("change", () => {
        setDirty();
        renderSummaries();
        if (input.id === "dynamic-model-routing") syncDynamicRoutingChrome();
      });
    });
    document.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => runAction(button.dataset.action)));
    document.querySelectorAll("[data-log-filter]").forEach((button) => button.addEventListener("click", () => {
      state.logFilter = button.dataset.logFilter;
      document.querySelectorAll("[data-log-filter]").forEach((candidate) => candidate.classList.toggle("active", candidate === button));
      renderLogs();
    }));
    byId("log-search-input").addEventListener("input", (event) => {
      state.logSearch = event.target.value;
      renderLogs();
    });
    document.querySelectorAll("[data-activity-filter]").forEach((button) => button.addEventListener("click", () => {
      state.activityFilter = button.dataset.activityFilter;
      renderActivity();
    }));
    byId("toggle-activity-live").addEventListener("click", () => {
      state.activityPaused = !state.activityPaused;
      state.activitySnapshot = state.activityPaused ? [...state.logs] : null;
      renderActivity();
    });
    byId("save-button").addEventListener("click", () => withBusy("save", () => saveConfig(), { progress: "Saving the configuration…" }));
    byId("hide-button").addEventListener("click", () => invoke("hide_to_tray").catch((error) => showToast(errorText(error), "error")));
    byId("refresh-tools").addEventListener("click", () => refreshTools());
    byId("diagnose-button").addEventListener("click", () => runDiagnostics());
    byId("diagnose-modal-close").addEventListener("click", closeDiagnoseModal);
    byId("diagnose-modal").addEventListener("click", (event) => {
      if (event.target === byId("diagnose-modal")) closeDiagnoseModal();
    });
    byId("refresh-execution-history").addEventListener("click", () => {
      void refreshPromptGrades();
      void refreshExecutionHistory();
    });
    byId("feedback-repo-chips").addEventListener("change", (event) => {
      const input = event.target.closest("[data-feedback-repo]");
      if (!input) return;
      const allIds = feedbackRepositories().map((repo) => repo.id);
      if (!input.dataset.feedbackRepo) {
        selectFeedbackRepositories(allIds);
        return;
      }
      const selected = new Set(state.feedbackRepoFilter);
      if (input.checked) selected.add(input.dataset.feedbackRepo);
      else selected.delete(input.dataset.feedbackRepo);
      selectFeedbackRepositories([...selected]);
    });
    const feedbackTabs = Array.from(document.querySelectorAll("[data-feedback-tab]"));
    feedbackTabs.forEach((button) => {
      button.addEventListener("click", () => showFeedbackTab(button.dataset.feedbackTab));
      button.addEventListener("keydown", (event) => {
        const steps = { ArrowRight: 1, ArrowLeft: -1, Home: "first", End: "last" };
        const step = steps[event.key];
        if (step === undefined) return;
        event.preventDefault();
        const current = feedbackTabs.indexOf(button);
        const index = step === "first" ? 0
          : step === "last" ? feedbackTabs.length - 1
          : (current + step + feedbackTabs.length) % feedbackTabs.length;
        showFeedbackTab(feedbackTabs[index].dataset.feedbackTab, { focus: true });
      });
    });
    showFeedbackTab(state.feedbackTab);
    const repositoryTabs = Array.from(document.querySelectorAll("[data-repository-tab]"));
    repositoryTabs.forEach((button) => {
      button.addEventListener("click", () => showRepositoryTab(button.dataset.repositoryTab));
      button.addEventListener("keydown", (event) => {
        const steps = { ArrowRight: 1, ArrowLeft: -1, Home: "first", End: "last" };
        const step = steps[event.key];
        if (step === undefined) return;
        event.preventDefault();
        const current = repositoryTabs.indexOf(button);
        const index = step === "first" ? 0
          : step === "last" ? repositoryTabs.length - 1
          : (current + step + repositoryTabs.length) % repositoryTabs.length;
        showRepositoryTab(repositoryTabs[index].dataset.repositoryTab, { focus: true });
      });
    });
    showRepositoryTab(state.repositoryTab);
    byId("prompt-grades-router-matrix").addEventListener("click", (event) => {
      const model = event.target.closest("[data-router-model]");
      if (model) {
        const page = promptGradesView();
        const selected = window.SwarmPromptGrades.toggleRouterModel(
          state.promptGradesRouter,
          state.promptGradesRouterModel,
          model.dataset.router,
          model.dataset.routerModel,
          page.routerMatrix,
        );
        state.promptGradesRouter = selected.router;
        state.promptGradesRouterModel = selected.model;
        state.promptGradesOffset = 0;
        showFeedbackTab("grades");
        void refreshPromptGrades({ quiet: true });
        return;
      }
      const row = event.target.closest("[data-router]");
      if (!row) return;
      const page = promptGradesView();
      state.promptGradesRouter = window.SwarmPromptGrades.toggleRouter(
        state.promptGradesRouter,
        row.dataset.router,
        page.routerMatrix,
      );
      state.promptGradesRouterModel = "";
      state.promptGradesOffset = 0;
      // Land on the grades themselves: the filter's whole point is the list.
      if (state.promptGradesRouter) showFeedbackTab("grades");
      void refreshPromptGrades({ quiet: true });
    });
    byId("prompt-grades-router-filter").addEventListener("click", (event) => {
      if (!event.target.closest("#prompt-grades-router-clear")) return;
      state.promptGradesRouter = "";
      state.promptGradesRouterModel = "";
      state.promptGradesOffset = 0;
      void refreshPromptGrades({ quiet: true });
    });
    byId("prompt-grades-bars").addEventListener("click", (event) => {
      const bar = event.target.closest("[data-grade]");
      if (!bar) return;
      state.promptGradesGrade = window.SwarmPromptGrades.toggleGrade(state.promptGradesGrade, bar.dataset.grade);
      state.promptGradesOffset = 0;
      void refreshPromptGrades({ quiet: true });
    });
    const gradeSearch = byId("prompt-grades-search");
    const queueGradeSearch = (immediate) => {
      state.promptGradesSearch = gradeSearch.value;
      state.promptGradesOffset = 0;
      clearTimeout(state.promptGradesSearchTimer);
      if (immediate) {
        void refreshPromptGrades({ quiet: true });
        return;
      }
      state.promptGradesSearchTimer = setTimeout(() => {
        state.promptGradesSearchTimer = null;
        state.promptGradesOffset = 0;
        void refreshPromptGrades({ quiet: true });
      }, 300);
    };
    gradeSearch.addEventListener("input", () => queueGradeSearch(false));
    gradeSearch.addEventListener("search", () => queueGradeSearch(true));
    byId("prompt-grades-prev").addEventListener("click", () => {
      const page = promptGradesView();
      const limit = Number(page.limit) || 10;
      state.promptGradesOffset = Math.max(0, (Number(page.offset) || 0) - limit);
      void refreshPromptGrades();
    });
    byId("prompt-grades-next").addEventListener("click", () => {
      const page = promptGradesView();
      const limit = Number(page.limit) || 10;
      const offset = Number(page.offset) || 0;
      const total = Number(page.total) || 0;
      if (offset + page.records.length >= total) return;
      state.promptGradesOffset = offset + limit;
      void refreshPromptGrades();
    });
    byId("import-execution-history").addEventListener("click", () => importExecutionHistory());
    byId("execution-history-sort").addEventListener("change", (event) => {
      state.executionHistorySort = event.target.value;
      state.executionHistoryOffset = 0;
      void refreshExecutionHistory();
    });
    const executionSearch = byId("execution-history-search");
    const queueExecutionSearch = (immediate) => {
      state.executionHistorySearch = executionSearch.value;
      state.executionHistoryOffset = 0;
      clearTimeout(state.executionHistorySearchTimer);
      if (immediate) {
        void refreshExecutionHistory({ quiet: true });
        return;
      }
      state.executionHistorySearchTimer = setTimeout(() => {
        state.executionHistorySearchTimer = null;
        state.executionHistoryOffset = 0;
        void refreshExecutionHistory({ quiet: true });
      }, 300);
    };
    executionSearch.addEventListener("input", () => queueExecutionSearch(false));
    executionSearch.addEventListener("search", () => queueExecutionSearch(true));
    byId("execution-history-prev").addEventListener("click", () => {
      const page = executionHistoryView();
      const limit = Number(page.limit) || 10;
      state.executionHistoryOffset = Math.max(0, (Number(page.offset) || 0) - limit);
      void refreshExecutionHistory();
    });
    byId("execution-history-next").addEventListener("click", () => {
      const page = executionHistoryView();
      const limit = Number(page.limit) || 10;
      const offset = Number(page.offset) || 0;
      const total = Number(page.total) || 0;
      if (offset + page.records.length >= total) return;
      state.executionHistoryOffset = offset + limit;
      void refreshExecutionHistory();
    });
    byId("active-repo-select").addEventListener("change", (event) => selectRepository(event.target.value));
    byId("add-repo").addEventListener("click", addRepository);
    byId("remove-repo").addEventListener("click", removeRepository);
    byId("prepare-workspace").addEventListener("click", prepareWorkspace);
    byId("reveal-workspace").addEventListener("click", revealWorkspace);
    byId("github-repository-input").addEventListener("blur", (event) => {
      const normalized = normalizeRepoRef(event.target.value);
      event.target.value = normalized ? githubUrl(normalized) : "";
      stashRepositoryForm();
      renderRepositorySelector();
      setDirty();
    });
    byId("check-update").addEventListener("click", () => checkForUpdate());
    byId("update-banner-install").addEventListener("click", applyUpdate);
    byId("update-banner-later").addEventListener("click", () => byId("update-banner").classList.add("hidden"));
    byId("setup-bots").addEventListener("click", setupBots);
    byId("recheck-bots").addEventListener("click", recheckBots);
    byId("verify-bots").addEventListener("click", verifyBots);
    byId("grant-bot-push-access").addEventListener("click", grantBotPushAccess);
    window.addEventListener("focus", () => {
      if (document.querySelector("#view-repository.active")) {
        void refreshBotReadiness({ quiet: true });
        void refreshBranchPushAccess({ quiet: true });
      }
    });
    byId("open-log-folder").addEventListener("click", () => invoke("open_automation_folder").catch((error) => showToast(errorText(error), "error")));
    byId("clear-log").addEventListener("click", () => {
      state.logs = [];
      if (state.activityPaused) state.activitySnapshot = [];
      renderLogs();
    });
  }

  async function initialize() {
    renderHelpConcepts();
    bindEvents();
    try {
      state.config = await invoke("get_config");
      bindConfig(state.config);
      renderWorkerProviderSummary();
      setDirty(false);
      state.logs = await invoke("get_recent_logs");
      // "Now working" replays the worker's own lines, so it keeps a separate,
      // deeper history: chatty test output must not push a long-running
      // issue's start line out of the 1000-line display buffer.
      state.workerLogs = (await invoke("get_recent_logs", { limit: 5000 })).filter(isWorkerLog);
      renderLogs();
      await listen("automation-log", (event) => {
        const line = formatLog(event.payload);
        state.logs.push(line);
        if (state.logs.length > 1000) state.logs.splice(0, state.logs.length - 1000);
        if (isWorkerLog(line)) {
          state.workerLogs.push(line);
          if (state.workerLogs.length > 5000) state.workerLogs.splice(0, state.workerLogs.length - 5000);
        }
        renderLogs();
      });
      await listen("update-available", (event) => showUpdateBanner(event.payload));
      await listen("system-permission-primed", (event) => showToast(event.payload));
      void refreshAppVersion();
      // Tool detection and repository inspection run independently. Keeping
      // them out of the startup await path prevents slow CLIs or network-backed
      // Git checks from freezing navigation and configuration editing.
      void refreshStatus();
      void refreshTools();
      void refreshBotReadiness({ quiet: true });
      void refreshBranchPushAccess({ quiet: true });
      void refreshPromotions({ quiet: true });
      window.setInterval(() => void refreshStatus({ quiet: true }), 2000);
      window.setInterval(() => {
        if (state.busy.size === 0 && document.querySelector("#view-repository.active")) {
          void refreshPromotions({ quiet: true });
        }
      }, 15000);
      window.addEventListener("focus", () => {
        if (state.busy.size === 0 && document.querySelector("#view-repository.active")) {
          void refreshPromotions({ quiet: true });
        }
      });
      document.addEventListener("visibilitychange", () => {
        if (!document.hidden && state.busy.size === 0 && document.querySelector("#view-repository.active")) {
          void refreshPromotions({ quiet: true });
        }
      });
      window.setInterval(() => {
        if (state.busy.size === 0 && !state.botReadinessPoll && document.querySelector("#view-repository.active")) {
          void refreshBotReadiness({ quiet: true });
        }
      }, 20000);
      window.setInterval(() => {
        if (state.busy.size === 0) void refreshTools({ quiet: true });
      }, 30000);
    } catch (error) {
      showToast(`Could not initialize the application: ${errorText(error)}`, "error");
    }
  }

  window.addEventListener("DOMContentLoaded", initialize);
})();
