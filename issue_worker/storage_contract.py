"""Reusable contract tests for ``storage.Storage`` implementations.

Mix ``StorageContract`` into a ``unittest.TestCase`` and implement
``new_storage``. It must return a *new* instance over the **same** backing
store on every call (a fresh process opening the same data); the contract uses
that to prove the exit-13/14 checkpoint resume flow survives a restart. It
must also return a store that is empty on the first call of each test.

    class PostgresStorageContract(StorageContract, unittest.TestCase):
        def new_storage(self):
            return PostgresStorage(self.dsn_for_this_test)
"""

from __future__ import annotations

from typing import Any

from ai_execution_history import ExecutionStart
from storage import (
    CHECKPOINT_KINDS,
    CURRENT,
    DEFAULT_TENANT,
    KEYED_CHECKPOINTS,
    SINGLETON_CHECKPOINTS,
    Storage,
    StorageError,
)

OTHER_TENANT = "tenant-b"
COLLECTION = "architecture_docs"
NOW = "2026-01-01T00:00:00+00:00"


def paused_state(issue: int = 415, **extra: Any) -> dict[str, Any]:
    return {
        "issue_number": issue, "issue_title": "Storage", "issue_url": "https://example.invalid/issues/1",
        "base_sha": "a" * 40, "ai_tool": "Claude", "model": "m", "effort": "high",
        "session_id": "s-1", "status": "quota_paused", **extra,
    }


def execution_start(issue: int = 415, repository: str = "o/r") -> ExecutionStart:
    return ExecutionStart(
        repository=repository, issue_number=issue, issue_url="https://example.invalid/i",
        issue_title="Title", issue_body="Body", provider="claude", model="m", effort="high",
        branch_name="ai/claude/issue-%d" % issue, application_version="0.0.0",
    )


class StorageContract:
    def new_storage(self) -> Storage:  # pragma: no cover - supplied by the implementation's test
        raise NotImplementedError

    def setUp(self) -> None:
        self.storage = self.new_storage()

    # -- tenants -----------------------------------------------------------
    def test_invalid_tenant_ids_are_rejected_on_every_group(self):
        for tenant in ("", "..", "a/b", "A", " x", "x" * 80, None):
            with self.subTest(tenant=tenant):
                for call in (
                    lambda: self.storage.read_checkpoint(tenant, "in-progress"),
                    lambda: self.storage.read_document(tenant, COLLECTION, "k"),
                    lambda: self.storage.read_artifact(tenant, "a.log"),
                    lambda: self.storage.execution_history(tenant),
                ):
                    with self.assertRaises(StorageError):
                        call()

    def test_tenants_are_isolated(self):
        s, a, b = self.storage, DEFAULT_TENANT, OTHER_TENANT
        s.write_checkpoint(a, "in-progress", CURRENT, {"issue_number": 1})
        s.write_checkpoint(a, "quota-paused", "1", paused_state(1))
        s.write_document(a, COLLECTION, "repo", {"schema": 1})
        s.write_artifact(a, "last-ai-output.log", "tenant a output")
        self.assertIsNone(s.read_checkpoint(b, "in-progress"))
        self.assertEqual(s.list_checkpoints(b, "quota-paused"), [])
        self.assertIsNone(s.read_document(b, COLLECTION, "repo"))
        self.assertEqual(s.list_documents(b, COLLECTION), [])
        self.assertIsNone(s.read_artifact(b, "last-ai-output.log"))
        self.assertEqual(s.list_artifacts(b), [])
        s.write_checkpoint(b, "in-progress", CURRENT, {"issue_number": 2})
        self.assertEqual(s.read_checkpoint(a, "in-progress")["issue_number"], 1)
        self.assertFalse(s.delete_checkpoint(b, "quota-paused", "1"))
        self.assertEqual(s.list_checkpoints(a, "quota-paused"), ["1"])

    def test_history_is_tenant_scoped(self):
        a = self.storage.execution_history(DEFAULT_TENANT)
        b = self.storage.execution_history(OTHER_TENANT)
        execution = a.create(execution_start(), NOW)
        self.assertTrue(a.execution_exists(execution))
        self.assertFalse(b.execution_exists(execution))
        self.assertEqual(b.final_statuses_for_issue("o/r", 415), [])
        a.record_token_usage_batch(execution, "o/r", 415, [{"id": "u1", "provider": "claude", "attempt_number": 1}])
        self.assertEqual(b.token_usage_for_issue("o/r", 415), [])

    # -- checkpoints -------------------------------------------------------
    def test_checkpoint_round_trip_replace_and_delete(self):
        s = self.storage
        self.assertIsNone(s.read_checkpoint(DEFAULT_TENANT, "in-progress"))
        s.write_checkpoint(DEFAULT_TENANT, "in-progress", CURRENT, {"issue_number": 415, "nested": {"a": [1, 2]}})
        self.assertEqual(s.read_checkpoint(DEFAULT_TENANT, "in-progress"),
                         {"issue_number": 415, "nested": {"a": [1, 2]}})
        s.write_checkpoint(DEFAULT_TENANT, "in-progress", CURRENT, {"issue_number": 416})
        self.assertEqual(s.read_checkpoint(DEFAULT_TENANT, "in-progress"), {"issue_number": 416})
        self.assertEqual(s.list_checkpoints(DEFAULT_TENANT, "in-progress"), [CURRENT])
        self.assertTrue(s.delete_checkpoint(DEFAULT_TENANT, "in-progress"))
        self.assertFalse(s.delete_checkpoint(DEFAULT_TENANT, "in-progress"))
        self.assertIsNone(s.read_checkpoint(DEFAULT_TENANT, "in-progress"))
        self.assertEqual(s.list_checkpoints(DEFAULT_TENANT, "in-progress"), [])

    def test_every_checkpoint_kind_is_usable(self):
        for kind in CHECKPOINT_KINDS:
            with self.subTest(kind=kind):
                key = CURRENT if kind in SINGLETON_CHECKPOINTS else "7"
                self.storage.write_checkpoint(DEFAULT_TENANT, kind, key, {"kind": kind})
                self.assertEqual(self.storage.read_checkpoint(DEFAULT_TENANT, kind, key), {"kind": kind})

    def test_keyed_checkpoints_list_sorted(self):
        for key in ("20", "3", "100-20260101T000000+0000"):
            self.storage.write_checkpoint(DEFAULT_TENANT, "closed-paused", key, {"k": key})
        self.assertEqual(self.storage.list_checkpoints(DEFAULT_TENANT, "closed-paused"),
                         sorted(["20", "3", "100-20260101T000000+0000"]))

    def test_unknown_kinds_and_bad_keys_are_rejected(self):
        s = self.storage
        with self.assertRaises(StorageError):
            s.write_checkpoint(DEFAULT_TENANT, "not-a-kind", CURRENT, {})
        with self.assertRaises(StorageError):
            s.read_checkpoint(DEFAULT_TENANT, "not-a-kind")
        with self.assertRaises(StorageError):
            s.write_checkpoint(DEFAULT_TENANT, "in-progress", "other", {})
        for key in ("", "../x", "a/b", ".hidden", "a b", "x" * 200):
            with self.subTest(key=key):
                with self.assertRaises(StorageError):
                    s.write_checkpoint(DEFAULT_TENANT, "quota-paused", key, {})
        with self.assertRaises(StorageError):
            s.write_checkpoint(DEFAULT_TENANT, "in-progress", CURRENT, ["not", "an", "object"])  # type: ignore[arg-type]
        self.assertEqual(s.list_checkpoints(DEFAULT_TENANT, "quota-paused"), [])

    def test_unserialisable_value_leaves_previous_checkpoint_intact(self):
        s = self.storage
        s.write_checkpoint(DEFAULT_TENANT, "in-progress", CURRENT, {"ok": True})
        with self.assertRaises(StorageError):
            s.write_checkpoint(DEFAULT_TENANT, "in-progress", CURRENT, {"bad": object()})
        self.assertEqual(s.read_checkpoint(DEFAULT_TENANT, "in-progress"), {"ok": True})

    def test_exit_13_epoch_yield_resumes_from_a_fresh_process(self):
        """Exit 13: the strict-mode epoch yield keeps the adversarial position
        in the in-progress checkpoint; the scheduler restarts the worker."""
        state = {
            "issue_number": 415, "ai_tool": "Claude", "base_sha": "b" * 40,
            "adversarial": {"phase": "fix", "epoch": 2, "round": 1, "suites": ["adversarial-1"]},
            "adversarial_security": {"disabled": True},
            "token_usage_events": [{"id": "e1", "total_tokens": 42}],
        }
        self.storage.write_checkpoint(DEFAULT_TENANT, "in-progress", CURRENT, state)
        restarted = self.new_storage()
        self.assertEqual(restarted.read_checkpoint(DEFAULT_TENANT, "in-progress"), state)
        resumed = dict(state, adversarial=dict(state["adversarial"], round=2))
        restarted.write_checkpoint(DEFAULT_TENANT, "in-progress", CURRENT, resumed)
        self.assertEqual(self.new_storage().read_checkpoint(DEFAULT_TENANT, "in-progress")["adversarial"]["round"], 2)

    def test_exit_14_automation_hold_and_quota_pause_resume_from_a_fresh_process(self):
        """Exit 14 retains the branch checkpoint; a quota pause shelves the
        in-progress state under its issue key. A restarted process finds both."""
        hold = {"issue_number": 415, "automation_failure": {"fingerprint": "f" * 16, "count": 3}}
        self.storage.write_checkpoint(DEFAULT_TENANT, "in-progress", CURRENT, hold)
        self.storage.write_checkpoint(DEFAULT_TENANT, "quota-paused", "7", paused_state(7))
        self.storage.write_checkpoint(DEFAULT_TENANT, "pending-delivery", CURRENT, {"issue_number": 9, "commit_sha": "c" * 40})
        restarted = self.new_storage()
        self.assertEqual(restarted.read_checkpoint(DEFAULT_TENANT, "in-progress"), hold)
        self.assertEqual(restarted.list_checkpoints(DEFAULT_TENANT, "quota-paused"), ["7"])
        self.assertEqual(restarted.read_checkpoint(DEFAULT_TENANT, "quota-paused", "7"), paused_state(7))
        self.assertEqual(restarted.read_checkpoint(DEFAULT_TENANT, "pending-delivery")["issue_number"], 9)
        # Shelve -> restore round trip, as `restore_paused` does.
        restarted.write_checkpoint(DEFAULT_TENANT, "in-progress", CURRENT, restarted.read_checkpoint(DEFAULT_TENANT, "quota-paused", "7"))
        restarted.delete_checkpoint(DEFAULT_TENANT, "quota-paused", "7")
        later = self.new_storage()
        self.assertEqual(later.read_checkpoint(DEFAULT_TENANT, "in-progress")["issue_number"], 7)
        self.assertEqual(later.list_checkpoints(DEFAULT_TENANT, "quota-paused"), [])

    # -- documents ---------------------------------------------------------
    def test_document_round_trip_list_and_delete(self):
        s = self.storage
        self.assertIsNone(s.read_document(DEFAULT_TENANT, COLLECTION, "o_r-1234"))
        s.write_document(DEFAULT_TENANT, COLLECTION, "o_r-1234", {"schema": 1, "entities": {"a": {"id": "a"}}})
        s.write_document(DEFAULT_TENANT, COLLECTION, "a_b-0001", {"schema": 1})
        self.assertEqual(self.new_storage().read_document(DEFAULT_TENANT, COLLECTION, "o_r-1234")["entities"], {"a": {"id": "a"}})
        self.assertEqual(s.list_documents(DEFAULT_TENANT, COLLECTION), ["a_b-0001", "o_r-1234"])
        self.assertTrue(s.delete_document(DEFAULT_TENANT, COLLECTION, "o_r-1234"))
        self.assertFalse(s.delete_document(DEFAULT_TENANT, COLLECTION, "o_r-1234"))

    def test_document_collections_and_keys_are_validated(self):
        with self.assertRaises(StorageError):
            self.storage.write_document(DEFAULT_TENANT, "nope", "k", {})
        with self.assertRaises(StorageError):
            self.storage.write_document(DEFAULT_TENANT, COLLECTION, "../escape", {})

    # -- artifacts ---------------------------------------------------------
    def test_artifact_write_append_read_delete_list(self):
        s = self.storage
        self.assertIsNone(s.read_artifact(DEFAULT_TENANT, "completed-issues"))
        s.append_artifact(DEFAULT_TENANT, "completed-issues", "10\n")
        s.append_artifact(DEFAULT_TENANT, "completed-issues", "11\n")
        self.assertEqual(self.new_storage().read_artifact(DEFAULT_TENANT, "completed-issues"), "10\n11\n")
        s.write_artifact(DEFAULT_TENANT, "last-ai-output.log", "first")
        s.write_artifact(DEFAULT_TENANT, "last-ai-output.log", "unicode ✓ é")
        self.assertEqual(s.read_artifact(DEFAULT_TENANT, "last-ai-output.log"), "unicode ✓ é")
        s.write_artifact(DEFAULT_TENANT, "last-ai-diagnostic.log", "")
        self.assertEqual(s.read_artifact(DEFAULT_TENANT, "last-ai-diagnostic.log"), "")
        self.assertEqual(s.list_artifacts(DEFAULT_TENANT),
                         ["completed-issues", "last-ai-diagnostic.log", "last-ai-output.log"])
        self.assertTrue(s.delete_artifact(DEFAULT_TENANT, "completed-issues"))
        self.assertFalse(s.delete_artifact(DEFAULT_TENANT, "completed-issues"))
        self.assertIsNone(s.read_artifact(DEFAULT_TENANT, "completed-issues"))

    def test_artifact_names_are_validated(self):
        for name in ("", "../x", "a/b", ".env", "x" * 200):
            with self.subTest(name=name):
                with self.assertRaises(StorageError):
                    self.storage.write_artifact(DEFAULT_TENANT, name, "x")
        for name in (*SINGLETON_CHECKPOINTS.values(), *KEYED_CHECKPOINTS.values(), COLLECTION, "tenants"):
            with self.subTest(reserved=name):
                with self.assertRaises(StorageError):
                    self.storage.write_artifact(DEFAULT_TENANT, name, "x")

    def test_scratch_output_becomes_durable_when_published(self):
        scratch = self.storage.scratch_path(DEFAULT_TENANT, "last-ai-output.log")
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text("cli result", encoding="utf-8")
        self.storage.publish_artifact(DEFAULT_TENANT, "last-ai-output.log")
        self.assertEqual(self.new_storage().read_artifact(DEFAULT_TENANT, "last-ai-output.log"), "cli result")

    # -- execution history and usage ---------------------------------------
    def test_asking_whether_history_exists_creates_nothing(self):
        self.assertFalse(self.storage.has_execution_history(OTHER_TENANT))
        self.assertFalse(self.storage.has_execution_history(OTHER_TENANT))  # still absent after asking
        self.storage.execution_history(OTHER_TENANT)
        self.assertTrue(self.storage.has_execution_history(OTHER_TENANT))
        self.assertTrue(self.new_storage().has_execution_history(OTHER_TENANT))
        self.assertFalse(self.storage.has_execution_history(DEFAULT_TENANT))
        with self.assertRaises(StorageError):
            self.storage.has_execution_history("Not A Tenant")

    def test_history_lifecycle_and_resume_from_a_fresh_process(self):
        history = self.storage.execution_history(DEFAULT_TENANT)
        execution = history.create(execution_start(), NOW)
        self.assertTrue(execution)
        history.update(execution, NOW, final_status="completed", adversarial_round_count=2)
        history.append(execution, "operational_notes", "note one", NOW)
        history.record_adversarial_round(execution, {"stage": "uat", "round_number": 0, "outcome": "findings"})
        history.record_adversarial_epoch(execution, {"stage": "uat", "epoch_number": 1, "first_round": 1, "last_round": 3})
        history.record_jev_decision({"decision_type": "COMPLEXITY", "execution_id": execution})
        history.finish_jev_outcomes(execution, "completed")
        second = history.create(execution_start(), NOW)
        self.assertNotEqual(second, execution)

        restarted = self.new_storage().execution_history(DEFAULT_TENANT)
        self.assertTrue(restarted.execution_exists(execution))
        self.assertFalse(restarted.execution_exists("missing"))
        self.assertEqual(restarted.final_statuses_for_issue("o/r", 415)[-1], "completed")
        self.assertEqual(len(restarted.final_statuses_for_issue("o/r", 415)), 2)
        self.assertEqual(restarted.final_statuses_for_issue("o/r", 999), [])

    def test_token_usage_records_are_idempotent_and_ordered(self):
        history = self.storage.execution_history(DEFAULT_TENANT)
        execution = history.create(execution_start(), NOW)
        events = [
            {"id": "u1", "provider": "claude", "model": "m", "attempt_number": 1, "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            {"id": "u2", "provider": "claude", "model": "m", "attempt_number": 1, "input_tokens": None, "output_tokens": None},
        ]
        history.record_token_usage_batch(execution, "o/r", 415, events)
        history.record_token_usage_batch(execution, "o/r", 415, events)  # a retried tick must not duplicate
        history.record_token_usage_batch(execution, "o/r", 415, [])
        rows = self.new_storage().execution_history(DEFAULT_TENANT).token_usage_for_execution(execution)
        self.assertEqual([row["id"] for row in rows], ["u1", "u2"])
        self.assertEqual(rows[0]["total_tokens"], 15)
        self.assertIsNone(rows[1]["input_tokens"])  # missing counters stay unavailable, never zero
        self.assertEqual(len(history.token_usage_for_issue("o/r", 415)), 2)
        self.assertEqual(history.token_usage_for_issue("o/r", 1), [])

    def test_history_import_is_idempotent_scoped_and_sanitized(self):
        """The desktop importer's seam: rows keep their ids, a second run adds
        nothing, a dry run writes nothing, an attempt clash renumbers."""
        history = self.storage.execution_history(DEFAULT_TENANT)
        existing = history.create(execution_start(issue=7), NOW)  # attempt 1 of issue 7 is taken here
        row = lambda **extra: {  # noqa: E731
            "repository": "o/r", "issue_number": 7, "issue_title": "T", "original_issue_body": "B",
            "ai_provider": "claude", "started_at": NOW, "updated_at": NOW, "final_status": "completed",
            "attempt_number": 1, **extra}
        tables = {
            "ai_executions": [
                row(execution_id="imp-1", operational_notes='["token: ghp_%s"]' % ("a" * 30)),
                row(execution_id="imp-2", issue_number=8),
                row(execution_id=existing),
            ],
            "adversarial_rounds": [
                {"round_id": "r1", "execution_id": "imp-1", "stage": "uat", "round_number": 0},
                {"round_id": "r2", "execution_id": "not-imported", "stage": "uat", "round_number": 0},
            ],
            "adversarial_epochs": [{"epoch_id": "e1", "execution_id": "imp-1", "stage": "uat", "epoch_number": 1}],
            "ai_token_usage": [
                {"id": "t1", "execution_id": "imp-1", "repository": "o/r", "issue_number": 7, "input_tokens": 5},
                {"id": "t2", "execution_id": "imp-1", "repository": "o/r", "issue_number": 7, "input_tokens": None},
            ],
            "jev_decisions": [{"decision_id": "d1", "execution_id": "imp-1", "decision": "ok"}],
            "jev_score_comparisons": [{"comparison_id": "c1", "execution_id": "imp-1"}],
        }
        dry = history.import_records(tables, dry_run=True)
        self.assertEqual(dry["ai_executions"], {"source": 3, "imported": 2, "existing": 1, "renumbered": 1, "orphaned": 0})
        self.assertFalse(history.execution_exists("imp-1"))  # a dry run wrote nothing
        first = history.import_records(tables)
        self.assertEqual(first, dry)
        self.assertEqual(first["adversarial_rounds"]["imported"], 1)
        self.assertEqual(first["adversarial_rounds"]["orphaned"], 1)
        self.assertEqual(first["ai_token_usage"]["imported"], 2)
        again = history.import_records(tables)
        for table, counts in again.items():
            with self.subTest(table=table):
                self.assertEqual(counts["imported"], 0)
        self.assertEqual(again["ai_executions"]["existing"], 3)
        self.assertTrue(self.new_storage().execution_history(DEFAULT_TENANT).execution_exists("imp-1"))
        self.assertEqual(len(history.final_statuses_for_issue("o/r", 7)), 2)  # existing + renumbered imp-1
        usage = history.token_usage_for_execution("imp-1")
        self.assertEqual([record["id"] for record in usage], ["t1", "t2"])
        self.assertIsNone(usage[1]["input_tokens"])  # unavailable stays unavailable
        # Invisible to another tenant (redaction is asserted in test_desktop_import).
        other = self.storage.execution_history(OTHER_TENANT)
        self.assertFalse(other.execution_exists("imp-1"))
        self.assertEqual(other.token_usage_for_issue("o/r", 7), [])
        with self.assertRaises(ValueError):
            history.import_records({"not_a_table": [{}]})
