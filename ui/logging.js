(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmLogging = api;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  "use strict";

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
      message: match[4].replace(/^\[[^\]]+\]\s*/, "").replace(/\s+/g, " ").trim(),
    };
  }

  function actionableLogEntry(raw) {
    const log = parseAutomationLog(raw);
    if (!log) return null;
    const { message } = log;
    const nonzeroExit = message.match(/exited with status\s+(-?\d+)/i);
    const capacityPause = /could not verify .* usage for pinned issue #\d+/i.test(message);
    const filedFinding = /out-of-scope adversarial UAT finding/i.test(message);
    const isError = /^(?:ERROR|FATAL):|traceback|panic(?:ked)?\b|permission denied|authentication failed|unrecognized arguments/i.test(message)
      || (!capacityPause && !filedFinding && /\bcould not\b|\bfailed\b/i.test(message))
      || /deferring synchronization and AI \(left untouched for manual review\)/i.test(message)
      || (nonzeroExit && ![0, 10, 11, 12].includes(Number(nonzeroExit[1])));
    if (isError) {
      return {
        ...log,
        level: "error",
        message: message.replace(/^(?:ERROR|FATAL):\s*/i, "") || "Unknown error",
        worker: log.source.toLowerCase().includes("issue worker"),
      };
    }

    let concise = "";
    let match = message.match(/Selected issue #(\d+) for rework/i);
    if (match) concise = `Issue #${match[1]} rework started`;
    match ||= message.match(/Selected oldest unprocessed assigned issue:\s*#(\d+)/i);
    if (!concise && match) concise = `Issue #${match[1]} work started`;
    match = message.match(/Paused issue #(\d+) because (.+?) usage is unavailable/i);
    if (match) concise = `Issue #${match[1]} work paused — ${match[2]} usage unavailable`;
    match = message.match(/Could not verify (.+?) usage for pinned issue #(\d+)/i);
    if (match) concise = `Issue #${match[2]} work paused — ${match[1]} usage unavailable`;
    if (/no enabled provider .* has at least .* remaining|queued issue work is waiting for ai capacity/i.test(message)) {
      concise = "Issue work paused — waiting for AI usage";
    }
    match = message.match(/preparing to resume.*?issue #(\d+)/i)
      || message.match(/restored (?:session .*? for |quota-paused )?issue #(\d+)/i)
      || message.match(/issue #(\d+).*?(?:preparing to resume|usage is available again)/i);
    if (match) concise = `Issue #${match[1]} work resumed`;
    match = message.match(/(?:Committed completed issue|Finished issue) #(\d+)/i);
    if (match) concise = `Issue #${match[1]} work completed`;
    match = message.match(/Filed out-of-scope adversarial UAT finding for #(\d+):\s*(?:\S*\/issues\/(\d+)|(.+))/i);
    if (match) {
      concise = match[2]
        ? `Out-of-scope finding for #${match[1]} filed as issue #${match[2]}`
        : `Out-of-scope finding for #${match[1]} filed: ${match[3]}`;
    }
    match = message.match(/Out-of-scope adversarial UAT finding already filed for #(\d+):\s*(?:\S*\/issues\/(\d+)|(.+))/i);
    if (match) {
      concise = match[2]
        ? `Out-of-scope finding for #${match[1]} already filed as issue #${match[2]}`
        : `Out-of-scope finding for #${match[1]} already filed: ${match[3]}`;
    }
    if (/GitHub did not return an issue URL for the out-of-scope adversarial UAT finding/i.test(message)) {
      concise = "Out-of-scope adversarial UAT finding was filed, but GitHub did not return its issue URL";
    }
    if (!concise) return null;
    return {
      ...log,
      level: "info",
      message: concise,
      worker: log.source.toLowerCase().includes("issue worker"),
    };
  }

  function actionableLogEntries(lines) {
    const entries = [];
    lines.forEach((raw) => {
      const entry = actionableLogEntry(raw);
      if (!entry) return;
      const previous = entries[entries.length - 1];
      if (previous && previous.level === entry.level && previous.source === entry.source && previous.message === entry.message) {
        entries[entries.length - 1] = entry;
      } else {
        entries.push(entry);
      }
    });
    return entries;
  }

  return { actionableLogEntries, actionableLogEntry, parseAutomationLog };
});
