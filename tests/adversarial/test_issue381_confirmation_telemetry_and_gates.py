"""Issue #381: CLI confirmation, telemetry attribution and routing evidence gates.

Oracle from the issue, derived before treating the implementation as correct:

* A CLI process launch is not proof a session exists. Claude confirms only a
  matching session-bearing system/result event. Recovery retries pass an
  explicit UUID with ``--session-id``, never ``--last``.
* Codex is the same: no ``--last``, and a stored flag is not a session id.
* Documentation review usage carries the documentation role and must not
  claim the implementer's session. Router usage is never attributed to an
  implementation session.
* Codex cumulative counters that reset (compaction) are unavailable, never
  a negative or double-counted invocation.
* Primary continuations omit unchanged engineering knowledge.
* Cache measurements enter routing only with sufficient recent evidence,
  including failed calls in the success-rate gate. 95% of 20 qualifies;
  two failures in 20 do not. Speculative savings from another role do not
  apply.
"""
from __future__ import annotations

import datetime as dt
import io
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

from ai_execution_history import ExecutionHistoryRepository  # noqa: E402
from dynamic_router import cache_adjusted_cost  # noqa: E402
from prompt_sessions import valid_session_id  # noqa: E402
from swarm_issue_worker import Config, IssueContext, ProviderChoice, Worker, build_parser  # noqa: E402
from token_usage import AgentType, NormalizedUsage, PromptType, invocation_usage  # noqa: E402
from usage_report import cache_routing_evidence  # noqa: E402

REPOSITORY = "acme/project"
REVIEW_REPLY = json.dumps({
    "impact": "none", "reason": "no change", "confidence": 0.9, "operations": [],
})


class WorkerFixture(unittest.TestCase):
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
            "--github-repository", REPOSITORY, "--no-dynamic-model-routing",
            "--no-require-bot-auth", "--github-apps-config", str(self.root / "none.json"),
        ]))
        self.worker = Worker(config)
        self.worker.issue = IssueContext(
            381, "Intelligent Prompt Caching", "Required behaviour",
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
        if not self.worker.choice.session_id:
            self.worker.choice.session_id = str(uuid.uuid4())
            self.worker.update_state(session_id=self.worker.choice.session_id)
        self.worker.prepare_cli_session()
        self.worker.update_state(session_started=True)
        self.worker.remember_cli_session(True)
        return self.worker.choice.session_id


class ClaudeConfirmationTests(WorkerFixture):
    def test_a_mismatched_claude_session_id_is_not_confirmed(self) -> None:
        requested = self.worker.choice.session_id
        foreign = str(uuid.uuid4())
        captured: list[list[str]] = []

        class FakeProcess:
            def __init__(self, command, **kwargs):
                captured.append([str(part) for part in command])
                self.stdin = mock.Mock()
                self.stdout = io.StringIO(json.dumps({
                    "type": "result", "session_id": foreign,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }) + "\n")
                self.returncode = 0

            def wait(self):
                return 0

        real_run = self.worker._run_claude

        def wrapped(prompt, env, activity="working"):
            with mock.patch("subprocess.Popen", FakeProcess):
                return real_run(prompt, env, activity)

        with mock.patch.object(self.worker, "provider_bin", return_value="/usr/bin/claude"), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch.object(self.worker, "_run_claude", side_effect=wrapped):
            self.assertEqual(self.worker.run_ai("implement the spec"), 0)

        stored = (self.worker.read_state().get("cli_sessions") or {}).get("primary") or {}
        self.assertFalse(stored.get("started"), "a different CLI session id was treated as confirmation")
        self.assertEqual(self.worker.choice.session_id, requested)
        self.worker.choice.resume = True
        self.worker.prepare_cli_session()
        self.assertFalse(self.worker.choice.resume)

    def test_failed_resume_retry_uses_session_id_and_never_last(self) -> None:
        old = self.confirm_session()
        calls: list[list[str]] = []

        class FakeProcess:
            def __init__(self, command, **kwargs):
                argv = [str(part) for part in command]
                calls.append(argv)
                self.stdin = mock.Mock()
                if len(calls) == 1:
                    self.stdout = io.StringIO(json.dumps({
                        "type": "result", "subtype": "error_during_execution",
                        "is_error": True, "session_id": old,
                        "errors": [f"No conversation found with session ID: {old}"],
                    }) + "\n")
                    self.returncode = 1
                else:
                    sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else str(uuid.uuid4())
                    self.stdout = io.StringIO(json.dumps({
                        "type": "result", "session_id": sid,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    }) + "\n")
                    self.returncode = 0

            def wait(self):
                return self.returncode

        real_run = self.worker._run_claude

        def wrapped(prompt, env, activity="working"):
            with mock.patch("subprocess.Popen", FakeProcess):
                return real_run(prompt, env, activity)

        self.worker.choice.resume = True
        self.worker.prepare_cli_session()
        with mock.patch.object(self.worker, "provider_bin", return_value="/usr/bin/claude"), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch.object(self.worker, "_run_claude", side_effect=wrapped):
            self.assertEqual(self.worker.run_ai("continue the work"), 0)

        self.assertEqual(len(calls), 2)
        self.assertIn("--resume", calls[0])
        self.assertEqual(calls[0][calls[0].index("--resume") + 1], old)
        self.assertNotIn("--last", calls[0])
        self.assertIn("--session-id", calls[1])
        self.assertNotIn("--resume", calls[1])
        self.assertNotIn("--last", calls[1])
        self.assertTrue(valid_session_id(calls[1][calls[1].index("--session-id") + 1]))


class CodexArgvTests(WorkerFixture):
    def test_codex_never_passes_last_and_uses_an_explicit_uuid_on_resume(self) -> None:
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-terra", "medium", "")
        self.worker.save_new_state(
            self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))
        minted = str(uuid.uuid4())
        captured: list[list[str]] = []

        real_run = self.worker._run_codex

        def fake_run(command, **kwargs):
            captured.append([str(part) for part in command])
            stdout = kwargs.get("stdout")
            payload = json.dumps({"type": "thread.started", "thread_id": minted}) + "\n"
            if hasattr(stdout, "write"):
                stdout.write(payload)
            return mock.Mock(returncode=0)

        def wrapped(prompt, env, activity="working"):
            with mock.patch("swarm_issue_worker.subprocess.run", side_effect=fake_run):
                return real_run(prompt, env, activity)

        with mock.patch.object(self.worker, "provider_bin", return_value="/usr/bin/codex"), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch.object(self.worker, "_run_codex", side_effect=wrapped):
            self.assertEqual(self.worker.run_ai("implement the spec"), 0)

        self.assertTrue(captured)
        first = captured[0]
        self.assertNotIn("--last", first)
        self.assertNotIn("resume", first)

        self.worker.choice.resume = True
        self.worker.prepare_cli_session()
        self.assertTrue(self.worker.choice.resume, "the confirmed Codex thread must be reusable")
        self.assertEqual(self.worker.choice.session_id, minted)

        captured.clear()
        with mock.patch.object(self.worker, "provider_bin", return_value="/usr/bin/codex"), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch.object(self.worker, "_run_codex", side_effect=wrapped):
            self.assertEqual(self.worker.run_ai("continue the work"), 0)

        argv = captured[0]
        self.assertIn("resume", argv)
        self.assertNotIn("--last", argv)
        self.assertIn(minted, argv)
        identifiers = [part for part in argv if valid_session_id(part)]
        self.assertEqual(identifiers, [minted])

    def test_stored_last_is_never_a_codex_thread_id(self) -> None:
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-terra", "medium", str(uuid.uuid4()))
        self.worker.save_new_state(
            self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))
        self.confirm_session()
        state = self.worker.read_state()
        state["cli_sessions"]["primary"]["id"] = "--last"
        self.worker.write_state(state)
        self.worker.choice.resume = True
        self.worker.choice.session_id = "--last"
        self.worker.prepare_cli_session()
        self.assertFalse(self.worker.choice.resume)
        self.assertNotEqual(self.worker.choice.session_id, "--last")


class TelemetryAttributionTests(WorkerFixture):
    def test_documentation_review_usage_carries_the_documentation_role(self) -> None:
        implementer = self.confirm_session()

        def runner(prompt, env, activity="working"):
            self.worker.ai_output_file.write_text(REVIEW_REPLY, encoding="utf-8")
            self.worker._last_ai_raw_output = json.dumps({
                "type": "result", "session_id": self.worker.choice.session_id,
                "usage": {"input_tokens": 3, "output_tokens": 2,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            })
            return 0

        with mock.patch.object(self.worker, "_run_claude", side_effect=runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}):
            self.worker._run_documentation_pass("Documentation impact review")

        review = self.worker.read_state()["token_usage_events"][-1]
        self.assertEqual(review.get("session_role"), "documentation")
        self.assertEqual(review.get("agent_type"), AgentType.DOCUMENTATION.value)
        self.assertEqual(review.get("prompt_type"), PromptType.REVIEW.value)
        self.assertFalse(review.get("session_reused"))
        self.assertNotEqual(review.get("agent_run_id"), implementer)
        report = self.worker.render_ai_usage_report()
        self.assertIn("| Documentation |", report)
        self.assertNotIn("| Primary |", report)

    def test_router_usage_is_not_attributed_to_the_implementation_session(self) -> None:
        implementer = str(uuid.uuid4())
        self.worker.choice = ProviderChoice("Claude", self.model, "high", implementer)
        self.worker.save_new_state(
            self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))
        spec = self.worker.config.require_spec("claude")
        self.worker.record_router_usage(
            host=spec, prompt_type="initial", attempt_number=1,
            usage=NormalizedUsage(input_tokens=10, output_tokens=2),
            started_at="2026-10-05T00:00:00+00:00", success=True,
        )
        event = self.worker.read_state()["token_usage_events"][-1]
        self.assertEqual(event["agent_type"], "router")
        self.assertEqual(event["session_role"], "router")
        self.assertEqual(event["agent_run_id"], "")
        self.assertFalse(event["session_reused"])
        self.assertNotEqual(event["agent_run_id"], implementer)

    def test_compaction_reset_of_cumulative_counters_is_unavailable(self) -> None:
        usage = NormalizedUsage(
            input_tokens=40, output_tokens=4, cache_read_tokens=10,
            usage_scope="session",
        )
        result = invocation_usage(usage, True, {
            "input_tokens": 900, "output_tokens": 80, "cache_read_tokens": 700,
        })
        self.assertIsNone(result.input_tokens)
        self.assertIsNone(result.output_tokens)
        self.assertIsNone(result.cache_read_tokens)

    def test_primary_continuation_omits_unchanged_engineering_knowledge(self) -> None:
        marker = "UNIQUE-KNOWLEDGE-381-CONT"
        with mock.patch.object(self.worker, "knowledge_context_section", return_value=marker), \
             mock.patch.object(self.worker, "load_resume_comments", return_value=[]):
            self.worker.choice.resume = False
            fresh = self.worker.build_prompt(False, "", False)
            self.worker.choice.resume = True
            continued = self.worker.build_prompt(False, "", False)
        self.assertIn(marker, fresh)
        self.assertNotIn(marker, continued)


class RoutingEvidenceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = ExecutionHistoryRepository(Path(self.temp.name) / "history.db")
        self.counter = 0

    def event(self, **overrides) -> dict:
        self.counter += 1
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        row = dict(
            id=f"e{self.counter}", sequence=self.counter, agent_type="primary",
            prompt_type="initial", provider="Claude", model="claude-sonnet-5",
            reasoning_effort="high", attempt_number=1, input_tokens=1000,
            output_tokens=10, reasoning_tokens=None, cached_input_tokens=200,
            cache_read_tokens=200, cache_write_tokens=0, total_tokens=1210,
            estimated_cost=0.008, currency="USD", started_at=now, completed_at=now,
            duration_ms=1000, success=True, error_type="", pricing_status="priced",
            pricing_version="2026-09-28",
            pricing_rate_id="claude/claude-sonnet-5@2026-01-01",
            pricing_source="https://www.anthropic.com/pricing",
            input_rate_per_million=3.0, cached_input_rate_per_million=0.3,
            cache_write_rate_per_million=3.75, output_rate_per_million=15.0,
            session_role="primary", cache_input_tokens=1200,
            cache_savings_estimate=0.002,
        )
        row.update(overrides)
        return row

    def add(self, count: int, *, issues: int = 5, **overrides) -> None:
        for index in range(count):
            self.repo.record_token_usage_batch(
                f"run-{self.counter + 1}", REPOSITORY, index % issues + 1,
                [self.event(**overrides)])

    def evidence(self, role: str = "primary") -> list:
        with self.repo.connect() as database:
            return cache_routing_evidence(database, REPOSITORY, role)

    def test_nineteen_successes_and_one_complete_failure_meet_the_gate(self) -> None:
        self.add(19)
        self.add(1, success=False)
        (item,) = self.evidence()
        self.assertEqual(item["samples"], 20)
        self.assertAlmostEqual(item["success_rate"], 0.95)

    def test_two_complete_failures_in_twenty_calls_are_not_evidence(self) -> None:
        self.add(18)
        self.add(2, success=False)
        self.assertEqual(self.evidence(), [])

    def test_primary_cache_evidence_does_not_discount_a_uat_route(self) -> None:
        self.add(20)
        (primary,) = self.evidence("primary")
        cost = cache_adjusted_cost(
            10.0, "claude", "claude-sonnet-5", "high", [primary])
        self.assertLess(cost, 10.0)
        self.assertEqual(
            cache_adjusted_cost(10.0, "claude", "claude-sonnet-5", "high", []),
            10.0,
        )
        uat = self.evidence("adversarial:test")
        self.assertEqual(uat, [])
        self.assertEqual(
            cache_adjusted_cost(10.0, "claude", "claude-sonnet-5", "high", uat),
            10.0,
        )


if __name__ == "__main__":
    unittest.main()
