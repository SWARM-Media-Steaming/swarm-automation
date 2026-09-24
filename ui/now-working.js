(function (root, factory) {
  const logging = typeof module === "object" && module.exports ? require("./logging.js") : root && root.SwarmLogging;
  const api = factory(logging);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmNowWorking = api;
})(typeof globalThis !== "undefined" ? globalThis : this, (logging) => {
  "use strict";

  // Derives the "Now working" overview dashboard: one row per issue the worker
  // is on right now, and a CI/CD issue the worker is fixing. Idle processes
  // and finished checks are omitted — a worker that is only polling the queue
  // is not current work.
  //
  // Issues and CI come from replaying the worker's log lines in order — the
  // worker has no structured "current work" channel.

  function normalizeRepo(value) {
    return String(value || "").trim().replace(/\.git$/i, "").replace(/^\/+|\/+$/g, "");
  }

  function logRows(logs, repositories, workerState) {
    const known = repositories.map((repo) => normalizeRepo(repo.name)).filter((name) => name.split("/").length === 2);
    const items = new Map();
    const repoBySource = new Map();
    let lastStarted = null;

    const resolveRepo = (source, message, label) => {
      const named = label || message.match(/^([^:\s]+\/[^:\s]+):/)?.[1];
      return normalizeRepo(named || repoBySource.get(source) || (known.length === 1 ? known[0] : ""));
    };
    const clear = (predicate) => {
      [...items.entries()].forEach(([key, item]) => { if (predicate(item)) items.delete(key); });
    };
    const start = (entry, repository, number, title, phase, kind = "issue") => {
      const key = `${repository}#${number}`;
      const item = {
        kind,
        key: `${kind}:${key}`,
        number,
        repository,
        title: title ? `#${number} ${title}` : `#${number}`,
        phase,
        provider: "",
        state: "running",
        since: entry.time,
      };
      items.set(`${kind}:${key}`, item);
      lastStarted = item;
    };
    const find = (repository, number) => items.get(`issue:${repository}#${number}`)
      || [...items.values()].find((item) => item.kind !== "adversarial" && String(item.number) === String(number));
    const related = (repository, number) => [...items.values()].filter((item) =>
      String(item.number) === String(number) && (!repository || item.repository === repository));
    const updateAdversarial = (entry, repository, number, round, maximum, title, phase) => {
      const key = `adversarial:${repository}#${number}`;
      const item = {
        kind: "adversarial",
        key,
        number,
        repository,
        title,
        phase,
        state: "running",
        since: entry.time,
      };
      items.set(key, item);
    };

    (logs || []).forEach((raw) => {
      const entry = logging.parseAutomationLog(raw);
      if (!entry || !/issue worker/i.test(entry.source)) return;
      const { source } = entry;
      // Parallel-repo runs prefix each worker line with "[owner/repo] " ahead
      // of the worker's own timestamp; drop every leading bracket group.
      const message = entry.message.replace(/^(?:\[[^\]]*\]\s*)+/, "");
      const label = entry.raw.match(/^\[[^\]]*\] \[.*?\/[^/\]]+\] \[([^\]\s]+\/[^\]\s]+)\]/)?.[1];
      const marker = message.match(/^=== repo:\s*([^\s]+\/[^\s=]+)\s*===$/i)?.[1];
      if (marker) {
        repoBySource.set(source, normalizeRepo(marker));
        return;
      }
      const repository = resolveRepo(source, message, label);
      let match;

      if (/exited with status|process stopped|Ctrl\+C received/i.test(message)) {
        items.clear();
        lastStarted = null;
      } else if (/Starting (?:a worker run|a cycle over)/i.test(message)) {
        clear((item) => item.state === "running");
      } else if ((match = message.match(/Selected oldest unprocessed assigned issue:\s*#(\d+)\s*(.*)$/i))) {
        start(entry, repository, match[1], match[2], "Picked up from the queue");
      } else if ((match = message.match(/Selected issue #(\d+) for rework.*?:\s*(.*)$/i))) {
        start(entry, repository, match[1], match[2], "Working on follow-up feedback");
      } else if ((match = message.match(/Working CI failure issue #(\d+).*?:\s*(.*)$/i))) {
        start(entry, repository, match[1], match[2], "Fixing a failing pipeline", "ci");
      } else if ((match = message.match(/^Selected (Claude|Codex|Grok) model(?:\s+(.+?)\s+with effort\s+(.+?)\s+for this run\.)?/i))) {
        if (lastStarted) {
          lastStarted.provider = match[1];
          lastStarted.model = match[2] || "";
          lastStarted.effort = match[3] || "";
        }
      } else if ((match = message.match(/^Pinned (Claude|Codex|Grok) model\s+(.+?)\s+session\s+\S+\s+with effort\s+(.+?)\s+for this continuation\.$/i))) {
        if (lastStarted) {
          lastStarted.provider = match[1];
          lastStarted.model = match[2];
          lastStarted.effort = match[3];
        }
      } else if ((match = message.match(/^(Claude|Codex|Grok) is working/i))) {
        if (lastStarted) {
          lastStarted.provider = match[1];
          lastStarted.phase = `${match[1]} is writing the change`;
        }
      } else if ((match = message.match(/^Adversarial UAT for issue #(\d+): starting fix\/re-test round (\d+) of (\d+)\.$/i))) {
        updateAdversarial(entry, repository, match[1], match[2], match[3], `Fix/re-test round ${match[2]} of ${match[3]}`, "Fix in progress");
      } else if ((match = message.match(/^Adversarial UAT for issue #(\d+): starting independent test run \(round (\d+) of (\d+)\)\.$/i))) {
        updateAdversarial(entry, repository, match[1], match[2], match[3], "Independent test run", `Round ${match[2]} of ${match[3]}`);
      } else if ((match = message.match(/^Adversarial UAT for issue #(\d+): starting re-test for round (\d+) of (\d+)\.$/i))) {
        updateAdversarial(entry, repository, match[1], match[2], match[3], `Fix/re-test round ${match[2]} of ${match[3]}`, "Re-test in progress");
      } else if ((match = message.match(/^Adversarial UAT for issue #(\d+): fix applied in round (\d+) of (\d+)\.$/i))) {
        updateAdversarial(entry, repository, match[1], match[2], match[3], `Fix/re-test round ${match[2]} of ${match[3]}`, "Fix applied; re-test pending");
      } else if ((match = message.match(/(?:Created issue branch|Continuing issue|Recreated interrupted issue branch).*?(?:#|issue-)(\d+)/i))) {
        const item = find(repository, match[1]);
        if (item) item.phase = "Issue branch ready";
      } else if ((match = message.match(/Committed completed issue #(\d+)/i))) {
        const item = find(repository, match[1]);
        if (item) item.phase = "Delivering the pull request";
      } else if ((match = message.match(/Paused issue #(\d+) because (\S+) usage is unavailable/i))
        || (match = message.match(/Could not verify (\S+) usage for pinned issue #(\d+)/i))) {
        const paused = /^Paused/i.test(message);
        const number = paused ? match[1] : match[2];
        const providerName = paused ? match[2] : match[1];
        const item = find(repository, number);
        if (item) {
          item.state = "paused";
          item.phase = `Waiting for ${providerName} usage`;
        }
        related(repository, number).forEach((relatedItem) => {
          if (relatedItem.kind === "adversarial") relatedItem.state = "paused";
        });
      } else if ((match = message.match(/Shelved quota-paused issue #(\d+)/i))) {
        const item = find(repository, match[1]);
        if (item) item.state = "paused";
        related(repository, match[1]).forEach((relatedItem) => {
          if (relatedItem.kind === "adversarial") relatedItem.state = "paused";
        });
      } else if ((match = message.match(/preparing to resume.*?issue #(\d+)/i))) {
        const item = find(repository, match[1])
          || (start(entry, repository, match[1], "", "Resuming saved session"), lastStarted);
        item.state = "running";
        item.phase = "Resuming saved session";
        related(repository, match[1]).forEach((relatedItem) => {
          if (relatedItem.kind === "adversarial") relatedItem.state = "running";
        });
      } else if ((match = message.match(/Finished issue #(\d+)/i))) {
        items.delete(`issue:${repository}#${match[1]}`);
        clear((item) => String(item.number) === match[1]);
      } else if (/Returned the clean local checkout/i.test(message)) {
        clear((item) => item.state === "running" && (!repository || item.repository === repository));
      }
    });

    const rows = [];
    if (["running", "paused"].includes(workerState)) {
      items.forEach((item) => {
        rows.push({
          kind: item.kind,
          key: item.key,
          title: item.title,
          detail: [item.provider, item.model, item.effort && `${item.effort} effort`, item.phase].filter(Boolean).join(" · "),
          repository: item.repository,
          state: item.state,
          since: item.since,
          issueNumber: item.number,
        });
      });
    }
    return rows;
  }

  function deriveNowWorking({ logs = [], workerState = "stopped", repositories = [] } = {}) {
    // A pipeline the worker is fixing is already a row from "Working CI failure
    // issue". A finished Actions check is a result, so it is not listed here.
    return logRows(logs, repositories, workerState);
  }

  return { deriveNowWorking };
});
