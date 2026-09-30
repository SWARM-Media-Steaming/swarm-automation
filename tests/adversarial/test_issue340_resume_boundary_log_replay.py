"""Issue #340 acceptance: a same-session quota resume must re-announce its stage.

Oracle (from the issue and adversarial-uat-testing.md, before reading the diff):
the Overview rebuilds its adversarial row from the tail of the worker log, so
after a quota pause the worker must emit the stable `Adversarial UAT for issue
#N: starting ...` boundary log and the `tester|fixer <Provider> model <m> with
effort <e>.` attribution log again once it resumes the same session, else a
rotated log tail loses the row. A normal (non-resume) run must not double-log.
Only providers and GitHub are faked; git and suite processes are real.
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
from ai_execution_history import ExecutionHistoryService  # noqa: E402
from swarm_issue_worker import IssueContext, ProviderChoice, ProviderUsage, Worker  # noqa: E402

START = "Adversarial UAT for issue #340: starting"
ATTRIB = "Adversarial UAT for issue #340: tester "


class ResumeBoundaryLogReplayTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def prepare(self) -> None:
        self.worker.config = dataclasses.replace(
            self.worker.config, adversarial_uat_enabled=True,
            ai_execution_history_enabled=True, execution_history_db=self.state / "history.sqlite3",
        )
        self.worker.history = ExecutionHistoryService(True, self.state / "history.sqlite3")
        self.worker.issue = IssueContext(340, "Keep Overview row", "Boundary logs must survive resume.",
                                         [], "https://example.invalid/issues/340")
        self.worker.choice = ProviderChoice("Claude", "impl-model", "high", "implementer-session")
        self.git("switch", "-c", "ai/claude/issue-340")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()
        (self.repo / "tracked.txt").write_text("fixed\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "[claude] Implementation (#340)")
        self.worker.initialize_adversarial(self.git("rev-parse", "HEAD"), "## Summary\nImplementation")

    def add_suite(self) -> None:
        d = self.repo / "tests/adversarial"
        d.mkdir(parents=True, exist_ok=True)
        (d / "test_issue340_fixture.py").write_text(
            "import unittest\nfrom pathlib import Path\n\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertEqual(Path('tracked.txt').read_text().strip(), 'fixed')\n",
            encoding="utf-8")
        definition = uat.read_definition(self.repo)
        definition["suites"] = [x for x in definition["suites"] if x.get("id") != "adversarial-340-fixture"]
        definition["suites"].append({
            "id": "adversarial-340-fixture", "name": "fixture", "origin": "adversarial",
            "enabled": True, "disruptive": False,
            "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests/adversarial",
                        "-p", "test_issue340_fixture.py"],
            "timeoutSeconds": 20})
        (self.repo / uat.DEFINITION).write_text(json.dumps(definition), encoding="utf-8")

    def role(self, prompt: str, activity: str = "") -> int:
        self.add_suite()
        if not self.worker.choice.session_id:
            self.worker.choice.session_id = "tester-session"
        self.worker.update_state(session_id=self.worker.choice.session_id, session_started=True)
        self.worker.ai_output_file.write_text(
            uat.RESULT_MARKER + ' {"dispute_resolution": "", "out_of_scope": []}', encoding="utf-8")
        return 0

    def gh(self, args, provider=None, body=None):
        if args[:2] in (["pr", "list"], ["issue", "list"]):
            return "[]"
        if args[:2] == ["pr", "create"]:
            return "https://example.invalid/pull/340"
        return ""

    def patches(self, role=None):
        s = contextlib.ExitStack()
        s.enter_context(mock.patch.object(self.worker, "provider_usage", return_value=ProviderUsage(0, 80)))
        s.enter_context(mock.patch.object(self.worker, "ensure_bot_auth"))
        s.enter_context(mock.patch.object(self.worker, "comments", return_value=[]))
        s.enter_context(mock.patch.object(self.worker, "run_ai", side_effect=role or self.role))
        s.enter_context(mock.patch.object(self.worker.github, "gh", side_effect=self.gh))
        s.enter_context(mock.patch.object(self.worker, "minor_bump_requested_by_trusted_user", return_value=False))
        return s

    def test_fresh_run_logs_each_boundary_exactly_once(self) -> None:
        self.prepare()
        out = io.StringIO()
        with self.patches(), contextlib.redirect_stdout(out), \
                mock.patch.object(self.worker, "finalize_issue"):
            self.worker.run_adversarial_delivery()
        lines = out.getvalue().splitlines()
        self.assertEqual(sum(START in l for l in lines), 1, out.getvalue())
        self.assertEqual(sum(ATTRIB in l for l in lines), 1, out.getvalue())

    def test_quota_resume_replays_round_and_attribution_logs_once(self) -> None:
        self.prepare()

        def quota(prompt, activity=""):
            self.role(prompt)
            self.worker.ai_output_file.write_text("usage limit")
            return 1
        with self.patches(quota), mock.patch.object(self.worker, "ai_failure_is_quota", return_value=True):
            self.assertEqual(self.worker.run_adversarial_delivery(), 11)
        config, issue = self.worker.config, self.worker.issue
        paused = self.worker.choice
        self.worker = Worker(config)
        out = io.StringIO()
        with self.patches(), contextlib.redirect_stdout(out), \
                mock.patch.object(self.worker, "issue_is_closed", return_value=False), \
                mock.patch.object(self.worker, "finalize_issue"):
            self.worker.prepare_paused_resume()
            self.worker.issue = issue
            self.worker.run_selected_issue()
        lines = out.getvalue().splitlines()
        starts = [l for l in lines if START in l]
        attribs = [l for l in lines if ATTRIB in l]
        self.assertEqual(len(starts), 1, out.getvalue())
        self.assertEqual(len(attribs), 1, out.getvalue())
        self.assertIn("independent", starts[0])
        self.assertIn("round 0 of 3", starts[0])
        self.assertIn(f"tester {paused.name} model {paused.model or '<unconfigured>'} with effort "
                      f"{paused.effort or '<unconfigured>'}.", attribs[0])
        # The round announcement must precede the attribution it qualifies.
        self.assertLess(out.getvalue().index(START), out.getvalue().index(ATTRIB))

    def test_finished_stage_replays_nothing(self) -> None:
        self.prepare()
        with self.patches(), mock.patch.object(self.worker, "finalize_issue"):
            self.worker.run_adversarial_delivery()
        out = io.StringIO()
        stage = self.worker.adversarial_stages()[0]
        with self.patches(), contextlib.redirect_stdout(out):
            self.worker.run_adversarial_stage(stage)
        self.assertNotIn(START, out.getvalue())
        self.assertNotIn(ATTRIB, out.getvalue())


if __name__ == "__main__":
    unittest.main()
