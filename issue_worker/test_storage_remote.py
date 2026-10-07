"""Tests for the hosted storage: object store, SQL layer and ``RemoteStorage``.

``StorageContract`` (the suite every backend must pass) runs here against

* ``RemoteStorage`` over an in-memory object store and SQLite history files
  (always on),
* ``RemoteStorage`` over ``S3ObjectStore`` talking to a local S3 test server that
  verifies every Signature V4 request (always on), and
* ``RemoteStorage`` over a live Postgres, when ``SWARM_TEST_POSTGRES_DSN`` is set
  and ``psycopg`` is importable (CI's ``storage-live`` job; locally start any
  Postgres and export the DSN).
"""

from __future__ import annotations

import hashlib
import http.server
import itertools
import os
import re
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import closing
from pathlib import Path

import ai_execution_history as history_module
import storage_remote
import storage_schema
from object_store import (
    MemoryObjectStore,
    ObjectStoreError,
    PreconditionFailed,
    S3ObjectStore,
    canonical_query,
    sigv4_signature,
)
from storage import CURRENT, DEFAULT_TENANT, StorageError
from storage_contract import COLLECTION, NOW, OTHER_TENANT, StorageContract, execution_start
from storage_remote import (
    POSTGRES,
    SQLITE,
    PostgresDatabase,
    RemoteStorage,
    SqliteDatabase,
    schema_name,
    translate,
)

ACCESS_KEY = "AKIATESTKEY"
SECRET_KEY = "test-secret-key/with+chars"


# -- a local S3 test server ------------------------------------------------------


class FakeS3(http.server.ThreadingHTTPServer):
    """Path-style S3 subset (PUT/GET/HEAD/DELETE/ListObjectsV2, conditional
    writes) that recomputes and checks the Signature V4 of every request."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _S3Handler)
        self.objects: dict[str, bytes] = {}
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=5)


class _S3Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    bucket = "swarm-test"

    def log_message(self, *args):  # silence
        pass

    def _reply(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, code: str) -> None:
        self._reply(status, f"<Error><Code>{code}</Code></Error>".encode(), {"Content-Type": "application/xml"})

    def _verified(self, body: bytes) -> bool:
        authorization = self.headers.get("Authorization", "")
        match = re.fullmatch(
            r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/s3/aws4_request, "
            r"SignedHeaders=([^,]+), Signature=([0-9a-f]{64})", authorization)
        if not match or match.group(1) != ACCESS_KEY:
            return False
        _, _, region, signed_headers, signature = match.groups()
        parts = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        headers = {name: self.headers.get(name, "") for name in signed_headers.split(";")}
        expected, _ = sigv4_signature(
            method=self.command, canonical_uri=parts.path, query=query, headers=headers,
            payload_sha256=hashlib.sha256(body).hexdigest(), amz_date=self.headers.get("x-amz-date", ""),
            region=region, service="s3", secret_key=SECRET_KEY,
        )
        return (
            expected == signature
            and self.headers.get("x-amz-content-sha256") == hashlib.sha256(body).hexdigest()
        )

    def _dispatch(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.requests.append((self.command, self.path))  # type: ignore[attr-defined]
        if not self._verified(body):
            return self._error(403, "SignatureDoesNotMatch")
        parts = urllib.parse.urlsplit(self.path)
        segments = urllib.parse.unquote(parts.path).lstrip("/").split("/", 1)
        if segments[0] != self.bucket:
            return self._error(404, "NoSuchBucket")
        key = segments[1] if len(segments) > 1 else ""
        store: dict[str, bytes] = self.server.objects  # type: ignore[attr-defined]
        with self.server.lock:  # type: ignore[attr-defined]
            if not key and self.command == "GET":
                return self._list(parts, store)
            etag = lambda data: '"%s"' % hashlib.md5(data).hexdigest()  # noqa: E731
            if self.command == "PUT":
                current = store.get(key)
                if self.headers.get("If-None-Match") == "*" and current is not None:
                    return self._error(412, "PreconditionFailed")
                match = self.headers.get("If-Match")
                if match is not None and (current is None or etag(current) != match):
                    return self._error(412, "PreconditionFailed")
                store[key] = body
                return self._reply(200, headers={"ETag": etag(body)})
            if self.command in ("GET", "HEAD"):
                if key not in store:
                    return self._error(404, "NoSuchKey")
                return self._reply(200, store[key], {"ETag": etag(store[key])})
            if self.command == "DELETE":
                store.pop(key, None)
                return self._reply(204)
        self._error(405, "MethodNotAllowed")

    def _list(self, parts, store: dict[str, bytes]) -> None:
        query = dict(urllib.parse.parse_qsl(parts.query))
        keys = sorted(key for key in store if key.startswith(query.get("prefix", "")))
        start = int(query.get("continuation-token") or 0)
        page = keys[start:start + 2]  # a tiny page proves the client follows continuation tokens
        more = start + 2 < len(keys)
        body = "<ListBucketResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">" + "".join(
            f"<Contents><Key>{key}</Key></Contents>" for key in page
        ) + f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>" + (
            f"<NextContinuationToken>{start + 2}</NextContinuationToken>" if more else ""
        ) + "</ListBucketResult>"
        self._reply(200, body.encode(), {"Content-Type": "application/xml"})

    do_GET = do_PUT = do_HEAD = do_DELETE = _dispatch


def s3_client(server: FakeS3, **overrides) -> S3ObjectStore:
    options = dict(endpoint=server.endpoint, bucket=_S3Handler.bucket, access_key=ACCESS_KEY, secret_key=SECRET_KEY)
    options.update(overrides)
    return S3ObjectStore(**options)


# -- the contract, per backend ------------------------------------------------------


class _TempDirectory:
    def temp(self) -> Path:
        if not hasattr(self, "_temporary_directory"):
            temporary = tempfile.TemporaryDirectory(prefix="swarm-remote-test.")
            self.addCleanup(temporary.cleanup)
            self._temporary_directory = Path(temporary.name)
        return self._temporary_directory


class MemoryRemoteContract(_TempDirectory, StorageContract, unittest.TestCase):
    def new_storage(self) -> RemoteStorage:
        if not hasattr(self, "_objects"):
            self._objects = MemoryObjectStore()
        return RemoteStorage(self._objects, SqliteDatabase(self.temp() / "history"), scratch_root=self.temp() / "scratch")


class S3RemoteContract(_TempDirectory, StorageContract, unittest.TestCase):
    _counter = itertools.count()

    @classmethod
    def setUpClass(cls):
        cls.server = FakeS3()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def new_storage(self) -> RemoteStorage:
        if not hasattr(self, "_prefix"):
            self._prefix = f"run{next(self._counter)}/"
        return RemoteStorage(
            s3_client(self.server), SqliteDatabase(self.temp() / "history"), prefix=self._prefix,
            scratch_root=self.temp() / "scratch",
        )


POSTGRES_DSN = os.environ.get("SWARM_TEST_POSTGRES_DSN", "")


def apply_sql(connection, sql: str) -> None:
    """Run a migration file one statement at a time.

    psycopg prepares each ``execute`` and rejects several commands in one
    string. The platform migrations are comment-prefixed and contain no
    semicolons inside literals, matching ``web/src/schema.rs``; a ``$$`` body
    (a DO block) is one statement.
    """
    code = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    parts = code.split("$$")
    statements, current = [], ""
    for index, part in enumerate(parts):
        if index % 2:  # inside a $$ body: keep its semicolons
            current += "$$" + part + "$$"
            continue
        pieces = part.split(";")
        current += pieces[0]
        for piece in pieces[1:]:
            statements.append(current)
            current = piece
    statements.append(current)
    for statement in statements:
        statement = statement.strip()
        if statement:
            connection.execute(statement)


def _psycopg():
    try:
        import psycopg  # noqa: PLC0415 - optional, test-only
    except ImportError:
        return None
    return psycopg


@unittest.skipUnless(POSTGRES_DSN and _psycopg(), "set SWARM_TEST_POSTGRES_DSN and install psycopg to run")
class PostgresRemoteContract(_TempDirectory, StorageContract, unittest.TestCase):
    def setUp(self):
        reset_postgres((DEFAULT_TENANT, OTHER_TENANT))
        super().setUp()

    def new_storage(self) -> RemoteStorage:
        if not hasattr(self, "_objects"):
            self._objects = MemoryObjectStore()
        return RemoteStorage(self._objects, postgres_database(), scratch_root=self.temp() / "scratch")


def postgres_database() -> PostgresDatabase:
    psycopg = _psycopg()
    return PostgresDatabase(lambda: psycopg.connect(POSTGRES_DSN))


def reset_postgres(tenants) -> None:
    psycopg = _psycopg()
    with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
        for tenant in tenants:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema_name(tenant)}" CASCADE')
        exists = connection.execute("SELECT to_regclass('swarm_storage.tenants')").fetchone()[0]
        if exists:
            connection.execute("DELETE FROM swarm_storage.tenants WHERE tenant_id = ANY(%s)", (list(tenants),))


# -- object store ---------------------------------------------------------------------


class ObjectStoreTests(unittest.TestCase):
    def check_conditional_writes(self, store) -> None:
        store.put("k", b"one", if_none_match=True)
        with self.assertRaises(PreconditionFailed):
            store.put("k", b"two", if_none_match=True)
        first = store.get("k")
        self.assertEqual(first.data, b"one")
        store.put("k", b"two", if_match=first.etag)
        with self.assertRaises(PreconditionFailed):
            store.put("k", b"three", if_match=first.etag)  # stale ETag
        with self.assertRaises(PreconditionFailed):
            store.put("missing", b"x", if_match='"nope"')
        self.assertEqual(store.get("k").data, b"two")
        self.assertIsNone(store.get("absent"))
        self.assertTrue(store.delete("k"))
        self.assertFalse(store.delete("k"))

    def test_memory_store_conditional_writes(self):
        self.check_conditional_writes(MemoryObjectStore())

    def test_s3_store_conditional_writes_and_paged_listing(self):
        server = FakeS3()
        self.addCleanup(server.stop)
        store = s3_client(server)
        self.check_conditional_writes(store)
        for name in ("a/1", "a/2", "a/3", "a/4", "a/5", "b/1"):
            store.put(name, name.encode())
        self.assertEqual(store.list("a/"), ["a/1", "a/2", "a/3", "a/4", "a/5"])
        self.assertEqual(store.list("zzz"), [])
        store.put("space key/é+plus.json", b"x")  # keys are percent-encoded in the signed path
        self.assertEqual(store.get("space key/é+plus.json").data, b"x")
        self.assertTrue(all(method in ("PUT", "GET", "HEAD", "DELETE") for method, _ in server.requests))

    def test_requests_are_signed_and_a_bad_secret_is_rejected(self):
        server = FakeS3()
        self.addCleanup(server.stop)
        with self.assertRaises(ObjectStoreError) as context:
            s3_client(server, secret_key="wrong-secret").put("k", b"x")
        self.assertIn("403", str(context.exception))
        self.assertIn("SignatureDoesNotMatch", str(context.exception))
        self.assertNotIn("wrong-secret", str(context.exception))
        self.assertEqual(server.objects, {})

    def test_credentials_stay_out_of_repr_and_endpoint_rules_hold(self):
        server = FakeS3()
        self.addCleanup(server.stop)
        text = repr(s3_client(server))
        self.assertNotIn(SECRET_KEY, text)
        self.assertNotIn(ACCESS_KEY, text)
        options = dict(bucket="b", access_key="a", secret_key="s")
        for bad in ("ftp://host", "https://user:pw@host", "https://host/path", "https://host?x=1", "host"):
            with self.subTest(endpoint=bad), self.assertRaises(ObjectStoreError):
                S3ObjectStore(endpoint=bad, **options)
        with self.assertRaises(ObjectStoreError):
            S3ObjectStore(endpoint="http://s3.example.com", **options)  # cleartext to a remote host
        S3ObjectStore(endpoint="http://localhost:9000", **options)
        S3ObjectStore(endpoint="http://127.0.0.1:9000", **options)
        S3ObjectStore(endpoint="http://minio.internal:9000", allow_insecure_http=True, **options)
        with self.assertRaises(ObjectStoreError):
            S3ObjectStore(endpoint="https://s3.example.com", bucket="", access_key="a", secret_key="s")

    def test_unreachable_endpoint_is_an_object_store_error(self):
        server = FakeS3()
        endpoint = server.endpoint
        server.stop()
        with self.assertRaises(ObjectStoreError):
            s3_client(server, endpoint=endpoint, timeout=2).get("k")

    def test_signature_matches_the_published_aws_test_vector(self):
        # "get-vanilla" from the AWS Signature Version 4 test suite.
        signature, signed = sigv4_signature(
            method="GET", canonical_uri="/", query={},
            headers={"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"},
            payload_sha256=hashlib.sha256(b"").hexdigest(), amz_date="20150830T123600Z",
            region="us-east-1", service="service", secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        )
        self.assertEqual(signed, "host;x-amz-date")
        self.assertEqual(signature, "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31")

    def test_canonical_query_is_sorted_and_encoded(self):
        self.assertEqual(canonical_query({"prefix": "a b/", "list-type": "2"}), "list-type=2&prefix=a%20b%2F")


# -- remote storage specifics ----------------------------------------------------------


class RemoteLayoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="swarm-remote-layout.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.objects = MemoryObjectStore()
        self.storage = RemoteStorage(self.objects, SqliteDatabase(self.root), prefix="swarm/", scratch_root=self.root / "s")

    def test_objects_live_under_the_tenant_prefix_with_local_bytes(self):
        s = self.storage
        s.write_checkpoint("acme", "in-progress", CURRENT, {"a": 1})
        s.write_checkpoint("acme", "quota-paused", "12", {"a": 1})
        s.write_document("acme", COLLECTION, "k", {"a": 1})
        s.write_artifact("acme", "last-ai-output.log", "x")
        self.assertEqual(self.objects.list(""), [
            "swarm/tenants/acme/artifacts/last-ai-output.log",
            "swarm/tenants/acme/checkpoints/in-progress/current.json",
            "swarm/tenants/acme/checkpoints/quota-paused/12.json",
            "swarm/tenants/acme/documents/architecture_docs/k.json",
        ])
        # Same bytes LocalStorage writes, so state moves between backends unchanged.
        self.assertEqual(self.objects.get("swarm/tenants/acme/checkpoints/in-progress/current.json").data,
                         b'{\n  "a": 1\n}\n')
        self.assertEqual(self.objects.get("swarm/tenants/acme/documents/architecture_docs/k.json").data,
                         b'{\n "a": 1\n}')

    def test_a_tenant_cannot_address_another_tenants_objects(self):
        self.storage.write_checkpoint("acme", "in-progress", CURRENT, {"secret": 1})
        for tenant in ("acme/../beta", "acme/", "../acme", "ACME", ""):
            with self.subTest(tenant=tenant), self.assertRaises(StorageError):
                self.storage.read_checkpoint(tenant, "in-progress")
        self.assertIsNone(self.storage.read_checkpoint("acme-2", "in-progress"))
        self.assertEqual(self.storage.list_artifacts("acme-2"), [])

    def test_corrupt_objects_raise_storage_error(self):
        self.objects.put("swarm/tenants/acme/checkpoints/in-progress/current.json", b"{torn")
        self.objects.put("swarm/tenants/acme/checkpoints/pending-delivery/current.json", b"[1]")
        for kind in ("in-progress", "pending-delivery"):
            with self.subTest(kind=kind), self.assertRaises(StorageError):
                self.storage.read_checkpoint("acme", kind)

    def test_object_store_failures_surface_as_storage_error(self):
        class Down(MemoryObjectStore):
            def get(self, key):
                raise ObjectStoreError("connection refused")

            def put(self, key, data, **kwargs):
                raise ObjectStoreError("connection refused")

            def list(self, prefix):
                raise ObjectStoreError("connection refused")

        storage = RemoteStorage(Down(), SqliteDatabase(self.root))
        for call in (
            lambda: storage.read_checkpoint("acme", "in-progress"),
            lambda: storage.write_checkpoint("acme", "in-progress", CURRENT, {}),
            lambda: storage.list_documents("acme", COLLECTION),
            lambda: storage.append_artifact("acme", "completed-issues", "1\n"),
        ):
            with self.assertRaises(StorageError):
                call()

    def test_append_survives_a_concurrent_writer(self):
        class Racy(MemoryObjectStore):
            raced = False

            def put(self, key, data, **kwargs):
                if not Racy.raced and kwargs.get("if_match"):
                    Racy.raced = True
                    super().put(key, self.get(key).data + b"other\n")
                super().put(key, data, **kwargs)

        objects = Racy()
        storage = RemoteStorage(objects, SqliteDatabase(self.root))
        storage.append_artifact("acme", "completed-issues", "1\n")
        storage.append_artifact("acme", "completed-issues", "2\n")  # loses the race once, then retries
        self.assertEqual(storage.read_artifact("acme", "completed-issues"), "1\nother\n2\n")

    def test_append_gives_up_under_constant_contention(self):
        class Always(MemoryObjectStore):
            def put(self, key, data, **kwargs):
                if kwargs.get("if_match") or kwargs.get("if_none_match"):
                    raise PreconditionFailed(key)
                super().put(key, data, **kwargs)

        storage = RemoteStorage(Always(), SqliteDatabase(self.root))
        with self.assertRaises(StorageError):
            storage.append_artifact("acme", "completed-issues", "1\n")

    def test_publishing_something_never_written_fails_loudly(self):
        with self.assertRaises(StorageError):
            self.storage.publish_artifact("acme", "last-ai-output.log")

    def test_prefix_must_be_a_safe_relative_path(self):
        for bad in ("/abs/", "no-slash", "a/../b/"):
            with self.subTest(prefix=bad), self.assertRaises(StorageError):
                RemoteStorage(self.objects, SqliteDatabase(self.root), prefix=bad)


# -- SQL layer ---------------------------------------------------------------------------


class TranslateTests(unittest.TestCase):
    def test_sqlite_is_untouched(self):
        sql = "INSERT OR IGNORE INTO t (a) VALUES (?)"
        self.assertEqual(translate(sql, SQLITE), sql)

    def test_postgres_rewrites_placeholders_and_conflict_clauses(self):
        self.assertEqual(translate("SELECT * FROM t WHERE a = ? AND b LIKE '%x?%'", POSTGRES),
                         "SELECT * FROM t WHERE a = %s AND b LIKE '%%x?%%'")
        self.assertEqual(translate("INSERT OR IGNORE INTO ai_token_usage (id, x) VALUES (?, ?)", POSTGRES),
                         "INSERT INTO ai_token_usage (id, x) VALUES (%s, %s) ON CONFLICT DO NOTHING")
        replaced = translate("INSERT OR REPLACE INTO jev_decisions (decision_id, a, b) VALUES (?, ?, ?)", POSTGRES)
        self.assertEqual(replaced, "INSERT INTO jev_decisions (decision_id, a, b) VALUES (%s, %s, %s) "
                                   "ON CONFLICT (decision_id) DO UPDATE SET a = EXCLUDED.a, b = EXCLUDED.b")
        with self.assertRaises(StorageError):
            translate("INSERT OR REPLACE INTO other (a) VALUES (?)", POSTGRES)

    def test_every_replace_statement_of_the_history_has_a_known_key(self):
        source = Path(history_module.__file__).read_text(encoding="utf-8")
        tables = set(re.findall(r"INSERT OR REPLACE INTO (\w+)", source))
        self.assertEqual(tables, set(storage_remote._REPLACE_KEYS))


class SchemaParityTests(unittest.TestCase):
    """The hosted creation script must stay the final shape of migrations 1-10."""

    def shape(self, database_path: Path):
        with closing(sqlite3.connect(database_path)) as database:
            tables = {
                table: [(column[1], column[2].upper().replace("BIGINT", "INTEGER").replace("DOUBLE PRECISION", "REAL"),
                         column[3], column[4], column[5])
                        for column in database.execute(f"PRAGMA table_info({table})")]
                for (table,) in database.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            }
            indexes = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
            versions = {row[0] for row in database.execute("SELECT version FROM schema_migrations")}
        return tables, indexes, versions

    def test_hosted_schema_matches_a_freshly_migrated_desktop_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            history_module.ExecutionHistoryRepository(Path(tmp) / "desktop.sqlite3")
            SqliteDatabase(Path(tmp) / "hosted").provision("acme")
            desktop = self.shape(Path(tmp) / "desktop.sqlite3")
            hosted = self.shape(Path(tmp) / "hosted" / "acme.sqlite3")
        # Tables the history module owns (the desktop file also holds knowledge/diagnostic tables).
        for table, columns in hosted[0].items():
            self.assertEqual(columns, desktop[0][table], table)
        self.assertEqual(set(hosted[0]), {"ai_executions", "adversarial_rounds", "adversarial_epochs", "ai_token_usage",
                                          "jev_decisions", "jev_score_comparisons", "schema_migrations"})
        self.assertEqual(hosted[1], {name for name in desktop[1] if name in hosted[1]})
        self.assertEqual(hosted[2], set(range(1, history_module.SCHEMA_VERSION + 1)))
        self.assertTrue({3, 5, 9} <= hosted[2])

    def test_every_index_of_the_history_is_created(self):
        indexes = [re.search(r"INDEX IF NOT EXISTS (\w+)", s).group(1)
                   for s in storage_schema.HISTORY_STATEMENTS if "CREATE INDEX" in s]
        self.assertEqual(len(indexes), 13)
        self.assertEqual(len(set(indexes)), 13)


class HostedHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="swarm-remote-history.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.storage = RemoteStorage(MemoryObjectStore(), SqliteDatabase(self.root))

    def test_hosted_history_is_the_repository_with_its_sanitizer(self):
        history = self.storage.execution_history(DEFAULT_TENANT)
        self.assertIsInstance(history, history_module.ExecutionHistoryRepository)
        execution = history.create(execution_start(), NOW)
        history.append(execution, "operational_notes", "token: ghp_" + "a" * 30, NOW)
        history.update(execution, NOW, changes_summary="api_key=sk-" + "b" * 30)
        with closing(sqlite3.connect(self.root / "default.sqlite3")) as database:
            notes, summary = database.execute(
                "SELECT operational_notes, changes_summary FROM ai_executions").fetchone()
        self.assertNotIn("ghp_", notes)
        self.assertNotIn("sk-bbbb", summary)
        self.assertIn("[REDACTED]", summary)

    def test_service_gating_and_error_absorption_hold_on_the_hosted_backend(self):
        service = history_module.ExecutionHistoryService(False, Path("/unused"), storage=self.storage)
        self.assertIsNone(service.repository)  # ai_execution_history_enabled gates it
        self.assertFalse(list(self.root.glob("*.sqlite3")))
        enabled = history_module.ExecutionHistoryService(True, Path("/unused"), storage=self.storage)
        self.assertTrue(enabled.start(execution_start(), NOW))

        class Boom:
            def execution_history(self, tenant):
                raise StorageError("db down")

        broken = history_module.ExecutionHistoryService(True, Path("/unused"), storage=Boom())
        self.assertEqual(broken.start(execution_start(), NOW), "")

    def test_driver_errors_become_storage_errors(self):
        class DriverError(Exception):
            __module__ = "fakedriver.errors"

        class Cursor:
            description = None

            def execute(self, sql, params=()):
                raise DriverError("server closed the connection")

            executemany = execute

        class Raw:
            def cursor(self):
                return Cursor()

            def commit(self):
                raise DriverError("commit failed")

            def rollback(self):
                pass

            def close(self):
                pass

        connection = storage_remote.Connection(Raw(), POSTGRES)
        with self.assertRaises(StorageError):
            connection.execute("SELECT 1")
        with self.assertRaises(StorageError):
            connection.executemany("INSERT INTO t VALUES (?)", [(1,)])
        with self.assertRaises(StorageError):
            with connection:
                pass
        with self.assertRaises(ValueError):  # a programming error is not storage unavailability
            with storage_remote.Connection(Raw(), POSTGRES):
                raise ValueError("bad field")

        def unreachable():
            raise DriverError("could not connect")

        with self.assertRaises(StorageError):
            PostgresDatabase(unreachable).provision("acme")

    def test_schema_names_fit_postgres_and_stay_distinct(self):
        self.assertEqual(schema_name("acme"), "t_acme")
        long_a, long_b = "a" * 63, "a" * 62 + "b"
        names = {schema_name(long_a), schema_name(long_b)}
        self.assertEqual(len(names), 2)
        self.assertTrue(all(len(name) <= 63 for name in names))
        for bad in ("Acme", "a b", "", "../x"):
            with self.assertRaises(StorageError):
                schema_name(bad)
        with self.assertRaises(StorageError):
            storage_remote.quote_identifier('x"; DROP SCHEMA public; --')


@unittest.skipUnless(POSTGRES_DSN and _psycopg(), "set SWARM_TEST_POSTGRES_DSN and install psycopg to run")
class PostgresSpecificTests(unittest.TestCase):
    def setUp(self):
        self.tenants = ("pgtest-a", "pgtest-b")
        reset_postgres(self.tenants)
        self.addCleanup(reset_postgres, self.tenants)
        self.storage = RemoteStorage(MemoryObjectStore(), postgres_database())

    def test_each_tenant_has_its_own_schema_and_registry_row(self):
        a = self.storage.execution_history("pgtest-a")
        self.storage.execution_history("pgtest-b")
        execution = a.create(execution_start(), NOW)
        psycopg = _psycopg()
        with psycopg.connect(POSTGRES_DSN) as connection:
            schemas = {row[0] for row in connection.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 't\\_pgtest%'")}
            registry = dict(connection.execute(
                "SELECT tenant_id, schema_name FROM swarm_storage.tenants WHERE tenant_id LIKE 'pgtest-%'").fetchall())
            in_a = connection.execute('SELECT count(*) FROM "t_pgtest-a".ai_executions').fetchone()[0]
            in_b = connection.execute('SELECT count(*) FROM "t_pgtest-b".ai_executions').fetchone()[0]
        self.assertEqual(schemas, {"t_pgtest-a", "t_pgtest-b"})
        self.assertEqual(registry, {"pgtest-a": "t_pgtest-a", "pgtest-b": "t_pgtest-b"})
        self.assertEqual((in_a, in_b), (1, 0))
        self.assertTrue(a.execution_exists(execution))

    def test_hosted_columns_match_the_desktop_schema(self):
        self.storage.execution_history("pgtest-a")
        with tempfile.TemporaryDirectory() as tmp:
            history_module.ExecutionHistoryRepository(Path(tmp) / "desktop.sqlite3")
            with closing(sqlite3.connect(Path(tmp) / "desktop.sqlite3")) as database:
                desktop = {table: {row[1] for row in database.execute(f"PRAGMA table_info({table})")}
                           for table in ("ai_executions", "adversarial_rounds", "adversarial_epochs",
                                         "ai_token_usage", "jev_decisions", "jev_score_comparisons")}
        psycopg = _psycopg()
        with psycopg.connect(POSTGRES_DSN) as connection:
            for table, columns in desktop.items():
                hosted = {row[0] for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = 't_pgtest-a' "
                    "AND table_name = %s", (table,))}
                self.assertEqual(hosted - {"seq"}, columns, table)
            versions = {row[0] for row in connection.execute('SELECT version FROM "t_pgtest-a".schema_migrations')}
        self.assertEqual(versions, set(range(1, history_module.SCHEMA_VERSION + 1)))

    def test_provisioning_twice_and_concurrently_is_safe(self):
        errors = []

        def provision():
            try:
                postgres_database().provision("pgtest-a")
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=provision) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        postgres_database().provision("pgtest-a")

    def test_concurrent_creates_get_distinct_attempt_numbers(self):
        history = self.storage.execution_history("pgtest-a")
        results = []
        errors = []

        def create():
            try:
                results.append(postgres_database().history("pgtest-a").create(execution_start(), NOW))
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=create) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 6)
        self.assertEqual(len(history.final_statuses_for_issue("o/r", 415)), 6)
        psycopg = _psycopg()
        with psycopg.connect(POSTGRES_DSN) as connection:
            attempts = sorted(row[0] for row in connection.execute('SELECT attempt_number FROM "t_pgtest-a".ai_executions'))
        self.assertEqual(attempts, [1, 2, 3, 4, 5, 6])

    def test_jev_records_are_replaced_by_id_and_nul_bytes_are_dropped(self):
        history = self.storage.execution_history("pgtest-a")
        execution = history.create(execution_start(), NOW)
        history.record_jev_decision({"decision_id": "d1", "execution_id": execution, "decision": "first"})
        history.record_jev_decision({"decision_id": "d1", "execution_id": execution, "decision": "second\x00!"})
        history.record_jev_score_comparison({"comparison_id": "c1", "execution_id": execution})
        history.record_jev_score_comparison({"comparison_id": "c1", "execution_id": execution, "workflow_outcome": "x"})
        history.finish_jev_outcomes(execution, "completed")
        psycopg = _psycopg()
        with psycopg.connect(POSTGRES_DSN) as connection:
            rows = connection.execute('SELECT decision, workflow_outcome FROM "t_pgtest-a".jev_decisions').fetchall()
            comparisons = connection.execute(
                'SELECT workflow_outcome FROM "t_pgtest-a".jev_score_comparisons').fetchall()
        self.assertEqual(rows, [("second!", "completed")])
        self.assertEqual(comparisons, [("x",)])


@unittest.skipUnless(POSTGRES_DSN and _psycopg(), "set SWARM_TEST_POSTGRES_DSN and install psycopg to run")
class PlatformSchemaTests(unittest.TestCase):
    """``web/migrations`` applies cleanly, twice, and enforces its constraints."""

    MIGRATIONS = Path(__file__).resolve().parent.parent / "web" / "migrations"
    SCHEMA = "platform_schema_test"

    def setUp(self):
        if not self.MIGRATIONS.is_dir():
            self.skipTest("web/migrations is not part of this checkout")
        psycopg = _psycopg()
        self.connection = psycopg.connect(POSTGRES_DSN, autocommit=True)
        self.addCleanup(self.connection.close)
        self.connection.execute(f"DROP SCHEMA IF EXISTS {self.SCHEMA} CASCADE")
        self.connection.execute(f"CREATE SCHEMA {self.SCHEMA}")
        self.addCleanup(lambda: self.connection.execute(f"DROP SCHEMA IF EXISTS {self.SCHEMA} CASCADE"))
        self.connection.execute(f"SET search_path TO {self.SCHEMA}")
        for path in sorted(self.MIGRATIONS.glob("*.sql")):
            apply_sql(self.connection, path.read_text(encoding="utf-8"))

    def tables(self):
        return {row[0] for row in self.connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (self.SCHEMA,))}

    def test_applies_twice_and_records_its_version(self):
        before = self.tables()
        for path in sorted(self.MIGRATIONS.glob("*.sql")):
            apply_sql(self.connection, path.read_text(encoding="utf-8"))
        self.assertEqual(self.tables(), before)
        self.assertTrue({"tenants", "users", "sessions", "user_identities", "admin_audit_log", "tenant_memberships", "tenant_provider_keys",
                         "tenant_plan_quotas", "tenant_budgets", "tenant_usage_ledger", "tenant_provider_reports",
                         "tenant_jobs", "webhook_deliveries"} <= before)
        self.assertEqual(self.connection.execute("SELECT version FROM platform_migrations").fetchall(),
                         [("0001_platform",), ("0002_identity",), ("0003_tenant_documents",), ("0004_personal_tenants",)])
        self.assertFalse({"web_users", "web_sessions"} & before)

    def test_tenant_rows_cascade_and_are_constrained(self):
        psycopg = _psycopg()
        run = self.connection.execute
        run("INSERT INTO tenants (tenant_id, installation_id, account_login, account_type) VALUES ('acme', 1, 'acme', 'Organization')")
        run("INSERT INTO tenant_provider_keys VALUES ('acme', 'claude', 1, 'k1', '\\x01', '\\x02', 5, 'u')")
        run("INSERT INTO tenant_usage_ledger (tenant_id, entry_id, period, provider, cost_usd) VALUES ('acme', 'e1', '2026-10', 'claude', NULL)")
        run("INSERT INTO tenant_jobs (tenant_id, job_id, provider) VALUES ('acme', 'j1', 'claude')")
        self.assertIsNone(run("SELECT cost_usd FROM tenant_usage_ledger").fetchone()[0])  # unpriced stays NULL
        for bad in (
            "INSERT INTO tenants (tenant_id, installation_id, account_login, account_type) VALUES ('Bad Id', 2, 'x', 'User')",
            "INSERT INTO tenants (tenant_id, installation_id, account_login, account_type) VALUES ('dup', 1, 'x', 'User')",
            "INSERT INTO tenant_usage_ledger (tenant_id, entry_id, period, provider) VALUES ('acme', 'e1', '2026-10', 'claude')",
            "INSERT INTO tenant_usage_ledger (tenant_id, entry_id, period, provider) VALUES ('acme', 'e2', '2026-13', 'claude')",
            "INSERT INTO tenant_jobs (tenant_id, job_id, provider) VALUES ('nobody', 'j2', 'claude')",
            "INSERT INTO tenant_provider_keys VALUES ('acme', 'gemini', 1, 'k', '\\x01', '\\x02', 5, 'u')",
        ):
            with self.subTest(statement=bad[:60]), self.assertRaises(psycopg.errors.Error):
                run(bad)
        run("DELETE FROM tenants WHERE tenant_id = 'acme'")
        for table in ("tenant_provider_keys", "tenant_usage_ledger", "tenant_jobs"):
            self.assertEqual(run(f"SELECT count(*) FROM {table}").fetchone()[0], 0, table)


    def test_a_personal_tenant_needs_no_installation_and_is_unique_per_owner(self):
        psycopg = _psycopg()
        run = self.connection.execute
        run("INSERT INTO users (id) VALUES ('u1'), ('u2')")
        run("INSERT INTO tenants (tenant_id, installation_id, account_login, account_type, owner_user_id) "
            "VALUES ('octo', NULL, 'octo', 'Personal', 'u1'), ('mona', NULL, 'mona', 'Personal', 'u2')")
        for bad in (
            # a second personal tenant for one owner
            "INSERT INTO tenants (tenant_id, installation_id, account_login, account_type, owner_user_id) "
            "VALUES ('octo-2', NULL, 'octo', 'Personal', 'u1')",
            # an id outside the tenant grammar
            "INSERT INTO tenants (tenant_id, installation_id, account_login, account_type, owner_user_id) "
            "VALUES ('Octo Cat', NULL, 'octo', 'Personal', 'u2')",
        ):
            with self.subTest(statement=bad[-40:]), self.assertRaises(psycopg.errors.Error):
                run(bad)
        # Installation tenants are still one per installation, and may share an owner.
        run("INSERT INTO tenants (tenant_id, installation_id, account_login, account_type, owner_user_id) "
            "VALUES ('acme', 7, 'acme', 'Organization', 'u1')")
        with self.assertRaises(psycopg.errors.Error):
            run("INSERT INTO tenants (tenant_id, installation_id, account_login, account_type) "
                "VALUES ('acme-2', 7, 'acme', 'Organization')")

    def test_identity_migration_upgrades_a_0001_database_and_backfills(self):
        run = self.connection.execute
        self.connection.execute("DROP SCHEMA IF EXISTS %s CASCADE" % self.SCHEMA)
        self.connection.execute("CREATE SCHEMA %s" % self.SCHEMA)
        self.connection.execute("SET search_path TO %s" % self.SCHEMA)
        files = sorted(self.MIGRATIONS.glob("*.sql"))
        apply_sql(self.connection, files[0].read_text(encoding="utf-8"))
        run("INSERT INTO web_users (id, github_id, login) VALUES ('u1', 4242, 'octo')")
        run("INSERT INTO web_sessions VALUES ('h', 'u1', 'c', 99)")
        for path in files[1:] + files[1:]:  # the second pass is the re-run
            apply_sql(self.connection, path.read_text(encoding="utf-8"))
        self.assertEqual(run("SELECT provider, subject, login FROM user_identities WHERE user_id = 'u1'").fetchall(),
                         [("github", "4242", "octo")])
        self.assertEqual(run("SELECT count(*) FROM sessions WHERE user_id = 'u1'").fetchone()[0], 1)
        columns = {row[0] for row in run(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = 'users'",
            (self.SCHEMA,))}
        self.assertTrue({"display_name", "avatar_url", "email", "is_platform_admin", "created_at",
                         "last_login_at"} <= columns)
        self.assertFalse({"github_id", "login"} & columns)
        self.assertIs(run("SELECT is_platform_admin FROM users").fetchone()[0], False)

    def test_identity_constraints_reject_orphans_and_duplicates(self):
        psycopg = _psycopg()
        run = self.connection.execute
        run("INSERT INTO users (id) VALUES ('u1')")
        run("INSERT INTO users (id) VALUES ('u2')")
        run("INSERT INTO tenants (tenant_id, installation_id, account_login, account_type, owner_user_id) "
            "VALUES ('acme', 1, 'acme', 'User', 'u1')")
        run("INSERT INTO user_identities (user_id, provider, subject, login) VALUES ('u1', 'github', '1', 'a')")
        run("INSERT INTO tenant_memberships VALUES ('acme', 'u1', 'owner')")
        for bad in (
            "INSERT INTO user_identities (user_id, provider, subject, login) VALUES ('ghost', 'github', '2', 'g')",
            "INSERT INTO user_identities (user_id, provider, subject, login) VALUES ('u2', 'github', '1', 'dup')",
            "INSERT INTO tenant_memberships VALUES ('acme', 'u1', 'member')",
            "INSERT INTO tenant_memberships VALUES ('acme', 'u2', 'admin')",
            "INSERT INTO tenant_memberships VALUES ('acme', 'ghost', 'member')",
            "INSERT INTO tenant_memberships VALUES ('nobody', 'u2', 'member')",
            "INSERT INTO tenants (tenant_id, installation_id, account_login, account_type, owner_user_id) "
            "VALUES ('b', 2, 'b', 'User', 'ghost')",
            "INSERT INTO sessions VALUES ('t', 'ghost', 'c', 1)",
            "INSERT INTO admin_audit_log (actor_user_id, target_user_id, action) VALUES ('ghost', 'u1', 'x')",
            "INSERT INTO admin_audit_log (actor_user_id, target_user_id, action) VALUES ('u1', 'ghost', 'x')",
        ):
            with self.subTest(statement=bad[:70]), self.assertRaises(psycopg.errors.Error):
                run(bad)
        # The same subject under another provider is a different identity.
        run("INSERT INTO user_identities (user_id, provider, subject, login) VALUES ('u2', 'oidc', '1', 'b')")

    def test_deleting_a_user_cascades_and_keeps_tenant_and_audit_rows(self):
        run = self.connection.execute
        run("INSERT INTO users (id) VALUES ('u1')")
        run("INSERT INTO users (id) VALUES ('u2')")
        run("INSERT INTO tenants (tenant_id, installation_id, account_login, account_type, owner_user_id) "
            "VALUES ('acme', 1, 'acme', 'User', 'u1')")
        run("INSERT INTO user_identities (user_id, provider, subject, login) VALUES ('u1', 'github', '1', 'a')")
        run("INSERT INTO sessions VALUES ('t', 'u1', 'c', 1)")
        run("INSERT INTO tenant_memberships VALUES ('acme', 'u1', 'owner')")
        run("INSERT INTO admin_audit_log (actor_user_id, target_user_id, action) VALUES ('u2', 'u1', 'suspend')")
        run("DELETE FROM users WHERE id = 'u1'")
        for table in ("user_identities", "sessions", "tenant_memberships"):
            self.assertEqual(run(f"SELECT count(*) FROM {table}").fetchone()[0], 0, table)
        self.assertEqual(run("SELECT owner_user_id FROM tenants").fetchone()[0], None)
        self.assertEqual(run("SELECT actor_user_id, target_user_id FROM admin_audit_log").fetchone(), ("u2", None))

    def test_every_foreign_key_column_is_indexed(self):
        rows = self.connection.execute(
            """SELECT c.conrelid::regclass::text, a.attname
               FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
               WHERE c.contype = 'f' AND c.connamespace = %s::regnamespace""", (self.SCHEMA,)).fetchall()
        self.assertTrue(rows)
        for table, column in rows:
            table = table.split(".")[-1]
            indexed = self.connection.execute(
                """SELECT 1 FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
                   WHERE i.indrelid = %s::regclass AND a.attname = %s""",
                (f"{self.SCHEMA}.{table}", column)).fetchone()
            self.assertIsNotNone(indexed, f"{table}.{column} has no index")


if __name__ == "__main__":
    unittest.main()
