#!/usr/bin/env python3
"""Smoke-check a running hosted stack (``docker compose up`` or a deployment).

    python3 scripts/web_smoke.py [http://localhost:8080]

It needs no GitHub App and signs nothing in: it proves the API answers, the
shared ``ui/`` is served with its CSP, the safe defaults hold (unauthenticated
requests are refused, an unsigned webhook is refused, dotfiles and test files
are not served) and nothing answers 5xx. The sign-in and job flow is the manual
walk-through in docs/web-architecture.md ("Acceptance flow"). Standard library
only; exits 0 when every check passes.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Callable, NamedTuple


class Result(NamedTuple):
    name: str
    ok: bool
    detail: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib hook
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(base: str, path: str, method: str = "GET", body: bytes | None = None, headers: dict | None = None):
    """``(status, headers, body)``; HTTP errors are answers, not exceptions."""
    request = urllib.request.Request(base.rstrip("/") + path, data=body, method=method, headers=headers or {})
    try:
        with _OPENER.open(request, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def _json(raw: bytes):
    try:
        return json.loads(raw)
    except ValueError:
        return None


def checks(base: str) -> list[Result]:
    results: list[Result] = []

    def check(name: str, test: Callable[[], str | None]) -> None:
        try:
            problem = test()
        except OSError as error:  # connection refused, DNS, timeout
            problem = f"unreachable: {error}"
        results.append(Result(name, problem is None, problem or "ok"))

    def health():
        status, _, raw = fetch(base, "/api/v1/health")
        data = _json(raw) or {}
        if status != 200 or data.get("status") != "ok":
            return f"expected 200 status ok, got {status}"

    def ui_served():
        status, headers, raw = fetch(base, "/")
        if status != 200 or b"<html" not in raw.lower():
            return f"expected the UI at /, got {status}"
        policy = headers.get("Content-Security-Policy", "")
        if "default-src 'none'" not in policy or "unsafe-inline" in policy:
            return "the CSP is missing or allows inline code"
        if headers.get("X-Content-Type-Options", "").lower() != "nosniff":
            return "X-Content-Type-Options: nosniff is missing"

    def anonymous_session():
        status, _, raw = fetch(base, "/api/v1/session")
        data = _json(raw) or {}
        if status != 200 or data.get("authenticated") is not False:
            return f"an anonymous session must say authenticated=false, got {status}"
        if "csrf_token" in data or "tenants" in data:
            return "an anonymous session leaked account fields"

    def tenants_need_a_session():
        status, _, _ = fetch(base, "/api/v1/tenants")
        if status != 401:
            return f"expected 401 without a session, got {status}"

    def writes_need_a_session():
        status, _, _ = fetch(
            base, "/api/v1/tenants/t0000000000000000/config", "PUT", b"{}", {"Content-Type": "application/json"}
        )
        if status not in (401, 403):
            return f"expected 401/403 for an unauthenticated write, got {status}"

    def unsigned_webhook_refused():
        status, _, _ = fetch(
            base, "/api/v1/webhooks/github", "POST", b"{}",
            {"Content-Type": "application/json", "X-GitHub-Event": "ping", "X-GitHub-Delivery": "smoke"},
        )
        if status not in (400, 401, 403):
            return f"an unsigned webhook must be refused, got {status}"

    def unknown_api_is_json_404():
        status, headers, raw = fetch(base, "/api/v1/does-not-exist")
        if status != 404 or _json(raw) is None:
            return f"expected a JSON 404, got {status}"

    def private_files_not_served():
        for path in ("/.env", "/api.test.js", "/README.md"):
            status, _, _ = fetch(base, path)
            if status == 200:
                return f"{path} must not be served"

    for name, test in (
        ("health", health),
        ("ui served with a strict CSP", ui_served),
        ("anonymous session is empty", anonymous_session),
        ("tenants need a session", tenants_need_a_session),
        ("writes need a session", writes_need_a_session),
        ("unsigned webhook refused", unsigned_webhook_refused),
        ("unknown API path is a JSON 404", unknown_api_is_json_404),
        ("dotfiles and tests are not served", private_files_not_served),
    ):
        check(name, test)
    return results


def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 1 else "http://localhost:8080"
    results = checks(base)
    for result in results:
        print(f"{'ok  ' if result.ok else 'FAIL'} {result.name}" + ("" if result.ok else f": {result.detail}"))
    failed = [result for result in results if not result.ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed against {base}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
