"""Issue #278 acceptance: verified verdicts, recovery, scope, and observability.

These expectations come from AC 3/4, 6/7, 8-13, 15 and 17, not the current
implementation. In particular, missing analysis is not a clean review, a
renamed finding is not a remediation, and a failed reproduction cannot be
described as a verified fix. All attacks target disposable local fixtures.
"""
import dataclasses
import json
import unittest
from unittest import mock

from harness import (LocalReviewFixture, VULNERABLE, HARDENED, SECURITY_ID,
                     UAT_ID, UNRELATED_ID, finding, report, security, uat)
from swarm_issue_worker import WorkerError
from ai_execution_history import sanitize_text


class ReviewContractTests(LocalReviewFixture, unittest.TestCase):
    def test_implementation_entry_point_automatically_runs_security_when_enabled(self):
        self.prepare()

        def implement_then_review(prompt, activity=""):
            if not self.worker.read_state().get(security.SECURITY_STAGE.key):
                (self.repo / "access.py").write_text(HARDENED + "\n# Owner access implemented.\n")
                self.worker.ai_output_file.write_text("## Summary\nImplemented owner authorization.")
                return 0
            return self.reviewer(prompt, activity)

        with self.patches(implement_then_review), mock.patch.object(self.worker, "comments", return_value=[]), \
                mock.patch.object(self.worker, "minor_bump_requested_by_trusted_user", return_value=False):
            self.assertEqual(self.worker.run_selected_issue(), 10)
        self.assertEqual([phase for phase, _ in self.calls], ["test"])
        self.assertEqual(self.loop()["status"], "PASS")
        self.deliver.assert_called_once()

    def test_disabled_security_does_not_invoke_a_reviewer_after_implementation(self):
        self.prepare()
        self.worker.config = dataclasses.replace(self.worker.config, adversarial_security_enabled=False)

        def implement(prompt, activity=""):
            self.calls.append(activity)
            (self.repo / "access.py").write_text(HARDENED + "\n# Owner access implemented.\n")
            self.worker.ai_output_file.write_text("## Summary\nImplemented owner authorization.")
            return 0

        with self.patches(implement), mock.patch.object(self.worker, "comments", return_value=[]), \
                mock.patch.object(self.worker, "minor_bump_requested_by_trusted_user", return_value=False):
            self.assertEqual(self.worker.run_selected_issue(), 10)
        self.assertEqual(self.calls, ["implementing"])
        self.assertNotIn(security.SECURITY_STAGE.key, self.worker.read_state())
        self.assertEqual(self.row()["security_outcome"], "disabled")
        self.deliver.assert_called_once()

    def test_dynamic_router_receives_security_task_context_on_every_review_phase(self):
        self.prepare(vulnerable=True)
        self.worker.config = dataclasses.replace(self.worker.config, dynamic_model_routing=True)
        self.start()
        with self.patches(), mock.patch.object(self.worker, "run_router", return_value="fixture decision") as router, \
                mock.patch.object(self.worker, "resolve_router_response", return_value={
                    "provider": "codex", "selected_model": "fixture-security-model", "reasoning_effort": "high"}):
            self.worker.run_adversarial_pipeline()
        self.assertEqual(router.call_count, 3)
        for call in router.call_args_list:
            self.assertIn("security", call.args[1].lower())
            self.assertIn("adversarial", call.args[1].lower())
            self.assertIn(self.worker.issue.body, call.args[1])
        self.assertEqual(self.loop()["status"], "FIXED")

    def test_unresolved_security_cap_does_not_enter_successful_automatic_delivery(self):
        self.prepare(vulnerable=True)
        self.worker.config = dataclasses.replace(self.worker.config, auto_approve=True,
                                                 auto_merge=True, auto_promote=True)
        self.start()

        def unresolved(prompt, activity=""):
            self.worker.ai_output_file.write_text(
                report(in_scope=[finding()]) if self.loop()["phase"] == "test" else "No fix available.")
            return 0

        normal_finalizer = self.worker.finalize_issue

        def delivery(commit_sha, *, allow_automation=True):
            self.assertFalse(allow_automation,
                             "Unresolved exploitable findings must not enable automatic merge/promotion")
            return "https://example.invalid/pull/902", "ai/claude/issue-278", commit_sha

        with self.patches(unresolved), mock.patch.object(self.worker, "deliver_pull_request", side_effect=delivery), \
                mock.patch.object(self.worker, "finalize_needs_input"):
            self.deliver.side_effect = normal_finalizer
            self.worker.run_adversarial_pipeline()
        self.assertEqual(self.row()["security_review_status"], "FAILED")

    def test_local_attack_fix_retest_and_separate_labelled_issue(self):
        self.prepare(vulnerable=True)
        self.start()
        outside = finding(title="Legacy download bypasses ownership", files=["legacy.py"])

        def role(prompt, activity=""):
            self.reviewer(prompt, activity)
            if self.loop()["phase"] == "test":
                vulnerable = (self.repo / "access.py").read_text() == VULNERABLE
                self.worker.ai_output_file.write_text(report(
                    in_scope=[finding()] if vulnerable else [], out_of_scope=[outside]))
            return 0

        with self.patches(role):
            self.assertEqual(self.worker.run_adversarial_pipeline(), 10)
        self.assertEqual([phase for phase, _ in self.calls], ["test", "fix", "test"])
        self.assertEqual(self.loop()["status"], "FIXED")
        self.assertEqual(self.loop()["results"][0]["exit_code"], 0)
        self.assertIn("Ran 1 test", self.loop()["results"][0]["output"])
        creates = [args for args, _ in self.api if args[:2] == ["issue", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertIn("adversarial-security", creates[0])
        self.assertIn("--assignee", creates[0])
        self.assertTrue(any(args[:2] == ["label", "create"] and "adversarial-security" in args
                            for args, _ in self.api))
        self.assertIn("https://example.invalid/issues/901", self.worker.adversarial_summary_line())
        self.deliver.assert_called_once()

    def test_empty_json_is_not_evidence_that_security_analysis_completed(self):
        self.prepare()
        self.start()

        def incomplete(prompt, activity=""):
            self.worker.ai_output_file.write_text(security.RESULT_MARKER + " {}")
            return 0

        with self.patches(incomplete):
            with self.assertRaises(WorkerError, msg="An empty report must not become PASS"):
                self.worker.run_adversarial_pipeline()
        self.deliver.assert_not_called()
        self.assertEqual(self.row()["security_review_status"], "FAILED")

    def test_complete_clean_report_may_have_no_executable_security_suite(self):
        self.prepare()
        self.start()

        def clean(prompt, activity=""):
            self.worker.ai_output_file.write_text(report())
            return 0

        with self.patches(clean):
            self.assertEqual(self.worker.run_adversarial_pipeline(), 10)
        self.assertEqual(self.loop()["status"], "PASS")
        self.assertEqual(self.loop()["round"], 0)

    def test_malformed_finding_records_failure_and_never_delivers(self):
        self.prepare()
        self.start()

        def malformed(prompt, activity=""):
            self.worker.ai_output_file.write_text(report(in_scope=[finding(evidence=None)]))
            return 0

        with self.patches(malformed), self.assertRaises(WorkerError):
            self.worker.run_adversarial_pipeline()
        self.assertEqual(self.row()["security_review_status"], "FAILED")
        self.deliver.assert_not_called()

    def test_successful_retry_replaces_the_failed_current_verdict(self):
        self.prepare()
        self.start()

        def broken(prompt, activity=""):
            self.worker.ai_output_file.write_text("Reviewer output was truncated.")
            return 0

        with self.patches(broken), self.assertRaises(WorkerError):
            self.worker.run_adversarial_pipeline()
        execution_id = self.worker.history.execution_id
        with self.patches():
            self.worker.run_adversarial_pipeline()
        self.assertEqual(self.worker.history.execution_id, execution_id)
        self.assertEqual(self.loop()["status"], "PASS", "A completed clean retry must supersede failure")
        self.assertEqual(self.row()["security_review_status"], "PASS")
        self.assertFalse(self.row()["security_review_error"], "Old errors may remain in events, not the current verdict")

    def test_failed_review_is_included_in_history_failure_rate(self):
        self.prepare()
        self.start()

        def broken(prompt, activity=""):
            self.worker.ai_output_file.write_text("Review unavailable")
            return 1

        with self.patches(broken), mock.patch.object(self.worker, "ai_failure_is_quota", return_value=False):
            with self.assertRaises(WorkerError):
                self.worker.run_adversarial_pipeline()
        self.assertEqual(self.row()["security_review_status"], "FAILED")
        stats = self.worker.history.repository.security_summary(self.worker.config.github_repository)
        self.assertEqual(stats["reviews"], 1, "Failed execution is a review attempt, not 'never ran'")
        self.assertEqual(stats["failedPercent"], 100.0)

    def test_framework_setup_error_is_recorded_as_a_security_failure(self):
        self.prepare()
        self.start()
        with self.patches(), mock.patch.object(self.worker, "prepare_adversarial_framework",
                                               side_effect=WorkerError("Fixture framework unavailable")):
            with self.assertRaises(WorkerError):
                self.worker.run_adversarial_pipeline()
        self.deliver.assert_not_called()
        self.assertEqual(self.row()["security_review_status"], "FAILED")

    def test_renaming_an_unfixed_vulnerability_does_not_verify_its_remediation(self):
        self.prepare(vulnerable=True)
        self.start()

        def never_fix(prompt, activity=""):
            loop = self.loop()
            if loop["phase"] == "test":
                title = "Record access bypasses owner authorization" if loop["round"] % 2 == 0 else "Record reader bypasses owner authorization"
                self.worker.ai_output_file.write_text(report(in_scope=[finding(title=title)]))
            else:
                self.worker.ai_output_file.write_text("No changes made; access.py is still vulnerable.")
            return 0

        with self.patches(never_fix):
            self.worker.run_adversarial_pipeline()
        self.assertEqual((self.repo / "access.py").read_text(), VULNERABLE)
        self.assertEqual(self.loop()["status"], "FAILED")
        metadata = json.loads(self.row()["security_findings"])
        self.assertEqual(metadata["inScopeFixed"], 0,
                         "Neither title wording was ever remediated; the code remained exploitable")

    def test_a_failing_reproduction_overrides_a_reviewers_claim_of_remediation(self):
        self.prepare(vulnerable=True)
        self.start()

        def false_clear(prompt, activity=""):
            loop = self.loop()
            if loop["phase"] == "test":
                if not self.definition["suites"]:
                    self.add_suite(SECURITY_ID)
                self.worker.ai_output_file.write_text(report(in_scope=[finding(suite_ids=[SECURITY_ID])]
                                                             if loop["round"] == 0 else []))
            else:
                self.worker.ai_output_file.write_text("No changes made.")
            return 0

        with self.patches(false_clear):
            self.worker.run_adversarial_pipeline()
        self.assertEqual(self.loop()["status"], "FAILED")
        self.assertNotEqual(self.loop()["results"][0]["exit_code"], 0)
        self.assertEqual(json.loads(self.row()["security_findings"])["inScopeFixed"], 0,
                         "A still-failing exploit regression is not a verified fix")

    def test_security_keeps_uat_scope_exclusions_without_refiling_unrelated_bugs(self):
        self.prepare(with_uat=True)
        self.start(uat.UAT_STAGE)

        def role(prompt, activity=""):
            state = self.worker.read_state()
            if state.get("adversarial", {}).get("phase") != "done":
                self.add_suite(UAT_ID, origin="adversarial", assertion="self.assertTrue(access['can_read']('alice', 'alice'))")
                self.add_suite(UNRELATED_ID, origin="adversarial", assertion="self.fail('Pre-existing legacy bug, unrelated to record authorization')")
                self.worker.ai_output_file.write_text(uat.RESULT_MARKER + json.dumps({
                    "dispute_resolution": "", "out_of_scope": [{"title": "Legacy export loses metadata",
                    "body": "Pre-existing legacy export failure; unrelated to record authorization.",
                    "suite_ids": [UNRELATED_ID]}]}))
                return 0
            return self.reviewer(prompt, activity)

        with self.patches(role):
            self.worker.run_adversarial_pipeline()
        self.assertEqual(self.worker.read_state()["adversarial"]["outcome"], "clean_first_pass")
        self.assertEqual(self.loop()["status"], "PASS",
                         "A second stage must not turn a previously excluded unrelated bug into a blocker")
        self.assertEqual(self.loop()["round"], 0)
        self.assertIn(UNRELATED_ID, [s["id"] for s in uat.read_definition(self.repo)["suites"]])
        self.assertIn(UAT_ID, [r["id"] for r in self.loop()["results"]])

    def test_known_token_in_duplicate_finding_title_is_scrubbed_from_logs(self):
        self.prepare()
        self.start()
        token = "ghp_" + "A" * 36  # Fake deterministic credential; never used for authentication.
        self.assertNotIn(token, sanitize_text(token), "The repository sanitizer recognizes this fixture")
        outside = finding(title=f"Legacy client embeds {token}", files=["legacy.py"])

        def role(prompt, activity=""):
            self.worker.ai_output_file.write_text(report(in_scope=[finding()] if self.loop()["round"] == 0 else [],
                                                       out_of_scope=[outside]))
            return 0

        with self.patches(role):
            self.worker.run_adversarial_pipeline()
        self.assertNotIn(token, self.output.getvalue(), "Duplicate-suppression logs must not disclose credentials")


if __name__ == "__main__":
    unittest.main()
