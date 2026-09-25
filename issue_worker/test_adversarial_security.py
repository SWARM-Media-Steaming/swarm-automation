"""UAT of the adversarial cybersecurity stage against real checkouts.

Only the coding providers and GitHub's API are faked: git delivery uses a local
bare remote and the security suites are real child processes, so a "verified
fix" here is a suite that actually ran and an independent reviewer that
actually stopped reporting the finding.
"""
import contextlib
import dataclasses
import io
import json
import sys
import unittest
from unittest import mock

import adversarial_security as security
import adversarial_uat as uat
import test_swarm_issue_worker as fixtures
from adversarial_core import MAX_ROUNDS
from ai_execution_history import ExecutionHistoryService
from swarm_issue_worker import IssueContext, ProviderChoice, ProviderUsage, Worker, WorkerError

VULNERABLE = 'TOKEN = "ghp_hardcodedtokenvalue"\n'
HARDENED = 'import os\n\nTOKEN = os.environ["SERVICE_TOKEN"]\n'

SECURITY_TEST = (
    "import unittest\n"
    "from pathlib import Path\n"
    "class SecretsRegression(unittest.TestCase):\n"
    "    def test_no_literal_token(self):\n"
    "        self.assertNotIn('ghp_', Path('service.py').read_text())\n"
)


def finding(**overrides):
    value = {
        "title": "Service token is hardcoded in service.py",
        "description": "service.py assigns a real GitHub token literal.",
        "severity": "High",
        "confidence": "high",
        "files": ["service.py"],
        "attack_scenario": "Anyone with read access to the repository reuses the token.",
        "impact": "Full API access as the token's owner.",
        "evidence": "service.py line 1 contains a ghp_ literal.",
        "remediation": "Read the token from the environment.",
    }
    value.update(overrides)
    return value


class AdversarialSecurityTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def prepare(self, *, hardened=False, uat_enabled=False, history=True, auto=False):
        self.worker.config = dataclasses.replace(
            self.worker.config, adversarial_security_enabled=True,
            adversarial_uat_enabled=uat_enabled, auto_approve=auto, auto_promote=auto,
            ai_execution_history_enabled=history,
            execution_history_db=self.state / "history.sqlite3",
        )
        self.worker.history = ExecutionHistoryService(history, self.state / "history.sqlite3")
        self.worker.issue = IssueContext(
            278, "Add a service client",
            "The client must authenticate without embedding credentials in the repository.",
            [], "https://example.invalid/issues/278")
        self.worker.choice = ProviderChoice("Claude", "fixer-model", "high", "implementer-session")
        self.git("switch", "-c", "ai/claude/issue-278")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()
        (self.repo / "service.py").write_text(HARDENED if hardened else VULNERABLE)
        (self.repo / "tracked.txt").write_text("fixed\n")
        self.git("add", ".")
        self.git("commit", "-qm", "[claude] Implementation (#278)")
        self.implementation = self.git("rev-parse", "HEAD")
        self.calls = []
        self.api = []
        self.bodies = []
        self.comments_posted = []
        self.open_security_issues = []
        self.delivered_state = {}

    # -- provider / GitHub doubles -------------------------------------------------

    def gh(self, args, provider=None, body=None):
        self.api.append(args)
        self.bodies.append((args, body))
        if args[:2] == ["issue", "list"]:
            if "--label" in args and security.FINDING_LABEL in args:
                return json.dumps(self.open_security_issues)
            return "[]"
        if args[:2] == ["pr", "list"]:
            return "[]"
        if args[:2] == ["pr", "create"]:
            return "https://example.invalid/pull/279"
        if args[:2] == ["issue", "create"]:
            return "https://example.invalid/issues/300"
        if args[:2] == ["issue", "comment"]:
            self.comments_posted.append(body)
        return ""

    def add_security_suite(self):
        directory = self.repo / "tests/adversarial/security"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "test_secrets.py").write_text(SECURITY_TEST)
        definition = uat.read_definition(self.repo)
        definition["suites"] = [
            s for s in definition["suites"] if s.get("id") != "adversarial-security-278"
        ] + [{
            "id": "adversarial-security-278", "name": "Secrets regression",
            "origin": security.ORIGIN, "timeoutSeconds": 20,
            "command": [sys.executable, "-m", "unittest", "discover",
                        "-s", "tests/adversarial/security"],
        }]
        (self.repo / uat.DEFINITION).write_text(json.dumps(definition))

    def add_uat_suite(self):
        directory = self.repo / "tests/adversarial"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "test_issue.py").write_text(
            "import unittest\nfrom pathlib import Path\n"
            "class Acceptance(unittest.TestCase):\n"
            "    def test_requirement(self):\n"
            "        self.assertEqual(Path('tracked.txt').read_text().strip(), 'fixed')\n"
        )
        definition = uat.read_definition(self.repo)
        definition["suites"] = [
            s for s in definition["suites"] if s.get("id") != "adversarial-278"
        ] + [{
            "id": "adversarial-278", "name": "Issue acceptance", "origin": "adversarial",
            "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests/adversarial",
                        "-p", "test_issue.py"],
            "timeoutSeconds": 20,
        }]
        (self.repo / uat.DEFINITION).write_text(json.dumps(definition))

    def security_report(self, **payload):
        body = {"summary": "Reviewed the client's credential handling.",
                "dispute_resolution": "", "in_scope": [], "out_of_scope": []}
        body.update(payload)
        return security.RESULT_MARKER + " " + json.dumps(body)

    def role(self, prompt, activity=""):
        """One reviewer/fixer turn: report while vulnerable, fix when asked."""
        state = self.worker.read_state()
        stage = uat.UAT_STAGE if state.get("adversarial", {}).get("phase") not in (None, "done") \
            else security.SECURITY_STAGE
        loop = state[stage.key]
        self.calls.append((stage.slug, loop["phase"], self.worker.choice.name, activity, prompt))
        if not self.worker.choice.session_id:
            self.worker.choice.session_id = f"session-{len(self.calls)}"
        self.worker.update_state(session_id=self.worker.choice.session_id, session_started=True)
        if stage is uat.UAT_STAGE:
            if loop["phase"] == "test":
                self.add_uat_suite()
                self.worker.ai_output_file.write_text(
                    uat.RESULT_MARKER + ' {"out_of_scope": [], "dispute_resolution": ""}')
            else:
                self.worker.ai_output_file.write_text("UAT fix")
            return 0
        if loop["phase"] == "test":
            self.add_security_suite()
            vulnerable = "ghp_" in (self.repo / "service.py").read_text()
            self.worker.ai_output_file.write_text(
                self.security_report(in_scope=[finding()] if vulnerable else []))
        else:
            (self.repo / "service.py").write_text(HARDENED)
            self.worker.ai_output_file.write_text("Removed the embedded credential.")
        return 0

    def patches(self, role=None):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(self.worker, "provider_usage",
                                              return_value=ProviderUsage(0, 80)))
        stack.enter_context(mock.patch.object(self.worker, "ensure_bot_auth"))
        stack.enter_context(mock.patch.object(self.worker, "comments", return_value=[]))
        stack.enter_context(mock.patch.object(self.worker, "run_ai", side_effect=role or self.role))
        stack.enter_context(mock.patch.object(self.worker.github, "gh", side_effect=self.gh))
        stack.enter_context(mock.patch.object(
            self.worker, "minor_bump_requested_by_trusted_user", return_value=False))
        # Delivery clears the in-progress checkpoint, so snapshot the finished
        # loop first; assertions are about what was delivered, not about what
        # survives on disk afterwards.
        deliver = self.worker.finalize_issue

        def capture(commit_sha, ai_output, *, allow_automation=True):
            self.delivered_state = self.worker.read_state()
            return deliver(commit_sha, ai_output, allow_automation=allow_automation)

        stack.enter_context(mock.patch.object(self.worker, "finalize_issue", side_effect=capture))
        return stack

    def start(self):
        self.worker.initialize_stage(security.SECURITY_STAGE, self.implementation,
                                     "## Summary\nSECRET implementer reasoning")

    def loop_state(self):
        state = (self.worker.read_state() if self.worker.in_progress_file.exists()
                 else self.delivered_state)
        return state["adversarial_security"]

    # -- the review itself ---------------------------------------------------------

    def test_clean_review_passes_without_a_fix_round_and_delivers_once(self):
        self.prepare(hardened=True)
        self.start()
        with self.patches(), mock.patch.object(self.worker, "push_ref",
                                               wraps=self.worker.push_ref) as push:
            self.assertEqual(self.worker.run_adversarial_pipeline(), 10)
        self.assertEqual([c[:2] for c in self.calls], [("security", "test")])
        self.assertEqual(self.calls[0][3], "running independent adversarial security review")
        loop = self.loop_state()
        self.assertEqual((loop["outcome"], loop["status"]), ("clean_first_pass", "PASS"))
        push.assert_called_once()
        self.assertEqual(len(self.comments_posted), 1)
        self.assertIn("Adversarial Cybersecurity: PASS", self.comments_posted[0])

    def test_in_scope_vulnerability_is_fixed_and_independently_reverified(self):
        self.prepare()
        self.start()
        with self.patches():
            self.assertEqual(self.worker.run_adversarial_pipeline(), 10)
        self.assertEqual([c[:2] for c in self.calls],
                         [("security", "test"), ("security", "fix"), ("security", "test")])
        # Each phase is a different, fresh provider context; the reviewer never
        # sees the implementer's reasoning.
        self.assertNotEqual(self.calls[1][2], self.calls[2][2])
        self.assertTrue(all("SECRET implementer reasoning" not in c[4] for c in self.calls))
        loop = self.loop_state()
        self.assertEqual((loop["outcome"], loop["status"]), ("resolved_after_n", "FIXED"))
        self.assertEqual(len(loop["fixed_findings"]), 1)
        self.assertEqual(loop["open_findings"], [])
        self.assertNotIn("ghp_", (self.repo / "service.py").read_text())
        self.assertIn("Adversarial Cybersecurity: FIXED", self.comments_posted[0])
        self.assertIn("- In-scope discovered: 1", self.comments_posted[0])
        self.assertIn("- High: 1", self.comments_posted[0])

    def test_history_records_security_rounds_beside_uat_rounds_of_the_same_issue(self):
        self.prepare(uat_enabled=True)
        self.worker.initialize_stage(uat.UAT_STAGE, self.implementation, "## Summary\nImplementation")
        with self.patches():
            self.assertEqual(self.worker.run_adversarial_pipeline(), 10)
        self.assertEqual([c[0] for c in self.calls][0], "uat")
        self.assertIn("security", [c[0] for c in self.calls])
        row = self.worker.history.repository.for_repository(
            self.worker.config.github_repository)[0]
        self.assertEqual(row["adversarial_outcome"], "clean_first_pass")
        self.assertEqual(row["security_outcome"], "resolved_after_n")
        self.assertEqual(row["security_review_status"], "FIXED")
        self.assertEqual(row["security_round_count"], 1)
        self.assertEqual(json.loads(row["security_findings"])["severity"]["High"], 1)
        rounds = self.worker.history.repository.adversarial_rounds_for(
            [row["execution_id"]])[row["execution_id"]]
        # A UAT round 0 and a security round 0 of the same execution are
        # distinct rows, not an upsert collision.
        self.assertEqual(sorted((r["stage"], r["round_number"]) for r in rounds),
                         [("security", 0), ("security", 1), ("uat", 0)])
        self.assertEqual([r["findings_found"] for r in rounds if r["stage"] == "security"], [1, 0])
        body = self.comments_posted[0]
        self.assertIn("- Adversarial UAT: clean first pass", body)
        self.assertIn("- Adversarial Cybersecurity: FIXED", body)

    def test_security_round_also_runs_the_uat_suites_so_a_hardening_regression_blocks(self):
        self.prepare(uat_enabled=True)
        self.worker.initialize_stage(uat.UAT_STAGE, self.implementation, "## Summary\nImplementation")
        broke_behaviour = []

        def role(prompt, activity=""):
            state = self.worker.read_state()
            security_loop = state.get("adversarial_security") or {}
            if security_loop.get("phase") == "fix" and not broke_behaviour:
                broke_behaviour.append(True)
                self.calls.append(("security", "fix", self.worker.choice.name, activity, prompt))
                (self.repo / "service.py").write_text(HARDENED)
                (self.repo / "tracked.txt").write_text("broken\n")
                self.worker.ai_output_file.write_text("Hardened, but broke the feature.")
                return 0
            if security_loop.get("phase") == "fix":
                (self.repo / "tracked.txt").write_text("fixed\n")
            return self.role(prompt, activity)

        with self.patches(role):
            self.assertEqual(self.worker.run_adversarial_pipeline(), 10)
        loop = self.loop_state()
        # The functional UAT suite failing inside the security round is what
        # forced the extra fix round, even though no finding was outstanding.
        self.assertGreaterEqual(loop["round"], 2)
        self.assertEqual(loop["outcome"], "resolved_after_n")
        self.assertEqual((self.repo / "tracked.txt").read_text().strip(), "fixed")

    def test_pipeline_resumes_a_later_stage_left_dirty_behind_an_already_finished_earlier_one(self):
        # Regression: UAT finishes cleanly, then the security stage's fix
        # round writes its fix to disk (response cached) but crashes before
        # committing it — the shape of a validate/commit failure like the
        # production index.lock collision. Every retry after that used to
        # re-check UAT's already-"done" worktree cleanliness on the way
        # through the pipeline and abort before the security stage, the one
        # actually stuck, ever got a turn to resume its cached response.
        self.prepare(uat_enabled=True)
        self.worker.initialize_stage(uat.UAT_STAGE, self.implementation, "## Summary\nImplementation")
        real_validate = self.worker.validate_stage_edits
        crashed = []

        def flaky_validate(stage, loop, report):
            if stage is security.SECURITY_STAGE and loop["phase"] == "fix" and not crashed:
                crashed.append(True)
                raise WorkerError("simulated index.lock crash mid-validation")
            return real_validate(stage, loop, report)

        with self.patches(), mock.patch.object(self.worker, "validate_stage_edits", side_effect=flaky_validate):
            with self.assertRaisesRegex(WorkerError, "simulated index.lock crash"):
                self.worker.run_adversarial_pipeline()
        self.assertNotIn("ghp_", (self.repo / "service.py").read_text())
        self.assertTrue(self.worker.worktree_status())
        with self.patches():
            self.assertEqual(self.worker.run_adversarial_pipeline(), 10)
        self.assertFalse(self.worker.worktree_status() if self.worker.in_progress_file.exists() else False)
        loop = self.loop_state()
        self.assertEqual(loop["outcome"], "resolved_after_n")

    def test_cap_hit_reports_failed_and_never_pass(self):
        self.prepare()
        self.start()

        def never_fix(prompt, activity=""):
            status = self.role(prompt, activity)
            (self.repo / "service.py").write_text(VULNERABLE)
            return status

        with self.patches(never_fix):
            self.assertEqual(self.worker.run_adversarial_pipeline(), 10)
        loop = self.loop_state()
        self.assertEqual((loop["round"], loop["outcome"], loop["status"]),
                         (MAX_ROUNDS, "cap_hit", "FAILED"))
        row = self.worker.history.repository.for_repository(
            self.worker.config.github_repository)[0]
        self.assertEqual(row["security_review_status"], "FAILED")
        self.assertNotIn("Adversarial Cybersecurity: PASS", self.comments_posted[0])
        self.assertIn("did not reach a clean state after six", self.comments_posted[0])
        self.assertIn("Service token is hardcoded", self.comments_posted[0])

    def test_a_review_that_cannot_execute_records_failed_rather_than_a_clean_pass(self):
        self.prepare(hardened=True)
        self.start()

        def broken_reviewer(prompt, activity=""):
            self.worker.ai_output_file.write_text("I could not complete the review.")
            return 0

        with self.patches(broken_reviewer), self.assertRaises(WorkerError):
            self.worker.run_adversarial_pipeline()
        loop = self.loop_state()
        self.assertEqual(loop["status"], "FAILED")
        self.assertNotEqual(loop.get("outcome"), "clean_first_pass")
        self.assertEqual(self.comments_posted, [])
        row = self.worker.history.repository.for_repository(
            self.worker.config.github_repository)[0]
        self.assertEqual(row["security_review_status"], "FAILED")
        self.assertEqual(row["security_outcome"], "")

    def test_review_is_skipped_entirely_when_the_setting_is_off(self):
        self.prepare(hardened=True)
        self.worker.config = dataclasses.replace(
            self.worker.config, adversarial_security_enabled=False)
        self.assertEqual(self.worker.adversarial_stages(), [])
        self.worker.record_disabled_adversarial_stages()
        row = self.worker.history.repository.for_repository(
            self.worker.config.github_repository)[0]
        self.assertEqual(row["security_outcome"], "disabled")
        self.assertEqual(row["security_review_status"], "")

    def test_end_to_end_deliberately_vulnerable_change_is_fixed_filed_labelled_and_reported(self):
        """The whole pipeline against one deliberately vulnerable change.

        One in-scope vulnerability the issue introduced, and one unrelated
        weakness elsewhere: the first must be fixed and re-verified inside the
        issue, the second must leave as its own labelled issue, and both must
        be visible on the original issue afterwards.
        """
        self.prepare()
        self.start()
        unrelated = finding(title="Legacy importer executes shell input",
                           files=["legacy/import.py"], severity="Critical",
                           description="legacy/import.py passes user input to a shell.",
                           attack_scenario="A crafted filename runs arbitrary commands.",
                           evidence="legacy/import.py calls os.system with request data.",
                           remediation="Use argv execution without a shell.")
        reported = []

        def reviewer(prompt, activity=""):
            loop = self.loop_state()
            if loop["phase"] == "fix":
                (self.repo / "service.py").write_text(HARDENED)
                self.worker.ai_output_file.write_text("Read the token from the environment.")
                return 0
            self.add_security_suite()
            vulnerable = "ghp_" in (self.repo / "service.py").read_text()
            reported.append(vulnerable)
            self.worker.ai_output_file.write_text(self.security_report(
                in_scope=[finding()] if vulnerable else [],
                out_of_scope=[unrelated] if vulnerable else []))
            return 0

        with self.patches(reviewer):
            self.assertEqual(self.worker.run_adversarial_pipeline(), 10)

        # detected, then fixed, then independently re-verified
        self.assertEqual(reported, [True, False])
        loop = self.loop_state()
        self.assertEqual(loop["status"], "FIXED")
        self.assertEqual([f["title"] for f in loop["fixed_findings"]],
                         ["Service token is hardcoded in service.py"])
        self.assertNotIn("ghp_", (self.repo / "service.py").read_text())

        # the fix is validated by a suite that actually ran
        self.assertTrue(loop["results"])
        self.assertTrue(all(result["exit_code"] == 0 for result in loop["results"]))
        self.assertIn("adversarial-security-278", [r["id"] for r in loop["results"]])

        # the unrelated weakness left as its own labelled issue, once
        created = [a for a in self.api if a[:2] == ["issue", "create"]]
        self.assertEqual(len(created), 1)
        self.assertIn(unrelated["title"], created[0])
        self.assertIn(security.FINDING_LABEL, created[0])
        self.assertEqual([d["url"] for d in loop["filed_finding_details"]],
                         ["https://example.invalid/issues/300"])

        # and all of it is reported back on the originating issue
        body = self.comments_posted[0]
        self.assertIn("Adversarial Cybersecurity: FIXED", body)
        self.assertIn("- In-scope discovered: 1", body)
        self.assertIn("- In-scope fixed: 1", body)
        self.assertIn("- Out-of-scope issues created: 1", body)
        self.assertIn("https://example.invalid/issues/300", body)
        self.assertIn("adversarial-security-278: passed", body)
        self.assertNotIn("SECRET implementer reasoning", body.split("<details>")[0])

    # -- out-of-scope findings -----------------------------------------------------

    def test_out_of_scope_finding_files_one_labelled_issue_with_full_triage_detail(self):
        self.prepare(hardened=True)
        self.start()
        outside = finding(title="Admin API accepts unauthenticated deletes",
                          files=["admin/api.py"], severity="Critical")

        def reviewer(prompt, activity=""):
            self.calls.append(("security", "test", self.worker.choice.name, activity, prompt))
            self.add_security_suite()
            self.worker.ai_output_file.write_text(self.security_report(out_of_scope=[outside]))
            return 0

        with self.patches(reviewer), contextlib.redirect_stdout(io.StringIO()) as output:
            self.worker.run_adversarial_pipeline()
        created = [a for a in self.api if a[:2] == ["issue", "create"]]
        self.assertEqual(len(created), 1)
        self.assertIn("--label", created[0])
        self.assertIn(security.FINDING_LABEL, created[0])
        labelled = [a for a in self.api if a[:2] == ["label", "create"]]
        self.assertTrue(any(security.FINDING_LABEL in a for a in labelled),
                        "the dedicated label must be provisioned when missing")
        body = next(value for args, value in self.bodies if args[:2] == ["issue", "create"])
        for expected in ("Severity:** Critical", "Confidence:** high", "admin/api.py",
                         "## Attack scenario", "## Impact", "## Evidence / reproduction",
                         "## Recommended remediation", "#278"):
            self.assertIn(expected, body)
        self.assertIn("Filed out-of-scope adversarial cybersecurity finding", output.getvalue())
        loop = self.loop_state()
        self.assertEqual(loop["status"], "FINDINGS_CREATED")
        self.assertEqual(len(loop["filed_finding_details"]), 1)

    def test_low_confidence_findings_neither_block_nor_reach_github(self):
        self.prepare(hardened=True)
        self.start()
        speculative_in_scope = finding(title="Timing side channel may leak the token",
                                       confidence="low", severity="Low")
        speculative_outside = finding(title="Legacy uploader might allow traversal",
                                      confidence="low", severity="Medium")

        def reviewer(prompt, activity=""):
            self.calls.append(("security", "test", self.worker.choice.name, activity, prompt))
            self.add_security_suite()
            self.worker.ai_output_file.write_text(self.security_report(
                in_scope=[speculative_in_scope], out_of_scope=[speculative_outside]))
            return 0

        with self.patches(reviewer):
            self.worker.run_adversarial_pipeline()
        loop = self.loop_state()
        self.assertEqual(loop["outcome"], "clean_first_pass")
        self.assertEqual(len(loop["advisory_findings"]), 1)
        self.assertEqual(loop["filed_finding_details"], [])
        self.assertFalse([a for a in self.api if a[:2] == ["issue", "create"]])

    def test_a_reworded_rediscovery_of_an_open_finding_is_not_filed_twice(self):
        self.prepare(hardened=True)
        self.start()
        self.open_security_issues = [{
            "title": "Admin API accepts unauthenticated deletes",
            "url": "https://example.invalid/issues/250",
        }]
        outside = finding(title="Admin API accepts unauthenticated delete",
                          files=["admin/api.py"])

        def reviewer(prompt, activity=""):
            self.add_security_suite()
            self.worker.ai_output_file.write_text(self.security_report(out_of_scope=[outside]))
            return 0

        with self.patches(reviewer), contextlib.redirect_stdout(io.StringIO()) as output:
            self.worker.run_adversarial_pipeline()
        self.assertFalse([a for a in self.api if a[:2] == ["issue", "create"]])
        self.assertIn("suppressed as a duplicate", output.getvalue())
        self.assertEqual(self.loop_state()["filed_finding_details"], [])

    # -- guard rails ---------------------------------------------------------------

    def test_reviewer_may_not_change_product_code_and_fixer_may_not_weaken_tests(self):
        self.prepare()
        self.start()

        def cheating(prompt, activity=""):
            loop = self.loop_state()
            if loop["phase"] == "test":
                self.add_security_suite()
                (self.repo / "service.py").write_text(HARDENED)
                self.worker.ai_output_file.write_text(self.security_report())
            return 0

        with self.patches(cheating), self.assertRaises(WorkerError) as raised:
            self.worker.run_adversarial_pipeline()
        self.assertIn("Tester changed product files", str(raised.exception))
        self.assertIn("ghp_", (self.repo / "service.py").read_text())

    def test_a_security_fixer_editing_its_own_tests_is_reverted_into_a_dispute(self):
        self.prepare()
        self.start()

        def tampering(prompt, activity=""):
            loop = self.loop_state()
            if loop["phase"] == "test":
                return self.role(prompt, activity)
            (self.repo / "service.py").write_text(HARDENED)
            (self.repo / "tests/adversarial/security/test_secrets.py").write_text(
                "import unittest\n"
                "class Pass(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
            self.worker.ai_output_file.write_text("Fixed.")
            return 0

        with self.patches(tampering):
            self.worker.run_adversarial_pipeline()
        self.assertIn("ghp_", (self.repo / "tests/adversarial/security/test_secrets.py").read_text())
        self.assertIn("worker restored", self.loop_state()["dispute"])

    def test_uat_and_security_own_separate_test_roots_and_suite_origins(self):
        self.assertTrue(security.SECURITY_STAGE.owns_path(
            "tests/adversarial/security/test_secrets.py"))
        self.assertFalse(uat.UAT_STAGE.owns_path(
            "tests/adversarial/security/test_secrets.py"))
        self.assertTrue(uat.UAT_STAGE.owns_path("tests/adversarial/test_issue.py"))
        self.assertFalse(security.SECURITY_STAGE.owns_path("tests/adversarial/test_issue.py"))
        self.assertNotEqual(uat.UAT_STAGE.origin, security.SECURITY_STAGE.origin)
        self.assertNotEqual(uat.UAT_STAGE.key, security.SECURITY_STAGE.key)
        self.assertNotEqual(uat.UAT_STAGE.finding_labels, security.SECURITY_STAGE.finding_labels)

    # -- report contract -----------------------------------------------------------

    def test_a_finding_without_severity_evidence_or_confidence_is_rejected(self):
        parse = security.SECURITY_STAGE.parse_report
        with self.assertRaises(ValueError):
            parse(security.RESULT_MARKER + json.dumps({"in_scope": [{"title": "x"}]}))
        with self.assertRaises(ValueError):
            parse(security.RESULT_MARKER + json.dumps(
                {"in_scope": [finding(severity="Catastrophic")]}))
        with self.assertRaises(ValueError):
            parse(security.RESULT_MARKER + json.dumps(
                {"out_of_scope": [finding(confidence="certain")]}))
        with self.assertRaises(ValueError):
            parse(security.RESULT_MARKER + json.dumps({"in_scope": [finding(evidence="  ")]}))
        report = parse(security.RESULT_MARKER + json.dumps(
            {"summary": "ok", "in_scope": [finding(severity="critical", confidence="HIGH")]}))
        self.assertEqual(report["in_scope"][0]["severity"], "Critical")
        self.assertEqual(report["in_scope"][0]["confidence"], "high")

    def test_the_router_is_told_this_is_a_security_analysis_task(self):
        self.assertIn("Security / adversarial code analysis",
                      security.SECURITY_STAGE.router_task)

    def test_the_review_prompt_carries_the_issue_diff_and_scope_rules(self):
        self.prepare()
        self.start()
        loop = self.loop_state()
        loop["stage_base"] = self.implementation
        prompt = self.worker.adversarial_prompt(security.SECURITY_STAGE, loop)
        for expected in ("independent adversarial security engineer", "IN SCOPE", "OUT OF SCOPE",
                         "service.py", "Files changed by this issue", "confidence",
                         "tests/adversarial/security/", security.RESULT_MARKER,
                         self.worker.issue.body):
            self.assertIn(expected, prompt)
        self.assertNotIn("SECRET implementer reasoning", prompt)


if __name__ == "__main__":
    unittest.main()
