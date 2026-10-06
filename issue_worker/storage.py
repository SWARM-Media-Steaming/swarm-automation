"""Tenant-scoped storage interface for the issue worker.

Everything the worker persists goes through one of four groups of operations:

* **checkpoints** - the resumable per-issue state (``in-progress``,
  ``quota-paused``, ``pending-delivery``, ...) that makes the exit-13 / exit-14
  resume flow work from a freshly started process;
* **documents** - durable product data such as the architecture model;
* **artifacts** - text logs and scratch outputs (the last AI output, the
  diagnostic log, the completed-issues list);
* **execution history** - the ``ai_executions`` family, including the
  per-prompt token-usage records (``ai_token_usage``).

Every operation takes a ``tenant`` first. A hosted implementation maps it to a
database tenant / bucket prefix; :class:`LocalStorage` keeps the desktop's
existing layout for ``DEFAULT_TENANT`` (byte-for-byte the same files the
worker always wrote) and puts any other tenant under ``tenants/<id>/``.

This module deliberately imports only the standard library: the worker image
must not pull in desktop-only code. ``docs/web-architecture.md`` documents the
contract; ``test_storage_contract.py`` is the reusable contract suite that any
implementation must pass.
"""

from __future__ import annotations

import abc
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

DEFAULT_TENANT = "default"
# Key of the single document of a singleton checkpoint kind.
CURRENT = "current"

_TENANT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_NAME_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._+-]{0,127}")
HISTORY_DATABASE_NAME = "swarm-automation.sqlite3"

# Checkpoint kinds and where the local implementation keeps them. A singleton
# kind has one document (key ``CURRENT``) in one file; a keyed kind has one
# document per key in a directory. These are the names the worker has always
# used, so existing state directories keep working unchanged.
SINGLETON_CHECKPOINTS: Mapping[str, str] = {
    "in-progress": "in-progress-issue.json",
    "pending-delivery": "pending-delivery.json",
    "integration-recovery": "integration-recovery.json",
    "promotion-blocked": "promotion-blocked.json",
}
KEYED_CHECKPOINTS: Mapping[str, str] = {
    "quota-paused": "quota-paused-issues",
    "closed-paused": "closed-paused-issues",
    "abandoned": "abandoned",
}
CHECKPOINT_KINDS = tuple(SINGLETON_CHECKPOINTS) + tuple(KEYED_CHECKPOINTS)
# ``tenant_config`` holds a tenant's settings (key ``app`` for the app-wide
# configuration, ``repo-<id>`` per repository). The desktop keeps its settings
# in ``config.json`` and never writes this collection; the hosted backend and
# the desktop importer do.
DOCUMENT_COLLECTIONS = ("architecture_docs", "tenant_config")
_TENANT_DIRECTORY = "tenants"


class StorageError(Exception):
    """Any storage failure: unreadable or corrupt data, invalid names, I/O.

    Implementations wrap their backend's exceptions in this so callers (the
    worker, the history facade) can treat "storage is unavailable" uniformly.
    """


@runtime_checkable
class ExecutionHistoryStore(Protocol):
    """The history operations the worker needs: lifecycle writes plus the
    per-prompt token-usage records. Reporting queries (Feedback, usage
    analytics) are read-side surfaces of the SQLite repository and are not part
    of this contract yet."""

    def create(self, start: Any, started_at: str) -> str: ...

    def update(self, execution_id: str, updated_at: str, **fields: Any) -> None: ...

    def append(self, execution_id: str, column: str, message: str, updated_at: str) -> None: ...

    def execution_exists(self, execution_id: str) -> bool: ...

    def final_statuses_for_issue(self, repository: str, issue_number: int) -> list[str]: ...

    def record_adversarial_round(self, execution_id: str, round_values: dict[str, Any]) -> None: ...

    def record_adversarial_epoch(self, execution_id: str, summary: dict[str, Any]) -> None: ...

    def record_token_usage_batch(
        self, execution_id: str, repository: str, issue_number: int, events: Sequence[dict[str, Any]]
    ) -> None: ...

    def token_usage_for_execution(self, execution_id: str) -> list[dict[str, Any]]: ...

    def token_usage_for_issue(self, repository: str, issue_number: int) -> list[dict[str, Any]]: ...

    def record_jev_decision(self, payload: Mapping[str, Any]) -> None: ...

    def record_jev_score_comparison(self, payload: Mapping[str, Any]) -> None: ...

    def finish_jev_outcomes(self, execution_id: str, outcome: str) -> None: ...

    def import_records(
        self, tables: Mapping[str, Sequence[Mapping[str, Any]]], *, dry_run: bool = False
    ) -> dict[str, dict[str, int]]:
        """Idempotently copy rows exported from another history store (the
        desktop importer). Returns ``{table: {source, imported, existing,
        renumbered, orphaned}}``; ``dry_run`` reports without writing."""
        ...


def validate_tenant(tenant: str) -> str:
    if not isinstance(tenant, str) or not _TENANT_RE.fullmatch(tenant):
        raise StorageError(f"Invalid tenant id: {tenant!r}")
    return tenant


def validate_name(name: str, what: str = "name") -> str:
    """Keys and artifact names are flat file-safe words: no separators, no
    leading dot, so they can never traverse out of their namespace."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise StorageError(f"Invalid {what}: {name!r}")
    return name


def check_checkpoint(kind: str, key: str) -> str:
    """Validate a checkpoint address and return its key (``CURRENT`` for a
    singleton kind). Shared by every implementation so the closed kind set and
    the key grammar cannot drift between backends."""
    if kind in SINGLETON_CHECKPOINTS:
        if key != CURRENT:
            raise StorageError(f"Checkpoint kind {kind!r} holds one document; key must be {CURRENT!r}")
        return CURRENT
    if kind in KEYED_CHECKPOINTS:
        return validate_name(key, "checkpoint key")
    raise StorageError(f"Unknown checkpoint kind: {kind!r}")


def check_collection(collection: str) -> str:
    if collection not in DOCUMENT_COLLECTIONS:
        raise StorageError(f"Unknown document collection: {collection!r}")
    return collection


def check_artifact_name(name: str) -> str:
    """Artifact names are flat words that cannot collide with a checkpoint
    file, a document directory or the tenant subtree."""
    validate_name(name, "artifact name")
    if name in RESERVED_ARTIFACT_NAMES:
        raise StorageError(f"Artifact name is reserved: {name!r}")
    return name


RESERVED_ARTIFACT_NAMES = frozenset(
    {*SINGLETON_CHECKPOINTS.values(), *KEYED_CHECKPOINTS.values(), *DOCUMENT_COLLECTIONS, _TENANT_DIRECTORY}
)


class Storage(abc.ABC):
    """Abstract tenant-scoped storage. Every method's first argument is the
    tenant; there is no tenant-less operation by construction.

    Checkpoint and document values are JSON objects (``dict``). Reads of a
    missing item return ``None`` / ``False`` / ``[]``; unreadable or corrupt
    data raises :class:`StorageError`. Writes are atomic: a concurrent or
    interrupted reader sees the old or the new value, never a torn one.
    """

    # -- checkpoints -------------------------------------------------------
    @abc.abstractmethod
    def read_checkpoint(self, tenant: str, kind: str, key: str = CURRENT) -> dict[str, Any] | None: ...

    @abc.abstractmethod
    def write_checkpoint(self, tenant: str, kind: str, key: str, value: Mapping[str, Any]) -> None: ...

    @abc.abstractmethod
    def delete_checkpoint(self, tenant: str, kind: str, key: str = CURRENT) -> bool:
        """Remove a checkpoint; ``True`` when something was removed."""

    @abc.abstractmethod
    def list_checkpoints(self, tenant: str, kind: str) -> list[str]:
        """Keys present for ``kind``, sorted. A singleton kind lists ``[CURRENT]``."""

    # -- documents ---------------------------------------------------------
    @abc.abstractmethod
    def read_document(self, tenant: str, collection: str, key: str) -> dict[str, Any] | None: ...

    @abc.abstractmethod
    def write_document(self, tenant: str, collection: str, key: str, value: Mapping[str, Any]) -> None: ...

    @abc.abstractmethod
    def delete_document(self, tenant: str, collection: str, key: str) -> bool: ...

    @abc.abstractmethod
    def list_documents(self, tenant: str, collection: str) -> list[str]: ...

    # -- artifacts ---------------------------------------------------------
    @abc.abstractmethod
    def write_artifact(self, tenant: str, name: str, text: str) -> None:
        """Replace an artifact's text."""

    @abc.abstractmethod
    def append_artifact(self, tenant: str, name: str, text: str) -> None:
        """Append text, creating the artifact when absent."""

    @abc.abstractmethod
    def read_artifact(self, tenant: str, name: str) -> str | None:
        """Artifact text (undecodable bytes replaced) or ``None`` when absent."""

    @abc.abstractmethod
    def delete_artifact(self, tenant: str, name: str) -> bool: ...

    @abc.abstractmethod
    def list_artifacts(self, tenant: str) -> list[str]: ...

    @abc.abstractmethod
    def scratch_path(self, tenant: str, name: str) -> Path:
        """A filesystem path the runtime can hand to a CLI subprocess (e.g.
        ``--output-last-message``). For a remote implementation this is a
        job-local file; call :meth:`publish_artifact` once it is complete."""

    @abc.abstractmethod
    def publish_artifact(self, tenant: str, name: str) -> None:
        """Make the content at :meth:`scratch_path` durable and readable
        through :meth:`read_artifact`. A no-op where scratch is the store."""

    # -- execution history and usage records --------------------------------
    @abc.abstractmethod
    def execution_history(self, tenant: str) -> ExecutionHistoryStore:
        """The tenant's execution-history store (schema version and migrations
        3, 5 and 9 semantics are the implementation's responsibility)."""

    def has_execution_history(self, tenant: str) -> bool:
        """Whether the tenant's history store already exists, without creating
        it (``execution_history`` provisions on first use). Implementations
        that cannot tell answer ``True``."""
        validate_tenant(tenant)
        return True


class LocalStorage(Storage):
    """Filesystem + SQLite implementation: the behavior the worker and desktop
    app have always had.

    ``root`` is the worker state directory. ``history_database`` is the
    app-wide SQLite file (default ``<root>/swarm-automation.sqlite3``);
    ``collection_dirs`` relocates a document collection (the desktop keeps
    architecture documents beside the history database, not under ``root``).
    The default tenant uses exactly those locations; any other tenant gets its
    own subtree under ``<root>/tenants/<tenant>/``.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        history_database: Path | str | None = None,
        collection_dirs: Mapping[str, Path | str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.history_database = Path(history_database) if history_database else None
        self.collection_dirs = {name: Path(path) for name, path in (collection_dirs or {}).items()}
        for name in self.collection_dirs:
            if name not in DOCUMENT_COLLECTIONS:
                raise StorageError(f"Unknown document collection: {name!r}")
        self._histories: dict[str, ExecutionHistoryStore] = {}

    # -- layout (local only) -------------------------------------------------
    def tenant_root(self, tenant: str) -> Path:
        validate_tenant(tenant)
        return self.root if tenant == DEFAULT_TENANT else self.root / _TENANT_DIRECTORY / tenant

    def checkpoint_path(self, tenant: str, kind: str, key: str = CURRENT) -> Path:
        """Where a checkpoint lives. The worker's legacy ``*_file`` / ``*_dir``
        attributes are derived from this so the layout has one owner."""
        base = self.tenant_root(tenant)
        key = check_checkpoint(kind, key)
        if kind in SINGLETON_CHECKPOINTS:
            return base / SINGLETON_CHECKPOINTS[kind]
        return base / KEYED_CHECKPOINTS[kind] / f"{key}.json"

    def checkpoint_directory(self, tenant: str, kind: str) -> Path:
        if kind not in KEYED_CHECKPOINTS:
            raise StorageError(f"Checkpoint kind {kind!r} is not keyed")
        return self.tenant_root(tenant) / KEYED_CHECKPOINTS[kind]

    def document_directory(self, tenant: str, collection: str) -> Path:
        check_collection(collection)
        validate_tenant(tenant)
        if tenant == DEFAULT_TENANT and collection in self.collection_dirs:
            return self.collection_dirs[collection]
        return self.tenant_root(tenant) / collection

    def document_path(self, tenant: str, collection: str, key: str) -> Path:
        return self.document_directory(tenant, collection) / f"{validate_name(key, 'document key')}.json"

    def artifact_path(self, tenant: str, name: str) -> Path:
        check_artifact_name(name)
        return self.tenant_root(tenant) / name

    def history_database_path(self, tenant: str) -> Path:
        validate_tenant(tenant)
        if tenant == DEFAULT_TENANT:
            return self.history_database or self.root / HISTORY_DATABASE_NAME
        name = (self.history_database or Path(HISTORY_DATABASE_NAME)).name
        return self.tenant_root(tenant) / name

    # -- checkpoints -------------------------------------------------------
    def read_checkpoint(self, tenant: str, kind: str, key: str = CURRENT) -> dict[str, Any] | None:
        return _read_json(self.checkpoint_path(tenant, kind, key))

    def write_checkpoint(self, tenant: str, kind: str, key: str, value: Mapping[str, Any]) -> None:
        _write_json(self.checkpoint_path(tenant, kind, key), value, indent=2, newline=True)

    def delete_checkpoint(self, tenant: str, kind: str, key: str = CURRENT) -> bool:
        return _unlink(self.checkpoint_path(tenant, kind, key))

    def list_checkpoints(self, tenant: str, kind: str) -> list[str]:
        if kind in SINGLETON_CHECKPOINTS:
            return [CURRENT] if self.checkpoint_path(tenant, kind).is_file() else []
        directory = self.checkpoint_directory(tenant, kind)
        return _stems(directory)

    # -- documents ---------------------------------------------------------
    def read_document(self, tenant: str, collection: str, key: str) -> dict[str, Any] | None:
        return _read_json(self.document_path(tenant, collection, key))

    def write_document(self, tenant: str, collection: str, key: str, value: Mapping[str, Any]) -> None:
        _write_json(self.document_path(tenant, collection, key), value, indent=1, newline=False)

    def delete_document(self, tenant: str, collection: str, key: str) -> bool:
        return _unlink(self.document_path(tenant, collection, key))

    def list_documents(self, tenant: str, collection: str) -> list[str]:
        return _stems(self.document_directory(tenant, collection))

    # -- artifacts ---------------------------------------------------------
    def write_artifact(self, tenant: str, name: str, text: str) -> None:
        path = self.artifact_path(tenant, name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as error:
            raise StorageError(f"Could not write artifact {name!r}: {error}") from error

    def append_artifact(self, tenant: str, name: str, text: str) -> None:
        path = self.artifact_path(tenant, name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(text)
        except OSError as error:
            raise StorageError(f"Could not append to artifact {name!r}: {error}") from error

    def read_artifact(self, tenant: str, name: str) -> str | None:
        path = self.artifact_path(tenant, name)
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return None
        except OSError as error:
            raise StorageError(f"Could not read artifact {name!r}: {error}") from error

    def delete_artifact(self, tenant: str, name: str) -> bool:
        return _unlink(self.artifact_path(tenant, name))

    def list_artifacts(self, tenant: str) -> list[str]:
        base = self.tenant_root(tenant)
        if not base.is_dir():
            return []
        return sorted(
            entry.name
            for entry in base.iterdir()
            if entry.is_file()
            and entry.name not in RESERVED_ARTIFACT_NAMES
            and _NAME_RE.fullmatch(entry.name)
            and entry.suffix != ".sqlite3"
            and not entry.name.endswith(("-wal", "-shm"))
        )

    def scratch_path(self, tenant: str, name: str) -> Path:
        return self.artifact_path(tenant, name)

    def publish_artifact(self, tenant: str, name: str) -> None:
        self.artifact_path(tenant, name)

    # -- execution history -------------------------------------------------
    def has_execution_history(self, tenant: str) -> bool:
        return tenant in self._histories or self.history_database_path(tenant).is_file()

    def execution_history(self, tenant: str) -> ExecutionHistoryStore:
        validate_tenant(tenant)
        if tenant not in self._histories:
            from ai_execution_history import ExecutionHistoryRepository  # lazy: it imports this module

            self._histories[tenant] = ExecutionHistoryRepository(self.history_database_path(tenant))
        return self._histories[tenant]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StorageError(f"Could not read {path.name}: {error}") from error
    try:
        value = json.loads(text)
    except ValueError as error:
        raise StorageError(f"{path.name} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise StorageError(f"{path.name} does not hold a JSON object")
    return value


def _write_json(path: Path, value: Mapping[str, Any], *, indent: int, newline: bool) -> None:
    if not isinstance(value, Mapping):
        raise StorageError("Only JSON objects can be stored")
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=indent, sort_keys=True)
            if newline:
                stream.write("\n")
        os.replace(temporary_path, path)
    except (OSError, TypeError, ValueError) as error:
        raise StorageError(f"Could not write {path.name}: {error}") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise StorageError(f"Could not remove {path.name}: {error}") from error
    return True


def _stems(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(entry.stem for entry in directory.glob("*.json") if entry.is_file() and not entry.name.startswith("."))
