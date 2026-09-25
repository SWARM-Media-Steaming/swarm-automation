"""Issue #210: out-of-scope UAT findings must be logged distinctly and remain
queryable from the execution-history path the Execution History panel uses.

Existing unit coverage checks the happy-path create + local-marker retry.
This suite covers the other filing/dedup/history edges the issue requires:
GitHub-search dedup (the retry after create-but-before-local-checkpoint),
gh create stdout that warns then prints the URL on the last line, multiple
findings in one report, history still logged when the SQLite store is off,
and the paged CLI the desktop actually queries.
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

import test_swarm_issue_worker as fixtures  # noqa: E402
from ai_execution_history import (  # noqa: E402
    ExecutionHistoryService,
    main as execution_history_main,
)
from swarm_issue_worker import IssueContext, ProviderChoice  # noqa: E402


class FiledFindingSurfaceTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def prepare(self, *, history=True):
        self.worker.config = dataclasses.replace(
            self.worker.config,
            adversarial_uat_enabled=True,
            ai_execution_history_enabled=history,
            execution_history_db=self.state / "history.sqlite3",
        )
        self.worker.history = ExecutionHistoryService(
            history, self.state / "history.sqlite3"
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
        self.worker.initialize_adversarial(
            self.git("rev-parse", "HEAD"), "## Summary\nImplementation"
        )

    def finding(self, title="Separate parser bug", body="Reproduction details."):
        return {"title": title, "body": body}

    def test_github_search_dedup_logs_existing_url_and_does_not_create_again(self):
        self.prepare()
        loop = self.worker.read_state()["adversarial"]
        finding = self.finding()
        digest = hashlib.sha256(json.dumps(finding, sort_keys=True).encode()).hexdigest()[:20]
        marker = f"<!-- swarm-issue-worker:adversarial-finding:issue:180;id:{digest} -->"

        def gh_real(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return json.dumps([{
                    "body": f"{marker}\nFound while testing #180; outside that delivery's scope.",
                    "url": "https://example.invalid/issues/182",
                }])
            if args[:2] == ["issue", "create"]:
                self.fail("GitHub-search dedup must not create a second issue")
            return ""

        with mock.patch.object(self.worker.github, "gh", side_effect=gh_real), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            self.worker.file_adversarial_findings(loop, [finding])

        log_output = captured.getvalue()
        self.assertNotIn("Filed out-of-scope adversarial UAT finding for #180", log_output)
        self.assertIn(
            "already filed for #180: https://example.invalid/issues/182",
            log_output,
        )
        self.assertEqual(
            self.worker.read_state()["adversarial"]["filed_finding_details"][0]["url"],
            "https://example.invalid/issues/182",
        )
        row = self.worker.history.repository.for_repository(
            self.worker.config.github_repository
        )[0]
        self.assertEqual(
            json.loads(row["adversarial_filed_findings"]),
            [{"title": "Separate parser bug", "url": "https://example.invalid/issues/182"}],
        )

    def test_gh_create_warning_then_url_is_logged_as_success(self):
        # monitor_repository_actions / github_issue_url_from_output take the
        # *last* stdout line. A label-creation warning followed by the issue
        # URL is a successful filing, not a missing-URL failure.
        self.prepare()
        loop = self.worker.read_state()["adversarial"]
        create_stdout = (
            "Warning: created label adversarial-uat\n"
            "https://example.invalid/issues/182"
        )

        def gh(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "create"]:
                return create_stdout
            return ""

        with mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            self.worker.file_adversarial_findings(loop, [self.finding()])

        log_output = captured.getvalue()
        self.assertIn(
            "Filed out-of-scope adversarial UAT finding for #180: "
            "https://example.invalid/issues/182",
            log_output,
        )
        self.assertNotIn("GitHub did not return an issue URL", log_output)

    def test_multiple_findings_are_each_logged_and_all_persisted(self):
        self.prepare()
        loop = self.worker.read_state()["adversarial"]
        findings = [
            self.finding("First unrelated crash", "repro 1"),
            self.finding("Second unrelated leak", "repro 2"),
        ]
        created = []

        def gh(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "create"]:
                number = 182 + len(created)
                created.append(args[args.index("--title") + 1])
                return f"https://example.invalid/issues/{number}"
            return ""

        with mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            self.worker.file_adversarial_findings(loop, findings)

        log_output = captured.getvalue()
        self.assertIn("https://example.invalid/issues/182", log_output)
        self.assertIn("https://example.invalid/issues/183", log_output)
        stored = json.loads(
            self.worker.history.repository.for_repository(
                self.worker.config.github_repository
            )[0]["adversarial_filed_findings"]
        )
        self.assertEqual(
            stored,
            [
                {"title": "First unrelated crash", "url": "https://example.invalid/issues/182"},
                {"title": "Second unrelated leak", "url": "https://example.invalid/issues/183"},
            ],
        )

    def test_history_disabled_still_logs_and_does_not_raise(self):
        self.prepare(history=False)
        loop = self.worker.read_state()["adversarial"]

        def gh(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "create"]:
                return "https://example.invalid/issues/182"
            return ""

        with mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            self.worker.file_adversarial_findings(loop, [self.finding()])

        self.assertIn(
            "Filed out-of-scope adversarial UAT finding for #180: "
            "https://example.invalid/issues/182",
            captured.getvalue(),
        )
        self.assertIsNone(self.worker.history.repository)

    def test_paged_history_cli_returns_decoded_findings_for_the_desktop(self):
        self.prepare()
        loop = self.worker.read_state()["adversarial"]

        def gh(args, provider=None, body=None):
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "create"]:
                return "https://example.invalid/issues/182"
            return ""

        with mock.patch.object(self.worker.github, "gh", side_effect=gh), \
                contextlib.redirect_stdout(io.StringIO()):
            self.worker.file_adversarial_findings(loop, [self.finding()])

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
        self.assertIn("records", page)
        self.assertEqual(
            page["records"][0]["adversarial_filed_findings"],
            [{"title": "Separate parser bug", "url": "https://example.invalid/issues/182"}],
        )

if __name__ == "__main__":
    unittest.main()
