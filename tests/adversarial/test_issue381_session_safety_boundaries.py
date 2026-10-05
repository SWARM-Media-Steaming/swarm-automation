"""Issue #381: fail-closed boundaries for native CLI session continuity.

Oracle derived from the issue before implementation inspection:

* Repository instructions affect execution context whether or not Git tracks
  them. Changing an ignored AGENTS.md must therefore invalidate continuity.
* Malformed legacy/session-adjacent state must degrade to a fresh session, not
  abort the worker before it can recover.
* Provider-native structured error codes for a missing session or exhausted
  context are resume failures even when their human-readable message is terse.
* A successful process exit without a provider-confirmed session identifier is
  not proof that a reusable session exists.
"""
from __future__ import annotations

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

from prompt_sessions import resume_failure  # noqa: E402
from swarm_issue_worker import Config, IssueContext, ProviderChoice, Worker, build_parser  # noqa: E402


class SessionSafetyFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "ai/codex/issue-381")
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
            381, "Intelligent Prompt Caching", "Session safety requirements", ["enhancement"],
            "https://example.invalid/381",
        )
        self.worker.choice = ProviderChoice(
            "Claude", "claude-sonnet-5", "high", str(uuid.uuid4()))
        self.worker.save_new_state(
            self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def remember_confirmed_session(self) -> str:
        self.worker.prepare_cli_session()
        self.worker.update_state(session_started=True)
        self.worker.remember_cli_session(True)
        return self.worker.choice.session_id


class InstructionContextBoundaryTests(SessionSafetyFixture):
    def test_ignored_repository_instruction_change_invalidates_reuse(self) -> None:
        # CLI instruction discovery is filesystem-based, not Git-index-based.
        # An operator may intentionally keep local repository guidance ignored.
        (self.repo / ".gitignore").write_text("AGENTS.md\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore local agent guidance")
        instructions = self.repo / "AGENTS.md"
        instructions.write_text("Always preserve the first invariant.\n", encoding="utf-8")
        old = self.remember_confirmed_session()

        instructions.write_text("Always preserve the second invariant.\n", encoding="utf-8")
        self.worker.choice.resume = True
        rebuilt = self.worker.prepare_cli_session()

        self.assertTrue(rebuilt, "changed ignored instructions require full current context")
        self.assertFalse(self.worker.choice.resume)
        self.assertNotEqual(self.worker.choice.session_id, old)


class MalformedStateBoundaryTests(SessionSafetyFixture):
    def test_malformed_adversarial_epoch_fails_closed_instead_of_crashing(self) -> None:
        self.worker.update_state(adversarial={
            "active": True, "phase": "fix", "epoch": "not-an-integer", "round": 1,
        })
        self.worker.choice.resume = True

        try:
            rebuilt = self.worker.prepare_cli_session()
        except (TypeError, ValueError) as error:
            self.fail(f"malformed persisted role metadata crashed session selection: {error}")

        self.assertTrue(rebuilt)
        self.assertFalse(self.worker.choice.resume)


class ProviderFailureEncodingTests(unittest.TestCase):
    def test_structured_resume_failure_codes_trigger_fresh_recovery(self) -> None:
        payloads = (
            {"type": "turn.failed", "error": {
                "code": "context_length_exceeded", "message": "Request rejected"}},
            {"type": "error", "error": {
                "type": "context_window_exceeded", "message": "Request rejected"}},
            {"type": "error", "code": "session_not_found", "message": "Request rejected"},
            {"type": "turn.failed", "error": {
                "code": "thread_not_found", "message": "Request rejected"}},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertTrue(resume_failure(json.dumps(payload)))


class SessionConfirmationBoundaryTests(SessionSafetyFixture):
    def test_success_without_provider_confirmation_is_not_reused(self) -> None:
        def runner(prompt, env, activity="working"):
            self.worker._last_ai_raw_output = json.dumps({
                "type": "result", "usage": {"input_tokens": 1, "output_tokens": 1},
            })
            return 0

        with mock.patch.object(self.worker, "_run_claude", side_effect=runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}):
            self.assertEqual(self.worker.run_ai("current requirements"), 0)

        stored = self.worker.read_state()["cli_sessions"]["primary"]
        self.assertFalse(stored["started"])
        unconfirmed = self.worker.choice.session_id
        self.worker.choice.resume = True
        self.assertTrue(self.worker.prepare_cli_session())
        self.assertFalse(self.worker.choice.resume)
        self.assertNotEqual(self.worker.choice.session_id, unconfirmed)


if __name__ == "__main__":
    unittest.main()
