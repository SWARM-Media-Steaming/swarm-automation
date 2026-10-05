"""Issue #381: failed compaction recovery and malformed rejection identity.

Oracle from the issue, derived before treating the implementation as correct:

* Session recovery must handle context compaction. Successful native
  compaction is left alone. A compaction that the CLI itself marks as an
  error (exhausted or corrupt context) is a resume failure: the worker
  must start one fresh session with the current spec, tests and findings.
  The word "compact_boundary" in an error event is not proof of success.
* Independent assessments stay independent. A rejected tester report starts
  a fresh assessment so the discarded conversation cannot be resumed. The
  presence of ``retry_rejection`` is that signal even when the payload is
  malformed or empty; only a later interrupted retry of the replacement
  assessment may resume itself.
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

SPEC_MARKER = "UNIQUE-SPEC-MARKER-381-FAILCLOSED-c8e1"


class FailClosedFixture(unittest.TestCase):
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

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def confirm_session(self) -> str:
        self.worker.prepare_cli_session()
        self.worker.update_state(session_started=True)
        self.worker.remember_cli_session(True)
        return self.worker.choice.session_id

    def next_turn(self) -> bool:
        self.worker.choice.resume = True
        self.worker.prepare_cli_session()
        return self.worker.choice.resume


class FailedCompactionRecoveryTests(FailClosedFixture):
    FAILED_COMPACTION = json.dumps({
        "type": "system",
        "subtype": "compact_boundary",
        "is_error": True,
        "session_id": "11111111-2222-4333-8444-555555555555",
        "message": "context window was full; compaction failed",
    })
    FAILED_COMPACTION_CODE = json.dumps({
        "type": "error",
        "subtype": "compact_boundary",
        "is_error": True,
        "error": {"code": "context_length_exceeded", "message": "compaction failed"},
    })

    def test_failed_compaction_events_are_resume_failures(self) -> None:
        cases = {
            "is_error compact_boundary": self.FAILED_COMPACTION,
            "compact_boundary plus context-length code": self.FAILED_COMPACTION_CODE,
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                self.assertTrue(
                    resume_failure(payload),
                    "a failed compaction event must recover, not be treated as success",
                )

    def test_successful_compaction_is_still_not_a_resume_failure(self) -> None:
        self.assertFalse(resume_failure(json.dumps({
            "type": "system", "subtype": "compact_boundary",
            "message": "context window was full; compacted successfully",
        })))

    def test_failed_compaction_retries_once_with_a_fresh_session(self) -> None:
        old = self.confirm_session()
        calls: list[tuple[bool, str, str]] = []

        def runner(prompt, env, activity="working"):
            calls.append((self.worker.choice.resume, self.worker.choice.session_id, prompt))
            if len(calls) == 1:
                self.worker._last_ai_raw_output = self.FAILED_COMPACTION + "\n"
                return 1
            self.worker.update_state(session_started=True)
            self.worker._last_ai_raw_output = ""
            return 0

        with mock.patch.object(self.worker, "_run_claude", side_effect=runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}):
            status = self.worker.run_ai("continue the work with current findings")

        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 2, "failed compaction must be retried fresh, exactly once")
        self.assertTrue(calls[0][0])
        self.assertEqual(calls[0][1], old)
        self.assertFalse(calls[1][0])
        self.assertNotEqual(calls[1][1], old)
        self.assertIn(SPEC_MARKER, calls[1][2])


class MalformedRejectionIdentityTests(FailClosedFixture):
    def test_empty_or_non_dict_retry_rejection_cannot_resume_the_discarded_session(self) -> None:
        for payload in ({}, [], "rejected", 1):
            with self.subTest(payload=payload):
                self.worker.update_state(adversarial={
                    "active": True, "phase": "test", "epoch": 1, "round": 0,
                })
                rejected = self.confirm_session()
                self.worker.update_state(adversarial={
                    "active": True, "phase": "test", "epoch": 1, "round": 0,
                    "retry_rejection": payload,
                })
                self.worker.choice.resume = True
                self.worker.choice.session_id = rejected
                rebuilt = self.worker.prepare_cli_session()
                self.assertFalse(
                    self.worker.choice.resume,
                    f"retry_rejection={payload!r} resumed the discarded assessment",
                )
                self.assertNotEqual(self.worker.choice.session_id, rejected)
                self.assertTrue(rebuilt)

    def test_an_interrupted_retry_after_a_well_formed_rejection_may_resume_itself(self) -> None:
        self.worker.update_state(adversarial={
            "active": True, "phase": "test", "epoch": 1, "round": 0,
            "retry_rejection": {"reason": "invalid tester result", "attempts": 1},
        })
        retry = self.confirm_session()
        self.worker.choice.session_id = retry
        self.assertTrue(self.next_turn())
        self.assertEqual(self.worker.choice.session_id, retry)


if __name__ == "__main__":
    unittest.main()
