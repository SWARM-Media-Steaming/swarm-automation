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

  // Rows derived from an adversarial stage's boundary logs rather than from
  // the issue queue. They hang off the same issue but are their own rows, so
  // "find the issue row" lookups must skip all of them, not just UAT's.
  const ADVERSARIAL_KINDS = new Set(["adversarial", "security"]);
  // Each adversarial agent emits the same four boundary logs under its own
  // label, so one parser serves every stage; adding a third agent is a row
  // here, not another branch in the replay chain below.
  const ADVERSARIAL_STAGES = [
    {
      label: "Adversarial UAT",
      kind: "adversarial",
      zero: "independent test run",
      zeroTitle: "Independent test run",
    },
    {
      label: "Adversarial Cybersecurity",
      kind: "security",
      zero: "independent security review",
      zeroTitle: "Independent security review",
    },
  ];

  function adversarialRound(message) {
    for (const stage of ADVERSARIAL_STAGES) {
      const head = message.match(new RegExp(`^${stage.label} for issue #(\\d+): (.+)\\.$`, "i"));
      if (!head) continue;
      const [, number, detail] = head;
      const rounds = [
        [/^starting fix\/re-test round (\d+) of (\d+)$/i, "Fix in progress"],
        [/^starting re-test for round (\d+) of (\d+)$/i, "Re-test in progress"],
        [/^fix applied in round (\d+) of (\d+)$/i, "Fix applied; re-test pending"],
      ];
      const assessment = detail.match(
        new RegExp(`^starting ${stage.zero} \\(round (\\d+) of (\\d+)\\)$`, "i"),
      );
      if (assessment) {
        return { stage, number, round: assessment[1], maximum: assessment[2],
          title: stage.zeroTitle, phase: `Round ${assessment[1]} of ${assessment[2]}` };
      }
      for (const [pattern, phase] of rounds) {
        const found = detail.match(pattern);
        if (found) {
          return { stage, number, round: found[1], maximum: found[2],
            title: `Fix/re-test round ${found[1]} of ${found[2]}`, phase };
        }
      }
      return null;
    }
    return null;
  }

  // The two terminal boundary logs every adversarial stage emits once its
  // review either fails outright or reaches a verdict. Neither is a round
  // update, so `adversarialRound` never matches them (it returns null once a
  // stage's label matches but no round phrasing does) — they need their own
  // parse so a failed or finished review does not keep showing as running.
  function adversarialTerminal(message) {
    for (const stage of ADVERSARIAL_STAGES) {
      const failed = message.match(new RegExp(`^${stage.label} for issue #(\\d+): review failed — (.+)$`, "i"));
      if (failed) return { stage, number: failed[1], type: "failed", reason: failed[2] };
      const completed = message.match(new RegExp(`^${stage.label} for issue #(\\d+): review completed with status (\\S+)\\.$`, "i"));
      if (completed) return { stage, number: completed[1], type: "completed", status: completed[2] };
    }
    return null;
  }

  function normalizeRepo(value) {
    return String(value || "").trim().replace(/\.git$/i, "").replace(/^\/+|\/+$/g, "");
  }

  function logRows(logs, repositories, workerState) {
    const known = repositories.map((repo) => normalizeRepo(repo.name)).filter((name) => name.split("/").length === 2);
    const items = new Map();
    const repoBySource = new Map();
    const lastStartedByRepository = new Map();
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
      if (repository) lastStartedByRepository.set(repository, item);
      lastStarted = item;
      return item;
    };
    const find = (repository, number) => {
      if (repository) {
        return items.get(`issue:${repository}#${number}`)
          || [...items.values()].find((item) => !ADVERSARIAL_KINDS.has(item.kind)
            && item.repository === repository && String(item.number) === String(number));
      }
      return [...items.values()].find((item) => !ADVERSARIAL_KINDS.has(item.kind) && String(item.number) === String(number));
    };
    const current = (repository) => {
      const item = repository ? lastStartedByRepository.get(repository) : lastStarted;
      return item && items.get(item.key) === item ? item : null;
    };
    const related = (repository, number) => [...items.values()].filter((item) =>
      String(item.number) === String(number) && (!repository || item.repository === repository));
    const updateAdversarial = (entry, repository, number, round, maximum, title, phase, kind = "adversarial") => {
      const key = `${kind}:${repository}#${number}`;
      const item = {
        kind,
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
      let adversarial;

      if (/exited with status|process stopped|Ctrl\+C received/i.test(message)) {
        if (repository) {
          clear((item) => item.repository === repository);
          lastStartedByRepository.delete(repository);
          if (lastStarted?.repository === repository) lastStarted = null;
        } else {
          items.clear();
          lastStartedByRepository.clear();
          lastStarted = null;
        }
      } else if (/Starting a cycle over/i.test(message)) {
        clear((item) => item.state === "running");
      } else if (/Starting a worker run/i.test(message)) {
        clear((item) => item.state === "running" && (!repository || item.repository === repository));
      } else if ((match = message.match(/Selected oldest unprocessed assigned issue:\s*#(\d+)\s*(.*)$/i))) {
        start(entry, repository, match[1], match[2], "Picked up from the queue");
      } else if ((match = message.match(/Selected issue #(\d+) for rework.*?:\s*(.*)$/i))) {
        start(entry, repository, match[1], match[2], "Working on follow-up feedback");
      } else if ((match = message.match(/Working CI failure issue #(\d+).*?:\s*(.*)$/i))) {
        start(entry, repository, match[1], match[2], "Fixing a failing pipeline", "ci");
      } else if ((match = message.match(/^Selected (Claude|Codex|Grok) model(?:\s+(.+?)\s+with effort\s+(.+?)\s+for this run\.)?/i))) {
        const item = current(repository);
        if (item) {
          item.provider = match[1];
          item.model = match[2] || "";
          item.effort = match[3] || "";
        }
      } else if ((match = message.match(/^Pinned (Claude|Codex|Grok) model\s+(.+?)\s+session\s+\S+\s+with effort\s+(.+?)\s+for this continuation\.$/i))) {
        if (lastStarted) {
          lastStarted.provider = match[1];
          lastStarted.model = match[2];
          lastStarted.effort = match[3];
        }
      } else if ((match = message.match(/^(Claude|Codex|Grok) is working/i))) {
        const item = current(repository);
        if (item) {
          item.provider = match[1];
          item.phase = `${match[1]} is writing the change`;
        }
      } else if ((adversarial = adversarialRound(message))) {
        updateAdversarial(entry, repository, adversarial.number, adversarial.round,
          adversarial.maximum, adversarial.title, adversarial.phase, adversarial.stage.kind);
      } else if ((adversarial = adversarialTerminal(message))) {
        const key = `${adversarial.stage.kind}:${repository}#${adversarial.number}`;
        if (adversarial.type === "failed") {
          const item = items.get(key) || [...items.values()].find((candidate) =>
            candidate.kind === adversarial.stage.kind && String(candidate.number) === adversarial.number
            && (!repository || candidate.repository === repository));
          if (item) {
            item.state = "error";
            item.phase = adversarial.reason;
          }
        } else {
          // A finished review (any verdict) is no longer active work; drop
          // its row rather than leave it looking like it is still running.
          clear((item) => item.kind === adversarial.stage.kind && String(item.number) === adversarial.number
            && (!repository || item.repository === repository));
        }
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
          if (ADVERSARIAL_KINDS.has(relatedItem.kind)) relatedItem.state = "paused";
        });
      } else if ((match = message.match(/Shelved quota-paused issue #(\d+)/i))) {
        const item = find(repository, match[1]);
        if (item) item.state = "paused";
        related(repository, match[1]).forEach((relatedItem) => {
          if (ADVERSARIAL_KINDS.has(relatedItem.kind)) relatedItem.state = "paused";
        });
      } else if ((match = message.match(/(?:preparing to resume.*?issue|restored session \S+ for issue|restored quota-paused issue) #(\d+)/i))) {
        const item = find(repository, match[1])
          || start(entry, repository, match[1], "", "Resuming saved session");
        item.state = "running";
        item.phase = "Resuming saved session";
        related(repository, match[1]).forEach((relatedItem) => {
          if (ADVERSARIAL_KINDS.has(relatedItem.kind)) relatedItem.state = "running";
        });
      } else if ((match = message.match(/Finished issue #(\d+)/i))) {
        // Issue numbers are repository-local.  A terminal line without a
        // repository label is unsafe to apply when several repositories are
        // configured, so leave the rows intact until an identified terminal
        // event arrives.
        if (repository) {
          clear((item) => String(item.number) === match[1] && item.repository === repository);
        }
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
          provider: item.provider || "",
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
