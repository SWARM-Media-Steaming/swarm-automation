"""Issue #280 (Per-Prompt AI Token Usage Tracking): cross-stage attribution
integrity and pre-state-file router event survival.

Derived expectations, from the issue spec, before inspecting the diff:

- Acceptance criterion #3 requires every usage record to correlate correctly
  to its actual agent/prompt/provider. Acceptance criterion #16 requires that
  concurrent/sequential agent activity "cannot mix token attribution" — one
  invocation's usage must never bleed into, overwrite, or be mislabeled as
  another's, even when several different agent types (router, primary, UAT,
  cybersecurity) run one after another within the same work-round and share
  the same in-memory/state event list.
- Acceptance criterion #10 requires aggregation "by... agent type... model...
  prompt type" to work without a schema redesign; a *mixed* dataset (more
  than one agent type in the same issue attempt) is the case that would
  actually expose an aggregation query filtering on the wrong column or
  double-counting rows across types.
- Acceptance criterion #9 requires UAT and cybersecurity adversarial usage to
  remain independently reportable, never merged under one generic label —
  again only meaningfully exercised by a sequence that contains *both*.
- ``swarm_issue_worker.Worker.save_new_state``'s own comment documents a
  specific invariant: "A pre-flight routing call can happen before this
  state file exists... carry forward whatever was already recorded in
  memory so it is not lost the moment the file is created." That really is
  the production ordering (``Worker.run`` calls
  ``maybe_apply_dynamic_routing`` before ``start_execution_history``/
  ``save_new_state``, the latter living inside ``prepare_repository``), but
  no existing test calls ``run_router`` *before* ``save_new_state`` and then
  checks that the event actually survived into the persisted state — every
  existing router-usage test either calls it after state already exists, or
  never persists state at all.

Neither of these is exercised end-to-end anywhere in the existing suite:
``issue_worker/test_token_usage.py`` only exercises the pure ``token_usage``
module against synthetic ``UsageRecord`` dicts built by a test helper, and
``issue_worker/test_swarm_issue_worker.py``'s per-agent tests each check only
the *last* recorded event after one stage's call — never that earlier,
differently-typed events already in the same list stay intact once later
ones are appended, and never the DB-level aggregation of a mixed-agent-type
dataset.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import test_swarm_issue_worker as fixtures  # noqa: E402
from token_usage import AgentType, NormalizedUsage, PromptType  # noqa: E402


def _claude_result_json(*, input_tokens: int, output_tokens: int) -> str:
    return json.dumps(
        {
            "type": "result",
            "result": "done",
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    )


class CrossStageTokenAttributionIntegrityTests(unittest.TestCase):
    """A single work-round that exercises every agent type (router, primary,
    UAT scan+fix, cybersecurity scan+fix) in the real production order, then
    verifies every one of the six resulting events — in memory, in the
    ``### AI Usage`` report, and once flushed to the execution-history DB —
    keeps its own distinct agent/prompt attribution and input-token count."""

    setUp = fixtures.WorkerTestCase.setUp
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    # (agent_type, prompt_type, input_tokens) expected for each of the six
    # calls below, in the order they are made.
    EXPECTED = [
        (AgentType.ROUTER.value, PromptType.INITIAL.value, 111),
        (AgentType.PRIMARY.value, PromptType.INITIAL.value, 222),
        (AgentType.ADVERSARIAL_UAT.value, PromptType.ADVERSARIAL_SCAN.value, 333),
        (AgentType.ADVERSARIAL_UAT.value, PromptType.REMEDIATION.value, 444),
        (AgentType.ADVERSARIAL_CYBERSECURITY.value, PromptType.ADVERSARIAL_SCAN.value, 555),
        (AgentType.ADVERSARIAL_CYBERSECURITY.value, PromptType.REMEDIATION.value, 666),
    ]

    def _run_claude_with(self, input_tokens: int, output_tokens: int):
        def _run(prompt: str, env: dict, activity: str = "") -> int:
            self.worker._last_ai_raw_output = _claude_result_json(
                input_tokens=input_tokens, output_tokens=output_tokens
            )
            self.worker.ai_output_file.write_text("done\n", encoding="utf-8")
            return 0

        return _run

    def test_mixed_agent_sequence_keeps_every_event_distinct_end_to_end(self) -> None:
        worker = self.worker
        history_db = self.state / "cross-stage-history.sqlite3"
        worker.config = dataclasses.replace(
            worker.config, ai_execution_history_enabled=True, execution_history_db=history_db,
        )
        worker.history = fixtures.ExecutionHistoryService(True, history_db)
        worker.issue = fixtures.IssueContext(501, "Title", "body", [], "https://example.invalid/501")
        worker.choice = fixtures.ProviderChoice("Claude", "test-model", "high", "session-cross-stage")
        worker.save_new_state(worker.issue, worker.choice, self.base_sha)
        worker.start_execution_history()
        execution_id = worker.history.execution_id
        self.assertTrue(execution_id)

        # 1. Router / pre-flight grading call.
        host = worker.config.spec("claude")

        def fake_router(*, usage_sink=None, **_kwargs) -> str:
            if usage_sink is not None:
                usage_sink.append(NormalizedUsage(input_tokens=111, output_tokens=11, total_tokens=122))
            return json.dumps({"prompt_grade": "B", "selected_provider": "claude"})

        with mock.patch("swarm_issue_worker.run_provider_router", side_effect=fake_router):
            worker.run_router(host, "grade this issue", [])

        # 2. Primary implementation call.
        with mock.patch.object(worker, "_run_claude", side_effect=self._run_claude_with(222, 22)):
            worker.run_ai("implement", activity="working")

        # 3. Adversarial UAT: round 0 independent assessment ("test" phase).
        worker.update_state(**{fixtures.UAT_STAGE.key: {"phase": "test", "round": 0, "active": True}})
        with mock.patch.object(worker, "_run_claude", side_effect=self._run_claude_with(333, 33)):
            worker.run_ai("uat scan", activity="testing")

        # 4. Adversarial UAT: fix round 1.
        worker.update_state(**{fixtures.UAT_STAGE.key: {"phase": "fix", "round": 1, "active": True}})
        with mock.patch.object(worker, "_run_claude", side_effect=self._run_claude_with(444, 44)):
            worker.run_ai("uat fix", activity="fixing")

        # 5. UAT finished; cybersecurity round 0 independent assessment starts.
        worker.update_state(
            **{
                fixtures.UAT_STAGE.key: {"phase": "test", "round": 0, "active": False},
                fixtures.SECURITY_STAGE.key: {"phase": "test", "round": 0, "active": True},
            }
        )
        with mock.patch.object(worker, "_run_claude", side_effect=self._run_claude_with(555, 55)):
            worker.run_ai("cyber scan", activity="testing")

        # 6. Cybersecurity: fix round 1.
        worker.update_state(**{fixtures.SECURITY_STAGE.key: {"phase": "fix", "round": 1, "active": True}})
        with mock.patch.object(worker, "_run_claude", side_effect=self._run_claude_with(666, 66)):
            worker.run_ai("cyber fix", activity="fixing")

        events = worker.read_state()["token_usage_events"]
        self.assertEqual(
            len(events), 6, f"expected exactly one usage event per invocation, got {len(events)}"
        )
        actual = [(event["agent_type"], event["prompt_type"], event["input_tokens"]) for event in events]
        self.assertEqual(
            actual,
            self.EXPECTED,
            "an earlier call's recorded agent/prompt attribution or token count changed once "
            "later, differently-typed calls were recorded — usage events are cross-attributed "
            "or overwritten instead of each staying independent (issue #280 items 4, 13, 16).",
        )

        # The GitHub-facing report must show every one of the six rows, with
        # UAT and cybersecurity independently labelled per item 9.
        report = worker.render_ai_usage_report()
        self.assertIn("AI Invocations: 6", report)
        self.assertIn("UAT Adversarial", report)
        self.assertIn("Cyber Adversarial", report)
        self.assertNotIn("| Adversarial |", report)
        self.assertIn(f"Input: {111 + 222 + 333 + 444 + 555 + 666:,}", report)

        # Flushing to the execution-history DB must preserve every row's own
        # distinct attribution and support per-agent-type aggregation without
        # cross-contamination (items 3, 10).
        worker.flush_token_usage_to_history()
        repository = fixtures.ExecutionHistoryRepository(history_db)
        rows = repository.token_usage_for_execution(execution_id)
        self.assertEqual(len(rows), 6)
        persisted = [(row["agent_type"], row["prompt_type"], row["input_tokens"]) for row in rows]
        self.assertEqual(persisted, self.EXPECTED)

        issue_rows = repository.token_usage_for_issue(worker.config.github_repository, 501)
        self.assertEqual(len(issue_rows), 6)

        uat_totals = repository.token_usage_totals(
            [worker.config.github_repository], agent_type=AgentType.ADVERSARIAL_UAT.value
        )
        self.assertEqual(uat_totals["invocations"], 2)
        self.assertEqual(uat_totals["inputTokens"], 333 + 444)

        cyber_totals = repository.token_usage_totals(
            [worker.config.github_repository], agent_type=AgentType.ADVERSARIAL_CYBERSECURITY.value
        )
        self.assertEqual(cyber_totals["invocations"], 2)
        self.assertEqual(cyber_totals["inputTokens"], 555 + 666)

        primary_totals = repository.token_usage_totals(
            [worker.config.github_repository], agent_type=AgentType.PRIMARY.value
        )
        self.assertEqual(primary_totals["invocations"], 1)
        self.assertEqual(primary_totals["inputTokens"], 222)

        overall_totals = repository.token_usage_totals([worker.config.github_repository])
        self.assertEqual(overall_totals["invocations"], 6)
        self.assertEqual(overall_totals["inputTokens"], 111 + 222 + 333 + 444 + 555 + 666)


class RouterUsageBeforeStateFileSurvivesIntoPersistedStateTests(unittest.TestCase):
    """``Worker.save_new_state``'s own comment says a pre-flight routing call
    made before the state file exists must not have its usage event lost
    once the file is created — this is exactly the real ordering
    ``Worker.run`` uses (``maybe_apply_dynamic_routing`` runs before
    ``save_new_state``, which lives inside ``prepare_repository``), but no
    existing test calls ``run_router`` before ``save_new_state`` and then
    checks the state file afterward."""

    setUp = fixtures.WorkerTestCase.setUp
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def test_router_event_recorded_before_state_exists_is_carried_into_save_new_state(self) -> None:
        worker = self.worker
        self.assertFalse(worker.in_progress_file.exists())
        worker.issue = fixtures.IssueContext(502, "Title", "body", [], "https://example.invalid/502")
        worker.choice = fixtures.ProviderChoice("Grok", "grok-4.6", "medium", "session-pre-state")
        host = worker.config.spec("grok")

        def fake_router(*, usage_sink=None, **_kwargs) -> str:
            if usage_sink is not None:
                usage_sink.append(NormalizedUsage(input_tokens=77, output_tokens=7, total_tokens=84))
            return json.dumps({"prompt_grade": "B", "selected_provider": "grok"})

        with mock.patch("swarm_issue_worker.run_provider_router", side_effect=fake_router):
            worker.run_router(host, "grade this issue", [])

        # Confirm the event only exists in memory so far, exactly the
        # "pre-flight call before the state file exists" scenario.
        self.assertFalse(worker.in_progress_file.exists())
        self.assertEqual(len(worker.token_usage_events), 1)

        worker.save_new_state(worker.issue, worker.choice, self.base_sha)

        self.assertTrue(worker.in_progress_file.exists())
        persisted = worker.read_state().get("token_usage_events", [])
        self.assertEqual(
            len(persisted), 1,
            "the router usage event recorded before the state file existed was lost when "
            "save_new_state created it — a pre-flight routing call's telemetry silently "
            "disappears for every brand-new issue attempt (issue #280 item 4).",
        )
        self.assertEqual(persisted[0]["agent_type"], AgentType.ROUTER.value)
        self.assertEqual(persisted[0]["input_tokens"], 77)


if __name__ == "__main__":
    unittest.main()
