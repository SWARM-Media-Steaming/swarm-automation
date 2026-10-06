"""Hosted ``Storage``: Postgres for execution history, S3 for everything else.

``RemoteStorage`` implements the same tenant-scoped contract as
``LocalStorage`` (``storage_contract.StorageContract`` runs against both):

* checkpoints, documents and artifacts are objects in an S3-compatible bucket
  under ``<prefix>tenants/<tenant>/...`` (``object_store.py``);
* execution history is a Postgres schema per tenant holding the same tables and
  columns as the desktop's SQLite file (``storage_schema.py``), driven by the
  same ``ExecutionHistoryRepository`` write paths, so the sanitizer, the
  ``(execution_id, stage, round_number)`` keys, the epoch/security/merge-policy
  columns and idempotent token-usage recording behave identically.

This module stays standard-library only. It does not import a database driver:
the deployment hands :class:`PostgresDatabase` a ``connect`` callable returning
a DB-API 2.0 connection (for example ``lambda: psycopg.connect(dsn)``), which
keeps the worker image free of third-party packages. :class:`SqliteDatabase`
runs the identical SQL on SQLite files, which is what lets the always-on tests
exercise this layer without a server.

Every driver or object-store failure is raised as ``StorageError``;
``ExecutionHistoryService`` absorbs it exactly like ``sqlite3.Error``.
"""

from __future__ import annotations

import abc
import hashlib
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

import storage_schema
from ai_execution_history import SCHEMA_VERSION, ExecutionHistoryRepository, sanitize_text
from object_store import ObjectStore, ObjectStoreError, PreconditionFailed
from storage import (
    CURRENT,
    SINGLETON_CHECKPOINTS,
    ExecutionHistoryStore,
    Storage,
    StorageError,
    check_artifact_name,
    check_checkpoint,
    check_collection,
    validate_name,
    validate_tenant,
)

REGISTRY_SCHEMA = "swarm_storage"
# Bounded optimistic-concurrency retries for an artifact append.
_APPEND_ATTEMPTS = 8


# -- SQL dialects and the DB-API adapter ---------------------------------------


class Dialect:
    """What differs between the two SQL engines the history layer runs on."""

    def __init__(self, name: str, placeholder: str, usage_seq_ddl: str, usage_order: str) -> None:
        self.name = name
        self.placeholder = placeholder
        self.usage_seq_ddl = usage_seq_ddl
        self.usage_order = usage_order


POSTGRES = Dialect("postgres", "%s", "seq BIGINT GENERATED ALWAYS AS IDENTITY,", "seq")
SQLITE = Dialect("sqlite", "?", "", "rowid")

# ``INSERT OR REPLACE`` keys, for the two tables the history writes that way.
_REPLACE_KEYS = {"jev_decisions": "decision_id", "jev_score_comparisons": "comparison_id"}
_INSERT_OR = re.compile(r"^\s*INSERT\s+OR\s+(IGNORE|REPLACE)\s+INTO\s+(\w+)\s*(\([^)]*\))?", re.IGNORECASE)


def translate(sql: str, dialect: Dialect) -> str:
    """Rewrite the SQLite-flavoured statements of ``ExecutionHistoryRepository``
    for ``dialect``. SQLite is the identity."""
    if dialect is SQLITE:
        return sql
    match = _INSERT_OR.match(sql)
    suffix = ""
    if match:
        mode, table, columns = match.group(1).upper(), match.group(2), match.group(3)
        sql = "INSERT INTO " + table + (" " + columns if columns else "") + sql[match.end():]
        if mode == "IGNORE":
            suffix = " ON CONFLICT DO NOTHING"
        else:
            key = _REPLACE_KEYS.get(table)
            names = [name.strip() for name in (columns or "").strip("()").split(",") if name.strip()]
            if not key or not names:
                raise StorageError(f"INSERT OR REPLACE is not supported for {table}")
            assignments = ", ".join(f"{name} = EXCLUDED.{name}" for name in names if name != key)
            suffix = f" ON CONFLICT ({key}) DO UPDATE SET {assignments}"
    out: list[str] = []
    quoted = False
    for char in sql.rstrip().rstrip(";"):
        if char == "'":
            quoted = not quoted
        if char == "%":
            out.append("%%")
        elif char == "?" and not quoted:
            out.append("%s")
        else:
            out.append(char)
    return "".join(out) + suffix


class Row(tuple):
    """A result row readable by position, by column name and as ``dict(row)``,
    like ``sqlite3.Row``."""

    _names: tuple[str, ...]

    def __new__(cls, names: Sequence[str], values: Iterable[Any]) -> "Row":
        row = super().__new__(cls, values)
        row._names = tuple(names)
        return row

    def __getitem__(self, item):  # type: ignore[override]
        if isinstance(item, str):
            try:
                return tuple.__getitem__(self, self._names.index(item))
            except ValueError:
                raise IndexError(f"No column named {item!r}") from None
        return tuple.__getitem__(self, item)

    def keys(self) -> list[str]:
        return list(self._names)


def _wrap(error: Exception) -> StorageError | None:
    """Driver exceptions (anything that is not a plain builtin programming
    error) and socket failures become ``StorageError``."""
    if isinstance(error, StorageError):
        return None
    if isinstance(error, OSError) or type(error).__module__.split(".")[0] != "builtins":
        return StorageError(f"History database error: {type(error).__name__}: {error}")
    return None


class _Cursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def _names(self) -> list[str]:
        return [column[0] for column in (self._cursor.description or ())]

    def fetchone(self) -> Row | None:
        row = self._cursor.fetchone()
        return None if row is None else Row(self._names(), row)

    def fetchall(self) -> list[Row]:
        names = self._names()
        return [Row(names, row) for row in self._cursor.fetchall()]

    def __iter__(self) -> Iterator[Row]:
        return iter(self.fetchall())

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description


class Connection:
    """A DB-API connection speaking ``ExecutionHistoryRepository``'s SQL.

    A ``with`` block commits (or rolls back) and then closes, the contract the
    repository relies on."""

    def __init__(self, raw: Any, dialect: Dialect) -> None:
        self._raw = raw
        self.dialect = dialect

    def _clean(self, params: Sequence[Any]) -> tuple[Any, ...]:
        if self.dialect is SQLITE:
            return tuple(params)
        # Postgres text cannot hold NUL; a stray one must not fail the write.
        return tuple(value.replace("\x00", "") if isinstance(value, str) else value for value in params)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Cursor:
        try:
            if sql.strip().upper() == "BEGIN IMMEDIATE":
                return self._begin()
            cursor = self._raw.cursor()
            cursor.execute(translate(sql, self.dialect), self._clean(params))
            return _Cursor(cursor)
        except Exception as error:  # noqa: BLE001 - re-raised, wrapped when it is the driver's
            wrapped = _wrap(error)
            if wrapped is None:
                raise
            raise wrapped from error

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        try:
            cursor = self._raw.cursor()
            cursor.executemany(translate(sql, self.dialect), [self._clean(row) for row in rows])
        except Exception as error:  # noqa: BLE001
            wrapped = _wrap(error)
            if wrapped is None:
                raise
            raise wrapped from error

    def _begin(self) -> _Cursor:
        cursor = self._raw.cursor()
        if self.dialect is SQLITE:
            cursor.execute("BEGIN IMMEDIATE")
        else:
            # Serialise writers within this tenant's schema for the transaction,
            # the Postgres counterpart of SQLite's write lock.
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()))")
        return _Cursor(cursor)

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            if exc_type is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        except Exception as error:  # noqa: BLE001
            wrapped = _wrap(error)
            if wrapped is None:
                raise
            raise wrapped from error
        finally:
            try:
                self._raw.close()
            except Exception:  # noqa: BLE001 - closing a broken connection is best effort
                pass
        return False


# -- databases -----------------------------------------------------------------


class Database(abc.ABC):
    """Hands out tenant-scoped, already-provisioned connections."""

    dialect: Dialect

    def __init__(self) -> None:
        self._histories: dict[str, ExecutionHistoryStore] = {}

    @abc.abstractmethod
    def connect(self, tenant: str) -> Connection:
        """A new connection scoped to the tenant's history schema."""

    @abc.abstractmethod
    def provision(self, tenant: str) -> None:
        """Create the tenant's history tables (idempotent, concurrency safe)."""

    @abc.abstractmethod
    def provisioned(self, tenant: str) -> bool:
        """Whether :meth:`provision` has run for the tenant; creates nothing."""

    def history(self, tenant: str) -> ExecutionHistoryStore:
        validate_tenant(tenant)
        if tenant not in self._histories:
            self._histories[tenant] = SqlExecutionHistory(self, tenant)
        return self._histories[tenant]

    def _statements(self) -> list[str]:
        values = {"usage_seq": self.dialect.usage_seq_ddl}
        return [statement.format(**values) if "{usage_seq}" in statement else statement
                for statement in storage_schema.HISTORY_STATEMENTS]


def schema_name(tenant: str) -> str:
    """The Postgres schema of a tenant: ``t_<id>``, shortened with a digest when
    the id would not fit the 63-byte identifier limit."""
    validate_tenant(tenant)
    name = "t_" + tenant
    if len(name) <= 63:
        return name
    return name[:46] + "_" + hashlib.sha256(tenant.encode()).hexdigest()[:16]


def quote_identifier(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9_-]{1,63}", name):
        raise StorageError(f"Unsafe schema name: {name!r}")
    return '"' + name + '"'


class PostgresDatabase(Database):
    """A schema per tenant in one Postgres database.

    ``connect`` returns a new DB-API connection with autocommit off each time
    it is called (``lambda: psycopg.connect(dsn)``)."""

    dialect = POSTGRES

    def __init__(self, connect: Callable[[], Any]) -> None:
        super().__init__()
        self._connect = connect

    def _raw(self) -> Any:
        try:
            return self._connect()
        except Exception as error:  # noqa: BLE001
            wrapped = _wrap(error)
            if wrapped is None:
                raise
            raise wrapped from error

    def connect(self, tenant: str) -> Connection:
        raw = self._raw()
        try:
            # Committed straight away: a SET inside a transaction that later
            # rolls back would be undone with it.
            raw.cursor().execute(f"SET search_path TO {quote_identifier(schema_name(tenant))}")
            raw.commit()
        except Exception as error:  # noqa: BLE001
            try:
                raw.close()
            except Exception:  # noqa: BLE001
                pass
            wrapped = _wrap(error)
            if wrapped is None:
                raise
            raise wrapped from error
        return Connection(raw, POSTGRES)

    def provisioned(self, tenant: str) -> bool:
        raw = self._raw()
        try:
            with Connection(raw, POSTGRES):
                cursor = raw.cursor()
                cursor.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = 'ai_executions'",
                    (schema_name(tenant),),
                )
                return cursor.fetchone() is not None
        except Exception as error:  # noqa: BLE001
            wrapped = _wrap(error)
            if wrapped is None:
                raise
            raise wrapped from error

    def provision(self, tenant: str) -> None:
        schema = schema_name(tenant)
        quoted = quote_identifier(schema)
        raw = self._raw()
        connection = Connection(raw, POSTGRES)
        try:
            with connection:
                cursor = raw.cursor()
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("swarm-storage:" + schema,))
                cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {REGISTRY_SCHEMA}")
                cursor.execute(
                    f"CREATE TABLE IF NOT EXISTS {REGISTRY_SCHEMA}.tenants (tenant_id TEXT PRIMARY KEY, "
                    "schema_name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
                cursor.execute(
                    f"INSERT INTO {REGISTRY_SCHEMA}.tenants (tenant_id, schema_name) VALUES (%s, %s) "
                    "ON CONFLICT (tenant_id) DO NOTHING",
                    (tenant, schema),
                )
                cursor.execute(f"SELECT schema_name FROM {REGISTRY_SCHEMA}.tenants WHERE tenant_id = %s", (tenant,))
                if cursor.fetchone()[0] != schema:
                    raise StorageError(f"Tenant {tenant!r} is registered under a different schema")
                cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {quoted}")
                cursor.execute(f"SET LOCAL search_path TO {quoted}")
                cursor.execute(storage_schema.CREATE_MIGRATIONS_TABLE)
                for statement in self._statements():
                    cursor.execute(statement)
                for version in range(1, SCHEMA_VERSION + 1):
                    cursor.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING", (version,)
                    )
        except StorageError:
            raise
        except Exception as error:  # noqa: BLE001
            wrapped = _wrap(error)
            if wrapped is None:
                raise
            raise wrapped from error


class SqliteDatabase(Database):
    """The same SQL on a SQLite file per tenant under ``directory``."""

    dialect = SQLITE

    def __init__(self, directory: Path | str) -> None:
        super().__init__()
        self.directory = Path(directory)

    def _path(self, tenant: str) -> Path:
        validate_tenant(tenant)
        return self.directory / f"{tenant}.sqlite3"

    def connect(self, tenant: str) -> Connection:
        try:
            raw = sqlite3.connect(self._path(tenant), timeout=10)
            raw.execute("PRAGMA foreign_keys = ON")
        except (sqlite3.Error, OSError) as error:
            raise StorageError(f"History database error: {error}") from error
        return Connection(raw, SQLITE)

    def provisioned(self, tenant: str) -> bool:
        return self._path(tenant).is_file()

    def provision(self, tenant: str) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.connect(tenant) as connection:
                connection.execute(storage_schema.CREATE_MIGRATIONS_TABLE)
                for statement in self._statements():
                    connection.execute(statement)
                for version in range(1, SCHEMA_VERSION + 1):
                    connection.execute("INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)", (version,))
        except OSError as error:
            raise StorageError(f"History database error: {error}") from error


class SqlExecutionHistory(ExecutionHistoryRepository):
    """``ExecutionHistoryRepository`` over a tenant's hosted schema.

    Only the connection, the migration and the two ``rowid``-ordered usage
    queries differ; every write path (and its sanitizer) is the repository's."""

    def __init__(self, database: Database, tenant: str) -> None:
        self._database = database
        self.tenant = validate_tenant(tenant)
        self.database_path = None  # type: ignore[assignment]  # not file based
        database.provision(tenant)

    def connect(self) -> Connection:  # type: ignore[override]
        return self._database.connect(self.tenant)

    def migrate(self) -> None:
        self._database.provision(self.tenant)

    def _usage_rows(self, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        order = self._database.dialect.usage_order
        with self.connect() as database:
            rows = database.execute(
                f"SELECT * FROM ai_token_usage WHERE {where} ORDER BY created_at, {order}", params
            ).fetchall()
        records = [dict(row) for row in rows]
        for record in records:
            record.pop("seq", None)
        return records

    def token_usage_for_execution(self, execution_id: str) -> list[dict[str, Any]]:
        return self._usage_rows("execution_id = ?", (execution_id,))

    def token_usage_for_issue(self, repository: str, issue_number: int) -> list[dict[str, Any]]:
        return self._usage_rows("repository = ? AND issue_number = ?", (sanitize_text(repository), int(issue_number)))


# -- the storage ---------------------------------------------------------------


def _encode(value: Mapping[str, Any], *, indent: int, newline: bool) -> bytes:
    if not isinstance(value, Mapping):
        raise StorageError("Only JSON objects can be stored")
    try:
        text = json.dumps(value, indent=indent, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise StorageError(f"Could not serialise value: {error}") from error
    return (text + ("\n" if newline else "")).encode("utf-8")


def _decode(data: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise StorageError(f"{what} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise StorageError(f"{what} does not hold a JSON object")
    return value


class RemoteStorage(Storage):
    """``Storage`` over an :class:`ObjectStore` and a :class:`Database`.

    ``prefix`` namespaces every key (``"swarm/"``) so several deployments can
    share a bucket. ``scratch_root`` is the job-local directory
    :meth:`scratch_path` hands to CLI subprocesses."""

    def __init__(
        self, objects: ObjectStore, database: Database, *, prefix: str = "",
        scratch_root: Path | str | None = None,
    ) -> None:
        if prefix and (not prefix.endswith("/") or prefix.startswith("/") or ".." in prefix.split("/")):
            raise StorageError("Object prefix must be a relative path ending in '/'")
        self.objects = objects
        self.database = database
        self.prefix = prefix
        self._scratch_root = Path(scratch_root) if scratch_root else None

    # -- keys ----------------------------------------------------------------
    def _base(self, tenant: str) -> str:
        return f"{self.prefix}tenants/{validate_tenant(tenant)}/"

    def _checkpoint_key(self, tenant: str, kind: str, key: str) -> str:
        key = check_checkpoint(kind, key)
        return f"{self._base(tenant)}checkpoints/{kind}/{key}.json"

    def _document_key(self, tenant: str, collection: str, key: str) -> str:
        check_collection(collection)
        return f"{self._base(tenant)}documents/{collection}/{validate_name(key, 'document key')}.json"

    def _artifact_key(self, tenant: str, name: str) -> str:
        return f"{self._base(tenant)}artifacts/{check_artifact_name(name)}"

    # -- object helpers ------------------------------------------------------
    def _get(self, key: str):
        try:
            return self.objects.get(key)
        except ObjectStoreError as error:
            raise StorageError(f"Object store read failed: {error}") from error

    def _put(self, key: str, data: bytes, content_type: str, **conditions: Any) -> None:
        try:
            self.objects.put(key, data, content_type=content_type, **conditions)
        except PreconditionFailed:
            raise
        except ObjectStoreError as error:
            raise StorageError(f"Object store write failed: {error}") from error

    def _delete(self, key: str) -> bool:
        try:
            return self.objects.delete(key)
        except ObjectStoreError as error:
            raise StorageError(f"Object store delete failed: {error}") from error

    def _keys(self, prefix: str) -> list[str]:
        try:
            return self.objects.list(prefix)
        except ObjectStoreError as error:
            raise StorageError(f"Object store listing failed: {error}") from error

    def _stems(self, prefix: str) -> list[str]:
        return sorted(
            name[: -len(".json")]
            for name in (key[len(prefix):] for key in self._keys(prefix))
            if name.endswith(".json") and "/" not in name and not name.startswith(".")
        )

    def _read_json(self, key: str, what: str) -> dict[str, Any] | None:
        found = self._get(key)
        return None if found is None else _decode(found.data, what)

    # -- checkpoints ---------------------------------------------------------
    def read_checkpoint(self, tenant: str, kind: str, key: str = CURRENT) -> dict[str, Any] | None:
        return self._read_json(self._checkpoint_key(tenant, kind, key), f"checkpoint {kind}")

    def write_checkpoint(self, tenant: str, kind: str, key: str, value: Mapping[str, Any]) -> None:
        object_key = self._checkpoint_key(tenant, kind, key)
        self._put(object_key, _encode(value, indent=2, newline=True), "application/json")

    def delete_checkpoint(self, tenant: str, kind: str, key: str = CURRENT) -> bool:
        return self._delete(self._checkpoint_key(tenant, kind, key))

    def list_checkpoints(self, tenant: str, kind: str) -> list[str]:
        if kind in SINGLETON_CHECKPOINTS:
            return [CURRENT] if self._get(self._checkpoint_key(tenant, kind, CURRENT)) is not None else []
        self._checkpoint_key(tenant, kind, "x")  # validates the kind
        return self._stems(f"{self._base(tenant)}checkpoints/{kind}/")

    # -- documents -----------------------------------------------------------
    def read_document(self, tenant: str, collection: str, key: str) -> dict[str, Any] | None:
        return self._read_json(self._document_key(tenant, collection, key), f"document {collection}/{key}")

    def write_document(self, tenant: str, collection: str, key: str, value: Mapping[str, Any]) -> None:
        self._put(self._document_key(tenant, collection, key), _encode(value, indent=1, newline=False), "application/json")

    def delete_document(self, tenant: str, collection: str, key: str) -> bool:
        return self._delete(self._document_key(tenant, collection, key))

    def list_documents(self, tenant: str, collection: str) -> list[str]:
        check_collection(collection)
        return self._stems(f"{self._base(tenant)}documents/{collection}/")

    # -- artifacts -----------------------------------------------------------
    def write_artifact(self, tenant: str, name: str, text: str) -> None:
        self._put(self._artifact_key(tenant, name), text.encode("utf-8"), "text/plain; charset=utf-8")

    def append_artifact(self, tenant: str, name: str, text: str) -> None:
        key = self._artifact_key(tenant, name)
        for _ in range(_APPEND_ATTEMPTS):
            current = self._get(key)
            data = (current.data if current else b"") + text.encode("utf-8")
            conditions = {"if_match": current.etag} if current else {"if_none_match": True}
            try:
                self._put(key, data, "text/plain; charset=utf-8", **conditions)
                return
            except PreconditionFailed:
                continue  # another writer appended first; re-read and retry
        raise StorageError(f"Could not append to artifact {name!r}: too much write contention")

    def read_artifact(self, tenant: str, name: str) -> str | None:
        found = self._get(self._artifact_key(tenant, name))
        return None if found is None else found.data.decode("utf-8", errors="replace")

    def delete_artifact(self, tenant: str, name: str) -> bool:
        return self._delete(self._artifact_key(tenant, name))

    def list_artifacts(self, tenant: str) -> list[str]:
        prefix = f"{self._base(tenant)}artifacts/"
        return sorted(key[len(prefix):] for key in self._keys(prefix) if "/" not in key[len(prefix):])

    def _scratch_dir(self) -> Path:
        if self._scratch_root is None:
            self._scratch_root = Path(tempfile.mkdtemp(prefix="swarm-scratch."))
        return self._scratch_root

    def scratch_path(self, tenant: str, name: str) -> Path:
        check_artifact_name(name)
        return self._scratch_dir() / validate_tenant(tenant) / name

    def publish_artifact(self, tenant: str, name: str) -> None:
        path = self.scratch_path(tenant, name)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise StorageError(f"Nothing to publish: {name!r} was never written") from None
        except OSError as error:
            raise StorageError(f"Could not read scratch {name!r}: {error}") from error
        self._put(self._artifact_key(tenant, name), data, "text/plain; charset=utf-8")

    # -- execution history -----------------------------------------------------
    def has_execution_history(self, tenant: str) -> bool:
        return self.database.provisioned(validate_tenant(tenant))

    def execution_history(self, tenant: str) -> ExecutionHistoryStore:
        return self.database.history(tenant)
