"""Issue #417 acceptance: hosted storage and a re-runnable desktop import.

Oracle (from the issue, before treating the implementation as correct):

* Local and hosted storage both keep tenant-scoped checkpoints, documents,
  artifacts and execution history, including a fresh-process resume.
* A fixture desktop install (settings, architecture docs, history) imports into
  one tenant. Running it again changes nothing. A dry run writes nothing.
* Provider credentials, key files, URLs and IP addresses never arrive in the
  tenant. History text still goes through the execution-history sanitizer.
* ``ai_execution_history_enabled`` still gates live history. Migrations 3, 5
  and 9 columns (security, epoch, merge policy) survive the copy.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ISSUE_WORKER_DIR = Path(__file__).resolve().parents[2] / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import ai_execution_history as history  # noqa: E402
import desktop_import  # noqa: E402
from object_store import MemoryObjectStore  # noqa: E402
from storage import DEFAULT_TENANT, LocalStorage  # noqa: E402
from storage_contract import NOW, StorageContract, execution_start  # noqa: E402
from storage_remote import RemoteStorage, SqliteDatabase  # noqa: E402

TOKEN = "ghp_" + "Q" * 36
KEY_MATERIAL = "-----BEGIN PRIVATE KEY-----\nUATKEYMATERIAL417\n-----END PRIVATE KEY-----"
ROOT = Path(__file__).resolve().parents[2]


def desktop_fixture(root: Path) -> dict[str, Path]:
    state = root / "state"
    (state / "architecture_docs").mkdir(parents=True)
    key_file = root / "github-apps.json"
    key_file.write_text(KEY_MATERIAL, encoding="utf-8")
    config = {
        "repositories": [{
            "id": "acme__web", "github_repository": "acme/web", "assignee": "dev",
            "repo_dir": "/Users/dev/web", "github_apps_config": str(key_file),
            "adversarial_best_effort_merge": False,
        }],
        "worker_state_dir": str(state),
        "workspace_root": "/Users/dev/checkouts",
        "preferred_provider": "auto",
        "github_token": TOKEN,
        "providers": [{"id": "claude", "enabled": True, "model": "m", "effort": "high", "bin": "/usr/bin/claude"}],
    }
    config_path = root / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (state / "architecture_docs" / "acme_web-uat.json").write_text(json.dumps({
        "schema": 1, "repository": "acme/web",
        "entities": {"edge": {"summary": f"host https://10.1.2.3/secret and {TOKEN}"}},
    }), encoding="utf-8")
    database = state / "swarm-automation.sqlite3"
    repository = history.ExecutionHistoryRepository(database)
    execution = repository.create(execution_start(issue=417, repository="acme/web"), NOW)
    repository.update(
        execution, NOW, final_status="completed", security_outcome="PASS",
        adversarial_epoch_count=1, security_epoch_count=1, adversarial_merge_policy="strict",
        adversarial_delivery="verified_clean", promotion_status="merged",
    )
    repository.record_adversarial_epoch(execution, {"stage": "uat", "epoch_number": 1, "merge_policy": "strict"})
    repository.append(execution, "operational_notes", "Delivered", NOW)
    # A secret written under the sanitizer (an older desktop row).
    with closing(sqlite3.connect(database)) as raw:
        raw.execute(
            "UPDATE ai_executions SET changes_summary = ? WHERE execution_id = ?",
            (f"token: {TOKEN}", execution),
        )
        raw.commit()
    return {"config": config_path, "state": state, "key_file": key_file, "execution": execution}


class BothBackendsResumeTests(unittest.TestCase):
    def _stores(self, root: Path):
        yield "local", LocalStorage(root / "local")
        yield "hosted", RemoteStorage(MemoryObjectStore(), SqliteDatabase(root / "hosted"))

    def test_exit_13_checkpoint_resumes_on_local_and_hosted_storage(self):
        state = {
            "issue_number": 417, "ai_tool": "Grok", "base_sha": "c" * 40,
            "adversarial": {"phase": "fix", "epoch": 1, "round": 0},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, storage in self._stores(root):
                with self.subTest(backend=name):
                    storage.write_checkpoint(DEFAULT_TENANT, "in-progress", "current", state)
                    restarted = (
                        LocalStorage(root / "local") if name == "local"
                        else RemoteStorage(storage.objects, storage.database)
                    )
                    self.assertEqual(restarted.read_checkpoint(DEFAULT_TENANT, "in-progress"), state)
                    self.assertIsNone(restarted.read_checkpoint("other-tenant", "in-progress"))


class DesktopImportAcceptanceTests(unittest.TestCase):
    def test_fixture_import_is_idempotent_and_drops_credentials_urls_and_ips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            desktop = desktop_fixture(root / "desktop")
            key_bytes = desktop["key_file"].read_bytes()
            storage = RemoteStorage(MemoryObjectStore(), SqliteDatabase(root / "hosted"))
            before_schema = list((root / "hosted").glob("*.sqlite3"))
            dry = desktop_import.run_import(
                storage, "acme", config_path=desktop["config"], state_dir=desktop["state"], dry_run=True,
            )
            self.assertTrue(dry["dry_run"])
            self.assertEqual(dry["history"]["tables"]["ai_executions"]["imported"], 1)
            self.assertEqual(list((root / "hosted").glob("*.sqlite3")), before_schema)
            self.assertIsNone(storage.read_document("acme", "tenant_config", "app"))

            first = desktop_import.run_import(
                storage, "acme", config_path=desktop["config"], state_dir=desktop["state"],
            )
            self.assertEqual(first["config"]["documents"]["app"], "imported")
            self.assertEqual(first["credentials"]["imported"], 0)
            blob = json.dumps({
                "app": storage.read_document("acme", "tenant_config", "app"),
                "repo": storage.read_document("acme", "tenant_config", "repo-acme__web"),
                "docs": storage.read_document("acme", "architecture_docs", "acme_web-uat"),
            })
            self.assertNotIn(TOKEN, blob)
            self.assertNotIn("UATKEYMATERIAL417", blob)
            self.assertNotIn("10.1.2.3", blob)
            self.assertNotIn("https://", blob)
            self.assertNotIn(str(desktop["key_file"]), blob)
            self.assertEqual(desktop["key_file"].read_bytes(), key_bytes)
            history_store = storage.execution_history("acme")
            self.assertTrue(history_store.execution_exists(desktop["execution"]))
            self.assertFalse(storage.execution_history("other-tenant").execution_exists(desktop["execution"]))
            with storage.database.connect("acme") as database:
                notes = database.execute(
                    "SELECT changes_summary, security_outcome, adversarial_merge_policy, adversarial_epoch_count "
                    "FROM ai_executions WHERE execution_id = ?", (desktop["execution"],),
                ).fetchone()
            self.assertNotIn(TOKEN, notes[0])
            self.assertIn("[REDACTED]", notes[0])
            self.assertEqual(tuple(notes[1:]), ("PASS", "strict", 1))

            second = desktop_import.run_import(
                storage, "acme", config_path=desktop["config"], state_dir=desktop["state"],
            )
            self.assertEqual(second["config"]["documents"]["app"], "unchanged")
            self.assertEqual(second["history"]["tables"]["ai_executions"]["imported"], 0)
            self.assertEqual(second["history"]["tables"]["ai_executions"]["existing"], 1)

    def test_disabled_history_does_not_provision_a_hosted_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = RemoteStorage(MemoryObjectStore(), SqliteDatabase(Path(tmp)))
            service = history.ExecutionHistoryService(False, Path(tmp) / "unused.sqlite3", storage=storage)
            self.assertIsNone(service.repository)
            self.assertEqual(service.start(execution_start(), NOW), "")
            self.assertEqual(list(Path(tmp).glob("*.sqlite3")), [])


class PlatformSchemaShapeTests(unittest.TestCase):
    def test_platform_migration_names_tenants_jobs_usage_and_sealed_keys(self):
        sql = (ROOT / "web" / "migrations" / "0001_platform.sql").read_text(encoding="utf-8")
        for table in (
            "web_users", "web_sessions", "tenants", "tenant_memberships", "tenant_provider_keys",
            "tenant_plan_quotas", "tenant_budgets", "tenant_usage_ledger", "tenant_provider_reports",
            "tenant_jobs", "webhook_deliveries",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table} ", sql)
        self.assertIn("wrapped_data_key  BYTEA", sql)
        self.assertIn("ciphertext        BYTEA", sql)
        keys = sql.split("CREATE TABLE IF NOT EXISTS tenant_provider_keys", 1)[1].split(");", 1)[0]
        self.assertNotIn("plaintext", keys.lower())
        self.assertNotIn("api_key", keys.lower())
        embedded = (ROOT / "web" / "src" / "schema.rs").read_text(encoding="utf-8")
        self.assertIn("0001_platform", embedded)
        self.assertIn("include_str!", embedded)

    def test_hosted_history_schema_keeps_epoch_security_and_merge_columns(self):
        text = (ISSUE_WORKER_DIR / "storage_schema.py").read_text(encoding="utf-8")
        for column in (
            "security_outcome", "security_epoch_count", "adversarial_epoch_count",
            "adversarial_merge_policy", "adversarial_delivery", "promotion_status",
        ):
            self.assertIn(column, text)
        self.assertIn("CREATE TABLE IF NOT EXISTS adversarial_epochs", text)


class ContractStillAppliesTests(StorageContract, unittest.TestCase):
    """The shared contract, on the hosted backend, from the UAT tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="swarm-uat-417.")
        self.addCleanup(self._tmp.cleanup)
        self._root = Path(self._tmp.name)
        self._objects = MemoryObjectStore()
        super().setUp()

    def new_storage(self):
        return RemoteStorage(self._objects, SqliteDatabase(self._root / "history"))


if __name__ == "__main__":
    unittest.main()
