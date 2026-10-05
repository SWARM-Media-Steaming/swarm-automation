"""Issue #381: compact_boundary error events must recover without lucky phrasing.

Oracle from the issue and the documented session-lifecycle contract, derived
before treating the implementation as correct:

* Native compaction that the CLI itself reports as an error is a resume
  failure and must get one automatic fresh retry with current spec, tests and
  findings. Human intervention is not required.
* The signal is the event, not a particular English sentence. A
  ``compact_boundary`` event that carries ``is_error`` is a failed compaction
  even when the message is terse, empty, or also mentions an earlier successful
  compact. The subtype alone is success evidence only on a non-error event.
* An error / ``turn.failed`` event that carries ``compact_boundary`` and a
  context-exhaustion code (for example ``context_length_exceeded``) is the same
  failure, including when ``is_error`` is omitted. Skipping every
  ``compact_boundary`` event that lacks ``is_error`` would swallow that code.
* Successful ``compact_boundary`` system events still must not reset a session.
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

SPEC_MARKER = "UNIQUE-SPEC-MARKER-381-COMPACT-SHAPE-e41b"
FINDING_MARKER = "adversarial-issue381-compaction-error-event-shapes"


def dumps(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))


class CompactionShapeFixture(unittest.TestCase):
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
                self.worker._last_ai_raw_output = payload if payload.endswith("\n") else payload + "\n"
                return 1
            self.worker.update_state(session_started=True)
            self.worker._last_ai_raw_output = ""
            return 0

        with mock.patch.object(self.worker, runner_name, side_effect=runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch.object(self.worker, "ai_failure_is_quota", return_value=False), \
             mock.patch.object(self.worker, "provider_capacity", return_value=0):
            status = self.worker.run_ai("continue the work with current findings")
        self.assertEqual(status, 0, "failed compaction must recover automatically in this invocation")
        return calls


class CompactBoundaryErrorClassificationTests(unittest.TestCase):
    def test_is_error_compact_boundary_does_not_need_a_context_window_phrase(self) -> None:
        cases = {
            "terse message": dumps({
                "type": "system", "subtype": "compact_boundary", "is_error": True,
                "message": "compaction failed",
            }),
            "no message": dumps({
                "type": "system", "subtype": "compact_boundary", "is_error": True,
            }),
            "errors array only": dumps({
                "type": "error", "subtype": "compact_boundary", "is_error": True,
                "errors": ["compaction failed"],
            }),
            "string is_error": dumps({
                "type": "system", "subtype": "compact_boundary", "is_error": "true",
                "message": "compaction failed",
            }),
            "numeric is_error": dumps({
                "type": "system", "subtype": "compact_boundary", "is_error": 1,
                "message": "compaction failed",
            }),
            "mentions an earlier successful compact": dumps({
                "type": "system", "subtype": "compact_boundary", "is_error": True,
                "message": "compaction failed (last successfully compacted checkpoint was stale)",
            }),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                self.assertTrue(
                    resume_failure(payload),
                    f"{name}: compact_boundary with is_error must recover without matching prose",
                )

    def test_error_typed_compact_boundary_keeps_context_length_codes(self) -> None:
        cases = {
            "error event, code, no is_error": dumps({
                "type": "error", "subtype": "compact_boundary",
                "error": {"code": "context_length_exceeded", "message": "compaction failed"},
            }),
            "turn.failed, code, no is_error": dumps({
                "type": "turn.failed", "subtype": "compact_boundary",
                "error": {"code": "context_length_exceeded"},
            }),
            "error event, is_error false, code present": dumps({
                "type": "error", "subtype": "compact_boundary", "is_error": False,
                "error": {"code": "context_length_exceeded"},
            }),
            "error event, hyphenated code": dumps({
                "type": "error", "subtype": "compact_boundary",
                "error": {"code": "context-length-exceeded"},
            }),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                self.assertTrue(
                    resume_failure(payload),
                    f"{name}: an error compact_boundary must not drop the exhaustion code",
                )

    def test_successful_compact_boundary_system_events_are_still_success(self) -> None:
        for payload in (
            dumps({"type": "system", "subtype": "compact_boundary"}),
            dumps({
                "type": "system", "subtype": "compact_boundary", "is_error": False,
                "message": "context window was full; compacted successfully",
            }),
        ):
            with self.subTest(payload=payload[:70]):
                self.assertFalse(resume_failure(payload))


class CompactBoundaryRecoveryTests(CompactionShapeFixture):
    TERSE_ERROR = dumps({
        "type": "system", "subtype": "compact_boundary", "is_error": True,
        "session_id": "11111111-2222-4333-8444-555555555555",
        "message": "compaction failed",
    })
    ERROR_EVENT_WITH_CODE = dumps({
        "type": "error", "subtype": "compact_boundary",
        "error": {"code": "context_length_exceeded", "message": "compaction failed"},
    })
    STREAM_THEN_TERSE_ERROR = "\n".join([
        dumps({"type": "system", "subtype": "compact_boundary"}),
        dumps({
            "type": "system", "subtype": "compact_boundary", "is_error": True,
            "message": "compaction failed",
        }),
    ])

    def test_terse_is_error_compact_boundary_retries_once_fresh(self) -> None:
        old = self.confirm_session()
        calls = self.run_with_first_payload(self.TERSE_ERROR)
        self.assertEqual(len(calls), 2, "failed compaction must be retried fresh, exactly once")
        self.assertTrue(calls[0][0])
        self.assertEqual(calls[0][1], old)
        self.assertFalse(calls[1][0])
        self.assertNotEqual(calls[1][1], old)
        self.assertIn(SPEC_MARKER, calls[1][2])

    def test_error_compact_boundary_with_exhaustion_code_retries_once_fresh(self) -> None:
        old = self.confirm_session()
        calls = self.run_with_first_payload(self.ERROR_EVENT_WITH_CODE)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0])
        self.assertEqual(calls[0][1], old)
        self.assertFalse(calls[1][0])
        self.assertNotEqual(calls[1][1], old)

    def test_later_failed_compact_in_a_stream_is_not_hidden_by_an_earlier_success(self) -> None:
        old = self.confirm_session()
        calls = self.run_with_first_payload(self.STREAM_THEN_TERSE_ERROR)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], old)
        self.assertNotEqual(calls[1][1], old)
        self.assertFalse(calls[1][0])

    def test_codex_turn_failed_compact_boundary_retries_with_a_fresh_thread(self) -> None:
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-terra", "medium", str(uuid.uuid4()))
        self.worker.save_new_state(
            self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))
        old = self.confirm_session()
        payload = dumps({
            "type": "turn.failed", "subtype": "compact_boundary",
            "error": {"code": "context_length_exceeded", "message": "compaction failed"},
        })
        calls = self.run_with_first_payload(payload, runner_name="_run_codex")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0])
        self.assertEqual(calls[0][1], old)
        self.assertFalse(calls[1][0])
        self.assertNotEqual(calls[1][1], old)


class RecoveredFixerContextTests(CompactionShapeFixture):
    def test_failed_compaction_rebuilds_the_fixer_spec_and_current_findings(self) -> None:
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

        payload = dumps({
            "type": "system", "subtype": "compact_boundary", "is_error": True,
            "message": "compaction failed",
        })
        calls: list[tuple[bool, str, str]] = []

        def runner(prompt, env, activity="working"):
            calls.append((self.worker.choice.resume, self.worker.choice.session_id, prompt))
            if len(calls) == 1:
                self.worker._last_ai_raw_output = payload + "\n"
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
        self.assertIn(SPEC_MARKER, recovered, "a recovered fixer needs the current specification")
        self.assertNotIn("unchanged specification is retained", recovered)
        self.assertIn("current patch", recovered)
        self.assertIn(FINDING_MARKER, recovered)


if __name__ == "__main__":
    unittest.main()
