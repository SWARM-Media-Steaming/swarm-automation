"""Epoch policy, fingerprints, history, and scheduler progress for issue #305.

These stay outside tests/adversarial/: that tree is the independent tester's
suite and is not edited to match a policy change.
"""
import tempfile
import unittest
from pathlib import Path

import adversarial_core as core
import install_swarm_issue_cron as runner_module
from ai_execution_history import (
    SCHEMA_VERSION,
    ExecutionHistoryRepository,
    ExecutionStart,
)


class EpochPolicyTests(unittest.TestCase):
    def test_best_effort_is_explicit_and_epochs_are_three_rounds(self):
        self.assertEqual(core.merge_policy_for(True), "best_effort")
        for value in (False, None, 0, 1, "true", "best_effort"):
            self.assertEqual(core.merge_policy_for(value), "strict")
        self.assertEqual(core.MAX_ROUNDS, 3)
        self.assertEqual([core.epoch_of(n) for n in range(0, 8)], [1, 1, 1, 1, 2, 2, 2, 3])
        self.assertEqual([core.round_in_epoch(n) for n in range(0, 7)], [0, 1, 2, 3, 1, 2, 3])
        self.assertEqual([core.epoch_exhausted(n) for n in range(0, 7)],
                         [False, False, False, True, False, False, True])
        self.assertEqual(core.raise_effort("medium", 1), "high")
        self.assertEqual(core.raise_effort("medium", 2), "xhigh")
        self.assertEqual(core.raise_effort("max", 2), "max")

    def test_identical_patches_and_failures_share_a_fingerprint(self):
        first = core.patch_fingerprint(
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"
        )
        shifted = core.patch_fingerprint(
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -40,1 +40,1 @@\n-old\n+new\n"
        )
        different = core.patch_fingerprint(
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,1 @@\n-old\n+other\n"
        )
        self.assertTrue(first)
        self.assertEqual(first, shifted)
        self.assertNotEqual(first, different)
        self.assertEqual(core.patch_fingerprint(""), "")

        slow = core.failure_fingerprint(
            [{"id": "adversarial-1", "exit_code": 1, "output": "failed in 12ms at /tmp/run-a"}],
            ["service token is hardcoded"],
        )
        later = core.failure_fingerprint(
            [{"id": "adversarial-1", "exit_code": 1, "output": "failed in 99ms at /var/folders/zz/run-b"}],
            ["service token is hardcoded"],
        )
        changed = core.failure_fingerprint(
            [{"id": "adversarial-1", "exit_code": 1, "output": "failed in 12ms at /tmp/run-a"}],
            ["a different finding"],
        )
        self.assertEqual(slow, later)
        self.assertNotEqual(slow, changed)
        self.assertEqual(core.failure_fingerprint([{"id": "ok", "exit_code": 0, "output": "pass"}], []), "")

    def test_scheduler_treats_epoch_yield_as_progress(self):
        status = runner_module.Runner._cycle_exit_status
        self.assertEqual(runner_module.ADVERSARIAL_EPOCH_YIELD_EXIT_CODE, 13)
        self.assertIn(13, runner_module.PROGRESS_EXIT_CODES)
        self.assertEqual(status([13]), 13)
        self.assertEqual(status([13, 0]), 13)
        self.assertEqual(status([13, 1]), 1)
        self.assertEqual(status([13, 12]), 13)
        self.assertEqual(status([10, 13]), 10)


class EpochHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = ExecutionHistoryRepository(Path(self.temporary.name) / "history.sqlite3")

    def tearDown(self):
        self.temporary.cleanup()

    def _start(self, number: int, when: str) -> str:
        return self.repository.create(ExecutionStart(
            repository="octocat/example", issue_number=number, issue_url="",
            issue_title=f"Issue {number}", issue_body="", provider="Claude", model="test",
            effort="high", branch_name="ai/claude/issue", application_version="test",
        ), when)

    def test_schema_nine_records_epochs_rounds_and_merge_filters(self):
        self.assertEqual(SCHEMA_VERSION, 9)
        clean = self._start(1, "2026-09-29T00:00:01+00:00")
        legacy_cap = self._start(2, "2026-09-29T00:00:02+00:00")
        best = self._start(3, "2026-09-29T00:00:03+00:00")
        self.repository.update(clean, "2026-09-29T00:01:00+00:00",
                               adversarial_outcome="clean_first_pass", adversarial_delivery="verified_clean",
                               adversarial_merge_policy="strict", adversarial_epoch_count=2,
                               promotion_status="promoted", promotion_url="https://example.invalid/pull/1")
        self.repository.update(legacy_cap, "2026-09-29T00:01:00+00:00", adversarial_outcome="cap_hit")
        self.repository.update(best, "2026-09-29T00:01:00+00:00",
                               adversarial_outcome="cap_hit", adversarial_delivery="best_effort",
                               adversarial_merge_policy="best_effort",
                               adversarial_unresolved={
                                   "before_merge": {"suites": [{"id": "adversarial-1"}], "findings": []},
                                   "after_merge": {"suites": [{"id": "adversarial-1"}], "findings": [],
                                                   "merged": True, "promotion": "not_configured"},
                               })
        self.repository.record_adversarial_round(best, {
            "round_number": 4, "stage": "uat", "progress": "no_progress",
            "findings": ["service token is hardcoded"], "usage": {"Claude": 70},
            "repeated_patch": True, "patch_fingerprint": "abc", "merge_policy": "best_effort",
        })
        rounds = self.repository.adversarial_rounds_for([best])[best]
        self.assertEqual(rounds[0]["epoch_number"], 2)
        self.assertEqual(rounds[0]["round_in_epoch"], 1)
        self.assertEqual(rounds[0]["progress"], "no_progress")
        self.assertIsInstance(rounds[0]["progress"], str)
        self.assertEqual(rounds[0]["open_findings"], ["service token is hardcoded"])
        self.assertEqual(rounds[0]["usage_json"], {"Claude": 70})
        self.assertTrue(rounds[0]["repeated_patch"])

        self.repository.record_adversarial_epoch(clean, {
            "stage": "uat", "epoch_number": 1, "first_round": 1, "last_round": 3,
            "merge_policy": "strict", "outcome": "exhausted", "progress": False,
            "escalation_reason": "epoch_exhausted,no_progress",
            "next_escalation": {"reason": "epoch_exhausted,no_progress", "effort_floor": "xhigh"},
            "failing_suites": ["adversarial-1"], "findings": [], "patch_fingerprints": ["abc"],
            "no_progress": {"identical_failures": True}, "usage": {"Claude": 40},
        })
        epochs = self.repository.adversarial_epochs_for([clean])[clean]
        self.assertEqual(epochs[0]["outcome"], "exhausted")
        self.assertEqual(epochs[0]["merge_policy"], "strict")
        self.assertIs(epochs[0]["progress"], False)
        self.assertEqual(epochs[0]["next_escalation"]["effort_floor"], "xhigh")
        self.assertEqual(epochs[0]["failing_suites"], ["adversarial-1"])
        self.assertTrue(epochs[0]["no_progress"]["identical_failures"])

        verified, verified_total, _, _ = self.repository.page_for_repository(
            "octocat/example", delivery="verified_clean")
        best_rows, best_total, _, _ = self.repository.page_for_repository(
            "octocat/example", delivery="best_effort")
        self.assertEqual(verified_total, 1)
        self.assertEqual(verified[0]["execution_id"], clean)
        self.assertEqual(best_total, 2)
        self.assertEqual({row["execution_id"] for row in best_rows}, {legacy_cap, best})
        summary = self.repository.adversarial_summary("octocat/example")
        self.assertEqual(summary["verifiedCleanCount"], 1)
        self.assertEqual(summary["bestEffortCount"], 2)
        with self.assertRaises(ValueError):
            self.repository.page_for_repository("octocat/example", delivery="maybe")

    def test_migration_nine_adds_epoch_columns_to_an_older_database(self):
        execution = self._start(8, "2026-09-29T00:00:08+00:00")
        self.repository.record_adversarial_round(execution, {"round_number": 1, "tester_provider": "Codex"})
        with self.repository.connect() as database:
            database.execute("DELETE FROM schema_migrations WHERE version = 9")
            for name in (
                "adversarial_epoch_count", "security_epoch_count", "adversarial_merge_policy",
                "adversarial_delivery", "adversarial_unresolved", "promotion_status", "promotion_url",
            ):
                database.execute(f"ALTER TABLE ai_executions DROP COLUMN {name}")
            for name in (
                "epoch_number", "round_in_epoch", "fixer_effort", "tester_effort",
                "escalation_reason", "patch_fingerprint", "repeated_patch",
                "failure_fingerprint", "repeated_failure", "progress", "merge_policy",
                "failing_suites", "open_findings", "usage_json",
            ):
                database.execute(f"ALTER TABLE adversarial_rounds DROP COLUMN {name}")
            database.execute("DROP TABLE adversarial_epochs")
        upgraded = ExecutionHistoryRepository(self.repository.database_path)
        with upgraded.connect() as database:
            execution_columns = {row[1] for row in database.execute("PRAGMA table_info(ai_executions)")}
            round_columns = {row[1] for row in database.execute("PRAGMA table_info(adversarial_rounds)")}
            self.assertIn(9, {row[0] for row in database.execute("SELECT version FROM schema_migrations")})
        self.assertIn("adversarial_delivery", execution_columns)
        self.assertIn("promotion_status", execution_columns)
        self.assertIn("patch_fingerprint", round_columns)
        self.assertIn("progress", round_columns)
        upgraded.record_adversarial_epoch(execution, {
            "stage": "security", "epoch_number": 2, "merge_policy": "strict", "outcome": "exhausted",
            "progress": True,
        })
        epochs = upgraded.adversarial_epochs_for([execution])[execution]
        self.assertEqual(epochs[0]["stage"], "security")
        self.assertEqual(epochs[0]["epoch_number"], 2)
        self.assertIs(epochs[0]["progress"], True)


if __name__ == "__main__":
    unittest.main()
