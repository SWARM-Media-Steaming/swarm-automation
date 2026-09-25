"""Issue #239 acceptance: malformed successful ``gh --json`` output is retryable.

The out-of-scope-finding path is deliberately best-effort.  A proxy, wrapper,
or partially written GitHub response can exit successfully while returning an
HTML error page rather than the JSON array requested by ``gh issue list
--json``.  That transport/protocol failure must neither stop delivery of the
unrelated issue under test nor mark the finding filed, so a later round can
retry it.  The same invariant applies while recovering a previously-created
finding whose first create response did not contain a URL.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
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
from ai_execution_history import ExecutionHistoryService  # noqa: E402
from swarm_issue_worker import IssueContext, ProviderChoice, ProviderUsage  # noqa: E402


class ExitZeroMalformedIssueListResilienceTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def prepare(self, *, fixed: bool) -> None:
        self.worker.config = dataclasses.replace(
            self.worker.config,
            adversarial_uat_enabled=True,
            ai_execution_history_enabled=True,
            execution_history_db=self.state / "history.sqlite3",
        )
        self.worker.history = ExecutionHistoryService(True, self.state / "history.sqlite3")
        self.worker.issue = IssueContext(
            180, "Require fixed output", "tracked.txt must contain fixed.", [],
            "https://example.invalid/issues/180",
        )
        self.worker.choice = ProviderChoice("Claude", "fixer-model", "high", "tester-session")
        self.git("switch", "-c", "ai/claude/issue-180")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()
        (self.repo / "tracked.txt").write_text("fixed\n" if fixed else "broken\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "[claude] Implementation (#180)")
        self.worker.initialize_adversarial(
            self.git("rev-parse", "HEAD"), "## Summary\nImplementation"
        )

    def add_passing_acceptance_test(self) -> None:
        tests = self.repo / "tests/adversarial"
        tests.mkdir(parents=True, exist_ok=True)
        (tests / "test_issue_239_fixture.py").write_text(
            "import unittest\nfrom pathlib import Path\n\n"
            "class Acceptance(unittest.TestCase):\n"
            "    def test_requirement(self):\n"
            "        self.assertEqual(Path('tracked.txt').read_text().strip(), 'fixed')\n"
        )
        definition = uat.read_definition(self.repo)
        definition["suites"] = [suite for suite in definition["suites"] if suite.get("id") != "adversarial-239-fixture"] + [{
            "id": "adversarial-239-fixture",
            "name": "Issue #239 delivery fixture",
            "origin": "adversarial",
            "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests/adversarial", "-p", "test_issue_239_fixture.py"],
            "timeoutSeconds": 20,
        }]
        (self.repo / uat.DEFINITION).write_text(json.dumps(definition))

    def test_html_issue_list_response_does_not_abort_a_clean_delivery(self):
        self.prepare(fixed=True)
        finding = {"title": "Independent parser defect", "body": "Unrelated reproduction."}

        def run_tester(prompt, activity=""):
            self.add_passing_acceptance_test()
            self.worker.ai_output_file.write_text(
                uat.RESULT_MARKER + json.dumps({"out_of_scope": [finding]})
            )
            return 0

        def gh(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return "<html><title>upstream timeout</title></html>"
            if args[:2] == ["pr", "list"]:
                return "[]"
            if args[:2] == ["pr", "create"]:
                return "https://example.invalid/pull/181"
            if args[:2] == ["issue", "create"]:
                raise AssertionError("a failed dedup lookup must not create an un-deduplicated issue")
            return ""

        with mock.patch.object(self.worker, "run_ai", side_effect=run_tester), \
                mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                mock.patch.object(self.worker, "ensure_bot_auth"), \
                mock.patch.object(self.worker, "comments", return_value=[]), \
                mock.patch.object(self.worker, "provider_usage", return_value=ProviderUsage(0, 80)), \
                mock.patch.object(self.worker, "minor_bump_requested_by_trusted_user", return_value=False), \
                mock.patch.object(self.worker, "finalize_issue"), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            exit_code = self.worker.run_adversarial_delivery()

        self.assertEqual(exit_code, 10)
        self.assertEqual(self.worker.read_state()["adversarial"]["outcome"], "clean_first_pass")
        self.assertEqual(self.worker.read_state()["adversarial"]["filed_findings"], [])
        self.assertIn("Could not file out-of-scope adversarial UAT finding for #180", captured.getvalue())
        self.assertIn("malformed JSON", captured.getvalue())

    def test_malformed_recovery_lookup_keeps_blank_url_retryable_until_valid_json_arrives(self):
        self.prepare(fixed=True)
        finding = {"title": "Independent parser defect", "body": "Unrelated reproduction."}
        loop = self.worker.read_state()["adversarial"]
        digest = hashlib.sha256(json.dumps(finding, sort_keys=True).encode()).hexdigest()[:20]
        marker = f"<!-- swarm-issue-worker:adversarial-finding:issue:180;id:{digest} -->"
        loop["filed_findings"] = [marker]
        loop["filed_finding_details"] = [{"marker": marker, "title": finding["title"], "url": ""}]
        self.worker.save_adversarial(loop)

        with mock.patch.object(self.worker.github, "gh", return_value="gateway returned HTML"), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            self.worker.file_adversarial_findings(loop, [finding])

        details = self.worker.read_state()["adversarial"]["filed_finding_details"]
        self.assertEqual(details, [{"marker": marker, "title": finding["title"], "url": ""}])
        self.assertIn("malformed JSON", captured.getvalue())

        def valid_gh(args, provider=None, body=None):
            self.assertEqual(args[:2], ["issue", "list"])
            return json.dumps([{"body": marker, "url": "https://example.invalid/issues/239"}])

        with mock.patch.object(self.worker.github, "gh", side_effect=valid_gh):
            self.worker.file_adversarial_findings(loop, [finding])

        self.assertEqual(
            self.worker.read_state()["adversarial"]["filed_finding_details"][0]["url"],
            "https://example.invalid/issues/239",
        )


if __name__ == "__main__":
    unittest.main()
