"""Local-implementation tests for the storage interface.

``StorageContract`` (storage_contract.py) is the reusable suite; this module
runs it against ``LocalStorage`` and adds what is specific to the local
layout: the on-disk names the desktop and worker have always used, the
unchanged history schema, and that no worker module imports desktop code.
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import ai_execution_history as history_module
import architecture_docs
from storage import CURRENT, DEFAULT_TENANT, LocalStorage, StorageError
from storage_contract import StorageContract, execution_start
from swarm_issue_worker import Config, Worker, build_parser

HERE = Path(__file__).resolve().parent


class LocalStorageContract(StorageContract, unittest.TestCase):
    def new_storage(self) -> LocalStorage:
        if not hasattr(self, "_root"):
            temporary = tempfile.TemporaryDirectory(prefix="swarm-storage-test.")
            self.addCleanup(temporary.cleanup)
            self._root = Path(temporary.name)
        return LocalStorage(self._root / "state", history_database=self._root / "history" / "swarm-automation.sqlite3")


class LocalLayoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="swarm-storage-layout.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.storage = LocalStorage(self.root)

    def test_default_tenant_uses_the_legacy_file_names(self):
        s = self.storage
        s.write_checkpoint(DEFAULT_TENANT, "in-progress", CURRENT, {"a": 1})
        s.write_checkpoint(DEFAULT_TENANT, "pending-delivery", CURRENT, {"a": 1})
        s.write_checkpoint(DEFAULT_TENANT, "integration-recovery", CURRENT, {"a": 1})
        s.write_checkpoint(DEFAULT_TENANT, "promotion-blocked", CURRENT, {"a": 1})
        s.write_checkpoint(DEFAULT_TENANT, "quota-paused", "12", {"a": 1})
        s.write_checkpoint(DEFAULT_TENANT, "closed-paused", "12", {"a": 1})
        s.write_artifact(DEFAULT_TENANT, "last-ai-output.log", "x")
        for relative in (
            "in-progress-issue.json", "pending-delivery.json", "integration-recovery.json",
            "promotion-blocked.json", "quota-paused-issues/12.json", "closed-paused-issues/12.json",
            "last-ai-output.log",
        ):
            self.assertTrue((self.root / relative).is_file(), relative)
        # Same JSON the worker's atomic_write_json always produced.
        self.assertEqual((self.root / "in-progress-issue.json").read_text(), '{\n  "a": 1\n}\n')

    def test_legacy_files_written_directly_are_readable(self):
        (self.root / "in-progress-issue.json").write_text('{"issue_number": 3}', encoding="utf-8")
        (self.root / "quota-paused-issues").mkdir()
        (self.root / "quota-paused-issues" / "3.json").write_text('{"issue_number": 3}', encoding="utf-8")
        self.assertEqual(self.storage.read_checkpoint(DEFAULT_TENANT, "in-progress"), {"issue_number": 3})
        self.assertEqual(self.storage.list_checkpoints(DEFAULT_TENANT, "quota-paused"), ["3"])

    def test_other_tenants_live_under_their_own_subtree(self):
        self.storage.write_checkpoint("acme", "in-progress", CURRENT, {"a": 1})
        self.assertTrue((self.root / "tenants" / "acme" / "in-progress-issue.json").is_file())
        self.assertFalse((self.root / "in-progress-issue.json").exists())
        self.assertEqual(self.storage.history_database_path("acme"),
                         self.root / "tenants" / "acme" / "swarm-automation.sqlite3")

    def test_corrupt_checkpoint_raises_storage_error(self):
        (self.root / "in-progress-issue.json").write_text("{torn", encoding="utf-8")
        with self.assertRaises(StorageError):
            self.storage.read_checkpoint(DEFAULT_TENANT, "in-progress")
        (self.root / "pending-delivery.json").write_text("[1]", encoding="utf-8")
        with self.assertRaises(StorageError):
            self.storage.read_checkpoint(DEFAULT_TENANT, "pending-delivery")

    def test_write_leaves_no_temporary_files(self):
        self.storage.write_checkpoint(DEFAULT_TENANT, "in-progress", CURRENT, {"a": 1})
        self.storage.write_document(DEFAULT_TENANT, "architecture_docs", "k", {"a": 1})
        self.assertEqual(sorted(p.name for p in self.root.rglob("*") if p.is_file() and p.name.startswith(".")), [])

    def test_collection_dirs_relocate_documents_for_the_default_tenant_only(self):
        docs = self.root / "elsewhere"
        storage = LocalStorage(self.root, collection_dirs={"architecture_docs": docs})
        storage.write_document(DEFAULT_TENANT, "architecture_docs", "k", {"a": 1})
        storage.write_document("acme", "architecture_docs", "k", {"a": 2})
        self.assertTrue((docs / "k.json").is_file())
        self.assertTrue((self.root / "tenants" / "acme" / "architecture_docs" / "k.json").is_file())
        with self.assertRaises(StorageError):
            LocalStorage(self.root, collection_dirs={"bogus": docs})


class HistorySchemaTests(unittest.TestCase):
    def test_local_history_keeps_every_migration_and_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalStorage(Path(tmp))
            repository = storage.execution_history(DEFAULT_TENANT)
            self.assertIsInstance(repository, history_module.ExecutionHistoryRepository)
            with closing(sqlite3.connect(Path(tmp) / "swarm-automation.sqlite3")) as database:
                versions = {row[0] for row in database.execute("SELECT version FROM schema_migrations")}
                columns = {row[1] for row in database.execute("PRAGMA table_info(ai_executions)")}
                tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(versions, set(range(1, history_module.SCHEMA_VERSION + 1)))
            self.assertTrue({3, 5, 9} <= versions)
            self.assertIn("ai_token_usage", tables)
            self.assertIn("adversarial_rounds", tables)
            self.assertTrue({"security_outcome", "security_epoch_count", "adversarial_merge_policy"} <= columns)

    def test_service_uses_the_storage_backend_and_absorbs_its_errors(self):
        class Broken:
            def execution_history(self, tenant):
                raise StorageError("backend unavailable")

        service = history_module.ExecutionHistoryService(True, Path("/nonexistent/x.sqlite3"), storage=Broken())
        self.assertIsNone(service.repository)
        self.assertIn("backend unavailable", service.error)
        self.assertEqual(service.start(execution_start(), "now"), "")
        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalStorage(Path(tmp))
            service = history_module.ExecutionHistoryService(True, Path(tmp) / "unused.sqlite3", storage=storage)
            execution = service.start(execution_start(), "2026-01-01T00:00:00+00:00")
            self.assertTrue(storage.execution_history(DEFAULT_TENANT).execution_exists(execution))
            self.assertFalse((Path(tmp) / "unused.sqlite3").exists())
            self.assertEqual(history_module.ExecutionHistoryService(False, Path(tmp) / "x", storage=storage).repository, None)


class ArchitectureStoreTests(unittest.TestCase):
    def test_store_persists_through_a_supplied_storage_per_tenant(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalStorage(Path(tmp))
            first = architecture_docs.ArchitectureStore(Path(tmp) / "unused", "o/r", storage=storage)
            second = architecture_docs.ArchitectureStore(Path(tmp) / "unused", "o/r", storage=storage, tenant="acme")
            snapshot = first.load()
            snapshot["entities"] = {"x": {"id": "x"}}
            first.save(snapshot)
            self.assertEqual(architecture_docs.ArchitectureStore(Path(tmp) / "unused", "o/r", storage=storage).load()["entities"],
                             {"x": {"id": "x"}})
            self.assertEqual(second.load()["entities"], {})
            self.assertFalse((Path(tmp) / "unused").exists())

    def test_default_layout_is_a_json_file_in_the_given_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = architecture_docs.ArchitectureStore(Path(tmp) / "architecture_docs", "o/r")
            store.save(store.load())
            self.assertTrue(store.path.is_file())
            self.assertEqual(store.path.parent, Path(tmp) / "architecture_docs")
            self.assertEqual(json.loads(store.path.read_text())["repository"], "o/r")


class WorkerStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="swarm-worker-storage.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "repo").mkdir()
        argv = [
            "--repo-dir", str(self.root / "repo"), "--state-dir", str(self.root / "state"),
            "--gh-bin", "/usr/bin/false", "--claude-bin", "", "--codex-bin", "", "--grok-bin", "",
            "--github-apps-config", str(self.root / "none.json"), "--no-require-bot-auth",
        ]
        self.config = Config.from_args(build_parser().parse_args(argv))
        self.worker = Worker(self.config)

    def test_worker_state_paths_come_from_the_storage_layout(self):
        s, t = self.worker.storage, self.worker.tenant
        self.assertEqual(t, DEFAULT_TENANT)
        self.assertEqual(self.worker.in_progress_file, s.checkpoint_path(t, "in-progress"))
        self.assertEqual(self.worker.pending_file, s.checkpoint_path(t, "pending-delivery"))
        self.assertEqual(self.worker.paused_dir, s.checkpoint_directory(t, "quota-paused"))
        self.assertEqual(self.worker.in_progress_file.name, "in-progress-issue.json")
        self.assertEqual(self.worker.paused_dir.name, "quota-paused-issues")
        self.assertEqual(self.worker.closed_paused_dir.name, "closed-paused-issues")
        self.assertEqual(self.worker.promotion_blocked_file().name, "promotion-blocked.json")
        self.assertEqual(self.worker.ai_output_file.name, "last-ai-output.log")
        self.assertEqual(self.worker.completed_file.name, "completed-issues")
        for path in (self.worker.in_progress_file, self.worker.ai_output_file, self.worker.completed_file):
            self.assertEqual(path.parent, self.config.state_dir)

    def test_checkpoint_written_by_the_worker_resumes_through_a_fresh_storage(self):
        state = {"issue_number": 415, "ai_tool": "Claude", "adversarial": {"epoch": 2, "round": 1}}
        self.worker.write_state(state)
        self.worker.record_completed(7)
        self.worker.ai_output_file.write_text("last output", encoding="utf-8")
        fresh = LocalStorage(self.config.state_dir, history_database=self.config.execution_history_db)
        self.assertEqual(fresh.read_checkpoint(DEFAULT_TENANT, "in-progress"), state)
        self.assertEqual(fresh.read_artifact(DEFAULT_TENANT, "completed-issues"), "7\n")
        self.assertEqual(fresh.read_artifact(DEFAULT_TENANT, "last-ai-output.log"), "last output")
        # And the reverse: a checkpoint another process wrote is what a new Worker sees.
        fresh.write_checkpoint(DEFAULT_TENANT, "in-progress", CURRENT, dict(state, issue_number=416))
        self.assertEqual(Worker(self.config).read_state()["issue_number"], 416)
        self.assertEqual(Worker(self.config).completed_numbers(), {7})

    def test_worker_history_runs_on_the_storage_backend(self):
        self.assertIs(self.worker.history.repository, self.worker.storage.execution_history(DEFAULT_TENANT))


class WorkerImageTests(unittest.TestCase):
    """The future worker image copies ``issue_worker/`` and nothing else."""

    def test_worker_modules_import_only_stdlib_and_siblings(self):
        siblings = {path.stem for path in HERE.glob("*.py")}
        stdlib = set(sys.stdlib_module_names)
        offenders = {}
        for path in sorted(HERE.glob("*.py")):
            if path.name.startswith("test_") or path.name == "storage_contract.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                else:
                    continue
                for name in names:
                    if name not in stdlib and name not in siblings:
                        offenders.setdefault(path.name, set()).add(name)
        self.assertEqual(offenders, {})

    def test_entrypoint_runs_from_an_isolated_copy_of_the_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "app" / "issue_worker"
            image.mkdir(parents=True)
            for path in HERE.glob("*.py"):
                if not path.name.startswith("test_"):
                    (image / path.name).write_bytes(path.read_bytes())
            environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
            result = subprocess.run(
                [sys.executable, "-I", str(image / "worker_entrypoint.py"), "--help"],
                cwd=tmp, env=environment, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--state-dir", result.stdout)
            probe = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import sys; sys.path.insert(0, sys.argv[1]); import storage, swarm_issue_worker; "
                 "print(swarm_issue_worker.Worker.__name__, storage.DEFAULT_TENANT)", str(image)],
                cwd=tmp, env=environment, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(probe.stdout.split(), ["Worker", "default"])


if __name__ == "__main__":
    unittest.main()
