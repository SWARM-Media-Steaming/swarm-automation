(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmRunNow = api;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  "use strict";

  // What the "Run now" button does for a service's current state, shared by
  // the click handler and the enable/disable rendering so the button can never
  // offer an action the handler would refuse.
  //
  //   "start"    — nothing is running: launch one cycle (--once).
  //   "request"  — the issue-worker scheduler is already running: ask it to
  //                scan every repository immediately and restart its timer,
  //                so an issue the user just readied is picked up now instead
  //                of at the next poll or scheduled window.
  //   "disabled" — nothing useful can happen (paused, busy, or unavailable).
  //
  // Only the issue worker can be interrupted this way.
  function runNowMode({ kind = "issue", processState = "stopped", available = true, busy = false } = {}) {
    if (busy || !available) return "disabled";
    if (processState === "stopped") return "start";
    if (kind === "issue" && processState === "running") return "request";
    return "disabled";
  }

  return { runNowMode };
});
