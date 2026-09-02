#!/usr/bin/env python3
"""Register and install the private SWARM Claude/Codex/Grok GitHub Apps.

This runs a loopback-only setup page. GitHub requires the owner to approve each
app registration and installation in a signed-in browser; everything after
those approvals (manifest exchange, PEM storage, and config writing) is
automatic.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from github_app_auth import DEFAULT_CONFIG_PATH, GitHubAppAuth


PROVIDERS = {
    "claude": {"name": "Swarm Claude Bot", "description": "Claude automation for the SWARM repository."},
    "codex": {"name": "Swarm Codex Bot", "description": "Codex automation for the SWARM repository."},
    "grok": {"name": "Swarm Grok Bot", "description": "Grok automation for the SWARM repository."},
}


def write_private(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)
    path.chmod(0o600)


class SetupState:
    def __init__(
        self,
        repository: str,
        config_path: Path,
        port: int,
        providers: tuple[str, ...] | None = None,
    ) -> None:
        self.repository = repository
        self.repository_owner = repository.partition("/")[0]
        self.repository_owner_type = ""
        self.config_path = config_path
        self.port = port
        self.providers = providers or tuple(PROVIDERS)
        self.csrf = {provider: secrets.token_urlsafe(32) for provider in self.providers}
        self.config: dict[str, Any] = {}
        if config_path.exists():
            self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self.valid_installations: set[str] = set()
        self.complete = threading.Event()

    def detect_repository_owner(self) -> None:
        """Determine whether the target repository belongs to a user or an org."""
        urls = (
            f"https://api.github.com/repos/{urllib.parse.quote(self.repository, safe='/')}",
            f"https://api.github.com/users/{urllib.parse.quote(self.repository_owner)}",
        )
        for url in urls:
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "swarm-github-bot-setup",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    value = json.load(response)
            except (OSError, ValueError, urllib.error.HTTPError):
                continue
            owner = value.get("owner", value)
            owner_type = str(owner.get("type", ""))
            if owner_type in {"User", "Organization"}:
                self.repository_owner_type = owner_type
                return
        raise RuntimeError(
            f"Could not determine whether {self.repository_owner} is a GitHub user or organization"
        )

    def registration_url(self) -> str:
        if self.repository_owner_type == "Organization":
            owner = urllib.parse.quote(self.repository_owner)
            return f"https://github.com/organizations/{owner}/settings/apps/new"
        return "https://github.com/settings/apps/new"

    def app_name(self, provider: str) -> str:
        suffix = f" SWARM {provider.title()}"
        owner = self.repository_owner[: max(1, 34 - len(suffix))]
        return f"{owner}{suffix}"

    def refresh_complete(self) -> None:
        if all(item in self.valid_installations for item in self.providers):
            self.complete.set()
        else:
            self.complete.clear()

    def app_exists(self, provider: str) -> bool:
        entry = self.config.get(provider, {})
        key = Path(str(entry.get("private_key_path", ""))).expanduser()
        if not key.is_absolute():
            key = self.config_path.parent / key
        return int(entry.get("app_id", 0)) > 0 and key.is_file()

    def validate_existing(self) -> None:
        """Validate registrations and confirm each app can access this repository."""
        self.detect_repository_owner()
        auth = GitHubAppAuth(self.config_path)
        changed = False
        for provider in self.providers:
            if not self.app_exists(provider):
                continue
            try:
                auth.app_profile(provider)
            except (OSError, RuntimeError, ValueError) as error:
                print(
                    f"{PROVIDERS[provider]['name']} registration could not be authenticated: {error}",
                    file=sys.stderr,
                )
                continue
            try:
                installation_id = auth.find_installation_for_repository(
                    provider, self.repository
                )
            except (OSError, RuntimeError, ValueError):
                installation_id = None
            if installation_id:
                if int(self.config[provider].get("installation_id", 0)) != installation_id:
                    self.config[provider]["installation_id"] = installation_id
                    changed = True
                self.valid_installations.add(provider)
            else:
                print(
                    f"{PROVIDERS[provider]['name']} exists, but it cannot access {self.repository}; "
                    "the setup page will offer to reinstall it.",
                    file=sys.stderr,
                )
        if changed:
            self.save_config()
        self.refresh_complete()

    def discover_reinstallations(self) -> None:
        if self.complete.is_set() or not self.config_path.exists():
            return
        auth = GitHubAppAuth(self.config_path)
        changed = False
        for provider in self.providers:
            if provider in self.valid_installations or not self.app_exists(provider):
                continue
            try:
                installation_id = auth.find_installation_for_repository(provider, self.repository)
            except (OSError, RuntimeError, ValueError):
                continue
            if installation_id:
                self.config[provider]["installation_id"] = installation_id
                self.valid_installations.add(provider)
                changed = True
                print(
                    f"Discovered {PROVIDERS[provider]['name']} installation {installation_id} "
                    f"for {self.repository}.",
                    file=sys.stderr,
                )
        if changed:
            self.save_config()
        self.refresh_complete()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def manifest(self, provider: str) -> dict[str, Any]:
        definition = PROVIDERS[provider]
        return {
            "name": self.app_name(provider),
            "url": f"https://github.com/{self.repository}",
            "description": definition["description"],
            "redirect_url": f"{self.base_url}/callback?provider={provider}",
            # Public so one app can be installed on every organization the
            # operator points SWARM at, not just the account that created it.
            # Permissions are still granted per installation.
            "public": True,
            "request_oauth_on_install": False,
            "default_events": [],
            "default_permissions": {
                "contents": "write",
                "issues": "write",
                "pull_requests": "write",
                "workflows": "write",
            },
        }

    def save_app(self, provider: str, response: dict[str, Any]) -> None:
        slug = str(response["slug"])
        key_path = self.config_path.parent / f"{slug}.pem"
        write_private(key_path, str(response["pem"]))
        self.config[provider] = {
            "app_id": int(response["id"]),
            "installation_id": 0,
            "private_key_path": str(key_path),
            "bot_login": f"{slug}[bot]",
            "bot_name": PROVIDERS[provider]["name"],
        }
        self.valid_installations.discard(provider)
        self.save_config()

    def save_installation(self, provider: str, installation_id: int) -> None:
        self.config[provider]["installation_id"] = installation_id
        self.valid_installations.add(provider)
        self.save_config()
        self.refresh_complete()

    def confirm_installation(self, provider: str, installation_id: int) -> bool:
        """Only persist a callback installation after GitHub proves repo access."""
        auth = GitHubAppAuth(self.config_path)
        discovered = auth.find_installation_for_repository(provider, self.repository)
        if discovered != installation_id:
            return False
        self.save_installation(provider, discovered)
        return True

    def save_config(self) -> None:
        write_private(self.config_path, json.dumps(self.config, indent=2, sort_keys=True) + "\n")


class Handler(BaseHTTPRequestHandler):
    server: "SetupServer"

    def log_message(self, format: str, *args: object) -> None:
        print(f"GitHub App setup: {format % args}", file=sys.stderr)

    def send_html(self, content: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = (
            "<!doctype html><meta charset='utf-8'><title>SWARM GitHub Bots</title>"
            "<style>body{font:16px system-ui;max-width:760px;margin:60px auto;padding:0 24px}"
            "button,a.button{display:inline-block;padding:12px 18px;background:#24292f;color:white;"
            "border:0;border-radius:6px;text-decoration:none;font-weight:600}li{margin:18px 0}code{background:#eee;padding:2px 5px}</style>"
            f"{content}"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/":
            items = []
            for provider in self.server.state.providers:
                definition = PROVIDERS[provider]
                configured = provider in self.server.state.valid_installations
                app_exists = self.server.state.app_exists(provider)
                status = (
                    "Configured"
                    if configured
                    else "App exists; needs installing on this repository's owner"
                    if app_exists
                    else "Not created yet"
                )
                action = ""
                if not configured and app_exists:
                    login = str(self.server.state.config[provider].get("bot_login", ""))
                    slug = login.removesuffix("[bot]")
                    action = (
                        f"<a class='button' target='_blank' rel='noopener' "
                        f"href='https://github.com/apps/{urllib.parse.quote(slug)}/installations/new'>"
                        f"Install {html.escape(definition['name'])}</a>"
                    )
                elif not configured:
                    manifest = html.escape(json.dumps(self.server.state.manifest(provider)))
                    state = urllib.parse.quote(self.server.state.csrf[provider])
                    registration_url = html.escape(self.server.state.registration_url())
                    action = (
                        f"<form action='{registration_url}?state={state}' method='post'>"
                        f"<input type='hidden' name='manifest' value='{manifest}'>"
                        f"<button type='submit'>Create {html.escape(definition['name'])}</button></form>"
                    )
                items.append(
                    f"<li><strong>{html.escape(definition['name'])}</strong> — {status}<br>"
                    f"{action}</li>"
                )
            self.send_html(
                "<meta http-equiv='refresh' content='4'>"
                "<h1>Set up SWARM GitHub bots</h1><p>Create each bot app once, then install it on "
                f"<strong>{html.escape(self.server.state.repository_owner)}</strong> — the account that owns "
                f"<code>{html.escape(self.server.state.repository)}</code>. On the install screen choose "
                "<strong>All repositories</strong> so you never have to repeat this for another repo in the "
                "same account. This page rechecks automatically.</p><ol>{}</ol>".format("".join(items))
            )
            return
        if parsed.path == "/callback":
            provider = query.get("provider", [""])[0]
            code = query.get("code", [""])[0]
            state = query.get("state", [""])[0]
            if provider not in self.server.state.providers or state != self.server.state.csrf.get(provider) or not code:
                self.send_html("<h1>Invalid callback</h1>", HTTPStatus.BAD_REQUEST)
                return
            try:
                request = urllib.request.Request(
                    f"https://api.github.com/app-manifests/{urllib.parse.quote(code)}/conversions",
                    data=b"{}",
                    method="POST",
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Content-Type": "application/json",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "User-Agent": "swarm-github-bot-setup",
                    },
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    app = json.load(response)
                self.server.state.save_app(provider, app)
            except (OSError, KeyError, ValueError, urllib.error.HTTPError) as error:
                self.send_html(f"<h1>App creation failed</h1><pre>{html.escape(str(error))}</pre>", HTTPStatus.BAD_GATEWAY)
                return
            slug = str(app["slug"])
            self.send_html(
                f"<h1>{html.escape(PROVIDERS[provider]['name'])} created</h1>"
                "<p>The private key was stored locally with mode 0600. Now install the app on "
                f"<strong>{html.escape(self.server.state.repository_owner)}</strong> and choose "
                "<strong>All repositories</strong>.</p>"
                f"<a class='button' target='_blank' rel='noopener' "
                f"href='https://github.com/apps/{html.escape(slug)}/installations/new'>Install app</a> "
                "<a class='button' href='/'>Return to setup status</a>"
            )
            return
        if parsed.path == "/installed":
            provider = query.get("provider", [""])[0]
            installation = query.get("installation_id", [""])[0]
            if provider not in self.server.state.providers or not installation.isdigit():
                self.send_html("<h1>Invalid installation callback</h1>", HTTPStatus.BAD_REQUEST)
                return
            try:
                confirmed = self.server.state.confirm_installation(provider, int(installation))
            except (OSError, RuntimeError, ValueError) as error:
                self.send_html(
                    "<h1>Installation is not ready yet</h1>"
                    "<p>GitHub has not made this repository installation visible to the app yet. "
                    "Return to the setup page; it will keep checking automatically.</p>"
                    f"<pre>{html.escape(str(error))}</pre><a class='button' href='/'>Continue</a>",
                    HTTPStatus.ACCEPTED,
                )
                return
            if not confirmed:
                self.send_html(
                    "<h1>Installation does not match this repository</h1>"
                    f"<p>Install the app on <code>{html.escape(self.server.state.repository)}</code>, "
                    "then return to the setup page.</p><a class='button' href='/'>Continue</a>",
                    HTTPStatus.BAD_REQUEST,
                )
                return
            next_provider = next(
                (
                    item
                    for item in self.server.state.providers
                    if item not in self.server.state.valid_installations
                ),
                None,
            )
            if next_provider:
                self.send_html(
                    f"<h1>{html.escape(PROVIDERS[provider]['name'])} installed</h1>"
                    f"<p>Return to the setup page to create {html.escape(PROVIDERS[next_provider]['name'])}.</p>"
                    "<a class='button' href='/'>Continue</a>"
                )
            else:
                self.send_html("<h1>All enabled SWARM bots are configured</h1><p>You may close this tab.</p>")
            return
        self.send_html("<h1>Not found</h1>", HTTPStatus.NOT_FOUND)


class SetupServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], state: SetupState) -> None:
        super().__init__(address, Handler)
        self.state = state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.getenv("SWARM_GITHUB_REPOSITORY", "DotNetRockStar/swarm"))
    parser.add_argument("--config", type=Path, default=Path(os.getenv("SWARM_GITHUB_APPS_CONFIG", DEFAULT_CONFIG_PATH)))
    parser.add_argument("--port", type=int, default=0, help="Loopback port; 0 chooses an unused port")
    parser.add_argument("--no-open", action="store_true", help="Print the setup URL without opening a browser")
    parser.add_argument(
        "--provider",
        action="append",
        choices=tuple(PROVIDERS),
        help="Enabled provider to configure (repeatable; defaults to all providers)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = args.config.expanduser().resolve()
    providers = tuple(dict.fromkeys(args.provider or PROVIDERS))
    placeholder = SetupState(args.repository, config_path, args.port, providers)
    placeholder.validate_existing()
    if placeholder.complete.is_set():
        auth = GitHubAppAuth(config_path)
        for provider in providers:
            auth.token(provider)
            print(f"Authenticated {PROVIDERS[provider]['name']} successfully.")
        print(f"Configuration already complete at {config_path}")
        return 0
    server = SetupServer(("127.0.0.1", args.port), placeholder)
    server.timeout = 2
    placeholder.port = int(server.server_address[1])
    print(f"SWARM GitHub Bot setup: {placeholder.base_url}/")
    if not args.no_open:
        webbrowser.open(f"{placeholder.base_url}/")
    try:
        while not placeholder.complete.is_set():
            server.handle_request()
            placeholder.discover_reinstallations()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    auth = GitHubAppAuth(config_path)
    for provider in providers:
        auth.token(provider)
        print(f"Authenticated {PROVIDERS[provider]['name']} successfully.")
    print(f"Configuration saved to {config_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
