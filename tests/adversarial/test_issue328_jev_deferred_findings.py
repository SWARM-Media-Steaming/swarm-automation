"""Adversarial acceptance tests for issue #328's five deferred Jev findings.

The fixtures are local and deterministic: no Jev binary, GitHub account, or
network access is required.  Expectations come from issue #328 and the public
Jev contract, especially the distinction between a recommendation and the
final Swarm-owned action.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER = ROOT / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

from ai_execution_history import PAGE_SIZE, ExecutionHistoryRepository, main as history_main  # noqa: E402
from decision_engine import DecisionResult, DecisionType, WorkflowAction, swarm_policy_action  # noqa: E402
from jev_cli import JevSettings  # noqa: E402
from swarm_issue_worker import Worker  # noqa: E402


class FailClosedAuthorityTests(unittest.TestCase):
    def test_every_unactionable_irreversible_action_has_a_conservative_empty_default(self) -> None:
        """An empty caller default must not promote Jev's recommendation.

        The metadata case is important: ``may_act_on`` treats that result as
        security-sensitive, so fail-closed selection must do the same.
        """
        settings = JevSettings(enabled=True)
        cases = (
            ("WORKFLOW", "FAIL", {}, "HUMAN_REVIEW"),
            ("WORKFLOW", "SKIP_UAT", {}, "RUN_UAT"),
            ("WORKFLOW", "SKIP_CYBER", {}, "RUN_CYBER"),
            ("WORKFLOW", "COMPLETE", {}, "HUMAN_REVIEW"),
            ("COMPLETION", "COMPLETE", {}, "NEEDS_HUMAN_REVIEW"),
            ("UAT_FINDING", "PASS", {}, "FIX_NOW"),
            ("CYBER_FINDING", "PASS", {}, "FIX_NOW"),
            ("WORKFLOW", "PASS", {"security": True}, "FIX_NOW"),
        )
        for kind, recommendation, metadata, expected in cases:
            with self.subTest(kind=kind, recommendation=recommendation, metadata=metadata):
                result = DecisionResult(
                    decision_type=kind,
                    decision=recommendation,
                    confidence=0.80,
                    source="jev",
                    metadata=metadata,
                )
                self.assertEqual(
                    swarm_policy_action(result, settings=settings, default=""),
                    expected,
                    "a non-actionable irreversible recommendation became Swarm's "
                    "action because the caller supplied no default",
                )

    def test_unavailable_high_confidence_complete_still_fails_closed_and_default_wins(self) -> None:
        settings = JevSettings(enabled=True)
        unavailable = DecisionResult(
            decision_type=DecisionType.COMPLETION.value,
            decision="COMPLETE",
            confidence=1.0,
            source="unavailable",
        )
        self.assertEqual(
            swarm_policy_action(unavailable, settings=settings, default=""),
            "NEEDS_HUMAN_REVIEW",
        )
        self.assertEqual(
            swarm_policy_action(unavailable, settings=settings, default="NEEDS_RETRY"),
            "NEEDS_RETRY",
        )


class _KnowledgeScorer:
    """Minimal receiver for the production knowledge-pack policy method."""

    score_knowledge_pack = Worker.score_knowledge_pack
    _is_security_context = staticmethod(Worker._is_security_context)

    def __init__(self) -> None:
        self.config = SimpleNamespace(jev=JevSettings(enabled=True, use_rag=True))

    def evaluate_decision(self, kind: str, context: dict) -> dict:
        if kind == DecisionType.RAG_SCOPE.value:
            return {"decision": "ORGANIZATION"}
        candidate = str(context.get("candidate") or "")
        relevance = 0.01 if candidate == "Prior production finding" else 0.90
        return {"decision": "KEEP", "scores": {"relevance": relevance}}


class RagCriticalContextTests(unittest.TestCase):
    def test_security_finding_provenance_survives_a_low_score_beyond_the_top_three(self) -> None:
        """Security provenance is stronger evidence than title word matching.

        Engineering-knowledge rows persist security findings with
        ``source_kind=security_finding`` and ``metadata.stage=security``.  A
        historical finding must remain even when four ordinary chunks rank
        above it and its human title contains no convenient keyword.
        """
        critical = {
            "title": "Prior production finding",
            "summary": "Login bypass permitted administrator impersonation.",
            "object_type": "finding",
            "source_kind": "security_finding",
            "metadata": {"stage": "security"},
        }
        pack = SimpleNamespace(
            items=[
                {"title": "Architecture", "summary": "services"},
                {"title": "Runbook", "summary": "operations"},
                {"title": "README", "summary": "setup"},
                {"title": "Release notes", "summary": "changes"},
                critical,
            ],
            metadata={},
        )
        kept = _KnowledgeScorer().score_knowledge_pack(pack)
        self.assertIn(
            critical,
            kept.items,
            "a persisted security finding was dropped solely because Jev gave it "
            "a low relevance score and four ordinary chunks ranked higher",
        )


class FeedbackHistoryFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "history.sqlite3"
        self.repo = ExecutionHistoryRepository(self.db)

    def record(
        self,
        execution_id: str,
        *,
        created_at: str,
        model: str,
        delta: float,
        cost: float | None,
        jev_model: str = "jev-default",
    ) -> None:
        self.repo.record_jev_score_comparison(
            {
                "comparison_id": f"cmp-{execution_id}",
                "execution_id": execution_id,
                "repository": "acme/app",
                "issue_number": 328,
                "created_at": created_at,
                "jev_status": "enabled",
                "baseline": {"normalized_score": 0.50, "model": "baseline-route"},
                "jev": {
                    "normalized_score": 0.60,
                    "confidence": 0.96,
                    "model": jev_model,
                },
                "modified": {
                    "normalized_score": 0.55,
                    "provider": "codex",
                    "model": model,
                },
                "delta": {
                    "absolute": delta,
                    "percent": delta * 100,
                    "routing_changed": False,
                },
                "estimated_jev_cost": cost,
                "workflow_outcome": "completed",
            }
        )

    def test_cli_combines_full_history_filters_and_keeps_both_date_boundaries(self) -> None:
        for index in range(PAGE_SIZE + 2):
            self.record(
                f"decoy-{index}",
                created_at=f"2026-09-{index + 1:02d}T12:00:00+00:00",
                model="expensive-route",
                delta=0.01,
                cost=0.50,
            )
        self.record(
            "from-boundary",
            created_at="2026-01-15T00:00:00+00:00",
            model="budget-route",
            delta=-0.20,
            cost=0.0009,
        )
        self.record(
            "to-boundary",
            created_at="2026-02-01T23:59:59+00:00",
            model="budget-route",
            delta=0.10,
            cost=0.001,
        )

        first_page = self.repo.jev_feedback(["acme/app"])
        self.assertNotIn(
            "from-boundary",
            {row["executionId"] for row in first_page["records"]},
            "fixture must put the cheap boundary row beyond page one",
        )

        stdout = StringIO()
        argv = [
            "--db",
            str(self.db),
            "--repository",
            "acme/app",
            "--jev-feedback",
            "--jev-provider",
            "budget-route",
            "--jev-from",
            "2026-01-15",
            "--jev-to",
            "2026-02-01",
            "--jev-min-delta",
            "0.10",
            "--jev-max-cost",
            "0.001",
        ]
        with mock.patch("sys.stdout", stdout):
            code = history_main(argv)
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["total"], 2)
        self.assertEqual(
            {row["executionId"] for row in payload["records"]},
            {"from-boundary", "to-boundary"},
            "provider/model, inclusive dates, absolute delta, and maximum cost "
            "must be applied in the history query before pagination",
        )
        self.assertEqual(payload["summary"]["comparisons"], 2)

    def test_provider_model_filter_includes_the_persisted_jev_model(self) -> None:
        self.record(
            "jev-model-only",
            created_at="2026-01-20T12:00:00+00:00",
            model="selected-route",
            delta=0.20,
            cost=0.001,
            jev_model="decision-model-unique",
        )
        page = self.repo.jev_feedback(["acme/app"], provider="decision-model-unique")
        self.assertEqual(
            [row["executionId"] for row in page["records"]],
            ["jev-model-only"],
            "the Feedback provider/model query omitted a row whose stored Jev "
            "model is the only matching provider/model field",
        )


if __name__ == "__main__":
    unittest.main()
