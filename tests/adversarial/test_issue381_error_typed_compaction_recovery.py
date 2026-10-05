"""Issue #381: error-typed compaction events recover in the same invocation.

Oracle from the issue and the session-lifecycle contract, derived before
treating the implementation as correct:

* Session failures, including exhausted context, recover automatically in the
  same invocation. Human intervention is not required.
* Native compaction that the CLI reports as an error is a resume failure.
  ``compact_boundary`` is success evidence only on a non-error event. An
  ``error`` / ``turn.failed`` event that carries that subtype is therefore a
  failed compaction even when ``is_error`` and a context-length code are both
  omitted, and even when the message is terse, empty, or success-sounding.
* A later ``compact_started`` or successful ``compact_boundary`` (including the
  camelCase ``compactMetadata`` shape the CLI actually emits) is not a failure.
* Every recovered attempt is accounted separately. The exhausted session is
  not the one remembered after a successful retry.
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

SPEC_MARKER = "UNIQUE-SPEC-MARKER-381-ERROR-TYPED-9c2a"
FINDING_MARKER = "adversarial-issue381-error-typed-compaction-recovery"


def dumps(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))


class ErrorTypedCompactionClassificationTests(unittest.TestCase):
    def test_error_typed_compact_boundary_is_a_failure_without_extra_flags(self) -> None:
        cases = {
            "error, terse message": dumps({
                "type": "error", "subtype": "compact_boundary",
                "message": "compaction failed",
            }),
            "error, empty": dumps({"type": "error", "subtype": "compact_boundary"}),
            "error, is_error omitted, no code": dumps({
                "type": "error", "subtype": "compact_boundary",
                "errors": ["compaction failed"],
            }),
            "error, is_error false, no code": dumps({
                "type": "error", "subtype": "compact_boundary", "is_error": False,
                "message": "compaction failed",
            }),
            "turn.failed, terse message": dumps({
                "type": "turn.failed", "subtype": "compact_boundary",
                "message": "compaction failed",
            }),
            "turn.failed, empty": dumps({"type": "turn.failed", "subtype": "compact_boundary"}),
            "error, success-sounding message": dumps({
                "type": "error", "subtype": "compact_boundary",
                "message": "compacted successfully",
            }),
            "turn.failed, mentions an earlier successful compact": dumps({
                "type": "turn.failed", "subtype": "compact_boundary",
                "error": {"message": "last successfully compacted checkpoint was stale"},
            }),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                self.assertTrue(
                    resume_failure(payload),
                    f"{name}: error-typed compact_boundary must recover without is_error or a code",
                )

    def test_non_error_compact_events_from_the_cli_are_still_success(self) -> None:
        for payload in (
            dumps({
                "type": "system", "subtype": "compact_boundary",
                "content": "Conversation compacted",
                "compactMetadata": {"trigger": "auto", "preTokens": 966342, "postTokens": 12000},
            }),
            dumps({
                "type": "system", "subtype": "compact_started",
                "compactMetadata": {"trigger": "auto", "preTokens": 967872},
            }),
            dumps({"type": "system", "subtype": "compact_boundary"}),
        ):
            with self.subTest(payload=payload[:80]):
                self.assertFalse(resume_failure(payload))


class CompactionRecoveryFixture(unittest.TestCase):
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
        self.assertEqual(status, 0, "failed compaction must recover automatically in this invocation")
        return calls


class ErrorTypedCompactionRecoveryTests(CompactionRecoveryFixture):
    ERROR_TYPED = dumps({
        "type": "error", "subtype": "compact_boundary",
        "message": "compaction failed",
    })
    TURN_FAILED = dumps({
        "type": "turn.failed", "subtype": "compact_boundary",
        "error": {"message": "compaction failed"},
    })

    def test_error_typed_compact_boundary_retries_once_fresh(self) -> None:
        old = self.confirm_session()
        calls = self.run_with_first_payload(self.ERROR_TYPED)
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

    def test_turn_failed_compact_boundary_without_a_code_retries_once_fresh(self) -> None:
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-terra", "medium", str(uuid.uuid4()))
        self.worker.save_new_state(
            self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))
        old = self.confirm_session()
        calls = self.run_with_first_payload(self.TURN_FAILED, runner_name="_run_codex")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0])
        self.assertEqual(calls[0][1], old)
        self.assertFalse(calls[1][0])
        self.assertNotEqual(calls[1][1], old)

    def test_recovered_attempts_are_accounted_separately(self) -> None:
        old = self.confirm_session()
        self.run_with_first_payload(self.ERROR_TYPED)
        events = self.worker.read_state()["token_usage_events"]
        self.assertGreaterEqual(len(events), 2)
        first, second = events[-2], events[-1]
        self.assertEqual(first["attempt_number"], 1)
        self.assertFalse(first["success"])
        self.assertTrue(first["session_reused"])
        self.assertEqual(first["agent_run_id"], old)
        self.assertEqual(second["attempt_number"], 2)
        self.assertTrue(second["success"])
        self.assertFalse(second["session_reused"])
        self.assertNotEqual(second["agent_run_id"], old)

    def test_a_second_compaction_failure_does_not_retry_again(self) -> None:
        self.confirm_session()
        calls: list[int] = []

        def runner(prompt, env, activity="working"):
            calls.append(1)
            self.worker._last_ai_raw_output = self.ERROR_TYPED + "\n"
            return 1

        with mock.patch.object(self.worker, "_run_claude", side_effect=runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch.object(self.worker, "ai_failure_is_quota", return_value=False), \
             mock.patch.object(self.worker, "provider_capacity", return_value=0):
            status = self.worker.run_ai("continue the work with current findings")
        self.assertEqual(status, 1)
        self.assertEqual(len(calls), 2, "exactly one fresh retry, then the failure stands")
        stored = (self.worker.read_state().get("cli_sessions") or {}).get("primary") or {}
        self.assertFalse(stored.get("started"), "a failed recovery must not look resumable")


class SecurityFixerCompactionRecoveryTests(CompactionRecoveryFixture):
    def test_error_typed_compaction_rebuilds_the_security_fixer_spec(self) -> None:
        self.worker.update_state(adversarial_security={
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
            "type": "error", "subtype": "compact_boundary",
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

        stage = next(item for item in ADVERSARIAL_STAGES if item.key == "adversarial_security")
        with mock.patch.object(self.worker, "_run_claude", side_effect=runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch.object(self.worker, "ai_failure_is_quota", return_value=False), \
             mock.patch.object(self.worker, "provider_capacity", return_value=0):
            status = self.worker.run_ai(self.worker.adversarial_prompt(
                stage, self.worker.read_state()["adversarial_security"]))

        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0])
        self.assertEqual(calls[0][1], old)
        self.assertFalse(calls[1][0])
        self.assertNotEqual(calls[1][1], old)
        recovered = calls[1][2]
        self.assertIn(SPEC_MARKER, recovered, "a recovered security fixer needs the current specification")
        self.assertNotIn("unchanged specification is retained", recovered)
        self.assertIn("current patch", recovered)
        self.assertIn(FINDING_MARKER, recovered)


if __name__ == "__main__":
    unittest.main()
