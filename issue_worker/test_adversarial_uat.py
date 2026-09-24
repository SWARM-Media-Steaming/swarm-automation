"""UAT of real checkouts, executable adversarial suites and durable worker phases.

Only coding providers and GitHub's API are faked; git delivery uses a local bare
remote and tests execute real child processes, including failures and timeouts.
"""
import contextlib
import dataclasses
import io
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

import ai_test_assist
import adversarial_uat as uat
import test_swarm_issue_worker as fixtures
from ai_execution_history import ExecutionHistoryRepository, ExecutionHistoryService, ExecutionStart
from dynamic_router import COMPLEXITY_SCALE_TOP, FRONTIER_COMPLEXITY_FLOOR
from swarm_issue_worker import Worker, ProviderChoice, ProviderUsage, IssueContext, WorkerError, iso_timestamp


class AdversarialUatTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def prepare(self, *, fixed=False, auto=False, history=True):
        self.worker.config = dataclasses.replace(
            self.worker.config, adversarial_uat_enabled=True, auto_approve=auto,
            auto_promote=auto, ai_execution_history_enabled=history, execution_history_db=self.state / "history.sqlite3",
        )
        self.worker.history = ExecutionHistoryService(history, self.state / "history.sqlite3")
        self.worker.issue = IssueContext(180, "Require fixed output", "tracked.txt must contain fixed, including on error paths.", [], "https://example.invalid/issues/180")
        self.worker.choice = ProviderChoice("Claude", "fixer-model", "high", "implementer-session")
        self.git("switch", "-c", "ai/claude/issue-180")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()
        (self.repo / "app.py").write_text("# Python application\n")
        (self.repo / "tracked.txt").write_text("fixed\n" if fixed else "broken\n")
        self.git("add", ".")
        self.git("commit", "-qm", "[claude] Implementation (#180)")
        self.worker.initialize_adversarial(self.git("rev-parse", "HEAD"), "## Summary\nSECRET implementer reasoning\n## Changes\nImplementation")
        self.calls = []
        self.api = []
        self.comments_posted = []

    def gh(self, args, provider=None, body=None):
        self.api.append(args)
        if args[:2] in (["pr", "list"], ["issue", "list"]):
            return "[]"
        if args[:2] == ["pr", "create"]:
            return "https://example.invalid/pull/181"
        if args[:2] == ["issue", "create"]:
            return "https://example.invalid/issues/182"
        if args[:2] == ["issue", "comment"]:
            self.comments_posted.append(body)
        return ""

    def add_tests(self, expected="fixed"):
        directory = self.repo / "tests/adversarial"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "test_issue.py").write_text(
            "import unittest\nfrom pathlib import Path\n"
            "class Acceptance(unittest.TestCase):\n"
            "    def test_requirement(self):\n"
            f"        self.assertEqual(Path('tracked.txt').read_text().strip(), {expected!r})\n"
        )
        definition = uat.read_definition(self.repo)
        definition["suites"] = [s for s in definition["suites"] if s.get("id") != "adversarial-180"] + [{
            "id": "adversarial-180", "name": "Issue acceptance", "origin": "adversarial",
            "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests/adversarial"],
            "timeoutSeconds": 20,
        }]
        (self.repo / uat.DEFINITION).write_text(json.dumps(definition))

    def role(self, prompt, activity=""):
        loop = self.worker.read_state()["adversarial"]
        self.calls.append((loop["phase"], self.worker.choice.name, self.worker.choice.session_id, self.worker.choice.resume, prompt, activity))
        self.assertNotIn("SECRET implementer reasoning", prompt)
        self.assertNotIn("--resume", prompt)
        # Mimic the CLI's session capture to exercise persistence.
        if not self.worker.choice.session_id:
            self.worker.choice.session_id = f"session-{len(self.calls)}"
        self.worker.update_state(session_id=self.worker.choice.session_id, session_started=True)
        if loop["phase"] == "test":
            if not (self.repo / "tests/adversarial/test_issue.py").exists():
                self.add_tests()
            output = uat.RESULT_MARKER + ' {"out_of_scope": [], "dispute_resolution": ""}'
        else:
            (self.repo / "tracked.txt").write_text("fixed\n")
            output = "Fixed based on failing boundary test."
        self.worker.ai_output_file.write_text(output)
        return 0

    def patches(self, role=None):
        import contextlib
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(self.worker, "provider_usage", return_value=ProviderUsage(0, 80)))
        stack.enter_context(mock.patch.object(self.worker, "ensure_bot_auth"))
        stack.enter_context(mock.patch.object(self.worker, "comments", return_value=[]))
        stack.enter_context(mock.patch.object(self.worker, "run_ai", side_effect=role or self.role))
        stack.enter_context(mock.patch.object(self.worker.github, "gh", side_effect=self.gh))
        stack.enter_context(mock.patch.object(self.worker, "minor_bump_requested_by_trusted_user", return_value=False))
        return stack

    def test_uat_real_failure_fix_retest_delivers_once_and_records_history(self):
        self.prepare()
        with self.patches(), mock.patch.object(self.worker, "push_ref", wraps=self.worker.push_ref) as push:
            self.assertEqual(self.worker.run_adversarial_delivery(), 10)
        self.assertEqual([c[0] for c in self.calls], ["test", "fix", "test"])
        self.assertEqual(self.calls[0][1], "Codex")
        self.assertNotEqual(self.calls[1][1], self.calls[2][1])
        self.assertTrue(all(not c[3] for c in self.calls))
        # Same-issue, cross-provider phase handoffs must be distinguishable in
        # the log, not just three identical "is working" lines.
        self.assertEqual(
            [c[5] for c in self.calls],
            [
                "running independent adversarial UAT",
                "fixing adversarial round 1 findings",
                "re-testing after adversarial fix round 1",
            ],
        )
        push.assert_called_once()
        self.assertEqual(sum(a[:2] == ["pr", "create"] for a in self.api), 1)
        self.assertEqual(len(self.comments_posted), 1)
        self.assertIn("resolved after 1 rounds, 1 test files added", self.comments_posted[0])
        row = self.worker.history.repository.for_repository(self.worker.config.github_repository)[0]
        self.assertEqual(row["adversarial_round_count"], 1)
        self.assertEqual(row["adversarial_outcome"], "resolved_after_n")
        rounds = self.worker.history.repository.adversarial_rounds_for([row["execution_id"]])[row["execution_id"]]
        self.assertEqual([r["tests_failing_after"] for r in rounds], [1, 0])
        self.assertEqual(self.git("rev-parse", "refs/remotes/origin/ai/claude/issue-180"), self.git("rev-parse", "HEAD"))

    def test_cap_publishes_failing_tests_but_never_approves_merges_or_cleans_branch(self):
        self.prepare(auto=True)
        def never_fix(prompt, activity=""):
            status = self.role(prompt)
            (self.repo / "tracked.txt").write_text("broken\n")
            return status
        with self.patches(never_fix), mock.patch.object(self.worker, "approve_pull_request") as approve, mock.patch.object(self.worker, "merge_pull_request") as merge, mock.patch.object(self.worker, "auto_promote_integration_branch") as promote, mock.patch.object(self.worker, "push_ref", wraps=self.worker.push_ref) as push, mock.patch.object(self.worker, "cleanup_no_code_branch") as cleanup:
            self.assertEqual(self.worker.run_adversarial_delivery(), 10)
        self.assertEqual([c[0] for c in self.calls].count("fix"), 6)
        self.assertEqual([c[0] for c in self.calls].count("test"), 7)
        push.assert_called_once()
        approve.assert_not_called(); merge.assert_not_called(); promote.assert_not_called(); cleanup.assert_not_called()
        self.assertEqual(self.git("branch", "--show-current"), "ai/claude/issue-180")
        self.assertEqual(len(self.comments_posted), 1)
        self.assertIn("Adversarial-test deadlock", self.comments_posted[0])
        self.assertIn("https://example.invalid/pull/181", self.comments_posted[0])
        self.assertTrue(any("AI Needs Input" in args for args in self.api))
        row = self.worker.history.repository.for_repository(self.worker.config.github_repository)[0]
        self.assertEqual((row["adversarial_round_count"], row["adversarial_outcome"], row["final_status"]), (6, "cap_hit", "awaiting_input"))
        self.assertEqual(row["pull_request_url"], "https://example.invalid/pull/181")

    def test_first_pass_same_provider_is_a_fresh_context_and_history_can_be_off(self):
        self.prepare(fixed=True, history=False)
        self.worker.config = dataclasses.replace(self.worker.config, providers=tuple(dataclasses.replace(s, enabled=s.key == "claude") for s in self.worker.config.providers))
        with self.patches(), mock.patch.object(self.worker, "finalize_issue"):
            self.worker.run_adversarial_delivery()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], "Claude")
        self.assertNotEqual(self.calls[0][2], "implementer-session")
        self.assertFalse(self.calls[0][3])
        self.assertEqual(self.worker.read_state()["adversarial"]["outcome"], "clean_first_pass")
        self.assertFalse((self.state / "history.sqlite3").exists())

    def test_fixer_cannot_edit_or_retire_tests_fresh_tester_adjudicates_dispute(self):
        self.prepare()
        def disputed(prompt, activity=""):
            loop = self.worker.read_state()["adversarial"]
            if loop["phase"] == "fix":
                self.calls.append(("fix", self.worker.choice.name, self.worker.choice.session_id, False, prompt))
                (self.repo / "tests/adversarial/test_issue.py").unlink()
                definition = uat.read_definition(self.repo); definition["suites"] = []
                (self.repo / uat.DEFINITION).write_text(json.dumps(definition))
                self.worker.ai_output_file.write_text("SWARM_TEST_DISPUTE: The test expects wrong; the spec says fixed.")
                (self.repo / "tracked.txt").write_text("fixed\n")
            else:
                self.role(prompt)
                if loop["round"] == 0:
                    self.add_tests("wrong")
                else:
                    self.assertIn("worker restored", prompt)
                    self.assertIn("'wrong'", (self.repo / "tests/adversarial/test_issue.py").read_text())
                    self.add_tests("fixed")
                    self.worker.ai_output_file.write_text(uat.RESULT_MARKER + ' {"dispute_resolution":"revised to spec: fixed", "out_of_scope":[]}')
            return 0
        with self.patches(disputed), mock.patch.object(self.worker, "finalize_issue"):
            self.worker.run_adversarial_delivery()
        loop = self.worker.read_state()["adversarial"]
        self.assertEqual(loop["outcome"], "resolved_after_n")
        self.assertTrue(loop["rounds"][-1]["disputed"])
        self.assertEqual(loop["rounds"][-1]["tests_modified"], 1)
        self.assertFalse(self.calls[-1][3])
        self.assertNotEqual(self.worker.choice.session_id, "session-1")

    def test_tester_may_not_fix_product_code(self):
        self.prepare()
        def illicit(prompt, activity=""):
            self.role(prompt)
            (self.repo / "tracked.txt").write_text("fixed\n")
            return 0
        with self.patches(illicit), mock.patch.object(self.worker, "push_ref") as push:
            with self.assertRaisesRegex(WorkerError, "Tester changed product files"):
                self.worker.run_adversarial_delivery()
        push.assert_not_called()

    def test_out_of_scope_finding_files_assigned_labelled_issue_once_without_blocking(self):
        self.prepare(fixed=True)
        finding = {"title": "Separate parser bug", "body": "Reproduction: malformed sibling endpoint crashes; unrelated to tracked.txt spec."}
        def external(prompt, activity=""):
            self.role(prompt)
            self.worker.ai_output_file.write_text(uat.RESULT_MARKER + json.dumps({"out_of_scope": [finding]}))
            return 0
        with self.patches(external), mock.patch.object(self.worker, "finalize_issue"), contextlib.redirect_stdout(io.StringIO()) as output:
            self.worker.run_adversarial_delivery()
            self.worker.file_adversarial_findings(self.worker.read_state()["adversarial"], [finding])
        creates = [a for a in self.api if a[:2] == ["issue", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertIn("--assignee", creates[0]); self.assertIn("adversarial-uat", creates[0])
        self.assertEqual(self.worker.read_state()["adversarial"]["outcome"], "clean_first_pass")
        self.assertIn("Filed out-of-scope adversarial UAT finding for #180: https://example.invalid/issues/182", output.getvalue())
        self.assertIn("already filed for #180: Separate parser bug", output.getvalue())
        row = self.worker.history.repository.for_repository(self.worker.config.github_repository)[0]
        self.assertEqual(json.loads(row["adversarial_filed_findings"]), [{
            "title": "Separate parser bug", "url": "https://example.invalid/issues/182",
        }])

    def test_malformed_gh_create_output_is_not_logged_as_a_successful_filing(self):
        self.prepare()
        finding = {"title": "Separate parser bug", "body": "Reproduction details."}
        loop = self.worker.read_state()["adversarial"]
        def gh(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "create"]:
                return "Warning: could not add label to issue\n(no url returned)"
            return ""
        with mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.worker.file_adversarial_findings(loop, [finding])
        log_output = output.getvalue()
        self.assertNotIn("Filed out-of-scope adversarial UAT finding for #180", log_output)
        self.assertIn("GitHub did not return an issue URL", log_output)
        details = self.worker.read_state()["adversarial"]["filed_finding_details"]
        self.assertEqual(details[0]["url"], "")
        row = self.worker.history.repository.for_repository(self.worker.config.github_repository)[0]
        self.assertEqual(json.loads(row["adversarial_filed_findings"]), [{
            "title": "Separate parser bug", "url": "",
        }])

    def test_github_failure_while_filing_a_finding_does_not_abort_delivery(self):
        # Issue #217: a GitHub error (rate limit, 403, network) raised from
        # inside file_adversarial_findings must not escape and abort the
        # round that called it — out-of-scope findings are specified not to
        # block delivery of the issue under test.
        self.prepare()
        finding = {"title": "Separate parser bug", "body": "Reproduction details."}
        loop = self.worker.read_state()["adversarial"]

        def gh(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                raise WorkerError("gh: HTTP 403: API rate limit exceeded")
            return ""

        with mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.worker.file_adversarial_findings(loop, [finding])

        log_output = output.getvalue()
        self.assertIn("Could not file out-of-scope adversarial UAT finding for #180", log_output)
        self.assertIn("rate limit", log_output)
        # Left un-filed so a later retry can still succeed, and never marked
        # filed without an actual GitHub issue behind it.
        self.assertEqual(self.worker.read_state()["adversarial"]["filed_findings"], [])
        self.assertEqual(self.worker.read_state()["adversarial"]["filed_finding_details"], [])

    def test_github_failure_while_filing_a_finding_lets_the_round_complete(self):
        self.prepare(fixed=True)
        finding = {"title": "Separate parser bug", "body": "Reproduction details, unrelated to tracked.txt."}

        def external(prompt, activity=""):
            self.role(prompt)
            self.worker.ai_output_file.write_text(uat.RESULT_MARKER + json.dumps({"out_of_scope": [finding]}))
            return 0

        def gh(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                raise WorkerError("gh: HTTP 403: API rate limit exceeded")
            if args[:2] in (["pr", "list"],):
                return "[]"
            if args[:2] == ["pr", "create"]:
                return "https://example.invalid/pull/181"
            if args[:2] == ["issue", "comment"]:
                self.comments_posted.append(body)
            return ""

        with mock.patch.object(self.worker, "run_ai", side_effect=external), \
                mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                mock.patch.object(self.worker, "ensure_bot_auth"), \
                mock.patch.object(self.worker, "comments", return_value=[]), \
                mock.patch.object(self.worker, "provider_usage", return_value=ProviderUsage(0, 80)), \
                mock.patch.object(self.worker, "minor_bump_requested_by_trusted_user", return_value=False), \
                mock.patch.object(self.worker, "finalize_issue"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            exit_code = self.worker.run_adversarial_delivery()

        self.assertEqual(exit_code, 10)
        self.assertIn("Could not file out-of-scope adversarial UAT finding for #180", output.getvalue())
        self.assertEqual(self.worker.read_state()["adversarial"]["outcome"], "clean_first_pass")

    def test_out_of_scope_finding_may_cite_a_pre_existing_non_adversarial_suite(self):
        # The tester prompt tells testers that when an existing suite fails
        # for an unrelated reason, they must retain it and "report its ID
        # in that finding's suite_ids array". known_suites used to only
        # recognize origin=='adversarial' IDs, so a tester following that
        # instruction to the letter (citing a real, pre-existing suite) was
        # rejected with "named unknown adversarial suites" — the 2026-09-23
        # production incident on issue #360, reproduced here.
        self.prepare(fixed=True)
        (self.repo / ".swarm").mkdir(exist_ok=True)
        (self.repo / uat.DEFINITION).write_text(json.dumps({
            "version": 1,
            "suites": [{"id": "roku-catalog-grouping", "name": "Roku grouping",
                        "command": [sys.executable, "-c", "pass"], "enabled": True}],
        }))
        finding = {
            "title": "Roku FirstEpisode() can select a season-0 special",
            "body": "reproduction evidence, orthogonal to this issue",
            "suite_ids": ["roku-catalog-grouping"],
        }
        def independent_tester(prompt, activity=""):
            self.role(prompt)
            self.worker.ai_output_file.write_text(
                uat.RESULT_MARKER + json.dumps({"dispute_resolution": "", "out_of_scope": [finding]})
            )
            return 0
        with self.patches(independent_tester), mock.patch.object(self.worker, "finalize_issue"):
            self.worker.run_adversarial_delivery()
        self.assertEqual(self.worker.read_state()["adversarial"]["outcome"], "clean_first_pass")

    def test_out_of_scope_finding_may_not_exclude_every_adversarial_suite(self):
        # In production, Claude's out-of-scope finding on issue #360 cited
        # BOTH a pre-existing suite and the one adversarial suite it had just
        # registered for this same issue — which happened to be the only
        # adversarial suite that round. Accepting that citation excludes the
        # only suite covering this issue from every future run_suites call —
        # run_suites then fails closed with a synthetic "no enabled
        # adversarial suite was registered" result every round, burning all
        # 6 rounds on an unfixable, manufactured failure before stalling on
        # "AI Needs Input". A citation that would leave zero adversarial
        # suites runnable must instead be rejected immediately so a fresh
        # tester gets a chance to do it correctly (see the sibling test for
        # the case that must still be allowed: a *new* suite registered
        # purely to record an unrelated regression, alongside others that
        # still cover this issue).
        self.prepare(fixed=True)
        finding = {
            "title": "Self-citation",
            "body": "cites its own new suite",
            "suite_ids": ["adversarial-180"],
        }
        def self_citing_tester(prompt, activity=""):
            self.role(prompt)
            self.worker.ai_output_file.write_text(
                uat.RESULT_MARKER + json.dumps({"dispute_resolution": "", "out_of_scope": [finding]})
            )
            return 0
        with self.patches(self_citing_tester):
            with self.assertRaisesRegex(WorkerError, "exclude every adversarial suite"):
                self.worker.run_adversarial_delivery()
        # Rejected as invalid, not silently accepted into a doomed loop.
        self.assertEqual(self.worker.read_state()["adversarial"]["outcome"], "")
        self.assertEqual(self.worker.read_state()["adversarial"]["round"], 0)

    def test_quota_resume_preserves_phase_session_history_and_skips_implementer(self):
        self.prepare(fixed=True)
        execution_id = self.worker.history.execution_id
        def quota(prompt, activity=""):
            self.role(prompt)
            self.worker.ai_output_file.write_text("usage limit")
            return 1
        with self.patches(quota), mock.patch.object(self.worker, "ai_failure_is_quota", return_value=True):
            self.assertEqual(self.worker.run_adversarial_delivery(), 11)
        self.assertTrue((self.worker.paused_dir / "180.json").is_file())
        saved = json.loads((self.worker.paused_dir / "180.json").read_text())
        session_id = saved["session_id"]
        config = self.worker.config
        original_issue = self.worker.issue
        self.worker = Worker(config)
        with self.patches(), mock.patch.object(self.worker, "issue_is_closed", return_value=False), mock.patch.object(self.worker, "finalize_issue"):
            self.worker.prepare_paused_resume()
            self.worker.issue = original_issue
            self.assertEqual(self.worker.run_selected_issue(), 10)
        self.assertEqual(self.worker.history.execution_id, execution_id)
        self.assertEqual(self.calls[-1][0], "test")
        self.assertEqual(self.calls[-1][2], session_id)
        self.assertTrue(self.calls[-1][3])
        self.assertEqual(self.worker.read_state()["adversarial"]["round"], 0)
        self.assertEqual(len(self.worker.history.repository.for_repository(config.github_repository)), 1)

    def test_bootstrap_is_no_write_manifest_first_and_reuses_persisted_choice(self):
        (self.repo / "app.py").write_text("print('app')")
        before = self.git("status", "--porcelain")
        with mock.patch.object(ai_test_assist, "generate") as generate:
            plan = ai_test_assist.bootstrap(str(self.repo))
        self.assertEqual(plan["framework"], "unittest")
        generate.assert_not_called()
        self.assertEqual(self.git("status", "--porcelain"), before)
        (self.repo / ".swarm").mkdir()
        (self.repo / uat.DEFINITION).write_text(json.dumps({"suites": [], "adversarialBootstrap": plan}))
        (self.repo / "Cargo.toml").write_text("[package]\nname='new-stack'")
        self.assertEqual(ai_test_assist.bootstrap(str(self.repo))["framework"], "unittest")
        (self.repo / uat.DEFINITION).unlink()
        with mock.patch.object(ai_test_assist, "generate") as generate:
            discovered = ai_test_assist.discover(str(self.repo), "codex", "", "", 1)
        generate.assert_not_called()
        self.assertEqual(discovered["suites"][0]["command"], ["cargo", "test"])

    def test_adversarial_activity_names_phase_and_round(self):
        self.assertEqual(uat.adversarial_activity({"phase": "test", "round": 0}), "running independent adversarial UAT")
        self.assertEqual(uat.adversarial_activity({"phase": "fix", "round": 1}), "fixing adversarial round 1 findings")
        self.assertEqual(uat.adversarial_activity({"phase": "test", "round": 1}), "re-testing after adversarial fix round 1")
        self.assertEqual(uat.adversarial_activity({"phase": "fix", "round": 3}), "fixing adversarial round 3 findings")

    def test_delivery_succeeds_when_swarm_dir_is_locally_excluded(self):
        # The desktop app keeps its own .swarm/ scratch drafts out of `git
        # status` by excluding the directory in the managed checkout's local
        # .git/info/exclude (never the repo's own .gitignore). A plain `git
        # add` refuses an ignored, previously-untracked path, so both call
        # sites that stage .swarm/tests.json as a real repository artifact
        # (bootstrap, and after every test round) must force it, or the
        # worker crash-loops every retry — the 2026-09-22/23 overnight
        # incident, reproduced here by excluding .swarm/ before delivery.
        self.prepare(fixed=True, history=False)
        (self.repo / ".git/info/exclude").write_text(".swarm/\n")
        self.worker.config = dataclasses.replace(self.worker.config, providers=tuple(dataclasses.replace(s, enabled=s.key == "claude") for s in self.worker.config.providers))
        with self.patches(), mock.patch.object(self.worker, "finalize_issue"):
            self.worker.run_adversarial_delivery()
        self.assertEqual(self.worker.read_state()["adversarial"]["outcome"], "clean_first_pass")
        self.assertIn(uat.DEFINITION, self.git("ls-files").splitlines())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_suite_runner_fails_closed_for_disabled_missing_command_and_timeout(self):
        base = {"id": "adversarial-fail", "command": [sys.executable, "-c", "raise SystemExit(3)"]}
        self.assertEqual(uat.run_suites(self.repo, [base])[0]["exit_code"], 3)
        self.assertNotEqual(uat.run_suites(self.repo, [{**base, "enabled": False}])[0]["exit_code"], 0)
        self.assertNotEqual(uat.run_suites(self.repo, [{**base, "command": []}])[0]["exit_code"], 0)
        self.assertEqual(uat.run_suites(self.repo, [{**base, "command": [sys.executable, "-c", "import time; time.sleep(10)"], "timeoutSeconds": 1}])[0]["exit_code"], 124)
        self.assertNotEqual(uat.run_suites(self.repo, [])[0]["exit_code"], 0)

    def test_toggle_replaces_same_session_instruction_but_question_stays_read_only(self):
        self.prepare()
        self.worker.config = dataclasses.replace(self.worker.config, require_issue_tests=True)
        with mock.patch.object(self.worker, "ensure_issue_images", return_value=[]):
            prompt = self.worker.build_prompt(False, "", False)
        self.assertNotIn("Also add or update UAT and integration tests", prompt)
        self.assertIn("independent adversarial tester", prompt)
        self.worker.issue.labels = ["Question"]
        with mock.patch.object(self.worker, "ensure_issue_images", return_value=[]):
            prompt = self.worker.build_prompt(False, "", False)
        self.assertNotIn("independent adversarial tester", prompt)
        self.assertIn("read-only", prompt)

    def test_history_migration_four_is_additive_idempotent_and_rounds_cascade(self):
        self.prepare()
        repository = self.worker.history.repository
        execution = self.worker.history.execution_id
        repository.update(execution, iso_timestamp(), adversarial_round_count=3, adversarial_outcome="resolved_after_n", capacity_consumed_percent=4.5,
                          adversarial_filed_findings=[{"title": "Separate bug", "url": "https://example.invalid/issues/182"}])
        repository.record_adversarial_round(execution, {"round_number": 1, "tester_provider": "Codex", "tests_added": 2})
        repository.record_adversarial_round(execution, {"round_number": 1, "tester_provider": "Grok", "tests_added": 3})
        repository.migrate()
        rows = repository.adversarial_rounds_for([execution])[execution]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tester_provider"], "Grok")
        self.assertEqual(repository.adversarial_summary(self.worker.config.github_repository)["averageRounds"], 3)
        with repository.connect() as database:
            self.assertEqual({r[0] for r in database.execute("SELECT version FROM schema_migrations")}, {1, 2, 3, 4})
            database.execute("DELETE FROM ai_executions WHERE execution_id = ?", (execution,))
            self.assertEqual(database.execute("SELECT COUNT(*) FROM adversarial_rounds").fetchone()[0], 0)

    def test_initial_worker_integration_runs_uat_before_any_delivery(self):
        # Exercise the initial run_selected_issue entry point, rather than
        # directly initializing the loop as the other focused scenarios do.
        self.prepare(fixed=True)
        state = self.worker.read_state()
        state.pop("adversarial")
        self.worker.write_state(state)
        def implement_then_test(prompt, activity=""):
            if not self.worker.read_state().get("adversarial"):
                (self.repo / "tracked.txt").write_text("fixed\n")
                (self.repo / "app.py").write_text("# completed implementation\n")
                self.worker.ai_output_file.write_text("## Summary\nSECRET implementer reasoning")
                return 0
            return self.role(prompt)
        with self.patches(implement_then_test), mock.patch.object(self.worker, "push_ref", wraps=self.worker.push_ref) as push:
            self.assertEqual(self.worker.run_selected_issue(), 10)
        self.assertEqual([c[0] for c in self.calls], ["test"])
        self.assertEqual(len(self.comments_posted), 2)
        self.assertIn("started working", self.comments_posted[0])
        self.assertIn("clean first pass", self.comments_posted[1])
        push.assert_called_once()

    def test_round_sort_applies_before_paging_and_aggregates_exclude_disabled(self):
        self.prepare()
        repo = self.worker.history.repository
        repo.update(self.worker.history.execution_id, iso_timestamp(), adversarial_outcome="disabled")
        for number in range(12):
            identity = repo.create(ExecutionStart(
                repository=self.worker.config.github_repository, issue_number=number, issue_url="", issue_title=f"Case {number}",
                issue_body="", provider="Claude", model="test", effort="high", branch_name="test", application_version="test",
            ), f"2026-09-22T00:00:{number:02d}+00:00")
            repo.update(identity, iso_timestamp(), adversarial_round_count=number % 7,
                        adversarial_outcome="clean_first_pass" if number % 7 == 0 else "cap_hit" if number % 7 == 6 else "resolved_after_n")
        rows, total, _, _ = repo.page_for_repository(self.worker.config.github_repository, sort="rounds_desc", limit=2)
        self.assertEqual(total, 13)
        self.assertEqual([r["adversarial_round_count"] for r in rows], [6, 5])
        summary = repo.adversarial_summary(self.worker.config.github_repository)
        self.assertEqual(summary["loops"], 12)
        self.assertEqual(summary["cleanFirstPassPercent"], 16.7)
        self.assertEqual(summary["capHitPercent"], 8.3)

    def test_migration_upgrades_populated_v2_without_changing_existing_execution(self):
        self.prepare()
        repo = self.worker.history.repository
        execution = self.worker.history.execution_id
        with repo.connect() as db:
            db.execute("DROP TABLE adversarial_rounds")
            for column in ("adversarial_filed_findings", "adversarial_round_count", "adversarial_outcome", "capacity_consumed_percent"):
                db.execute(f"ALTER TABLE ai_executions DROP COLUMN {column}")
            db.execute("DELETE FROM schema_migrations WHERE version IN (3, 4)")
        upgraded = ExecutionHistoryRepository(repo.database_path)
        row = upgraded.for_repository(self.worker.config.github_repository)[0]
        self.assertEqual(row["execution_id"], execution)
        self.assertEqual(row["adversarial_round_count"], 0)
        self.assertEqual(row["adversarial_outcome"], "")
        self.assertIsNone(row["capacity_consumed_percent"])
        self.assertEqual(row["adversarial_filed_findings"], "[]")
        self.assertEqual(upgraded.adversarial_summary(self.worker.config.github_repository)["loops"], 0)

    def test_preflight_grade_is_recorded_when_routing_is_off(self):
        self.prepare()
        self.worker.config = dataclasses.replace(self.worker.config, dynamic_model_routing=False)
        self.worker.choice = ProviderChoice("Grok", "grok-4.6", "medium", "uat-session")
        payload = json.dumps({
            "task_type": "debugging",
            "complexity": 4,
            "risk": "low",
            "context_requirement": "small",
            "selected_provider": "grok",
            "provider_reason": "Grok is already on this issue.",
            "selected_model": "grok-4.6",
            "reasoning_effort": "high",
            "confidence": 0.8,
            "prompt_grade": "B",
            "grade_reason": "Clear enough to grade.",
            "complexity_reason": "Small scripted change.",
        })
        with mock.patch("swarm_issue_worker.run_provider_router", return_value=payload):
            self.worker.maybe_apply_dynamic_routing()
        self.assertEqual(self.worker.choice.name, "Grok")
        self.assertEqual(self.worker.choice.model, "grok-4.6")
        self.assertEqual(self.worker.choice.effort, "medium")
        self.assertEqual(self.worker.routing["model_source"], "configured")
        self.assertEqual(self.worker.routing["prompt_grade"], "B")
        self.assertEqual(self.worker.routing["router_suggested_model"], "grok-4.6")
        self.assertEqual(self.worker.routing["router_suggested_effort"], "high")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        with mock.patch.object(self.worker, "comments", return_value=[]), mock.patch.object(
            self.worker.github, "gh", return_value=""
        ) as github:
            self.worker.post_started_comment()
        notice = github.call_args.args[2]
        self.assertIn("Set in SWARM Automation", notice)
        self.assertIn("Router recommendation: Grok 4.6 at High reasoning", notice)
        self.assertNotIn("Dynamic Model Routing applied", notice)

    def test_cost_consideration_reaches_scored_routing_on_an_invalid_model_name(self):
        self.prepare()
        self.worker.config = dataclasses.replace(
            self.worker.config, dynamic_model_routing=True, routing_optimization="cost"
        )
        payload = json.dumps({
            "task_type": "debugging",
            "complexity": 1,
            "risk": "low",
            "context_requirement": "small",
            "selected_provider": "claude",
            "provider_reason": "Small documentation change.",
            "selected_model": "not-a-real-model",
            "reasoning_effort": "low",
            "confidence": 0.8,
            "prompt_grade": "B",
            "grade_reason": "Clear enough to grade.",
            "complexity_reason": "One-line documentation edit.",
        })
        with mock.patch("swarm_issue_worker.run_provider_router", return_value=payload):
            self.worker.maybe_apply_dynamic_routing()
        self.assertEqual(self.worker.routing["model_source"], "tier")
        self.assertTrue(self.worker.routing["cost_consideration_enabled"])
        self.assertEqual(self.worker.routing["routing_optimization"], "cost")
        self.assertEqual(self.worker.choice.model, "claude-haiku-4-5")
        self.assertEqual(self.worker.choice.effort, "low")

    def test_best_fit_routing_handoff_uses_the_scored_grok_fallback(self):
        """A new cross-provider handoff uses the reusable scorer's Grok pick."""
        self.prepare()
        self.worker.config = dataclasses.replace(
            self.worker.config, dynamic_model_routing=True, routing_optimization="best"
        )
        # A saved attempt is intentionally pinned to its existing provider.
        # Remove it to exercise a new-attempt handoff, which is the contract
        # this UAT covers.
        self.worker.in_progress_file.unlink()
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-luna", "medium", "uat-session")
        self.worker.provider_usages = {
            "Claude": ProviderUsage(0, 80.0, "week 80% remaining"),
            "Codex": ProviderUsage(0, 90.0, "week 90% remaining"),
            "Grok": ProviderUsage(0, 70.0, "week 70% remaining"),
        }
        self.worker.provider_priority = ("Codex", "Claude", "Grok")
        payload = json.dumps({
            "task_type": "debugging",
            "complexity": 7,
            "risk": "medium",
            "context_requirement": "large",
            "selected_provider": "grok",
            "provider_reason": "Grok is best suited to this debugging task.",
            # This model belongs to the router provider, not Grok. The retry
            # path must use the reusable scorer for the selected provider.
            "selected_model": "gpt-5.6-luna",
            "reasoning_effort": "low",
            "confidence": 0.8,
            "prompt_grade": "B",
            "grade_reason": "Clear enough to grade.",
            "complexity_reason": "Requires a multi-file debugging pass.",
        })
        with mock.patch("swarm_issue_worker.run_provider_router", return_value=payload):
            self.worker.maybe_apply_dynamic_routing()
        self.assertEqual(self.worker.choice.name, "Grok")
        self.assertEqual(self.worker.choice.model, "grok-4.7")
        self.assertEqual(self.worker.choice.effort, "xhigh")
        self.assertEqual(self.worker.routing["model_source"], "tier")
        self.assertFalse(self.worker.routing["cost_consideration_enabled"])

    def test_cost_routing_prompt_holds_frontier_models_to_the_complexity_floor(self):
        self.prepare()
        self.worker.config = dataclasses.replace(
            self.worker.config, dynamic_model_routing=True, routing_optimization="cost"
        )
        loop = self.worker.read_state()["adversarial"]
        with self.patches(), mock.patch.object(
            self.worker, "run_router", return_value="router response"
        ) as router, mock.patch.object(
            self.worker,
            "resolve_router_response",
            return_value={"provider": "grok", "selected_model": "grok-4.6", "reasoning_effort": "high"},
        ):
            self.worker.choose_adversarial_provider(loop)
        prompt = router.call_args.args[1]
        self.assertIn("Routing preference: optimize for cost.", prompt)
        self.assertIn(f"Frontier complexity floor: {FRONTIER_COMPLEXITY_FLOOR}", prompt)
        self.assertIn(f"Complexity scale top: {COMPLEXITY_SCALE_TOP}", prompt)
        self.assertIn("A frontier model is a last resort.", prompt)
        self.assertIn("High risk is not a license to pick a frontier model below the floor.", prompt)
        self.assertNotIn("escalate to a stronger", prompt)

    def test_router_is_not_pinned_by_the_existing_issue_branch(self):
        self.prepare()
        self.worker.config = dataclasses.replace(self.worker.config, dynamic_model_routing=True)
        loop = self.worker.read_state()["adversarial"]
        with self.patches(), mock.patch.object(self.worker, "run_router", return_value="router response"), mock.patch.object(self.worker, "resolve_router_response", return_value={"provider": "grok", "selected_model": "test-grok", "reasoning_effort": "high"}) as resolve:
            choice = self.worker.choose_adversarial_provider(loop)
        self.assertEqual(choice.name, "Grok")
        self.assertEqual(choice.model, "test-grok")
        self.assertFalse(choice.resume)
        self.assertEqual({c.key for c in resolve.call_args.kwargs["candidates"]}, {"codex", "grok"})

    def test_zero_test_success_is_not_a_clean_uat_pass(self):
        suite = {"id": "adversarial-empty", "command": [sys.executable, "-c", "print('Ran 0 tests in 0.001s')"]}
        result = uat.run_suites(self.repo, [suite])[0]
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("No tests were collected", result["output"])

    def test_unrelated_failing_suite_remains_scheduled_but_does_not_block(self):
        self.prepare(fixed=True)
        def tester(prompt, activity=""):
            self.role(prompt)
            (self.repo / "tests/adversarial/unrelated.py").write_text("raise SystemExit(2)\n")
            definition = uat.read_definition(self.repo)
            definition["suites"].append({"id": "adversarial-unrelated", "origin": "adversarial", "name": "Prior regression",
                                         "command": [sys.executable, "tests/adversarial/unrelated.py"]})
            (self.repo / uat.DEFINITION).write_text(json.dumps(definition))
            self.worker.ai_output_file.write_text(uat.RESULT_MARKER + json.dumps({"out_of_scope": [{
                "title": "Unrelated failure", "body": "Evidence: sibling endpoint failure predates the requested tracked.txt behavior.",
                "suite_ids": ["adversarial-unrelated"],
            }]}))
            return 0
        with self.patches(tester), mock.patch.object(self.worker, "finalize_issue"):
            self.worker.run_adversarial_delivery()
        loop = self.worker.read_state()["adversarial"]
        self.assertEqual(loop["outcome"], "clean_first_pass")
        unrelated = [s for s in uat.read_definition(self.repo)["suites"] if s["id"] == "adversarial-unrelated"]
        self.assertEqual(uat.run_suites(self.repo, unrelated)[0]["exit_code"], 2)
        self.assertTrue(any(args[:2] == ["issue", "create"] for args in self.api))

    def test_cap_delivery_receipt_prevents_second_push_when_terminal_comment_retries(self):
        self.prepare()
        def never_fix(prompt, activity=""):
            result = self.role(prompt)
            (self.repo / "tracked.txt").write_text("broken\n")
            return result
        with self.patches(never_fix), mock.patch.object(self.worker, "deliver_pull_request", return_value=("https://example.invalid/pull/181", "ai/claude/issue-180", self.git("rev-parse", "HEAD"))) as delivery, mock.patch.object(self.worker, "finalize_needs_input", side_effect=[WorkerError("temporary GitHub comment failure"), None]):
            with self.assertRaisesRegex(WorkerError, "temporary GitHub"):
                self.worker.run_adversarial_delivery()
            calls = len(self.calls)
            self.worker.run_adversarial_delivery()
        self.assertEqual(len(self.calls), calls)
        delivery.assert_called_once()

    def test_unknown_stack_bootstrap_uses_portable_harness_without_waiting_for_access(self):
        with mock.patch.object(ai_test_assist, "generate", return_value={"ok": False, "error": "unsupported provider"}):
            plan = ai_test_assist.bootstrap(str(self.repo), "codex", "", "", primary_language="unknown")
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["framework"], "unittest")
        self.assertIn("black-box", plan["instructions"])
        self.assertFalse((self.repo / uat.DEFINITION).exists())

    def test_future_pr_reconciliation_cannot_auto_merge_a_cap_hit(self):
        self.prepare(auto=True)
        payload = [{"url": "https://example.invalid/pull/181", "state": "OPEN", "headRefName": "ai/claude/issue-180",
                    "headRefOid": self.git("rev-parse", "HEAD"), "isDraft": False, "mergeable": "MERGEABLE",
                    "reviewDecision": "APPROVED", "body": uat.CAP_HIT_PR_NOTICE + "Implementation"}]
        with mock.patch.object(self.worker.github, "gh", return_value=json.dumps(payload)), mock.patch.object(self.worker, "approve_pull_request") as approve, mock.patch.object(self.worker, "merge_pull_request") as merge:
            self.worker.reconcile_issue_pull_requests()
        approve.assert_not_called(); merge.assert_not_called()

    def test_reused_cap_hit_pr_is_held_before_push_and_only_clean_uat_releases_it(self):
        self.prepare()
        existing = {"url": "https://example.invalid/pull/181", "state": "OPEN", "body": "Keep reviewer notes"}
        events = []
        def gh(args, provider=None, body=None):
            if args[:2] == ["pr", "list"]:
                return json.dumps([existing])
            if args[:2] == ["pr", "edit"]:
                events.append("hold" if uat.CAP_HIT_PR_MARKER in body else "release")
                existing["body"] = body
            return ""
        def push(*args, **kwargs):
            import subprocess
            events.append("push")
            return subprocess.CompletedProcess([], 0, "", "")
        head = self.git("rev-parse", "HEAD")
        with mock.patch.object(self.worker.github, "gh", side_effect=gh), mock.patch.object(self.worker, "push_ref", side_effect=push):
            self.worker.deliver_pull_request(head, allow_automation=False)
            self.assertEqual(events, ["hold", "push"])
            self.assertIn("Keep reviewer notes", existing["body"])
            with self.assertRaisesRegex(WorkerError, "requires a passing UAT"):
                self.worker.deliver_pull_request(head)
            loop = self.worker.read_state()["adversarial"]
            loop["outcome"] = "resolved_after_n"
            self.worker.save_adversarial(loop)
            self.worker.deliver_pull_request(head)
        self.assertEqual(existing["body"], "Keep reviewer notes")
        self.assertEqual(events[-2:], ["push", "release"])

    def test_malformed_report_retries_fresh_instead_of_replaying_invalid_checkpoint(self):
        self.prepare(fixed=True)
        def malformed(prompt, activity=""):
            self.role(prompt)
            self.worker.ai_output_file.write_text("The tests should pass.")
            return 0
        with self.patches(malformed):
            with self.assertRaisesRegex(WorkerError, "exactly one SWARM_ADVERSARIAL_RESULT"):
                self.worker.run_adversarial_delivery()
        saved = self.worker.read_state()["adversarial"]
        self.assertIsNone(saved["response"])
        self.assertFalse(saved["active"])
        self.assertEqual(saved["retry_stage_base"], saved["stage_base"])
        self.assertIn("exactly one SWARM_ADVERSARIAL_RESULT", saved["retry_rejection"]["reason"])
        self.assertIn(uat.DEFINITION, saved["retry_rejection"]["paths"])
        self.assertFalse((self.repo / "tests/adversarial/test_issue.py").exists())
        self.assertEqual(uat.read_definition(self.repo)["suites"], [])
        rejected_patch = self.state / "last-rejected-adversarial.patch"
        self.assertTrue(rejected_patch.exists())
        self.assertIn("test_issue.py", rejected_patch.read_text())
        with self.patches(), mock.patch.object(self.worker, "finalize_issue"):
            self.worker.run_adversarial_delivery()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(self.calls[-1][3])
        self.assertIn("A prior tester result was rejected and its edits were rolled back", self.calls[-1][4])
        self.assertNotIn("retry_rejection", self.worker.read_state()["adversarial"])
        self.assertEqual(self.worker.read_state()["adversarial"]["outcome"], "clean_first_pass")


class ResultPayloadTests(unittest.TestCase):
    """A pure function, tested directly against formatting a tester actually
    produced in production (2026-09) rather than only the hand-written happy
    path: a well-formed, double-quoted result whose JSON is pretty-printed
    across multiple lines instead of packed onto the marker's own line, which
    used to be silently truncated to its opening brace and rejected as a
    JSON-decode error even though nothing was wrong with the tester's report.
    """

    def test_single_line_json_on_the_marker_line(self):
        output = "All suites passed.\n" + uat.RESULT_MARKER + ' {"dispute_resolution":"","out_of_scope":[]}\n'
        self.assertEqual(uat.result_payload(output), {"dispute_resolution": "", "out_of_scope": []})

    def test_pretty_printed_json_spanning_multiple_lines(self):
        output = (
            "All suites passed.\n"
            + uat.RESULT_MARKER + " {\n"
            '  "dispute_resolution": "",\n'
            '  "out_of_scope": []\n'
            "}\n"
        )
        self.assertEqual(uat.result_payload(output), {"dispute_resolution": "", "out_of_scope": []})

    def test_trailing_markdown_fence_after_the_json(self):
        output = "```\n" + uat.RESULT_MARKER + ' {"dispute_resolution":"","out_of_scope":[]}\n```\n'
        self.assertEqual(uat.result_payload(output), {"dispute_resolution": "", "out_of_scope": []})

    def test_indented_marker_line(self):
        output = "  " + uat.RESULT_MARKER + ' {"dispute_resolution":"","out_of_scope":[]}'
        self.assertEqual(uat.result_payload(output), {"dispute_resolution": "", "out_of_scope": []})

    def test_no_marker_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly one SWARM_ADVERSARIAL_RESULT"):
            uat.result_payload("The tests should pass.")

    def test_two_marker_lines_are_rejected(self):
        output = uat.RESULT_MARKER + " {}\n" + uat.RESULT_MARKER + " {}\n"
        with self.assertRaisesRegex(ValueError, "exactly one SWARM_ADVERSARIAL_RESULT"):
            uat.result_payload(output)

    def test_genuinely_invalid_json_still_raises(self):
        output = uat.RESULT_MARKER + " {'dispute_resolution': ''}\n"
        with self.assertRaises(ValueError):
            uat.result_payload(output)
