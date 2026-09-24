"""Issue #278 failure and observability contracts, using the worker boundary.

AC 17 requires a reviewer that cannot execute to persist FAILED, and the
shared retry model must allow a fresh corrected report. The observability
section prohibits credentials in logs even on an error path. These tests use
synthetic errors and a fake, never-authenticated token; no network is used.
"""
import unittest
from unittest import mock

from harness import LocalReviewFixture, SECURITY_ID, finding, report
from swarm_issue_worker import WorkerError


class SecurityFailureRecoveryTests(LocalReviewFixture, unittest.TestCase):
    def test_speculative_scope_claim_cannot_remove_a_failing_security_check(self):
        # The issue requires verified fixes. The repository's cybersecurity
        # rules additionally say low-confidence findings are advisory only:
        # they must not act on the review or silently retire its checks.
        self.prepare(vulnerable=True)
        self.start()

        def reviewer(prompt, activity=""):
            if self.loop()["phase"] == "test":
                if not self.definition["suites"]:
                    self.add_suite(SECURITY_ID)
                self.worker.ai_output_file.write_text(report(out_of_scope=[finding(
                    confidence="low", suite_ids=[SECURITY_ID],
                    description="The access failure might predate the issue; provenance is uncertain.")]))
            else:
                self.worker.ai_output_file.write_text("No change to the vulnerable access check.")
            return 0

        with self.patches(reviewer):
            self.worker.run_adversarial_pipeline()
        self.assertEqual(self.loop()["status"], "FAILED",
                         "Speculation cannot turn a failing ownership check into a clean security PASS")
        self.assertNotIn(SECURITY_ID, self.loop()["excluded_suites"])
        self.assertTrue(any(r["id"] == SECURITY_ID and r["exit_code"] != 0
                            for r in self.loop()["results"]))
        self.assertFalse(self.deliver.call_args.kwargs["allow_automation"])

    def test_missing_coding_executable_records_failed_review(self):
        self.prepare()
        self.start()
        real_run_ai = self.worker.run_ai
        # Exercise the real provider adapter's unavailable-executable error,
        # rather than faking a nonzero exit (already covered elsewhere).
        with self.patches(real_run_ai), \
                mock.patch.object(self.worker, "provider_environment", return_value={}), \
                mock.patch.object(self.worker, "provider_bin", return_value=""):
            with self.assertRaises(WorkerError) as raised:
                self.worker.run_adversarial_pipeline()
        self.assertIn("executable is unavailable", str(raised.exception))
        self.deliver.assert_not_called()
        self.assertEqual(self.row()["security_review_status"], "FAILED",
                         "A thrown provider error is still a failed review attempt")
        self.assertTrue(self.row()["security_review_error"])
        self.assertIn("review failed", self.output.getvalue())

    def invalid_reference_report(self):
        return report(out_of_scope=[finding(
            title="Legacy access bypass", files=["legacy.py"],
            suite_ids=["adversarial-security-no-such-suite"])])

    def test_unknown_suite_reference_records_failure_without_delivery(self):
        self.prepare()
        self.start()

        def reviewer(prompt, activity=""):
            self.worker.ai_output_file.write_text(self.invalid_reference_report())
            return 0

        with self.patches(reviewer), self.assertRaises(WorkerError):
            self.worker.run_adversarial_pipeline()
        self.deliver.assert_not_called()
        self.assertEqual(self.row()["security_review_status"], "FAILED",
                         "Invalid review references must not disappear from failure history")
        stats = self.worker.history.repository.security_summary(self.worker.config.github_repository)
        self.assertEqual(stats["failedPercent"], 100.0)

    def test_invalid_reference_is_replaced_by_a_fresh_corrected_review_on_retry(self):
        self.prepare()
        self.start()
        invocations = []

        def reviewer(prompt, activity=""):
            invocations.append(prompt)
            self.worker.ai_output_file.write_text(
                self.invalid_reference_report() if len(invocations) == 1 else report())
            return 0

        with self.patches(reviewer):
            with self.assertRaises(WorkerError):
                self.worker.run_adversarial_pipeline()
            try:
                result = self.worker.run_adversarial_pipeline()
            except WorkerError as error:
                self.fail(f"Retry replayed a rejected report instead of obtaining the corrected one: {error}")
        self.assertEqual(result, 10)
        self.assertEqual(len(invocations), 2)
        self.assertEqual(self.row()["security_review_status"], "PASS")
        self.assertFalse(self.row()["security_review_error"])
        self.deliver.assert_called_once()

    def test_security_setup_failure_redacts_credentials_from_operator_logs(self):
        self.prepare()
        self.start()
        token = "ghp_" + "SYNTHETIC" * 5
        reason = f"GitHub authentication failed: Authorization: Bearer {token}"
        with self.patches(), mock.patch.object(self.worker, "ensure_bot_auth",
                                               side_effect=WorkerError(reason)):
            with self.assertRaises(WorkerError):
                self.worker.run_adversarial_pipeline()
        self.assertEqual(self.row()["security_review_status"], "FAILED")
        self.assertNotIn(token, self.row()["security_review_error"])
        self.assertFalse(token in self.output.getvalue(),
                         "Security failure logs disclosed the credential even though history sanitized it")
        self.assertIn("review failed", self.output.getvalue())
        self.deliver.assert_not_called()

    def test_nonzero_coding_exit_records_failure_and_clean_retry_can_pass(self):
        self.prepare()
        self.start()
        invocations = []

        def reviewer(prompt, activity=""):
            invocations.append(prompt)
            self.worker.ai_output_file.write_text("Provider unavailable" if len(invocations) == 1 else report())
            return 1 if len(invocations) == 1 else 0

        with self.patches(reviewer), mock.patch.object(self.worker, "ai_failure_is_quota", return_value=False):
            with self.assertRaises(WorkerError):
                self.worker.run_adversarial_pipeline()
            self.assertEqual(self.row()["security_review_status"], "FAILED")
            self.assertEqual(self.worker.run_adversarial_pipeline(), 10)
        self.assertEqual(len(invocations), 2)
        self.assertEqual(self.row()["security_review_status"], "PASS")
        self.assertFalse(self.row()["security_review_error"])


if __name__ == "__main__":
    unittest.main()
