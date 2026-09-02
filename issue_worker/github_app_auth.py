#!/usr/bin/env python3
"""GitHub App authentication for SWARM's Claude, Codex, and Grok automations.

The module intentionally has no third-party dependencies. It signs the GitHub
App JWT with the system OpenSSL binary, exchanges it for a short-lived
installation token, and never writes that token to disk.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "swarm" / "github-apps.json"
API_VERSION = "2022-11-28"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _app_slug(bot_login: str) -> str:
    """`swarm-claude-bot[bot]` -> `swarm-claude-bot`, the slug in install URLs."""
    return bot_login.removesuffix("[bot]")


def _owner_of(repository: str | None) -> str | None:
    """`Owner/name` -> `Owner`; `None` for an unqualified or empty value."""
    if repository and "/" in repository:
        owner = repository.split("/", 1)[0].strip()
        return owner or None
    return None


def _request(method: str, url: str, bearer: str, payload: dict[str, Any] | None = None) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "swarm-issue-worker",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub returned HTTP {error.code}: {detail}") from error


@dataclass(frozen=True)
class AppDefinition:
    app_id: int
    installation_id: int
    private_key_path: Path
    bot_login: str
    bot_name: str
    bot_email: str
    # One app can be installed on several accounts (e.g. a personal org plus a
    # federation org). `installations` maps a casefolded owner login to its
    # installation ID; `installation_id` is the legacy single-owner fallback
    # used only when no repository owner is known.
    installations: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, provider: str, value: dict[str, Any], base_dir: Path) -> "AppDefinition":
        required = ("app_id", "private_key_path", "bot_login")
        missing = [key for key in required if not value.get(key)]
        if missing:
            raise ValueError(f"{provider} GitHub App config is missing: {', '.join(missing)}")
        key_path = Path(os.path.expanduser(str(value["private_key_path"])))
        if not key_path.is_absolute():
            key_path = base_dir / key_path
        login = str(value["bot_login"])
        raw_installations = value.get("installations")
        installations: dict[str, int] = {}
        if isinstance(raw_installations, dict):
            for owner, identifier in raw_installations.items():
                try:
                    numeric = int(identifier)
                except (TypeError, ValueError):
                    continue
                if numeric > 0:
                    installations[str(owner).casefold()] = numeric
        return cls(
            app_id=int(value["app_id"]),
            installation_id=int(value.get("installation_id") or 0),
            private_key_path=key_path,
            bot_login=login,
            bot_name=str(value.get("bot_name") or login.removesuffix("[bot]").replace("-", " ").title()),
            bot_email=str(value.get("bot_email") or ""),
            installations=installations,
        )


class GitHubAppAuth:
    """Loads per-provider app definitions and mints in-memory tokens."""

    def __init__(
        self,
        config_path: Path | str = DEFAULT_CONFIG_PATH,
        openssl_bin: str = "openssl",
        repository: str | None = None,
    ) -> None:
        self.config_path = Path(config_path).expanduser()
        self.openssl_bin = openssl_bin
        # A worker process handles exactly one repository, so the installation
        # to authenticate against is fixed for the life of this instance.
        self.repository = repository or None
        self._owner = _owner_of(repository)
        self._definitions: dict[str, AppDefinition] = {}
        self._tokens: dict[str, tuple[str, float]] = {}
        if self.config_path.exists():
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            for provider in ("claude", "codex", "grok"):
                if isinstance(raw.get(provider), dict):
                    try:
                        self._definitions[provider] = AppDefinition.from_mapping(
                            provider, raw[provider], self.config_path.parent
                        )
                    except ValueError:
                        # The setup assistant persists the app ID/key before
                        # installation completes. Treat that provider as not
                        # configured until an installation ID is added.
                        continue

    def configured(self, provider: str) -> bool:
        """Whether an app entry with at least one installation exists. Cheap and
        network-free; call `verify_installation` to confirm the bound
        repository's owner is actually covered."""
        definition = self._definitions.get(provider.lower())
        if not definition:
            return False
        return definition.installation_id > 0 or bool(definition.installations)

    def definition(self, provider: str) -> AppDefinition:
        key = provider.lower()
        try:
            definition = self._definitions[key]
        except KeyError as error:
            raise RuntimeError(
                f"No GitHub App is configured for {provider}; expected {self.config_path}"
            ) from error
        if not definition.private_key_path.is_file():
            raise RuntimeError(f"GitHub App private key was not found: {definition.private_key_path}")
        if definition.private_key_path.stat().st_mode & 0o077:
            raise RuntimeError(
                f"GitHub App private key permissions are too broad; run chmod 600 {definition.private_key_path}"
            )
        return definition

    def _jwt(self, definition: AppDefinition) -> str:
        now = int(time.time())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        claims = _b64url(
            json.dumps(
                {"iat": now - 60, "exp": now + 540, "iss": str(definition.app_id)},
                separators=(",", ":"),
            ).encode()
        )
        unsigned = f"{header}.{claims}".encode("ascii")
        signed = subprocess.run(
            [self.openssl_bin, "dgst", "-sha256", "-sign", str(definition.private_key_path)],
            input=unsigned,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if signed.returncode != 0:
            raise RuntimeError(
                f"OpenSSL could not sign the GitHub App JWT: {signed.stderr.decode(errors='replace').strip()}"
            )
        return f"{header}.{claims}.{_b64url(signed.stdout)}"

    def installation_id_for(self, provider: str) -> int:
        """Installation ID to authenticate the bound repository against.

        With a repository owner known, resolve it from the `installations` map,
        discovering and persisting it on a miss. Without one, fall back to the
        legacy single `installation_id`.
        """
        key = provider.lower()
        definition = self.definition(key)
        owner = self._owner
        if owner is None:
            if definition.installation_id > 0:
                return definition.installation_id
            raise RuntimeError(f"GitHub App {provider} has not been installed on a repository yet")
        existing = definition.installations.get(owner.casefold())
        if existing:
            return existing
        discovered = self.find_installation_for_owner(key, owner)
        if discovered:
            self._remember_installation(key, owner, discovered)
            return discovered
        raise RuntimeError(
            f"GitHub App '{definition.bot_login}' is not installed on '{owner}' "
            f"(needed for {self.repository}). Install it at "
            f"https://github.com/apps/{_app_slug(definition.bot_login)}/installations/new, "
            f"grant it access to the repository, then rerun."
        )

    def verify_installation(self, provider: str) -> int:
        """Resolve and cache the bound repository's installation now, raising a
        clear error if the app is not installed on its owner. Call once at
        startup so failures surface before any GitHub write is attempted."""
        return self.installation_id_for(provider)

    def token(self, provider: str) -> str:
        key = provider.lower()
        installation_id = self.installation_id_for(key)
        cache_key = f"{key}:{installation_id}"
        cached = self._tokens.get(cache_key)
        if cached and cached[1] > time.time() + 120:
            return cached[0]
        definition = self.definition(key)
        response = _request(
            "POST",
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            self._jwt(definition),
            {},
        )
        token = str(response["token"])
        # GitHub installation tokens currently last one hour. Cache for at most
        # 50 minutes so clock skew cannot leak an expired token into a long run.
        self._tokens[cache_key] = (token, time.time() + 3000)
        return token

    def find_installation_for_owner(self, provider: str, owner: str) -> int | None:
        """Installation ID of this app on `owner`, or None if it is not
        installed there. One API call, no per-installation token minting."""
        definition = self.definition(provider)
        jwt = self._jwt(definition)
        installations = _request(
            "GET", "https://api.github.com/app/installations?per_page=100", jwt
        )
        target = owner.casefold()
        for installation in installations:
            account = installation.get("account") or {}
            if str(account.get("login", "")).casefold() == target:
                identifier = int(installation.get("id", 0))
                return identifier or None
        return None

    def _remember_installation(self, provider: str, owner: str, installation_id: int) -> None:
        """Cache a resolved installation in memory and, best-effort, back into
        the config file so the next run skips discovery."""
        definition = self._definitions.get(provider)
        if definition is not None:
            merged = dict(definition.installations)
            merged[owner.casefold()] = installation_id
            self._definitions[provider] = replace(definition, installations=merged)
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            entry = raw.get(provider)
            if not isinstance(entry, dict):
                return
            stored = entry.get("installations")
            if not isinstance(stored, dict):
                stored = {}
            stored[owner] = installation_id
            entry["installations"] = stored
            raw[provider] = entry
            temporary = self.config_path.with_name(self.config_path.name + ".tmp")
            temporary.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self.config_path)
        except (OSError, json.JSONDecodeError):
            # The in-memory cache still serves this run; persistence is a
            # convenience, not a correctness requirement.
            pass

    def app_profile(self, provider: str) -> dict[str, Any]:
        """Return the app registration, proving the saved app ID/key still match."""
        definition = self.definition(provider)
        return dict(_request("GET", "https://api.github.com/app", self._jwt(definition)))

    def installation_covers_repository(self, provider: str, installation_id: int) -> bool:
        """Whether `installation_id` grants access to the bound repository.

        An "All repositories" installation always does; a "Only select
        repositories" one only if this repo is in its list.
        """
        if not self.repository:
            return True
        definition = self.definition(provider)
        jwt = self._jwt(definition)
        info = _request(
            "GET", f"https://api.github.com/app/installations/{installation_id}", jwt
        )
        if str(info.get("repository_selection", "")).lower() == "all":
            return True
        response = _request(
            "POST",
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            jwt,
            {},
        )
        token = str(response["token"])
        target = self.repository.casefold()
        repositories = _request(
            "GET", "https://api.github.com/installation/repositories?per_page=100", token
        )
        return any(
            str(item.get("full_name", "")).casefold() == target
            for item in repositories.get("repositories", [])
        )

    def repository_status(self, provider: str) -> dict[str, Any]:
        """Structured readiness of `provider`'s app for the bound repository, for
        the desktop UI to render a checklist. Never raises; failures come back as
        ``state == "error"``. A ``ready`` result also persists the resolved
        installation so the worker skips discovery on its next run."""
        key = provider.lower()
        definition = self._definitions.get(key)
        slug = _app_slug(definition.bot_login) if definition else f"swarm-{key}-bot"
        owner = self._owner or ""
        result: dict[str, Any] = {
            "provider": key,
            "owner": owner,
            "repository": self.repository or "",
            "slug": slug,
            "installUrl": f"https://github.com/apps/{slug}/installations/new",
            "installationId": 0,
        }
        if definition is None or (
            definition.installation_id <= 0 and not definition.installations
        ):
            return {
                **result,
                "state": "unconfigured",
                "message": f"The {key} bot app has not been created yet.",
            }
        bot = definition.bot_login
        if not owner:
            if definition.installation_id > 0:
                return {
                    **result,
                    "installationId": definition.installation_id,
                    "state": "ready",
                    "message": f"{bot} is configured.",
                }
            return {
                **result,
                "state": "not_installed_on_owner",
                "message": f"{bot} has no installation configured.",
            }
        try:
            installation_id = definition.installations.get(owner.casefold())
            if not installation_id:
                installation_id = self.find_installation_for_owner(key, owner)
            if not installation_id:
                return {
                    **result,
                    "state": "not_installed_on_owner",
                    "message": f"{bot} is not installed on {owner}.",
                }
            result["installationId"] = installation_id
            if not self.installation_covers_repository(key, installation_id):
                return {
                    **result,
                    "state": "no_repo_access",
                    "message": (
                        f"{bot} is installed on {owner} but has not been granted "
                        f"{self.repository}. Re-run the install and choose "
                        "“All repositories”."
                    ),
                }
        except (OSError, RuntimeError, ValueError) as error:
            return {**result, "state": "error", "message": str(error)}
        self._remember_installation(key, owner, installation_id)
        return {
            **result,
            "state": "ready",
            "message": f"{bot} can act on {self.repository}.",
        }

    def find_installation_for_repository(self, provider: str, repository: str) -> int | None:
        """Discover a replacement installation ID for an existing app.

        This repairs configs after an app was uninstalled/reinstalled without
        requiring the user to copy an installation ID out of a browser URL.
        """
        definition = self.definition(provider)
        jwt = self._jwt(definition)
        installations = _request(
            "GET", "https://api.github.com/app/installations?per_page=100", jwt
        )
        target = repository.casefold()
        owner = repository.split("/", 1)[0].casefold() if "/" in repository else ""
        for installation in installations:
            installation_id = int(installation.get("id", 0))
            if installation_id <= 0:
                continue
            account = installation.get("account") or {}
            # An "All repositories" install on the owning account covers this
            # repo without a per-installation token round-trip.
            if (
                owner
                and str(account.get("login", "")).casefold() == owner
                and str(installation.get("repository_selection", "")).lower() == "all"
            ):
                return installation_id
            response = _request(
                "POST",
                f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                jwt,
                {},
            )
            token = str(response["token"])
            repositories = _request(
                "GET", "https://api.github.com/installation/repositories?per_page=100", token
            )
            if any(
                str(item.get("full_name", "")).casefold() == target
                for item in repositories.get("repositories", [])
            ):
                return installation_id
        return None

    def bot_environment(self, provider: str) -> dict[str, str]:
        definition = self.definition(provider)
        email = definition.bot_email
        if not email:
            token = self.token(provider)
            profile = _request(
                "GET",
                f"https://api.github.com/users/{urllib.parse.quote(definition.bot_login, safe='')}",
                token,
            )
            email = f"{profile['id']}+{definition.bot_login}@users.noreply.github.com"
        return {
            "GH_TOKEN": self.token(provider),
            "GITHUB_TOKEN": self.token(provider),
            "GIT_AUTHOR_NAME": definition.bot_name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": definition.bot_name,
            "GIT_COMMITTER_EMAIL": email,
        }

    def completion_authors(self) -> set[str]:
        return {definition.bot_login for definition in self._definitions.values()}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(os.getenv("SWARM_GITHUB_APPS_CONFIG", DEFAULT_CONFIG_PATH)))
    parser.add_argument("--openssl-bin", default=os.getenv("OPENSSL_BIN", "openssl"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("identity", "check"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--provider", required=True, choices=("claude", "codex", "grok"))
    status = subparsers.add_parser(
        "repo-status", help="Print JSON readiness of a provider's app for a repository"
    )
    status.add_argument("--provider", required=True, choices=("claude", "codex", "grok"))
    status.add_argument(
        "--repository", default=os.getenv("SWARM_GITHUB_REPOSITORY", ""), help="owner/name"
    )
    execute = subparsers.add_parser("exec", help="Run a command as the selected bot")
    execute.add_argument("--provider", required=True, choices=("claude", "codex", "grok"))
    execute.add_argument("command_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repository = getattr(args, "repository", "") or None
    auth = GitHubAppAuth(args.config, args.openssl_bin, repository=repository)
    if args.command == "repo-status":
        try:
            print(json.dumps(auth.repository_status(args.provider)))
        except (OSError, RuntimeError, ValueError) as error:
            print(json.dumps({"provider": args.provider, "state": "error", "message": str(error)}))
        return 0
    if args.command == "identity":
        identity = auth.bot_environment(args.provider)
        identity.pop("GH_TOKEN", None)
        identity.pop("GITHUB_TOKEN", None)
        print(json.dumps(identity, indent=2, sort_keys=True))
    elif args.command == "check":
        definition = auth.definition(args.provider)
        auth.token(args.provider)
        print(f"{args.provider}: authenticated as {definition.bot_login}")
    else:
        command = list(args.command_args)
        if command and command[0] == "--":
            command.pop(0)
        if not command:
            raise ValueError("exec requires a command after --")
        environment = os.environ.copy()
        environment.pop("SWARM_SMTP_PASSWORD", None)
        environment.update(auth.bot_environment(args.provider))
        if Path(command[0]).name == "git":
            # Make HTTPS git operations use the installation token without
            # embedding it in argv, a remote URL, or persistent Git config.
            with tempfile.TemporaryDirectory(prefix="swarm-git-askpass.") as temporary:
                askpass = Path(temporary) / "askpass.sh"
                askpass.write_text(
                    "#!/bin/sh\n"
                    "case \"$1\" in\n"
                    "  *Username*) printf '%s\\n' x-access-token ;;\n"
                    "  *) printf '%s\\n' \"$SWARM_GITHUB_APP_PUSH_TOKEN\" ;;\n"
                    "esac\n",
                    encoding="utf-8",
                )
                askpass.chmod(0o700)
                environment.update(
                    {
                        "GIT_ASKPASS": str(askpass),
                        "GIT_TERMINAL_PROMPT": "0",
                        "SWARM_GITHUB_APP_PUSH_TOKEN": environment["GH_TOKEN"],
                    }
                )
                return subprocess.run(command, env=environment, check=False).returncode
        return subprocess.run(command, env=environment, check=False).returncode
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
