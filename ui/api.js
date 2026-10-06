(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmApi = api;
})(typeof window === "undefined" ? globalThis : window, () => {
  "use strict";

  // The one transport seam for `ui/`. Everything else calls `invoke(name, args)`
  // and `listen(event, cb)`; this module decides how they reach the backend:
  //
  //   desktop  window.__TAURI__ is present -> delegate to Tauri, unchanged.
  //   web      window.__TAURI__ is absent  -> fetch() for commands and
  //            EventSource (Server-Sent Events) for events.
  //
  // The web side is driven entirely by the two data tables below. A command or
  // event that has no row is not available on web: `invoke` rejects with an
  // `UnavailableOnWebError` and `listen` resolves a no-op unlisten, so the rest
  // of the UI keeps working. The web backend fills the tables in; nothing else
  // in `ui/` changes when it does.

  const API_BASE = "/api/v1";

  // Command name -> endpoint. One row per command the web backend serves:
  //   { method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE", path: "/repos/{repoId}/status" }
  // `{name}` in the path is filled from `args[name]` (URL-encoded) and consumed;
  // the remaining args go in the query string for GET/DELETE and as a JSON body
  // otherwise. `method` defaults to "POST".
  const COMMANDS = {};

  // Event name -> Server-Sent Events stream. One row per event the web backend
  // emits: { path: "/events/automation-log", sse: "automation-log" }. `sse` is
  // the SSE `event:` type to listen for and defaults to the event name. Each
  // message's `data` is JSON and becomes the callback's `payload`, the same
  // `{ payload }` shape Tauri delivers.
  const EVENTS = {};

  class UnavailableOnWebError extends Error {
    constructor(kind, name) {
      super(`${name} is not available in the web version yet.`);
      this.name = "UnavailableOnWebError";
      this.code = "not_available_on_web";
      this.kind = kind;
      this.target = name;
    }
  }

  class ApiError extends Error {
    constructor(message, status, body) {
      super(message);
      this.name = "ApiError";
      this.code = "api_error";
      this.status = status;
      this.body = body;
    }
  }

  function isUnavailableOnWeb(error) {
    return Boolean(error) && error.code === "not_available_on_web";
  }

  function has(table, key) {
    return Object.prototype.hasOwnProperty.call(table, key);
  }

  function buildRequest(base, route, args) {
    const method = String(route.method || "POST").toUpperCase();
    const rest = { ...(args || {}) };
    const path = String(route.path).replace(/\{(\w+)\}/g, (_, key) => {
      const value = rest[key];
      delete rest[key];
      return encodeURIComponent(value === undefined || value === null ? "" : String(value));
    });
    const init = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
    let url = `${base}${path}`;
    const keys = Object.keys(rest);
    if (method === "GET" || method === "DELETE") {
      const query = new URLSearchParams();
      keys.forEach((key) => {
        if (rest[key] === undefined || rest[key] === null) return;
        const value = rest[key];
        query.set(key, typeof value === "object" ? JSON.stringify(value) : String(value));
      });
      const text = query.toString();
      if (text) url += `${url.includes("?") ? "&" : "?"}${text}`;
    } else if (keys.length) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(rest);
    }
    return { url, init };
  }

  async function readBody(response) {
    if (response.status === 204) return null;
    const text = await response.text();
    if (!text) return null;
    try { return JSON.parse(text); } catch (_) { return text; }
  }

  function failureMessage(response, body) {
    if (typeof body === "string" && body.trim()) return body.trim();
    if (body && typeof body.error === "string") return body.error;
    if (body && typeof body.message === "string") return body.message;
    return `Request failed with status ${response.status}.`;
  }

  // `env` is injectable so both transports can be tested without a browser:
  //   tauri        the `window.__TAURI__` object, or undefined for web
  //   fetch / EventSource   the web transport's primitives
  //   commands / events     override the tables (tests, or a future backend)
  function createApi(env = {}) {
    const base = env.base === undefined ? API_BASE : env.base;
    const commands = env.commands || COMMANDS;
    const events = env.events || EVENTS;
    const tauri = env.tauri;
    const desktop = Boolean(tauri && tauri.core && tauri.event);

    async function invoke(name, args) {
      if (desktop) return tauri.core.invoke(name, args);
      if (!has(commands, name)) throw new UnavailableOnWebError("command", name);
      const fetchFn = env.fetch;
      if (typeof fetchFn !== "function") throw new ApiError("This browser cannot reach the SWARM Automation service.", 0, null);
      const { url, init } = buildRequest(base, commands[name], args);
      let response;
      try {
        response = await fetchFn(url, init);
      } catch (error) {
        throw new ApiError(`Could not reach the SWARM Automation service: ${error && error.message ? error.message : error}`, 0, null);
      }
      const body = await readBody(response);
      if (!response.ok) throw new ApiError(failureMessage(response, body), response.status, body);
      return body;
    }

    async function listen(event, callback) {
      if (desktop) return tauri.event.listen(event, callback);
      const noop = () => {};
      if (!has(events, event) || typeof env.EventSource !== "function") return noop;
      const route = events[event];
      const source = new env.EventSource(`${base}${route.path}`);
      source.addEventListener(route.sse || event, (message) => {
        let payload = message.data;
        try { payload = JSON.parse(message.data); } catch (_) { /* keep the raw text */ }
        callback({ event, payload });
      });
      return () => source.close();
    }

    return { invoke, listen, transport: desktop ? "tauri" : "web" };
  }

  // The default instance reads the browser globals at call time, so it works
  // whenever Tauri injects its bridge and needs nothing in Node.
  function current() {
    const win = typeof window === "undefined" ? undefined : window;
    return createApi({
      tauri: win && win.__TAURI__,
      fetch: win && typeof win.fetch === "function" ? win.fetch.bind(win) : undefined,
      EventSource: win && win.EventSource,
    });
  }

  return {
    API_BASE,
    COMMANDS,
    EVENTS,
    UnavailableOnWebError,
    ApiError,
    isUnavailableOnWeb,
    createApi,
    invoke: (name, args) => current().invoke(name, args),
    listen: (event, callback) => current().listen(event, callback),
    transport: () => current().transport,
  };
});
