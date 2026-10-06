"""Open a ``Storage`` from a target spec or from the environment.

``local:<directory>`` is ``LocalStorage`` (a desktop-style state directory;
other tenants live under ``tenants/<id>/``). ``hosted`` is ``RemoteStorage``
configured entirely from the environment, so no credential is ever a command-line
argument or a file in the repository:

====================================  =========================================
``SWARM_STORAGE_POSTGRES_DSN``        Postgres connection string
``SWARM_STORAGE_POSTGRES_DRIVER``     ``module:callable`` DB-API ``connect``,
                                      e.g. ``psycopg:connect``
``SWARM_STORAGE_S3_ENDPOINT``         ``https://s3.us-east-1.amazonaws.com`` or
                                      ``http://localhost:9000`` (MinIO)
``SWARM_STORAGE_S3_BUCKET``           bucket name
``SWARM_STORAGE_S3_ACCESS_KEY_ID`` /  credentials (``..._SESSION_TOKEN`` optional)
``SWARM_STORAGE_S3_SECRET_ACCESS_KEY``
``SWARM_STORAGE_S3_REGION``           default ``us-east-1``
``SWARM_STORAGE_S3_PREFIX``           optional key prefix, ends in ``/``
``SWARM_STORAGE_S3_ADDRESSING``       ``path`` (default) or ``virtual``
``SWARM_STORAGE_S3_ALLOW_INSECURE_HTTP``  ``1`` to permit cleartext to a
                                      non-loopback host (private networks only)
``SWARM_STORAGE_SCRATCH_DIR``         job-local scratch directory
====================================  =========================================

The worker image is standard library only, so it does not bundle a Postgres
driver: the deployment installs one and names it in
``SWARM_STORAGE_POSTGRES_DRIVER``. Nothing is imported until that variable is
set, and error messages name variables, never values.
"""

from __future__ import annotations

import importlib
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from object_store import ObjectStoreError, S3ObjectStore
from storage import LocalStorage, Storage, StorageError
from storage_remote import PostgresDatabase, RemoteStorage

_DRIVER_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):([A-Za-z_][A-Za-z0-9_]*)")
REQUIRED = (
    "SWARM_STORAGE_POSTGRES_DSN",
    "SWARM_STORAGE_POSTGRES_DRIVER",
    "SWARM_STORAGE_S3_ENDPOINT",
    "SWARM_STORAGE_S3_BUCKET",
    "SWARM_STORAGE_S3_ACCESS_KEY_ID",
    "SWARM_STORAGE_S3_SECRET_ACCESS_KEY",
)


def open_storage(target: str, env: Mapping[str, str] | None = None) -> Storage:
    if target.startswith("local:") and target[len("local:"):]:
        return LocalStorage(Path(target[len("local:"):]).expanduser())
    if target == "hosted":
        return hosted_storage(env if env is not None else os.environ)
    raise StorageError("Storage target must be 'local:<directory>' or 'hosted'")


def load_connect(driver: str, dsn: str) -> Callable[[], Any]:
    """``module:callable`` -> a zero-argument ``connect`` bound to ``dsn``."""
    match = _DRIVER_RE.fullmatch(driver.strip())
    if not match:
        raise StorageError("SWARM_STORAGE_POSTGRES_DRIVER must look like 'module:callable'")
    try:
        connect = getattr(importlib.import_module(match.group(1)), match.group(2))
    except (ImportError, AttributeError) as error:
        raise StorageError(f"Postgres driver {driver!r} is not available: {error.__class__.__name__}") from error
    if not callable(connect):
        raise StorageError(f"Postgres driver {driver!r} is not callable")
    return lambda: connect(dsn)


def hosted_storage(env: Mapping[str, str]) -> RemoteStorage:
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        raise StorageError("Hosted storage needs " + ", ".join(missing))
    try:
        objects = S3ObjectStore(
            endpoint=env["SWARM_STORAGE_S3_ENDPOINT"],
            bucket=env["SWARM_STORAGE_S3_BUCKET"],
            access_key=env["SWARM_STORAGE_S3_ACCESS_KEY_ID"],
            secret_key=env["SWARM_STORAGE_S3_SECRET_ACCESS_KEY"],
            session_token=env.get("SWARM_STORAGE_S3_SESSION_TOKEN", ""),
            region=env.get("SWARM_STORAGE_S3_REGION") or "us-east-1",
            path_style=(env.get("SWARM_STORAGE_S3_ADDRESSING") or "path") != "virtual",
            allow_insecure_http=env.get("SWARM_STORAGE_S3_ALLOW_INSECURE_HTTP") == "1",
        )
    except ObjectStoreError as error:
        raise StorageError(f"Object store configuration: {error}") from error
    database = PostgresDatabase(load_connect(env["SWARM_STORAGE_POSTGRES_DRIVER"], env["SWARM_STORAGE_POSTGRES_DSN"]))
    return RemoteStorage(
        objects, database, prefix=env.get("SWARM_STORAGE_S3_PREFIX", ""),
        scratch_root=env.get("SWARM_STORAGE_SCRATCH_DIR") or None,
    )
