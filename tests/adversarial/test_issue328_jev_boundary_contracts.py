"""Boundary contracts for issue #328's deferred Jev findings.

These tests use only local fixtures.  They cover fail-closed decisions just
below the security threshold, independent security provenance fields, and a
provider/model match that lives beyond the first Feedback page.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER = ROOT / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

from ai_execution_history import (  # noqa: E402
    PAGE_SIZE,
    ExecutionHistoryRepository,
    ExecutionStart,
)
from decision_engine import DecisionResult, DecisionType, swarm_policy_action  # noqa: E402
from jev_cli import JevSettings  # noqa: E402
from swarm_issue_worker import Worker  # noqa: E402


class FailClosedBoundaryTests(unittest.TestCase):
    def test_nonblocking_finding_actions_fail_closed_below_security_threshold(self) -> None:
        settings = JevSettings(enabled=True, confidence_security=0.95)
        recommendations = ("PASS", "SKIP_UAT", "SKIP_CYBER")
        for kind in (DecisionType.UAT_FINDING.value, DecisionType.CYBER_FINDING.value):
            for recommendation in recommendations:
                with self.subTest(kind=kind, recommendation=recommendation):
                    result = DecisionResult(
                        decision_type=kind,
                        decision=recommendation,
                        confidence=0.949999,
                        source="jev",
                    )
                    action = swarm_policy_action(result, settings=settings, default="")
                    self.assertNotEqual(
                        action,
                        recommendation,
                        "a below-threshold irreversible recommendation became the Swarm action",
                    )
                    self.assertIn(
                        action,
                        {"FIX_NOW", "RUN_UAT", "RUN_CYBER"},
                        "fail-closed selection did not choose a conservative action",
                    )

    def test_exact_security_threshold_cannot_override_a_blocking_finding(self) -> None:
        result = DecisionResult(
            decision_type=DecisionType.CYBER_FINDING.value,
            decision="PASS",
            confidence=0.95,
            source="jev",
        )
        self.assertEqual(
            swarm_policy_action(
                result,
                settings=JevSettings(enabled=True, confidence_security=0.95),
                blocking_security=True,
                default="",
            ),
            "FIX_NOW",
        )


class _KnowledgeScorer:
    score_knowledge_pack = Worker.score_knowledge_pack
    _is_security_context = staticmethod(Worker._is_security_context)

    def __init__(self) -> None:
        self.config = SimpleNamespace(jev=JevSettings(enabled=True, use_rag=True))

    def evaluate_decision(self, kind: str, context: dict) -> dict:
        if kind == DecisionType.RAG_SCOPE.value:
            return {"decision": "ORGANIZATION"}
        candidate = str(context.get("candidate") or "")
        relevance = 0.01 if candidate.startswith("Prior finding") else 0.90
        return {"decision": "KEEP", "scores": {"relevance": relevance}}


class RagProvenanceBoundaryTests(unittest.TestCase):
    def test_each_canonical_security_provenance_field_survives_independently(self) -> None:
        source_only = {
            "title": "Prior finding from source kind",
            "summary": "Opaque historical finding",
            "source_kind": "SECURITY_FINDING",
        }
        stage_only = {
            "title": "Prior finding from stage",
            "summary": "Opaque historical finding",
            "metadata": {"stage": "Security"},
        }
        ordinary = [
            {"title": f"Ordinary context {index}", "summary": "routine"}
            for index in range(4)
        ]
        pack = SimpleNamespace(items=[*ordinary, source_only, stage_only], metadata={})
        kept = _KnowledgeScorer().score_knowledge_pack(pack)
        self.assertIn(source_only, kept.items)
        self.assertIn(stage_only, kept.items)


class FeedbackExecutionModelFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = ExecutionHistoryRepository(Path(temporary.name) / "history.sqlite3")

    def record_comparison(self, execution_id: str, created_at: str) -> None:
        self.repo.record_jev_score_comparison(
            {
                "comparison_id": f"cmp-{execution_id}",
                "execution_id": execution_id,
                "repository": "acme/app",
                "issue_number": 328,
                "created_at": created_at,
                "jev_status": "enabled",
                "baseline": {"normalized_score": 0.50},
                "jev": {"normalized_score": 0.60, "confidence": 0.96},
                "modified": {"normalized_score": 0.55},
                "delta": {
                    "absolute": 0.05,
                    "percent": 10.0,
                    "routing_changed": False,
                },
                "estimated_jev_cost": 0.001,
                "workflow_outcome": "completed",
            }
        )

    def test_execution_model_match_is_found_beyond_the_first_page(self) -> None:
        """Provider/model filtering must inspect the joined execution model.

        Older or baseline-only comparison rows may not duplicate the selected
        model into their comparison payload.  The execution record remains the
        authoritative model source and the SQL filter must run before paging.
        """
        target = self.repo.create(
            ExecutionStart(
                repository="acme/app",
                issue_number=328,
                issue_url="",
                issue_title="Execution model only",
                issue_body="",
                provider="codex",
                model="execution-model-unique",
                effort="medium",
                branch_name="ai/codex/issue-328",
                application_version="fixture",
            ),
            "2026-01-01T12:00:00+00:00",
        )
        self.record_comparison(target, "2026-01-01T12:00:00+00:00")
        for index in range(PAGE_SIZE + 1):
            self.record_comparison(
                f"decoy-{index}",
                f"2026-09-{index + 1:02d}T12:00:00+00:00",
            )

        first_page = self.repo.jev_feedback(["acme/app"])
        self.assertNotIn(
            target,
            {row["executionId"] for row in first_page["records"]},
            "fixture must place the execution-model row beyond page one",
        )
        filtered = self.repo.jev_feedback(
            ["acme/app"], provider="execution-model-unique"
        )
        self.assertEqual(
            [row["executionId"] for row in filtered["records"]],
            [target],
            "the full-history provider/model query ignored ai_executions.model",
        )
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["summary"]["comparisons"], 1)


if __name__ == "__main__":
    unittest.main()
