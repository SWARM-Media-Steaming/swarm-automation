"""Desktop -> tenant importer: a fixture desktop install, imported twice.

The fixture is built the way a real install is: ``config.json``, an
``architecture_docs/`` directory and a history database written by the real
``ExecutionHistoryRepository`` (executions, adversarial rounds and epochs,
token usage, Jev records), with secret-shaped text planted where a desktop could
plausibly have kept some.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import ai_execution_history as history
import desktop_import
import storage_factory
from object_store import MemoryObjectStore
from storage import DEFAULT_TENANT, LocalStorage, StorageError
from storage_contract import NOW, execution_start
from storage_remote import RemoteStorage, SqliteDatabase
from test_storage_remote import POSTGRES_DSN, _psycopg, postgres_database, reset_postgres

TOKEN = "ghp_" + "Z" * 30
PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQ\n-----END PRIVATE KEY-----"


def build_desktop(root: Path) -> dict[str, Path]:
    """A desktop install under ``root``: returns its config, state dir and database."""
    state = root / "state"
    (state / "architecture_docs").mkdir(parents=True)
    config = {
        "repositories": [
            {"id": "acme__web", "github_repository": "acme/web", "assignee": "dev", "auto_promote": True,
             "adversarial_uat_enabled": True, "architecture_docs_enabled": True, "repo_dir": "/Users/dev/web",
             "github_apps_config": "/Users/dev/.config/swarm/github-apps-acme__web.json"},
            {"id": "acme__api", "github_repository": "acme/api", "assignee": "dev",
             "routing_cap_claude_model": "sonnet", "routing_cap_claude_effort": "high"},
        ],
        "workspace_root": "/Users/dev/checkouts",
        "worker_state_dir": str(state),
        "gh_bin": "/opt/homebrew/bin/gh",
        "python_bin": "/usr/bin/python3",
        "preferred_provider": "auto",
        "dynamic_model_routing": True,
        "minimum_remaining_percent": 15,
        "knowledge_context_token_limit": 4000,
        "providers": [
            {"id": "claude", "enabled": True, "model": "m", "effort": "high", "bin": "/usr/local/bin/claude"},
            {"id": "codex", "enabled": False, "model": "", "effort": "", "bin": ""},
        ],
        # Not fields the desktop writes today; planted to prove nothing credential-shaped survives.
        "github_token": TOKEN,
        "extras": {"api_key": "sk-" + "q" * 30, "note": f"use token: {TOKEN} here", "key_block": PEM},
    }
    config_path = root / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    (state / "architecture_docs" / "acme_web-1a2b3c4d.json").write_text(json.dumps({
        "schema": 1, "repository": "acme/web", "updatedAt": "2026-01-01T00:00:00+00:00",
        "entities": {"api": {"id": "api", "name": "API",
                     "summary": f"see https://internal.example/x and {TOKEN} at 10.1.2.3"}},
        "pending": [], "reviews": []}), encoding="utf-8")
    (state / "architecture_docs" / "broken.json").write_text("{not json", encoding="utf-8")
    (state / "architecture_docs" / "not-a-snapshot.json").write_text('{"hello": 1}', encoding="utf-8")

    database = state / "swarm-automation.sqlite3"
    repository = history.ExecutionHistoryRepository(database)
    first = repository.create(execution_start(issue=1, repository="acme/web"), NOW)
    repository.update(first, NOW, final_status="completed", adversarial_round_count=2, security_outcome="PASS",
                      adversarial_epoch_count=1, adversarial_merge_policy="strict", promotion_status="merged",
                      routing_decision={"prompt_grade": "B", "router_provider": "claude"})
    repository.append(first, "operational_notes", "Delivered", NOW)
    repository.record_adversarial_round(first, {"stage": "uat", "round_number": 0, "outcome": "findings"})
    repository.record_adversarial_round(first, {"stage": "security", "round_number": 0, "outcome": "clean"})
    repository.record_adversarial_epoch(first, {"stage": "uat", "epoch_number": 1, "first_round": 1, "last_round": 3})
    repository.record_token_usage_batch(first, "acme/web", 1, [
        {"id": "u1", "provider": "claude", "model": "m", "attempt_number": 1, "input_tokens": 10, "total_tokens": 15},
        {"id": "u2", "provider": "claude", "model": "m", "attempt_number": 1, "input_tokens": None},
    ])
    repository.record_jev_decision({"decision_id": "d1", "execution_id": first, "decision_type": "COMPLEXITY", "decision": "ok"})
    repository.record_jev_score_comparison({"comparison_id": "c1", "execution_id": first})
    second = repository.create(execution_start(issue=1, repository="acme/web"), NOW)  # attempt 2 of issue 1
    repository.update(second, NOW, final_status="failed")
    third = repository.create(execution_start(issue=2, repository="acme/api"), NOW)
    repository.update(third, NOW, final_status="completed")
    # A secret that reached the desktop file before the sanitizer existed.
    with closing(sqlite3.connect(database)) as raw:
        raw.execute("UPDATE ai_executions SET changes_summary = ? WHERE execution_id = ?", (f"token: {TOKEN}", third))
        raw.execute("UPDATE ai_executions SET operational_notes = ? WHERE execution_id = ?",
                    (json.dumps(["ok", f"Authorization: Bearer {TOKEN}"]), third))
        raw.commit()
    return {"config": config_path, "state": state, "database": database,
            "executions": {"first": first, "second": second, "third": third}}


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ImporterCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="swarm-import-test.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.desktop = build_desktop(self.root / "desktop")
        self.target = self.root / "hosted"
        self.storage = LocalStorage(self.target)

    def run_import(self, storage=None, **options):
        options.setdefault("config_path", self.desktop["config"])
        return desktop_import.run_import(storage or self.storage, "acme", **options)

    def tenant_rows(self, sql, params=()):
        with closing(sqlite3.connect(self.storage.history_database_path("acme"))) as database:
            return database.execute(sql, params).fetchall()

    def table_dump(self):
        with closing(sqlite3.connect(self.storage.history_database_path("acme"))) as database:
            return {table: database.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                    for table in history.IMPORT_TABLES}


class ImportTests(ImporterCase):
    def test_imports_configuration_documentation_and_history_into_the_tenant(self):
        report = self.run_import()
        self.assertEqual(report["config"]["documents"],
                         {"app": "imported", "repo-acme__web": "imported", "repo-acme__api": "imported"})
        self.assertEqual(report["architecture"]["documents"], {"acme_web-1a2b3c4d": "imported"})
        self.assertEqual(sorted(report["architecture"]["skipped_unreadable"]), ["broken.json", "not-a-snapshot.json"])
        tables = report["history"]["tables"]
        self.assertEqual(tables["ai_executions"], {"source": 3, "imported": 3, "existing": 0, "renumbered": 0, "orphaned": 0})
        self.assertEqual({t: c["imported"] for t, c in tables.items()},
                         {"ai_executions": 3, "adversarial_rounds": 2, "adversarial_epochs": 1,
                          "ai_token_usage": 2, "jev_decisions": 1, "jev_score_comparisons": 1})

        app = self.storage.read_document("acme", "tenant_config", "app")["settings"]
        self.assertTrue(app["dynamic_model_routing"])
        self.assertEqual(app["minimum_remaining_percent"], 15)
        self.assertEqual(app["knowledge_context_token_limit"], 4000)  # "token" in a name is not a credential
        self.assertEqual([p["id"] for p in app["providers"]], ["claude", "codex"])
        repo = self.storage.read_document("acme", "tenant_config", "repo-acme__web")["settings"]
        self.assertTrue(repo["auto_promote"] and repo["adversarial_uat_enabled"])
        self.assertEqual(self.storage.read_document("acme", "tenant_config", "repo-acme__api")["settings"]
                         ["routing_cap_claude_model"], "sonnet")
        documentation = self.storage.read_document("acme", "architecture_docs", "acme_web-1a2b3c4d")
        self.assertNotIn(TOKEN, json.dumps(documentation))
        self.assertNotIn("internal.example", json.dumps(documentation))
        self.assertNotIn("10.1.2.3", json.dumps(documentation))  # architecture URL/IP redaction

        ids = {row[0] for row in self.tenant_rows("SELECT execution_id FROM ai_executions")}
        self.assertEqual(ids, set(self.desktop["executions"].values()))  # source ids are kept
        attempts = self.tenant_rows("SELECT attempt_number FROM ai_executions WHERE issue_number = 1 ORDER BY 1")
        self.assertEqual(attempts, [(1,), (2,)])
        self.assertEqual(self.tenant_rows("SELECT security_outcome, adversarial_merge_policy FROM ai_executions "
                                          "WHERE execution_id = ?", (self.desktop["executions"]["first"],)),
                         [("PASS", "strict")])
        rounds = self.tenant_rows("SELECT stage, round_number FROM adversarial_rounds ORDER BY stage")
        self.assertEqual(rounds, [("security", 0), ("uat", 0)])
        usage = self.storage.execution_history("acme").token_usage_for_execution(self.desktop["executions"]["first"])
        self.assertEqual([row["id"] for row in usage], ["u1", "u2"])
        self.assertIsNone(usage[1]["input_tokens"])  # unavailable stays unavailable, never zero
        routing = json.loads(self.tenant_rows("SELECT routing_decision FROM ai_executions WHERE execution_id = ?",
                                              (self.desktop["executions"]["first"],))[0][0])
        self.assertEqual(routing["prompt_grade"], "B")  # structure survives; only secrets are redacted

    def test_running_twice_changes_nothing(self):
        self.run_import()
        before = self.table_dump()
        documents = {key: self.storage.read_document("acme", "tenant_config", key)
                     for key in self.storage.list_documents("acme", "tenant_config")}
        second = self.run_import()
        self.assertEqual(self.table_dump(), before)
        for table, counts in second["history"]["tables"].items():
            self.assertEqual((counts["imported"], counts["renumbered"]), (0, 0), table)
        self.assertEqual(second["history"]["tables"]["ai_executions"]["existing"], 3)
        self.assertEqual(set(second["config"]["documents"].values()), {"unchanged"})
        self.assertEqual(set(second["architecture"]["documents"].values()), {"unchanged"})
        self.assertEqual({key: self.storage.read_document("acme", "tenant_config", key)
                          for key in self.storage.list_documents("acme", "tenant_config")}, documents)

    def test_dry_run_writes_nothing_and_predicts_the_real_run(self):
        dry = self.run_import(dry_run=True)
        self.assertFalse(self.target.exists())  # not even an empty tenant tree
        self.assertEqual(self.storage.list_documents("acme", "tenant_config"), [])
        real = self.run_import()
        self.assertTrue(dry["dry_run"] and not real["dry_run"])
        self.assertEqual(dry["config"], real["config"])
        self.assertEqual(dry["architecture"], real["architecture"])
        self.assertEqual(dry["history"]["tables"], real["history"]["tables"])

    def test_dry_run_against_a_populated_tenant_reports_clashes_without_changing_it(self):
        history_store = self.storage.execution_history("acme")
        history_store.create(execution_start(issue=2, repository="acme/api"), NOW)  # attempt 1 of acme/api#2 taken
        before = self.table_dump()
        dry = self.run_import(dry_run=True, sections=("history",), state_dir=self.desktop["state"])
        self.assertEqual(dry["history"]["tables"]["ai_executions"]["renumbered"], 1)
        self.assertEqual(self.table_dump(), before)
        real = self.run_import(sections=("history",), state_dir=self.desktop["state"])
        self.assertEqual(real["history"]["tables"]["ai_executions"]["renumbered"], 1)
        self.assertEqual(self.tenant_rows("SELECT attempt_number FROM ai_executions WHERE issue_number = 2 "
                                          "AND repository = 'acme/api' ORDER BY 1"), [(1,), (2,)])
        again = self.run_import(sections=("history",), state_dir=self.desktop["state"])
        self.assertEqual(again["history"]["tables"]["ai_executions"]["renumbered"], 0)

    def test_no_credential_reaches_the_tenant_or_the_report(self):
        report = self.run_import()
        self.assertEqual(report["credentials"]["imported"], 0)
        self.assertEqual(report["config"]["credentials_dropped"],
                         ["extras.api_key", "extras.key_block", "github_token"])
        written = "".join(path.read_text(errors="replace") for path in self.target.rglob("*.json"))
        for secret in (TOKEN, "sk-" + "q" * 30, "BEGIN PRIVATE KEY", "MIIEvQ"):
            self.assertNotIn(secret, written)
            self.assertNotIn(secret, json.dumps(report))
            self.assertNotIn(secret, desktop_import.render_report(report))
            self.assertNotIn(secret.encode(), self.storage.history_database_path("acme").read_bytes())
        self.assertEqual(self.storage.read_document("acme", "tenant_config", "app")["settings"]["extras"]["note"],
                         "use token: [REDACTED] here")
        self.assertEqual(self.tenant_rows("SELECT changes_summary FROM ai_executions WHERE execution_id = ?",
                                          (self.desktop["executions"]["third"],)), [("token: [REDACTED]",)])
        notes = json.loads(self.tenant_rows("SELECT operational_notes FROM ai_executions WHERE execution_id = ?",
                                            (self.desktop["executions"]["third"],))[0][0])
        self.assertEqual(notes, ["ok", "Authorization: Bearer [REDACTED]"])
        for document in (self.storage.read_document("acme", "tenant_config", key)
                         for key in self.storage.list_documents("acme", "tenant_config")):
            text = json.dumps(document)
            for local in ("/Users/dev", "/opt/homebrew", "/usr/bin/python3", "github-apps", "workspace_root", "repo_dir"):
                self.assertNotIn(local, text)

    def test_the_desktop_install_is_never_modified(self):
        paths = [self.desktop["config"], self.desktop["database"], *self.desktop["state"].glob("architecture_docs/*")]
        before = {path: fingerprint(path) for path in paths}
        listing = sorted(path.name for path in self.desktop["state"].iterdir())
        self.run_import()
        self.run_import(dry_run=True)
        self.assertEqual({path: fingerprint(path) for path in paths}, before)
        self.assertEqual(sorted(path.name for path in self.desktop["state"].iterdir()), listing)

    def test_default_tenant_and_other_tenants_are_untouched(self):
        self.run_import()
        self.assertEqual(self.storage.list_documents(DEFAULT_TENANT, "tenant_config"), [])
        self.assertEqual(self.storage.list_documents("other", "architecture_docs"), [])
        self.assertFalse(self.storage.execution_history("other").execution_exists(self.desktop["executions"]["first"]))
        self.assertFalse((self.target / "swarm-automation.sqlite3").exists())

    def test_existing_tenant_settings_are_kept_unless_overwrite_is_asked_for(self):
        self.run_import()
        edited = self.storage.read_document("acme", "tenant_config", "app")
        edited["settings"]["minimum_remaining_percent"] = 40
        self.storage.write_document("acme", "tenant_config", "app", edited)
        kept = self.run_import()
        self.assertEqual(kept["config"]["documents"]["app"], "kept")
        self.assertEqual(self.storage.read_document("acme", "tenant_config", "app")["settings"]["minimum_remaining_percent"], 40)
        replaced = self.run_import(overwrite=True)
        self.assertEqual(replaced["config"]["documents"]["app"], "overwritten")
        self.assertEqual(self.storage.read_document("acme", "tenant_config", "app")["settings"]["minimum_remaining_percent"], 15)

    def test_sections_can_be_chosen(self):
        report = self.run_import(sections=("architecture",), state_dir=self.desktop["state"])
        self.assertEqual(set(report), {"tenant", "dry_run", "overwrite", "sections", "credentials", "architecture"})
        self.assertEqual(self.storage.list_documents("acme", "tenant_config"), [])

    def test_a_desktop_without_history_or_documentation_still_imports_its_settings(self):
        bare = self.root / "bare"
        bare.mkdir()
        config = json.loads(self.desktop["config"].read_text())
        config["worker_state_dir"] = str(bare / "state")
        (bare / "config.json").write_text(json.dumps(config))
        report = self.run_import(config_path=bare / "config.json")
        self.assertEqual(report["history"]["status"], "absent")
        self.assertEqual(report["architecture"]["documents"], {})
        self.assertEqual(report["config"]["documents"]["app"], "imported")

    def test_a_legacy_single_repository_config_becomes_a_repository_document(self):
        config = {"github_repository": "acme/old", "assignee": "dev", "base_branch": "main",
                  "claude_model": "m", "claude_bin": "/x/claude", "dynamic_model_routing": False}
        (self.root / "legacy.json").write_text(json.dumps(config))
        report = self.run_import(config_path=self.root / "legacy.json", sections=("config",))
        self.assertEqual(set(report["config"]["documents"]), {"app", "repo-acme__old"})
        app = self.storage.read_document("acme", "tenant_config", "app")["settings"]
        self.assertEqual(app, {"dynamic_model_routing": False})
        self.assertEqual(self.storage.read_document("acme", "tenant_config", "repo-acme__old")["settings"]["assignee"], "dev")

    def test_an_older_or_newer_source_schema_imports_the_columns_both_know(self):
        old = self.root / "old.sqlite3"
        with closing(sqlite3.connect(old)) as raw:
            raw.executescript("""
                CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT);
                INSERT INTO schema_migrations (version) VALUES (1), (99);
                CREATE TABLE ai_executions (execution_id TEXT PRIMARY KEY, repository TEXT, issue_number INTEGER,
                    issue_title TEXT, original_issue_body TEXT, ai_provider TEXT, started_at TEXT, final_status TEXT,
                    attempt_number INTEGER, updated_at TEXT, column_from_the_future TEXT);
                INSERT INTO ai_executions VALUES ('old-1', 'a/b', 3, 'T', 'B', 'claude', 'now', 'completed', 1, 'now', 'x');
            """)
        report = self.run_import(sections=("history",), history_db=old)
        self.assertEqual(report["history"]["source_schema_version"], 99)
        self.assertTrue(report["history"]["newer_than_this_importer"])
        self.assertEqual(report["history"]["tables"]["ai_executions"]["imported"], 1)
        self.assertEqual(self.tenant_rows("SELECT security_outcome, adversarial_round_count FROM ai_executions"), [("", 0)])

    def test_problems_are_reported_rather_than_half_done(self):
        with self.assertRaises(desktop_import.ImportProblem):
            self.run_import(config_path=self.root / "missing.json")
        (self.root / "bad.json").write_text("[1]")
        with self.assertRaises(desktop_import.ImportProblem):
            self.run_import(config_path=self.root / "bad.json")
        with self.assertRaises(desktop_import.ImportProblem):
            self.run_import(sections=("history",), config_path=None)
        with self.assertRaises(desktop_import.ImportProblem):
            self.run_import(sections=("nonsense",))
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"this is not a database" * 100)
        with self.assertRaises(desktop_import.ImportProblem):
            self.run_import(sections=("history",), history_db=corrupt)
        with self.assertRaises(StorageError):
            desktop_import.run_import(self.storage, "Bad Tenant", config_path=self.desktop["config"])

    def test_a_failure_part_way_through_the_history_imports_nothing(self):
        tables, _ = desktop_import.read_history_tables(self.desktop["database"])
        tables["jev_decisions"][0]["decision_id"] = "d1"
        tables["ai_token_usage"].append({"id": "boom", "input_tokens": object()})  # cannot be stored
        tables["ai_token_usage"][-1]["execution_id"] = "x"
        with self.assertRaises(Exception):
            self.storage.execution_history("acme").import_records(tables)
        self.assertEqual(self.tenant_rows("SELECT count(*) FROM ai_executions"), [(0,)])


class RemoteTargetTests(unittest.TestCase):
    def test_import_into_hosted_storage_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = build_desktop(Path(tmp) / "desktop")
            storage = RemoteStorage(MemoryObjectStore(), SqliteDatabase(Path(tmp) / "pg"))
            first = desktop_import.run_import(storage, "acme", config_path=desktop["config"])
            second = desktop_import.run_import(storage, "acme", config_path=desktop["config"])
            self.assertEqual(first["history"]["tables"]["ai_executions"]["imported"], 3)
            self.assertEqual(second["history"]["tables"]["ai_executions"]["imported"], 0)
            self.assertEqual(storage.list_documents("acme", "tenant_config"), ["app", "repo-acme__api", "repo-acme__web"])
            self.assertEqual(storage.list_documents("acme", "architecture_docs"), ["acme_web-1a2b3c4d"])
            self.assertEqual(storage.list_documents("beta", "tenant_config"), [])


@unittest.skipUnless(POSTGRES_DSN and _psycopg(), "set SWARM_TEST_POSTGRES_DSN and install psycopg to run")
class PostgresImportTests(unittest.TestCase):
    def test_import_into_postgres_is_safe_to_run_twice(self):
        tenants = ("pgimport",)
        reset_postgres(tenants)
        self.addCleanup(reset_postgres, tenants)
        with tempfile.TemporaryDirectory() as tmp:
            desktop = build_desktop(Path(tmp) / "desktop")
            storage = RemoteStorage(MemoryObjectStore(), postgres_database())
            dry = desktop_import.run_import(storage, "pgimport", config_path=desktop["config"], dry_run=True)
            self.assertFalse(storage.has_execution_history("pgimport"))  # a dry run created no schema
            self.assertEqual(storage.list_documents("pgimport", "tenant_config"), [])
            first = desktop_import.run_import(storage, "pgimport", config_path=desktop["config"])
            self.assertEqual(dry["history"]["tables"], first["history"]["tables"])
            second = desktop_import.run_import(storage, "pgimport", config_path=desktop["config"])
            self.assertEqual(first["history"]["tables"]["ai_token_usage"]["imported"], 2)
            for counts in second["history"]["tables"].values():
                self.assertEqual((counts["imported"], counts["renumbered"]), (0, 0))
            psycopg = _psycopg()
            with psycopg.connect(POSTGRES_DSN) as connection:
                count = connection.execute('SELECT count(*) FROM "t_pgimport".ai_executions').fetchone()[0]
                notes = connection.execute(
                    'SELECT operational_notes FROM "t_pgimport".ai_executions WHERE execution_id = %s',
                    (desktop["executions"]["third"],)).fetchone()[0]
            self.assertEqual(count, 3)
            self.assertNotIn(TOKEN, notes)


class CommandLineTests(ImporterCase):
    def invoke(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = desktop_import.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def common(self, *extra):
        return ["--tenant", "acme", "--target", f"local:{self.target}", "--config", str(self.desktop["config"]), *extra]

    def test_dry_run_prints_the_documented_report(self):
        code, out, err = self.invoke(*self.common("--dry-run"))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("SWARM Automation desktop import (dry run: nothing was written)", out)
        self.assertIn("Tenant: acme", out)
        self.assertRegex(out, r"app\s+would import")
        self.assertRegex(out, r"repo-acme__web\s+would import")
        self.assertRegex(out, r"ai_executions\s+3 in source: 3 would import, 0 already present")
        self.assertRegex(out, r"ai_token_usage\s+2 in source: 2 would import, 0 already present")
        self.assertIn("credential-like settings dropped: extras.api_key, extras.key_block, github_token", out)
        self.assertIn("Credentials: none imported.", out)
        self.assertFalse(self.target.exists())

    def test_real_run_then_rerun_report(self):
        self.assertEqual(self.invoke(*self.common())[0], 0)
        code, out, _ = self.invoke(*self.common())
        self.assertEqual(code, 0)
        self.assertNotIn("dry run", out)
        self.assertRegex(out, r"app\s+unchanged")
        self.assertRegex(out, r"ai_executions\s+3 in source: 0 imported, 3 already present")

    def test_json_output_and_only(self):
        code, out, _ = self.invoke(*self.common("--json", "--dry-run", "--only", "config"))
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["sections"], ["config"])
        self.assertEqual(report["config"]["documents"]["app"], "imported")
        self.assertNotIn("history", report)

    def test_errors_exit_nonzero_without_a_traceback(self):
        code, _, err = self.invoke("--tenant", "acme", "--target", f"local:{self.target}", "--config", "/nope/config.json")
        self.assertEqual(code, 1)
        self.assertIn("Import failed: Desktop configuration not found", err)
        code, _, err = self.invoke(*self.common(), "--only", "bogus")
        self.assertEqual(code, 1)
        code, _, err = self.invoke("--tenant", "acme", "--target", "somewhere", "--config", str(self.desktop["config"]))
        self.assertEqual(code, 1)
        self.assertIn("Storage target", err)

    def test_hosted_import_refuses_the_default_tenant(self):
        code, _, err = self.invoke("--tenant", "default", "--config", str(self.desktop["config"]))
        self.assertEqual(code, 2)
        self.assertIn("default", err)


class StorageFactoryTests(unittest.TestCase):
    ENV = {
        "SWARM_STORAGE_POSTGRES_DSN": "postgresql://user:hunter2@db/app",
        "SWARM_STORAGE_POSTGRES_DRIVER": "sqlite3:connect",
        "SWARM_STORAGE_S3_ENDPOINT": "http://127.0.0.1:9000",
        "SWARM_STORAGE_S3_BUCKET": "swarm",
        "SWARM_STORAGE_S3_ACCESS_KEY_ID": "AKIAEXAMPLE",
        "SWARM_STORAGE_S3_SECRET_ACCESS_KEY": "s3cr3t-value",
    }

    def test_hosted_storage_is_built_from_the_environment(self):
        storage = storage_factory.open_storage("hosted", {**self.ENV, "SWARM_STORAGE_S3_PREFIX": "swarm/"})
        self.assertIsInstance(storage, RemoteStorage)
        self.assertEqual(storage.prefix, "swarm/")
        self.assertNotIn("s3cr3t-value", repr(storage.objects))

    def test_missing_variables_are_named_but_no_value_is_echoed(self):
        env = {k: v for k, v in self.ENV.items() if k not in ("SWARM_STORAGE_S3_BUCKET", "SWARM_STORAGE_S3_SECRET_ACCESS_KEY")}
        with self.assertRaises(StorageError) as context:
            storage_factory.open_storage("hosted", env)
        message = str(context.exception)
        self.assertIn("SWARM_STORAGE_S3_BUCKET", message)
        self.assertIn("SWARM_STORAGE_S3_SECRET_ACCESS_KEY", message)
        self.assertNotIn("hunter2", message)

    def test_driver_must_be_an_explicit_importable_callable(self):
        for bad in ("psycopg", "os.path:", "bad driver:x", "no_such_module_xyz:connect", "sqlite3:no_such_attr", "os:sep"):
            with self.subTest(driver=bad), self.assertRaises(StorageError) as context:
                storage_factory.open_storage("hosted", {**self.ENV, "SWARM_STORAGE_POSTGRES_DRIVER": bad})
            self.assertNotIn("hunter2", str(context.exception))

    def test_cleartext_s3_to_a_remote_host_is_refused(self):
        with self.assertRaises(StorageError):
            storage_factory.open_storage("hosted", {**self.ENV, "SWARM_STORAGE_S3_ENDPOINT": "http://s3.example.com"})
        storage_factory.open_storage("hosted", {**self.ENV, "SWARM_STORAGE_S3_ENDPOINT": "http://minio.internal:9000",
                                                "SWARM_STORAGE_S3_ALLOW_INSECURE_HTTP": "1"})

    def test_local_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsInstance(storage_factory.open_storage(f"local:{tmp}"), LocalStorage)
        for bad in ("local:", "elsewhere", ""):
            with self.assertRaises(StorageError):
                storage_factory.open_storage(bad)


if __name__ == "__main__":
    unittest.main()
