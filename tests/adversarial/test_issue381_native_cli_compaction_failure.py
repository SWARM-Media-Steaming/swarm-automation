"""Issue #381: recover from the compaction failures the CLIs actually emit.

Oracle from the issue, derived from the CLI event contract rather than from
the implementation:

* Session failures, including exhausted context, recover automatically in the
  same invocation. Native compaction stays under each CLI. When the CLI reports
  that compaction itself failed, the worker must start one fresh session with
  the current specification, tests and findings.
* Claude Code stream-json does not mark failed compaction as
  ``subtype=compact_boundary`` with ``is_error``. Successful compaction is
  ``system/compact_boundary``. Failed compaction is a ``system/status`` event
  with ``compact_result=failed`` (and optional ``compact_error``), a result
  event whose errors name compaction failure, or the CLI's own
  "Compaction failed" prose. Those are resume failures. A status event with
  ``compact_result=success`` is not.
* Codex reports a successful compact as ``context_compacted``. That event is
  not a resume failure. A later ``turn.failed`` still recovers.
* A later failed compact in the same stream is never hidden by an earlier
  successful compact_boundary.
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
from swarm_issue_worker import (  # noqa: E402
    ADVERSARIAL_STAGES,
    Config,
    IssueContext,
    ProviderChoice,
    Worker,
    build_parser,
)

SPEC_MARKER = "UNIQUE-SPEC-MARKER-381-NATIVE-COMPACT-7e4b"
FINDING_MARKER = "adversarial-issue381-native-cli-compaction-failure"


def dumps(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))


# Shapes taken from Claude Code 2.1.289 stream-json (system/status carries
# compact_result/compact_error; compact_boundary is the success boundary).
CLAUDE_STATUS_FAILED = dumps({
    "type": "system", "subtype": "status", "status": "requesting",
    "compact_result": "failed",
    "compact_error": "conversation could not be reduced below the context",
    "session_id": "11111111-2222-4333-8444-555555555555",
})
CLAUDE_STATUS_FAILED_NO_MESSAGE = dumps({
    "type": "system", "subtype": "status", "compact_result": "failed",
})
CLAUDE_STATUS_SUCCESS = dumps({
    "type": "system", "subtype": "status", "status": "requesting",
    "compact_result": "success",
})
CLAUDE_COMPACT_BOUNDARY_SUCCESS = dumps({
    "type": "system", "subtype": "compact_boundary",
    "content": "Conversation compacted",
    "session_id": "11111111-2222-4333-8444-555555555555",
    "compact_metadata": {"trigger": "auto", "pre_tokens": 966342},
})
CLAUDE_RESULT_COMPACTION_FAILED = dumps({
    "type": "result", "subtype": "error_during_execution", "is_error": True,
    "session_id": "11111111-2222-4333-8444-555555555555",
    "errors": ["automatic compaction failed: conversation could not be reduced below the context"],
    "result": "Compaction failed \u00b7 conversation could not be reduced below the context",
})
CLAUDE_PROSE_COMPACTION_FAILED = (
    "Compaction failed \u00b7 conversation could not be reduced below the context\n"
)
CODEX_CONTEXT_COMPACTED = dumps({
    "type": "event_msg", "payload": {"type": "context_compacted"},
})


class NativeCliCompactionClassificationTests(unittest.TestCase):
    def test_claude_status_compact_result_failed_is_a_resume_failure(self) -> None:
        for name, payload in {
            "status failed with compact_error": CLAUDE_STATUS_FAILED,
            "status failed, no error text": CLAUDE_STATUS_FAILED_NO_MESSAGE,
            "result errors name compaction failure": CLAUDE_RESULT_COMPACTION_FAILED,
            "cli prose": CLAUDE_PROSE_COMPACTION_FAILED,
        }.items():
            with self.subTest(name=name):
                self.assertTrue(
                    resume_failure(payload),
                    f"{name}: the CLI reported a failed compaction and must recover",
                )

    def test_successful_native_compaction_events_are_not_resume_failures(self) -> None:
        for name, payload in {
            "claude compact_boundary": CLAUDE_COMPACT_BOUNDARY_SUCCESS,
            "claude status compact_result success": CLAUDE_STATUS_SUCCESS,
            "codex context_compacted": CODEX_CONTEXT_COMPACTED,
        }.items():
            with self.subTest(name=name):
                self.assertFalse(
                    resume_failure(payload),
                    f"{name}: successful native compaction must be left alone",
                )

    def test_later_status_failure_is_not_hidden_by_an_earlier_compact_boundary(self) -> None:
        stream = "\n".join([CLAUDE_COMPACT_BOUNDARY_SUCCESS, CLAUDE_STATUS_FAILED])
        self.assertTrue(
            resume_failure(stream),
            "a later compact_result=failed must recover even after a successful boundary",
        )


class NativeCliCompactionFixture(unittest.TestCase):
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

    def run_with_first_payload(self, payload: str, runner_name: str = "_run_claude") -> list:
        calls: list[tuple[bool, str, str]] = []

        def runner(prompt, env, activity="working"):
            calls.append((self.worker.choice.resume, self.worker.choice.session_id, prompt))
            if len(calls) == 1:
                self.worker._last_ai_raw_output = (
                    payload if payload.endswith("\n") else payload + "\n"
                )
                return 1
            self.worker.update_state(session_started=True)
            self.worker._last_ai_raw_output = json.dumps({
                "type": "result", "session_id": self.worker.choice.session_id,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            })
            return 0

        with mock.patch.object(self.worker, runner_name, side_effect=runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch.object(self.worker, "ai_failure_is_quota", return_value=False), \
             mock.patch.object(self.worker, "provider_capacity", return_value=0):
            status = self.worker.run_ai("continue the work with current findings")
        self.assertEqual(
            status, 0,
            "a CLI-reported compaction failure must recover automatically in this invocation",
        )
        return calls


class NativeCliCompactionRecoveryTests(NativeCliCompactionFixture):
    def test_status_compact_result_failed_retries_once_fresh(self) -> None:
        old = self.confirm_session()
        calls = self.run_with_first_payload(CLAUDE_STATUS_FAILED)
        self.assertEqual(len(calls), 2, "failed compaction must be retried fresh, exactly once")
        self.assertTrue(calls[0][0])
        self.assertEqual(calls[0][1], old)
        self.assertFalse(calls[1][0])
        self.assertNotEqual(calls[1][1], old)
        self.assertIn(SPEC_MARKER, calls[1][2])
        stored = (self.worker.read_state().get("cli_sessions") or {}).get("primary") or {}
        self.assertEqual(stored.get("id"), calls[1][1])
        self.assertTrue(stored.get("started"))
        self.assertNotEqual(stored.get("id"), old)

    def test_result_compaction_failure_retries_once_fresh(self) -> None:
        old = self.confirm_session()
        calls = self.run_with_first_payload(CLAUDE_RESULT_COMPACTION_FAILED)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], old)
        self.assertNotEqual(calls[1][1], old)
        self.assertFalse(calls[1][0])
        self.assertIn(SPEC_MARKER, calls[1][2])

    def test_prose_compaction_failure_retries_once_fresh(self) -> None:
        old = self.confirm_session()
        calls = self.run_with_first_payload(CLAUDE_PROSE_COMPACTION_FAILED)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], old)
        self.assertNotEqual(calls[1][1], old)

    def test_successful_status_compact_does_not_retry(self) -> None:
        old = self.confirm_session()
        calls: list[int] = []

        def runner(prompt, env, activity="working"):
            calls.append(1)
            self.worker._last_ai_raw_output = CLAUDE_STATUS_SUCCESS + "\n"
            return 1

        with mock.patch.object(self.worker, "_run_claude", side_effect=runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch.object(self.worker, "ai_failure_is_quota", return_value=False), \
             mock.patch.object(self.worker, "provider_capacity", return_value=0):
            status = self.worker.run_ai("continue the work with current findings")
        self.assertEqual(status, 1)
        self.assertEqual(len(calls), 1, "successful compact_result must not look like a resume failure")
        self.assertEqual(self.worker.choice.session_id, old)


class NativeCliFixerCompactionRecoveryTests(NativeCliCompactionFixture):
    def test_status_compact_failure_rebuilds_the_fixer_spec(self) -> None:
        self.worker.update_state(adversarial={
            "active": True, "phase": "fix", "epoch": 1, "round": 1,
            "results": [{"id": FINDING_MARKER, "exit_code": 1}],
            "rounds": [{"round": 0}],
        })
        (self.repo / "source.py").write_text("value = 2  # current patch\n", encoding="utf-8")
        old = self.confirm_session()
        self.worker.choice.resume = True
        self.worker.prepare_cli_session()
        self.assertTrue(self.worker.choice.resume)

        calls: list[tuple[bool, str, str]] = []

        def runner(prompt, env, activity="working"):
            calls.append((self.worker.choice.resume, self.worker.choice.session_id, prompt))
            if len(calls) == 1:
                self.worker._last_ai_raw_output = CLAUDE_STATUS_FAILED + "\n"
                return 1
            self.worker.update_state(session_started=True)
            self.worker._last_ai_raw_output = ""
            return 0

        stage = next(item for item in ADVERSARIAL_STAGES if item.key == "adversarial")
        with mock.patch.object(self.worker, "_run_claude", side_effect=runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch.object(self.worker, "ai_failure_is_quota", return_value=False), \
             mock.patch.object(self.worker, "provider_capacity", return_value=0):
            status = self.worker.run_ai(self.worker.adversarial_prompt(
                stage, self.worker.read_state()["adversarial"]))

        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0])
        self.assertEqual(calls[0][1], old)
        self.assertFalse(calls[1][0])
        self.assertNotEqual(calls[1][1], old)
        recovered = calls[1][2]
        self.assertIn(SPEC_MARKER, recovered)
        self.assertNotIn("unchanged specification is retained", recovered)
        self.assertIn("current patch", recovered)
        self.assertIn(FINDING_MARKER, recovered)


if __name__ == "__main__":
    unittest.main()
