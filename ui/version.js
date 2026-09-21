(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmVersion = api;
})(typeof window === "undefined" ? globalThis : window, () => {
  "use strict";

  // The published build is whatever `app_version` reports, including a
  // `-beta.<run>` or `+main.<run>` suffix. Show that string unchanged, with
  // a single leading "v".
  function formatBuildVersion(raw) {
    const version = String(raw ?? "").trim();
    if (!version) return "";
    return /^v/i.test(version) ? version : `v${version}`;
  }

  return { formatBuildVersion };
});
