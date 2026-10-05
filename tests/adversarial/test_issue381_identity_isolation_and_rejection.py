"""Issue #381: execution-identity isolation, rejected assessments, failed runs.

Oracle from the issue, derived before treating the implementation as correct:

* Sessions are tracked by issue, provider, model, agent role and execution
  context. A different GitHub repository, checkout path, or base commit is a
  different execution context even when the session UUID is copied.
* Never reuse a session across unrelated GitHub issues merely to improve
  cache utilization. Transplanted ``cli_sessions`` metadata must not resume.
* Independent adversarial assessments stay independent. A rejected tester
  report starts a fresh assessment; resuming the rejected conversation would
  keep the invalid approach in native CLI context.
* A failed execution that is not a quota pause starts a fresh session.
  Quota pauses still preserve the session.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ISSUE_WORKER_DIR = Path(__file__).resolve().parents[2] / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

from swarm_issue_worker import Config, IssueContext, ProviderChoice, Worker, build_parser  # noqa: E402

SPEC_MARKER = "UNIQUE-SPEC-MARKER-381-ISO-4d91"


class IsolationFixture(unittest.TestCase):
    provider = "Claude"
    model = "claude-sonnet-5"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "ai/claude/issue-381")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "source.py").write_text("value = 1\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        config = Config.from_args(build_parser().parse_args([
            "--repo-dir", str(self.repo), "--state-dir", str(self.root / "state"),
            "--github-repository", "acme/project", "--no-dynamic-model-routing",
            "--no-require-bot-auth", "--github-apps-config", str(self.root / "none.json"),
        ]))
        self.worker = Worker(config)
        self.worker.issue = IssueContext(
            381, "Intelligent Prompt Caching", f"Required behaviour {SPEC_MARKER}",
            ["enhancement"], "https://example.invalid/381",
        )
        self.worker.choice = ProviderChoice(
            self.provider, self.model, "high", str(uuid.uuid4()))
        self.worker.save_new_state(
            self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))

    def git(self, *args: str, repo: Path | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(repo or self.repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def confirm_session(self) -> str:
        self.worker.prepare_cli_session()
        self.worker.update_state(session_started=True)
        self.worker.remember_cli_session(True)
        return self.worker.choice.session_id

    def next_turn(self, resume: bool = True) -> bool:
        self.worker.choice.resume = resume
        self.worker.prepare_cli_session()
        return self.worker.choice.resume


class ExecutionIdentityTests(IsolationFixture):
    def test_a_different_github_repository_does_not_reuse_the_session(self) -> None:
        old = self.confirm_session()
        self.worker.config = dataclasses.replace(
            self.worker.config, github_repository="other/repo")
        self.assertFalse(self.next_turn())
        self.assertNotEqual(self.worker.choice.session_id, old)

    def test_a_different_checkout_path_does_not_reuse_the_session(self) -> None:
        old = self.confirm_session()
        other = self.root / "other-checkout"
        other.mkdir()
        self.git("init", "-q", "-b", "ai/claude/issue-381", repo=other)
        self.git("config", "user.email", "test@example.invalid", repo=other)
        self.git("config", "user.name", "Test", repo=other)
        (other / "source.py").write_text("value = 1\n", encoding="utf-8")
        self.git("add", ".", repo=other)
        self.git("commit", "-qm", "initial", repo=other)
        self.worker.config = dataclasses.replace(self.worker.config, repo_dir=other)
        self.assertFalse(self.next_turn())
        self.assertNotEqual(self.worker.choice.session_id, old)

    def test_a_changed_base_commit_does_not_reuse_the_session(self) -> None:
        old = self.confirm_session()
        state = self.worker.read_state()
        state["base_sha"] = "0" * 40
        self.worker.write_state(state)
        self.assertFalse(self.next_turn())
        self.assertNotEqual(self.worker.choice.session_id, old)

    def test_transplanted_session_metadata_cannot_follow_a_different_issue(self) -> None:
        old = self.confirm_session()
        sessions = json.loads(json.dumps(self.worker.read_state()["cli_sessions"]))
        self.worker.issue = IssueContext(
            999, "Unrelated", "A different specification", ["bug"],
            "https://example.invalid/999",
        )
        self.worker.choice = ProviderChoice(
            self.provider, self.model, "high", old)
        self.worker.choice.resume = True
        state = self.worker.read_state()
        state["cli_sessions"] = sessions
        self.worker.write_state(state)
        rebuilt = self.worker.prepare_cli_session()
        self.assertFalse(
            self.worker.choice.resume,
            "session metadata from another issue was resumed")
        self.assertNotEqual(self.worker.choice.session_id, old)
        self.assertTrue(rebuilt, "a transplanted session must rebuild current context")


class RejectedAssessmentTests(IsolationFixture):
    def test_a_rejected_uat_report_cannot_resume_its_own_native_session(self) -> None:
        self.worker.update_state(adversarial={
            "active": True, "phase": "test", "epoch": 1, "round": 0,
        })
        rejected = self.confirm_session()
        self.worker.update_state(adversarial={
            "active": True, "phase": "test", "epoch": 1, "round": 0,
            "retry_rejection": {
                "reason": "invalid tester result",
                "paths": ["tests/adversarial/test_issue381_identity_isolation_and_rejection.py"],
                "attempts": 1,
            },
        })
        self.worker.choice.resume = True
        self.worker.choice.session_id = rejected
        rebuilt = self.worker.prepare_cli_session()
        self.assertFalse(
            self.worker.choice.resume,
            "a rejected assessment resumed its own native CLI session")
        self.assertNotEqual(self.worker.choice.session_id, rejected)
        self.assertTrue(rebuilt, "the replacement assessment needs the full current spec")

    def test_a_rejected_security_report_cannot_resume_its_own_native_session(self) -> None:
        self.worker.update_state(adversarial_security={
            "active": True, "phase": "test", "epoch": 1, "round": 0,
        })
        rejected = self.confirm_session()
        self.worker.update_state(adversarial_security={
            "active": True, "phase": "test", "epoch": 1, "round": 0,
            "retry_rejection": {
                "reason": "invalid tester result", "paths": [], "attempts": 2,
            },
        })
        self.worker.choice.resume = True
        self.worker.choice.session_id = rejected
        self.worker.prepare_cli_session()
        self.assertFalse(self.worker.choice.resume)
        self.assertNotEqual(self.worker.choice.session_id, rejected)


class FailedExecutionTests(IsolationFixture):
    def test_a_non_quota_crash_is_not_reused(self) -> None:
        old = self.confirm_session()

        def runner(prompt, env, activity="working"):
            self.worker._last_ai_raw_output = "fatal: segmentation fault in provider CLI"
            self.worker.ai_diagnostic_file.write_text(
                "fatal: segmentation fault in provider CLI", encoding="utf-8")
            return 1

        self.worker.choice.resume = True
        self.worker.prepare_cli_session()
        with mock.patch.object(self.worker, "_run_claude", side_effect=runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch.object(self.worker, "ai_failure_is_quota", return_value=False), \
             mock.patch.object(self.worker, "provider_capacity", return_value=0):
            self.assertEqual(self.worker.run_ai("continue the work"), 1)

        started = (self.worker.read_state().get("cli_sessions") or {}).get("primary", {}).get("started")
        self.assertFalse(started, "a crashed execution remained marked reusable")
        self.worker.choice.resume = True
        self.assertFalse(self.next_turn())
        self.assertNotEqual(self.worker.choice.session_id, old)

    def test_a_quota_pause_still_preserves_the_session(self) -> None:
        old = self.confirm_session()

        def runner(prompt, env, activity="working"):
            self.worker._last_ai_raw_output = "You've hit your usage limit"
            self.worker.ai_diagnostic_file.write_text(
                "You've hit your usage limit", encoding="utf-8")
            return 1

        self.worker.choice.resume = True
        self.worker.prepare_cli_session()
        with mock.patch.object(self.worker, "_run_claude", side_effect=runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch.object(self.worker, "ai_failure_is_quota", return_value=True):
            self.assertEqual(self.worker.run_ai("continue the work"), 1)

        started = (self.worker.read_state().get("cli_sessions") or {}).get("primary", {}).get("started")
        self.assertTrue(started)
        self.worker.choice.resume = True
        self.assertTrue(self.next_turn())
        self.assertEqual(self.worker.choice.session_id, old)


if __name__ == "__main__":
    unittest.main()
