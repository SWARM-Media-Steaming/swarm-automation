#!/usr/bin/env python3
"""One-time, re-runnable import of a desktop install into a tenant.

Reads what a SWARM Automation desktop install keeps on disk and writes it into
one tenant of a ``Storage`` (the hosted Postgres/S3 backend, or a local state
directory):

* **configuration** - ``config.json`` becomes a ``tenant_config`` document
  ``app`` plus one ``repo-<id>`` document per monitored repository. Settings
  that only mean something on that machine (paths, executables, the GitHub App
  key file location) and anything credential-shaped are left out;
* **architecture documentation** - each ``architecture_docs/<repo>.json``
  snapshot, redacted again on the way in;
* **execution history** - the ``ai_executions`` family, per-round/epoch
  records, token usage and Jev decisions, through
  ``ExecutionHistoryStore.import_records`` (the history sanitizer applies).

Provider credentials are never imported: this tool never opens the keychain,
the GitHub App key files or a provider CLI's login; tenants enter their API keys
in the web app.

Idempotent: history rows keep their ids, documents already present are not
rewritten (``--overwrite`` replaces them), so running it twice changes nothing.
``--dry-run`` performs the whole import and rolls it back, so its report is what
a real run would do. See ``docs/desktop-import.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping, Sequence

import ai_execution_history as history
import architecture_docs
from storage import (
    DEFAULT_TENANT,
    HISTORY_DATABASE_NAME,
    LocalStorage,
    Storage,
    StorageError,
    validate_name,
    validate_tenant,
)
from storage_factory import open_storage

CONFIG_COLLECTION = "tenant_config"
ARCHITECTURE_COLLECTION = "architecture_docs"
APP_KEY = "app"
SECTIONS = ("config", "architecture", "history")
CONFIG_SCHEMA = 1

# Settings that only describe the desktop machine.
LOCAL_ONLY_APP_KEYS = frozenset({
    "workspace_root", "worker_state_dir", "gh_bin", "python_bin", "jev_bin",
    "terminal_automation_permission_primed", "repo_dir", "github_apps_config",
    "claude_bin", "codex_bin", "profile_name",
})
LOCAL_ONLY_PROVIDER_KEYS = frozenset({"bin"})
LOCAL_ONLY_REPO_KEYS = frozenset({"repo_dir", "github_apps_config"})
# Pre-multi-repository settings the desktop folds into ``repositories[0]``.
LEGACY_REPO_KEYS = (
    "github_repository", "assignee", "trusted_followup_authors", "completion_authors", "ready_label",
    "base_branch", "remote_name", "github_host", "require_bot_auth", "auto_approve", "auto_merge",
    "require_issue_tests", "adversarial_uat_enabled", "adversarial_security_enabled",
    "adversarial_best_effort_merge", "update_claude_assets_enabled", "architecture_docs_enabled",
    "allow_environment_only_summary", "branch_prefix",
)
LEGACY_APP_ONLY_KEYS = frozenset({*LEGACY_REPO_KEYS, "claude_model", "claude_effort", "codex_model", "codex_effort"})

_SECRET_FRAGMENTS = ("secret", "password", "passwd", "api_key", "apikey", "credential", "private_key", "authorization")
_SECRET_NAMES = frozenset({"token", "access_token", "auth_token", "refresh_token", "bearer", "pem"})
_PEM = re.compile(r"-----BEGIN [^-]*(?:PRIVATE KEY|CREDENTIALS)[^-]*-----")


class ImportProblem(Exception):
    """Something the operator must fix before the import can run."""


# -- configuration ---------------------------------------------------------------------


def repo_slug(repository: str) -> str:
    """The desktop's ``repo_slug``: ``owner/name`` -> ``owner__name``."""
    return "".join(
        char if char.isascii() and (char.isalnum() or char in "_-.") else "-"
        for char in repository.strip().replace("/", "__")
    )


def _credential_like(name: str) -> bool:
    lowered = name.strip().lower().replace("-", "_")
    return (
        lowered in _SECRET_NAMES
        or any(fragment in lowered for fragment in _SECRET_FRAGMENTS)
        or (lowered.endswith("_token") and not lowered.endswith(("_limit", "_count")))
    )


def strip_credentials(value: Any, path: str = "") -> tuple[Any, list[str]]:
    """``value`` without credential-shaped keys or key blocks, plus the paths
    dropped (names only; a dropped value is never reported)."""
    dropped: list[str] = []
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            where = f"{path}.{key}" if path else str(key)
            if _credential_like(str(key)):
                dropped.append(where)
                continue
            clean[str(key)], inner = strip_credentials(item, where)
            dropped.extend(inner)
        return clean, dropped
    if isinstance(value, list):
        items = []
        for index, item in enumerate(value):
            cleaned, inner = strip_credentials(item, f"{path}[{index}]")
            items.append(cleaned)
            dropped.extend(inner)
        return items, dropped
    if isinstance(value, str):
        if _PEM.search(value):
            return "", [path]
        return history.sanitize_text(value), dropped
    return value, dropped


def read_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ImportProblem(f"Desktop configuration not found: {path}") from None
    except (OSError, ValueError) as error:
        raise ImportProblem(f"Desktop configuration is unreadable: {path}: {error}") from None
    if not isinstance(value, dict):
        raise ImportProblem(f"Desktop configuration is not a JSON object: {path}")
    return value


def config_documents(config: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """``({document key: document}, accounting)`` for a desktop ``config.json``."""
    repositories = [dict(repo) for repo in config.get("repositories") or [] if isinstance(repo, Mapping)]
    if not repositories and config.get("github_repository"):
        legacy = {key: config[key] for key in LEGACY_REPO_KEYS if key in config}
        legacy["id"] = repo_slug(str(config["github_repository"]))
        repositories = [legacy]
    left_out: list[str] = []
    dropped: list[str] = []

    app: dict[str, Any] = {}
    for key, value in config.items():
        if key == "repositories" or key in LEGACY_APP_ONLY_KEYS:
            continue
        if key in LOCAL_ONLY_APP_KEYS:
            left_out.append(key)
            continue
        if key == "providers" and isinstance(value, list):
            providers = []
            for provider in value:
                if isinstance(provider, Mapping):
                    left_out.extend(f"providers.{provider.get('id', '?')}.{k}" for k in provider if k in LOCAL_ONLY_PROVIDER_KEYS)
                    providers.append({k: v for k, v in provider.items() if k not in LOCAL_ONLY_PROVIDER_KEYS})
            value = providers
        app[key] = value
    app, inner = strip_credentials(app)
    dropped.extend(inner)

    documents = {APP_KEY: _config_document(app)}
    for repo in repositories:
        repository = str(repo.get("github_repository") or "").strip()
        identifier = str(repo.get("id") or repo_slug(repository))
        if not identifier:
            continue
        left_out.extend(f"repositories.{identifier}.{name}" for name in repo if name in LOCAL_ONLY_REPO_KEYS)
        settings, inner = strip_credentials(
            {name: value for name, value in repo.items() if name not in LOCAL_ONLY_REPO_KEYS},
            f"repositories.{identifier}",
        )
        dropped.extend(inner)
        settings["id"] = identifier
        documents[_repo_key(identifier)] = _config_document(settings)
    return documents, {"left_out": sorted(left_out), "credentials_dropped": sorted(dropped)}


def _config_document(settings: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema": CONFIG_SCHEMA, "source": "desktop-import", "settings": dict(settings)}


def _repo_key(identifier: str) -> str:
    key = "repo-" + identifier
    try:
        return validate_name(key, "repository config key")
    except StorageError:
        return "repo-" + hashlib.sha256(identifier.encode()).hexdigest()[:24]


# -- sources ---------------------------------------------------------------------------------


def _redact_tree(value: Any) -> Any:
    if isinstance(value, str):
        return architecture_docs.redact(value, max(len(value), 1))
    if isinstance(value, list):
        return [_redact_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_tree(item) for key, item in value.items()}
    return value


def read_architecture_documents(directory: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    documents: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    if not directory.is_dir():
        return documents, skipped
    for entry in sorted(directory.glob("*.json")):
        try:
            validate_name(entry.stem, "document key")
            value = json.loads(entry.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("schema"), int):
                raise ValueError("not an architecture snapshot")
        except (OSError, ValueError, StorageError):
            skipped.append(entry.name)
            continue
        documents[entry.stem] = _redact_tree(value)
    return documents, skipped


def read_history_tables(database_path: Path) -> tuple[dict[str, list[dict[str, Any]]], int | None]:
    """Rows of the history tables from a *copy* of the desktop database (the
    source is never opened for writing, and a write-ahead log is honored)."""
    tables: dict[str, list[dict[str, Any]]] = {}
    with tempfile.TemporaryDirectory(prefix="swarm-import.") as scratch:
        copy = Path(scratch) / "history.sqlite3"
        try:
            shutil.copyfile(database_path, copy)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(database_path) + suffix)
                if sidecar.is_file():
                    shutil.copyfile(sidecar, Path(str(copy) + suffix))
            with closing(sqlite3.connect(copy)) as database:
                database.row_factory = sqlite3.Row
                present = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                for table in history.IMPORT_TABLES:
                    if table not in present:
                        continue
                    order = "created_at, rowid" if table == "ai_token_usage" else "rowid"
                    tables[table] = [dict(row) for row in database.execute(f"SELECT * FROM {table} ORDER BY {order}")]
                version = None
                if "schema_migrations" in present:
                    version = database.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        except (OSError, sqlite3.Error) as error:
            raise ImportProblem(f"Desktop history database is unreadable: {database_path}: {error}") from None
    return tables, version


# -- the import ---------------------------------------------------------------------------------


def _document_status(storage: Storage, tenant: str, collection: str, key: str, document: Mapping[str, Any],
                     same, *, overwrite: bool, dry_run: bool) -> str:
    current = storage.read_document(tenant, collection, key)
    if current is None:
        status = "imported"
    elif same(current, document):
        return "unchanged"
    elif overwrite:
        status = "overwritten"
    else:
        return "kept"
    if not dry_run:
        storage.write_document(tenant, collection, key, document)
    return status


def run_import(
    storage: Storage, tenant: str, *, config_path: Path | None = None, state_dir: Path | None = None,
    history_db: Path | None = None, architecture_dir: Path | None = None,
    sections: Sequence[str] = SECTIONS, dry_run: bool = False, overwrite: bool = False,
) -> dict[str, Any]:
    validate_tenant(tenant)
    unknown = set(sections) - set(SECTIONS)
    if unknown:
        raise ImportProblem(f"Unknown sections: {sorted(unknown)}")
    report: dict[str, Any] = {
        "tenant": tenant, "dry_run": dry_run, "overwrite": overwrite, "sections": list(sections),
        "credentials": {"imported": 0},
    }
    if "config" in sections:
        if config_path is None:
            raise ImportProblem("--config is required to import configuration")
        config = read_config(config_path)
        documents, accounting = config_documents(config)
        statuses = {
            key: _document_status(
                storage, tenant, CONFIG_COLLECTION, key, document,
                lambda current, new: current.get("settings") == new["settings"], overwrite=overwrite, dry_run=dry_run,
            )
            for key, document in documents.items()
        }
        report["config"] = {"documents": statuses, **accounting}
        report["credentials"]["dropped"] = accounting["credentials_dropped"]
        state_dir = state_dir or _configured_state_dir(config)
    if "architecture" in sections:
        directory = architecture_dir or (state_dir / "architecture_docs" if state_dir else None)
        if directory is None:
            raise ImportProblem("--state-dir (or --architecture-dir) is required to import architecture documentation")
        documents, skipped = read_architecture_documents(directory)
        report["architecture"] = {
            "directory": str(directory),
            "documents": {
                key: _document_status(
                    storage, tenant, ARCHITECTURE_COLLECTION, key, document,
                    lambda current, new: current == new, overwrite=overwrite, dry_run=dry_run,
                )
                for key, document in documents.items()
            },
            "skipped_unreadable": skipped,
        }
    if "history" in sections:
        database = history_db or (state_dir / HISTORY_DATABASE_NAME if state_dir else None)
        if database is None:
            raise ImportProblem("--state-dir (or --history-db) is required to import execution history")
        if not database.is_file():
            report["history"] = {"database": str(database), "status": "absent", "tables": {}}
        else:
            tables, version = read_history_tables(database)
            if dry_run and not storage.has_execution_history(tenant):
                # Nothing exists yet, so a first import is an import into an empty
                # history: rehearse it on a scratch one instead of creating the
                # tenant's history schema just to roll the rows back.
                with tempfile.TemporaryDirectory(prefix="swarm-import-dry.") as scratch:
                    counts = LocalStorage(scratch).execution_history(tenant).import_records(tables, dry_run=True)
            else:
                counts = storage.execution_history(tenant).import_records(tables, dry_run=dry_run)
            report["history"] = {
                "database": str(database), "status": "read", "source_schema_version": version,
                "newer_than_this_importer": bool(version and version > history.SCHEMA_VERSION), "tables": counts,
            }
    return report


def _configured_state_dir(config: Mapping[str, Any]) -> Path | None:
    value = str(config.get("worker_state_dir") or "").strip()
    return Path(value).expanduser() if value else None


# -- reporting ------------------------------------------------------------------------------------


def render_report(report: Mapping[str, Any]) -> str:
    dry = report["dry_run"]
    verb = {"imported": "would import", "overwritten": "would overwrite"} if dry else {}
    lines = [
        "SWARM Automation desktop import" + (" (dry run: nothing was written)" if dry else ""),
        f"Tenant: {report['tenant']}",
    ]

    def document_lines(documents: Mapping[str, str]) -> None:
        for key, status in documents.items():
            lines.append(f"  {key:<44} {verb.get(status, status)}")

    config = report.get("config")
    if config:
        lines += ["", "Configuration"]
        document_lines(config["documents"])
        if config["left_out"]:
            lines.append(f"  left out as desktop-only: {', '.join(config['left_out'])}")
        if config["credentials_dropped"]:
            lines.append(f"  credential-like settings dropped: {', '.join(config['credentials_dropped'])}")
    architecture = report.get("architecture")
    if architecture:
        lines += ["", "Architecture documentation"]
        if architecture["documents"]:
            document_lines(architecture["documents"])
        else:
            lines.append("  none found")
        if architecture["skipped_unreadable"]:
            lines.append(f"  skipped (not a snapshot): {', '.join(architecture['skipped_unreadable'])}")
    summary = report.get("history")
    if summary:
        lines += ["", "Execution history"]
        if summary["status"] == "absent":
            lines.append(f"  no database at {summary['database']}")
        else:
            for table, counts in summary["tables"].items():
                detail = [f"{counts['imported']} {'would import' if dry else 'imported'}",
                          f"{counts['existing']} already present"]
                if counts["renumbered"]:
                    detail.append(f"{counts['renumbered']} {'would be ' if dry else ''}renumbered (attempt number taken)")
                if counts["orphaned"]:
                    detail.append(f"{counts['orphaned']} skipped (no parent execution)")
                lines.append(f"  {table:<24} {counts['source']} in source: " + ", ".join(detail))
            if summary["newer_than_this_importer"]:
                lines.append("  note: the source schema is newer than this importer; known columns were copied")
    lines += [
        "",
        "Credentials: none imported. Provider API keys, the keychain and GitHub App key files are never "
        "read; enter provider keys in the web app.",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import a SWARM Automation desktop install into a tenant.")
    parser.add_argument("--tenant", required=True, help="destination tenant id")
    parser.add_argument("--target", default="hosted", help="'hosted' (storage from SWARM_STORAGE_* variables) "
                        "or 'local:<directory>' (default: hosted)")
    parser.add_argument("--config", type=Path, help="desktop config.json")
    parser.add_argument("--state-dir", type=Path, help="desktop worker state directory "
                        "(default: worker_state_dir from the config)")
    parser.add_argument("--history-db", type=Path, help="override <state-dir>/swarm-automation.sqlite3")
    parser.add_argument("--architecture-dir", type=Path, help="override <state-dir>/architecture_docs")
    parser.add_argument("--only", help=f"comma-separated subset of: {', '.join(SECTIONS)}")
    parser.add_argument("--dry-run", action="store_true", help="report what would change and write nothing")
    parser.add_argument("--overwrite", action="store_true",
                        help="replace configuration and documents the tenant already has (default: keep them)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sections = tuple(part.strip() for part in args.only.split(",") if part.strip()) if args.only else SECTIONS
    if args.tenant == DEFAULT_TENANT and args.target == "hosted":
        print("Refusing to import into the 'default' tenant of hosted storage; name a real tenant.", file=sys.stderr)
        return 2
    try:
        storage = open_storage(args.target)
        report = run_import(
            storage, args.tenant, config_path=args.config, state_dir=args.state_dir, history_db=args.history_db,
            architecture_dir=args.architecture_dir, sections=sections, dry_run=args.dry_run, overwrite=args.overwrite,
        )
    except (ImportProblem, StorageError) as error:
        print(f"Import failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
