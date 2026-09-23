"""Issue #213: adversarial UAT must emit stable Overview-panel boundary logs.

The Now Working panel replays worker stdout. Independent assessment, each
fix/re-test round, a completed fix, and its re-test have to log the issue
number and round/max with the `Adversarial UAT for issue #...` prefix so the
UI can show live progress. Selection for a new run must keep the
`Selected {provider} model {model} with effort {effort} for this run.` form.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import adversarial_uat as uat  # noqa: E402
import test_swarm_issue_worker as fixtures  # noqa: E402
from ai_execution_history import ExecutionHistoryService  # noqa: E402
from swarm_issue_worker import IssueContext, ProviderChoice, ProviderUsage  # noqa: E402


class NowWorkingBoundaryLogTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def prepare(self, *, fixed=False):
        self.worker.config = dataclasses.replace(
            self.worker.config,
            adversarial_uat_enabled=True,
            auto_approve=False,
            auto_promote=False,
            ai_execution_history_enabled=True,
            execution_history_db=self.state / "history.sqlite3",
        )
        self.worker.history = ExecutionHistoryService(
            True, self.state / "history.sqlite3"
        )
        self.worker.issue = IssueContext(
            180,
            "Require fixed output",
            "tracked.txt must contain fixed, including on error paths.",
            [],
            "https://example.invalid/issues/180",
        )
        self.worker.choice = ProviderChoice(
            "Claude", "fixer-model", "high", "implementer-session"
        )
        self.git("switch", "-c", "ai/claude/issue-180")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()
        (self.repo / "app.py").write_text("# Python application\n")
        (self.repo / "tracked.txt").write_text("fixed\n" if fixed else "broken\n")
        self.git("add", ".")
        self.git("commit", "-qm", "[claude] Implementation (#180)")
        self.worker.initialize_adversarial(
            self.git("rev-parse", "HEAD"), "## Summary\nImplementation"
        )
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
        definition["suites"] = [
            s for s in definition["suites"] if s.get("id") != "adversarial-180"
        ] + [{
            "id": "adversarial-180",
            "name": "Issue acceptance",
            "origin": "adversarial",
            "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests/adversarial"],
            "timeoutSeconds": 20,
        }]
        (self.repo / uat.DEFINITION).write_text(json.dumps(definition))

    def role(self, prompt, activity=""):
        loop = self.worker.read_state()["adversarial"]
        self.calls.append((loop["phase"], self.worker.choice.name))
        if not self.worker.choice.session_id:
            self.worker.choice.session_id = f"session-{len(self.calls)}"
        self.worker.update_state(
            session_id=self.worker.choice.session_id, session_started=True
        )
        if loop["phase"] == "test":
            if not (self.repo / "tests/adversarial/test_issue.py").exists():
                self.add_tests()
            self.worker.ai_output_file.write_text(
                uat.RESULT_MARKER + ' {"out_of_scope": [], "dispute_resolution": ""}'
            )
        else:
            (self.repo / "tracked.txt").write_text("fixed\n")
            self.worker.ai_output_file.write_text("Fixed based on failing boundary test.")
        return 0

    def patches(self, role=None):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(
            self.worker, "provider_usage", return_value=ProviderUsage(0, 80)
        ))
        stack.enter_context(mock.patch.object(self.worker, "ensure_bot_auth"))
        stack.enter_context(mock.patch.object(self.worker, "comments", return_value=[]))
        stack.enter_context(mock.patch.object(self.worker, "run_ai", side_effect=role or self.role))
        stack.enter_context(mock.patch.object(self.worker.github, "gh", side_effect=self.gh))
        stack.enter_context(mock.patch.object(
            self.worker, "minor_bump_requested_by_trusted_user", return_value=False
        ))
        return stack

    def captured_delivery(self, role=None):
        with self.patches(role), contextlib.redirect_stdout(io.StringIO()) as output:
            status = self.worker.run_adversarial_delivery()
        return status, output.getvalue()

    def assert_log(self, output: str, message: str) -> None:
        self.assertIn(
            message,
            output,
            f"worker stdout missing Overview boundary log {message!r}:\n{output}",
        )

    def test_selection_log_keeps_provider_model_and_effort_in_one_line(self) -> None:
        source = (ISSUE_WORKER_DIR / "swarm_issue_worker.py").read_text(encoding="utf-8")
        self.assertIn(
            'log(f"Selected {self.choice.name} model {self.choice.model} '
            'with effort {self.choice.effort} for this run.")',
            source,
            "Now Working parses this exact selection line for provider/model/effort",
        )

    def test_independent_then_fix_then_retest_emit_round_boundary_logs(self) -> None:
        self.prepare(fixed=False)
        status, output = self.captured_delivery()
        self.assertEqual(status, 10)
        independent = (
            "Adversarial UAT for issue #180: starting independent test run "
            f"(round 0 of {uat.MAX_ROUNDS})."
        )
        fix_start = (
            "Adversarial UAT for issue #180: starting fix/re-test round "
            f"1 of {uat.MAX_ROUNDS}."
        )
        fix_applied = (
            f"Adversarial UAT for issue #180: fix applied in round 1 of {uat.MAX_ROUNDS}."
        )
        retest = (
            "Adversarial UAT for issue #180: starting re-test for round "
            f"1 of {uat.MAX_ROUNDS}."
        )
        self.assert_log(output, independent)
        self.assert_log(output, fix_start)
        self.assert_log(output, fix_applied)
        self.assert_log(output, retest)
        self.assertLess(output.index(independent), output.index(fix_start))
        self.assertLess(output.index(fix_start), output.index(fix_applied))
        self.assertLess(output.index(fix_applied), output.index(retest))
        self.assertNotIn(
            f"starting fix/re-test round 2 of {uat.MAX_ROUNDS}.",
            output,
        )

    def test_clean_first_pass_logs_independent_assessment_only(self) -> None:
        self.prepare(fixed=True)
        with mock.patch.object(self.worker, "finalize_issue"):
            status, output = self.captured_delivery()
        self.assertEqual(status, 10)
        self.assert_log(
            output,
            "Adversarial UAT for issue #180: starting independent test run "
            f"(round 0 of {uat.MAX_ROUNDS}).",
        )
        self.assertNotIn("starting fix/re-test round", output)
        self.assertNotIn("fix applied in round", output)
        self.assertNotIn("starting re-test for round", output)

    def test_cap_hit_logs_the_sixth_counted_round(self) -> None:
        self.prepare(fixed=False)

        def never_fix(prompt, activity=""):
            status = self.role(prompt)
            (self.repo / "tracked.txt").write_text("broken\n")
            return status

        status, output = self.captured_delivery(never_fix)
        self.assertEqual(status, 10)
        for round_number in range(1, uat.MAX_ROUNDS + 1):
            self.assert_log(
                output,
                "Adversarial UAT for issue #180: starting fix/re-test round "
                f"{round_number} of {uat.MAX_ROUNDS}.",
            )
            self.assert_log(
                output,
                "Adversarial UAT for issue #180: fix applied in round "
                f"{round_number} of {uat.MAX_ROUNDS}.",
            )
            self.assert_log(
                output,
                "Adversarial UAT for issue #180: starting re-test for round "
                f"{round_number} of {uat.MAX_ROUNDS}.",
            )
        self.assertEqual(uat.MAX_ROUNDS, 6)
        self.assertIsNone(
            re.search(
                r"starting fix/re-test round 7 of ",
                output,
            )
        )
