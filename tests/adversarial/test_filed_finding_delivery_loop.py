"""Issue #210: filing an out-of-scope finding during a real adversarial
delivery round must leave a user-visible receipt after that round finishes.

Isolated calls to `file_adversarial_findings` are not enough. The delivery
loop writes adversarial outcome/capacity onto the same execution-history row
after filing (`adversarial_uat.py` round completion). Those later updates
must keep the filed issue's title and URL, because that row is what the
Execution History panel reads. An empty `out_of_scope` list must not pretend
a finding was filed.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
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
from ai_execution_history import (  # noqa: E402
    ExecutionHistoryService,
    main as execution_history_main,
)
from swarm_issue_worker import IssueContext, ProviderChoice  # noqa: E402


class FiledFindingDeliveryLoopTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def prepare(self, *, fixed=True):
        self.worker.config = dataclasses.replace(
            self.worker.config,
            adversarial_uat_enabled=True,
            ai_execution_history_enabled=True,
            execution_history_db=self.state / "history.sqlite3",
        )
        self.worker.history = ExecutionHistoryService(
            True, self.state / "history.sqlite3"
        )
        self.worker.issue = IssueContext(
            180,
            "Require fixed output",
            "tracked.txt must contain fixed.",
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
        from swarm_issue_worker import ProviderUsage
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

    def history_row(self):
        return self.worker.history.repository.for_repository(
            self.worker.config.github_repository
        )[0]

    def test_delivery_logs_the_new_issue_url_and_keeps_it_after_outcome_is_written(self):
        self.prepare(fixed=True)
        finding = {
            "title": "Separate parser bug",
            "body": "Reproduction: malformed sibling endpoint crashes; unrelated to tracked.txt.",
        }

        def tester(prompt, activity=""):
            self.role(prompt)
            self.worker.ai_output_file.write_text(
                uat.RESULT_MARKER + json.dumps({"out_of_scope": [finding], "dispute_resolution": ""})
            )
            return 0

        with self.patches(tester), mock.patch.object(self.worker, "finalize_issue"), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            exit_code = self.worker.run_adversarial_delivery()

        self.assertEqual(exit_code, 10)
        log_output = captured.getvalue()
        self.assertIn(
            "Filed out-of-scope adversarial UAT finding for #180: "
            "https://example.invalid/issues/182",
            log_output,
        )
        self.assertNotIn("already filed for #180", log_output)
        creates = [args for args in self.api if args[:2] == ["issue", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertIn("adversarial-uat", creates[0])
        self.assertIn("--assignee", creates[0])

        loop = self.worker.read_state()["adversarial"]
        self.assertEqual(loop["outcome"], "clean_first_pass")
        row = self.history_row()
        self.assertEqual(row["adversarial_outcome"], "clean_first_pass")
        self.assertEqual(
            json.loads(row["adversarial_filed_findings"]),
            [{"title": "Separate parser bug", "url": "https://example.invalid/issues/182"}],
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = execution_history_main([
                "--db", str(self.worker.config.execution_history_db),
                "--repository", self.worker.config.github_repository,
                "--limit", "10",
                "--offset", "0",
            ])
        self.assertEqual(code, 0)
        page = json.loads(buf.getvalue())
        record = page["records"][0]
        self.assertEqual(record["adversarial_outcome"], "clean_first_pass")
        self.assertEqual(
            record["adversarial_filed_findings"],
            [{"title": "Separate parser bug", "url": "https://example.invalid/issues/182"}],
        )

    def test_clean_pass_without_findings_does_not_log_or_persist_a_filing(self):
        self.prepare(fixed=True)
        with self.patches(), mock.patch.object(self.worker, "finalize_issue"), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            self.worker.run_adversarial_delivery()

        log_output = captured.getvalue()
        self.assertNotIn("out-of-scope adversarial UAT finding", log_output.lower())
        self.assertNotIn("Filed out-of-scope", log_output)
        row = self.history_row()
        self.assertEqual(json.loads(row["adversarial_filed_findings"]), [])
        self.assertEqual(row["adversarial_outcome"], "clean_first_pass")


if __name__ == "__main__":
    unittest.main()
