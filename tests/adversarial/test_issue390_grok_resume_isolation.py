"""Issue #390: Grok must not inherit a Claude/Codex native-cache resume.

Oracle (derived from the finding before treating the implementation as correct):

* Native CLI cache is Claude/Codex only. Grok keeps its existing ``--resume``
  path for a session it actually started.
* Official provider handoff mints a new Grok session and sets ``resume=False``.
* ``prepare_cli_session`` / ``run_ai`` must fail closed when a confirmed Claude
  (or Codex) session is replaced with Grok using that UUID and ``resume=True``:
  Grok must not keep ``resume=True`` and ``_run_grok`` must not receive
  ``--resume``.
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

from prompt_sessions import valid_session_id  # noqa: E402
from swarm_issue_worker import Config, IssueContext, ProviderChoice, Worker, build_parser  # noqa: E402


class GrokResumeIsolationFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "ai/claude/issue-390")
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
            390, "Grok resume isolation", "Do not pass Claude sessions to Grok",
            ["bug"], "https://example.invalid/390",
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

    def confirm_session(self) -> str:
        if not self.worker.choice.session_id:
            self.worker.choice.session_id = str(uuid.uuid4())
            self.worker.update_state(session_id=self.worker.choice.session_id)
        self.worker.prepare_cli_session()
        self.worker.update_state(session_started=True)
        self.worker.remember_cli_session(True)
        return self.worker.choice.session_id


class ClaudeToGrokHandoffTests(GrokResumeIsolationFixture):
    def test_prepare_cli_session_clears_resume_when_grok_inherits_claude(self) -> None:
        claude = self.confirm_session()
        self.worker.choice = ProviderChoice("Grok", "grok-4.6", "medium", claude, resume=True)
        rebuilt = self.worker.prepare_cli_session()
        self.assertTrue(rebuilt, "Grok has no Claude transcript and needs current context")
        self.assertFalse(self.worker.choice.resume)
        self.assertNotEqual(self.worker.choice.session_id, claude)
        self.assertTrue(valid_session_id(self.worker.choice.session_id))

    def test_run_ai_invokes_grok_without_resume_after_claude_session(self) -> None:
        claude = self.confirm_session()
        seen: list[tuple[bool, str]] = []

        def grok_runner(prompt, env, activity="working"):
            seen.append((self.worker.choice.resume, self.worker.choice.session_id))
            self.worker._last_ai_raw_output = json.dumps({
                "text": "done", "sessionId": self.worker.choice.session_id,
            })
            return 0

        self.worker.choice = ProviderChoice("Grok", "grok-4.6", "medium", claude, resume=True)
        with mock.patch.object(self.worker, "_run_grok", side_effect=grok_runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}):
            status = self.worker.run_ai("continue the implementation")
        self.assertEqual(status, 0)
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0][0], "_run_grok was invoked with resume=True")
        self.assertNotEqual(seen[0][1], claude)

    def test_grok_cli_gets_session_id_not_resume_after_claude(self) -> None:
        claude = self.confirm_session()
        captured: dict[str, list[str]] = {}

        def fake_run(command, **kwargs):
            captured["command"] = list(command)
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps({"text": "ok"}),
            )

        self.worker.choice = ProviderChoice("Grok", "grok-4.6", "medium", claude, resume=True)
        with mock.patch.object(self.worker, "provider_bin", return_value="/bin/echo"), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch("swarm_issue_worker.subprocess.run", side_effect=fake_run):
            self.assertEqual(self.worker.run_ai("continue the implementation"), 0)
        command = captured["command"]
        self.assertNotIn("--resume", command)
        self.assertIn("--session-id", command)
        self.assertNotEqual(command[command.index("--session-id") + 1], claude)

    def test_codex_session_is_also_not_resumed_by_grok(self) -> None:
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-terra", "medium", str(uuid.uuid4()))
        self.worker.save_new_state(
            self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))
        codex = self.confirm_session()
        self.worker.choice = ProviderChoice("Grok", "grok-4.6", "medium", codex, resume=True)
        self.worker.prepare_cli_session()
        self.assertFalse(self.worker.choice.resume)
        self.assertNotEqual(self.worker.choice.session_id, codex)


class GrokOwnSessionTests(GrokResumeIsolationFixture):
    def test_grok_still_resumes_a_session_it_started(self) -> None:
        grok_id = str(uuid.uuid4())
        self.worker.choice = ProviderChoice("Grok", "grok-4.6", "medium", grok_id)
        self.worker.update_state_for_choice(self.worker.choice)
        self.worker.update_state(session_started=True)
        self.worker.choice.resume = True
        rebuilt = self.worker.prepare_cli_session()
        self.assertFalse(rebuilt)
        self.assertTrue(self.worker.choice.resume)
        self.assertEqual(self.worker.choice.session_id, grok_id)

    def test_grok_own_resume_passes_resume_to_the_cli(self) -> None:
        grok_id = str(uuid.uuid4())
        self.worker.choice = ProviderChoice("Grok", "grok-4.6", "medium", grok_id)
        self.worker.update_state_for_choice(self.worker.choice)
        self.worker.update_state(session_started=True)
        self.worker.choice.resume = True
        captured: dict[str, list[str]] = {}

        def fake_run(command, **kwargs):
            captured["command"] = list(command)
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"text": "ok"}))

        with mock.patch.object(self.worker, "provider_bin", return_value="/bin/echo"), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch("swarm_issue_worker.subprocess.run", side_effect=fake_run):
            self.assertEqual(self.worker.run_ai("continue"), 0)
        command = captured["command"]
        self.assertIn("--resume", command)
        self.assertEqual(command[command.index("--resume") + 1], grok_id)
        self.assertNotIn("--session-id", command)


if __name__ == "__main__":
    unittest.main()
