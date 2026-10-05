"""Issue #389: architecture documentation review is its own per-agent bucket.

Oracle, derived from the finding against #381 before treating the current
attribution as correct:

* ``Worker._run_documentation_pass`` already mints an independent session
  (``session_role=documentation``, ``session_reused=false``, distinct
  ``agent_run_id``). That contract is not this issue.
* GitHub per-agent totals and Feedback agent grouping read ``agent_type``,
  the older #280 bucket. ``infer_ai_agent_context`` still classified the
  documentation pass as ``primary``, so the review inflated Primary.
* The documentation pass must remain independently reportable after a
  confirmed implementer session, including when an adversarial loop is
  still marked active in state, and including a recovered retry of the
  same review.
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

from ai_execution_history import ExecutionHistoryRepository, ExecutionHistoryService  # noqa: E402
from swarm_issue_worker import Config, IssueContext, ProviderChoice, Worker, build_parser  # noqa: E402
from token_usage import AgentType, PromptType  # noqa: E402
from adversarial_uat import UAT_STAGE  # noqa: E402

REVIEW_REPLY = json.dumps({
    "impact": "none", "reason": "no change", "confidence": 0.9, "operations": [],
})
REVIEW_PROMPT = "Documentation impact review"


class DocumentationAgentTypeTests(unittest.TestCase):
    provider = "Claude"
    model = "claude-sonnet-5"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "ai/xai/issue-389")
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
            389, "Documentation agent type", "Required behaviour",
            ["bug"], "https://example.invalid/389",
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

    def _claude_usage(self, input_tokens: int, output_tokens: int, *, body: str = "done") -> str:
        return json.dumps({
            "type": "result",
            "session_id": self.worker.choice.session_id,
            "usage": {
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            },
            "result": body,
        })

    def confirm_implementer(self) -> str:
        self.worker.prepare_cli_session()
        self.worker.update_state(session_started=True)
        self.worker.remember_cli_session(True)
        return self.worker.choice.session_id

    def run_primary(self, input_tokens: int = 100, output_tokens: int = 20) -> None:
        def runner(prompt, env, activity="working"):
            self.worker.ai_output_file.write_text("implemented\n", encoding="utf-8")
            self.worker._last_ai_raw_output = self._claude_usage(input_tokens, output_tokens)
            return 0

        with mock.patch.object(self.worker, "_run_claude", side_effect=runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}):
            self.assertEqual(self.worker.run_ai("implement the issue"), 0)

    def run_documentation(self, input_tokens: int = 30, output_tokens: int = 5) -> None:
        def runner(prompt, env, activity="working"):
            self.worker.ai_output_file.write_text(REVIEW_REPLY, encoding="utf-8")
            self.worker._last_ai_raw_output = self._claude_usage(
                input_tokens, output_tokens, body=REVIEW_REPLY)
            return 0

        with mock.patch.object(self.worker, "_run_claude", side_effect=runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}):
            self.worker._run_documentation_pass(REVIEW_PROMPT)

    def test_documentation_review_is_not_primary_in_usage_or_github_totals(self) -> None:
        implementer = self.confirm_implementer()
        self.run_primary(100, 20)
        self.run_documentation(30, 5)

        events = self.worker.read_state()["token_usage_events"]
        self.assertEqual(len(events), 2)
        primary, review = events
        self.assertEqual(primary["agent_type"], AgentType.PRIMARY.value)
        self.assertEqual(primary["input_tokens"], 100)
        self.assertEqual(review["agent_type"], AgentType.DOCUMENTATION.value)
        self.assertEqual(review["prompt_type"], PromptType.REVIEW.value)
        self.assertEqual(review["session_role"], "documentation")
        self.assertFalse(review["session_reused"])
        self.assertNotEqual(review["agent_run_id"], implementer)
        self.assertNotEqual(review["agent_run_id"], primary["agent_run_id"])
        self.assertEqual(review["input_tokens"], 30)

        report = self.worker.render_ai_usage_report()
        self.assertIn("| Primary |", report)
        self.assertIn("| Documentation |", report)
        self.assertIn("**AI Invocations:** 2", report)
        self.assertIn("**Input:** 130", report)

    def test_primary_totals_exclude_documentation_tokens_after_flush(self) -> None:
        history_db = self.root / "history.sqlite3"
        self.worker.config = dataclasses.replace(
            self.worker.config, ai_execution_history_enabled=True,
            execution_history_db=history_db,
        )
        self.worker.history = ExecutionHistoryService(True, history_db)
        self.worker.start_execution_history()
        self.confirm_implementer()
        self.run_primary(100, 20)
        self.run_documentation(30, 5)
        self.worker.flush_token_usage_to_history()

        repository = ExecutionHistoryRepository(history_db)
        primary = repository.token_usage_totals(
            [self.worker.config.github_repository], agent_type=AgentType.PRIMARY.value)
        docs = repository.token_usage_totals(
            [self.worker.config.github_repository], agent_type=AgentType.DOCUMENTATION.value)
        self.assertEqual(primary["invocations"], 1)
        self.assertEqual(primary["inputTokens"], 100)
        self.assertEqual(docs["invocations"], 1)
        self.assertEqual(docs["inputTokens"], 30)

    def test_independent_pass_wins_over_an_active_adversarial_loop(self) -> None:
        self.worker.update_state(
            **{UAT_STAGE.key: {"phase": "test", "round": 0, "active": True}}
        )
        self.run_documentation(11, 2)
        event = self.worker.read_state()["token_usage_events"][-1]
        self.assertEqual(event["agent_type"], AgentType.DOCUMENTATION.value)
        self.assertEqual(event["prompt_type"], PromptType.REVIEW.value)
        self.assertNotEqual(event["agent_type"], AgentType.ADVERSARIAL_UAT.value)
        self.assertNotEqual(event["agent_type"], AgentType.PRIMARY.value)

    def test_documentation_recovery_retry_stays_in_the_documentation_bucket(self) -> None:
        self.worker._independent_ai_pass = True
        try:
            agent, prompt = self.worker.infer_ai_agent_context(2)
        finally:
            self.worker._independent_ai_pass = False
        self.assertEqual(agent, AgentType.DOCUMENTATION.value)
        self.assertEqual(prompt, PromptType.RETRY.value)


if __name__ == "__main__":
    unittest.main()
