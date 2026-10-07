(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmLanding = api;
})(typeof window === "undefined" ? globalThis : window, () => {
  "use strict";

  // The hosted landing page's "Ask about SWARM" assistant. It is deliberately
  // not a model: every answer is a fixed paragraph chosen from `TOPICS` by
  // keyword scoring, so it can only ever return text written here. It makes no
  // request of any kind (no fetch, no URL, no test run, no integration call),
  // never produces code, and refuses anything that is not a question about how
  // the app works. No DOM; `app.js` renders what `reply` returns.
  const MAX_QUESTION_LENGTH = 400;

  const SUGGESTIONS = [
    "What does SWARM Automation do?",
    "How does an issue become a pull request?",
    "Which AI providers does it use?",
    "Is it safe to try?",
  ];

  const TOPICS = [
    {
      id: "overview",
      keywords: ["what", "swarm", "automation", "app", "does", "overview", "about", "purpose", "product", "do"],
      answer:
        "SWARM Automation turns your GitHub issues into reviewed pull requests. You label an issue, an AI worker (Claude, Codex or Grok) implements it on its own branch, and the result comes back as a pull request with a plain-language summary. You stay in control of what gets merged.",
    },
    {
      id: "workflow",
      keywords: ["workflow", "issue", "pull", "request", "pr", "branch", "work", "works", "process", "steps", "flow", "deliver", "delivery", "commit", "merge"],
      answer:
        "The flow is: a trusted author files or labels a GitHub issue, SWARM grades it and picks the model, the worker implements it on an issue branch, optional adversarial reviews test it, and the commit is pushed and opened as a pull request. The issue thread gets a comment at each step (started, completed, paused, or needs your input), so you always know the state without reading logs.",
    },
    {
      id: "providers",
      keywords: ["provider", "providers", "claude", "codex", "grok", "model", "models", "ai", "llm", "openai", "anthropic", "xai"],
      answer:
        "SWARM works with three providers: Claude, Codex and Grok. You bring your own provider keys. They are stored write-only and sealed, and a job only ever receives the one key it needs.",
    },
    {
      id: "routing",
      keywords: ["routing", "router", "cost", "costs", "cheap", "cheapest", "price", "pricing", "expensive", "complexity", "effort", "dynamic", "budget", "quota", "usage", "spend"],
      answer:
        "Dynamic model routing scores each issue against the size and complexity of your repository, then picks the least expensive model and reasoning effort that is capable of the task. You can pin a model manually, cap the model per provider, and set budgets and a minimum remaining quota so work pauses instead of overspending.",
    },
    {
      id: "adversarial",
      keywords: ["adversarial", "adversarially", "uat", "test", "tests", "testing", "quality", "review", "reviewed", "cybersecurity", "security", "bugs", "verify", "verified"],
      answer:
        "Two optional independent reviewers can check each change before delivery. Adversarial UAT writes and runs fresh tests against the change, and the Adversarial Cybersecurity agent looks for vulnerabilities. Each has fresh context and never sees the implementer's reasoning. Problems found are fixed and re-tested before the pull request is opened.",
    },
    {
      id: "safety",
      keywords: ["safe", "safety", "secure", "trust", "trusted", "privacy", "private", "secret", "secrets", "credential", "credentials", "keys", "data", "tenant", "isolation", "risk", "control", "confidence", "protect"],
      answer:
        "Your work is isolated per tenant. Provider keys are write-only and never shown again, jobs run in their own containers with a repository-scoped GitHub token, and only trusted authors can direct the AI. Nothing merges without your configured approval rules, and every change lands on a branch you can review first.",
    },
    {
      id: "start",
      keywords: ["start", "started", "begin", "setup", "set", "install", "sign", "login", "log", "account", "join", "try", "trial", "get", "onboard", "onboarding"],
      answer:
        "Getting started takes a few minutes: sign in with GitHub, install the SWARM GitHub App on the repositories you want, add a provider key, and label an issue. Use the Sign in with GitHub button on this page to begin. You do not need to change your existing workflow.",
    },
    {
      id: "knowledge",
      keywords: ["knowledge", "architecture", "documentation", "docs", "history", "feedback", "jev", "learn", "memory", "analytics", "report", "dashboard", "visibility"],
      answer:
        "Beyond implementing issues, SWARM keeps an engineering knowledge base, can maintain interactive architecture documentation for a repository, and records execution history, per-prompt token usage and costs. Jev, a fast advisory decision layer, helps with small workflow decisions but never overrides Swarm's safety gates.",
    },
    {
      id: "desktop",
      keywords: ["desktop", "hosted", "web", "cloud", "local", "mac", "windows", "tauri", "self", "run", "runs", "where"],
      answer:
        "SWARM Automation is available as a desktop control center and as this hosted web version. Both run the same worker, so the behavior, comments and pull requests look the same either way.",
    },
  ];

  // Requests to do something rather than to learn something, and anything that
  // asks for code, a fetch, a test run or a way around the rules.
  const REFUSAL_PATTERNS = [
    /\b(write|generate|give|show|produce|create|draft|print|output|paste)\b[^.?!]{0,40}\b(code|script|snippet|function|program|regex|query|sql|payload|exploit|command)\b/i,
    /\b(run|execute|launch|trigger|start)\b[^.?!]{0,30}\b(tests?|scripts?|commands?|jobs?|workflow|build|scan|exploit)\b/i,
    /\b(fetch|visit|open|browse|curl|wget|ping|request|call|download|crawl)\b[^.?!]{0,30}(\bhttps?:|\burl\b|\bwebsite\b|\bendpoint\b|\bapi\b|\bsite\b|\bwww\.)/i,
    /https?:\/\/|\bwww\./i,
    /\b(ignore|disregard|forget|override|bypass|jailbreak)\b[^.?!]{0,40}\b(instructions?|rules?|prompt|guardrails?|restrictions?|system)\b/i,
    /\b(system prompt|developer message|pretend|roleplay|act as|you are now)\b/i,
    /\b(hack|malware|ransomware|phish|ddos|sql injection|xss|keylogger|steal|exfiltrat\w*)\b/i,
    /\b(api key|password|token|private key)\b[^.?!]{0,20}\b(is|are|here|:)/i,
    /```|<script|<\/|\bsudo\b|\brm -rf\b|\bselect\b[^.?!]{0,20}\bfrom\b/i,
  ];

  const REFUSAL_REPLY =
    "I can only answer questions about how SWARM Automation works. I can't write code, run anything, open links or take actions. Try asking how issues become pull requests, which providers it supports, or how it keeps your work safe.";
  const OFF_TOPIC_REPLY =
    "I can only answer questions about SWARM Automation and how it works. Try asking what it does, how the workflow runs, how model routing keeps costs down, or what you need to get started.";
  const EMPTY_REPLY = "Type a question about SWARM Automation and I will explain how it works.";
  const LONG_REPLY = `Please keep your question under ${MAX_QUESTION_LENGTH} characters.`;

  function words(text) {
    return String(text || "").toLowerCase().match(/[a-z0-9]+/g) || [];
  }

  function bestTopic(question) {
    const asked = new Set(words(question));
    let best = null;
    let bestScore = 0;
    TOPICS.forEach((topic) => {
      const score = topic.keywords.reduce((sum, keyword) => sum + (asked.has(keyword) ? (GENERIC.has(keyword) ? 0.1 : 1) : 0), 0);
      if (score > bestScore) { best = topic; bestScore = score; }
    });
    // Generic words ("what", "do", "get") only break ties: alone they are not evidence of a topic.
    return bestScore >= 1 ? best : null;
  }

  const GENERIC = new Set(["what", "does", "do", "get", "set", "log", "run", "runs", "where", "about", "work", "works", "app", "data", "ai", "web", "local", "self", "start", "started", "steps"]);

  // `{ kind, text, topic }`; kind is "answer", "refusal", "off-topic", "empty"
  // or "too-long". Pure: the same question always returns the same text.
  function reply(question) {
    const text = String(question == null ? "" : question).trim();
    if (!text) return { kind: "empty", text: EMPTY_REPLY, topic: null };
    if (text.length > MAX_QUESTION_LENGTH) return { kind: "too-long", text: LONG_REPLY, topic: null };
    if (REFUSAL_PATTERNS.some((pattern) => pattern.test(text))) return { kind: "refusal", text: REFUSAL_REPLY, topic: null };
    if (/^(hi|hello|hey|thanks|thank you)\b/i.test(text) && words(text).length <= 3) {
      return { kind: "answer", text: "Hi! Ask me anything about how SWARM Automation works: the workflow, model routing, safety, or how to get started.", topic: "greeting" };
    }
    const topic = bestTopic(text);
    if (!topic) return { kind: "off-topic", text: OFF_TOPIC_REPLY, topic: null };
    return { kind: "answer", text: topic.answer, topic: topic.id };
  }

  return { MAX_QUESTION_LENGTH, SUGGESTIONS, TOPICS, reply };
});
