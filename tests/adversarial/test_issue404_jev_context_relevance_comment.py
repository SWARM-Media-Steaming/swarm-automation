"""Issue #404: Context Relevance stays out of GitHub Workflow Decisions.

Per-candidate RAG ratings remain in jev_decisions and in Jev-call totals.
They must not appear on the issue comment, and the heading must disappear
when they are the only workflow records.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
ADVERSARIAL = Path(__file__).resolve().parent
ISSUE_WORKER = ROOT / "issue_worker"
for path in (ISSUE_WORKER, ADVERSARIAL / "issue299"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ai_execution_history import ExecutionHistoryRepository  # noqa: E402
from decision_engine import DecisionType, Source, summarize_decisions  # noqa: E402
from harness import JevWorkerFixture  # noqa: E402


class ContextRelevanceCommentContractTests(JevWorkerFixture, unittest.TestCase):
    def test_worker_comment_lists_rag_scope_and_completion_only(self) -> None:
        db = self.enable_history()
        self.bind_issue(number=404, title="Hide context relevance on comments")

        def evaluate(kind, _context):
            if kind == DecisionType.RAG_SCOPE.value:
                return self.decision(
                    decision_type=kind,
                    decision="REPOSITORY",
                    confidence=0.91,
                    latency_ms=40,
                    estimated_cost=0.0002,
                    llm_calls_avoided=1,
                )
            if kind == DecisionType.COMPLETION.value:
                return self.decision(
                    decision_type=kind,
                    decision="COMPLETE",
                    confidence=0.95,
                    latency_ms=80,
                    estimated_cost=0.0003,
                )
            return self.decision(
                decision_type=DecisionType.CONTEXT_RELEVANCE.value,
                decision="LOW",
                confidence=0.97,
                scores={"relevance": 0.08},
                latency_ms=12,
                estimated_cost=0.00001,
            )

        self.install_jev_engine(evaluate)
        self.worker.evaluate_decision(DecisionType.RAG_SCOPE.value, {"default_action": "REPOSITORY"})
        for index in range(6):
            self.worker.evaluate_decision(
                DecisionType.CONTEXT_RELEVANCE.value,
                {"candidate": f"chunk-{index}"},
            )
        self.worker.evaluate_decision(
            DecisionType.COMPLETION.value,
            {"default_action": "COMPLETE"},
        )

        engine = self.worker.decision_engine()
        summary = summarize_decisions(engine.records)
        report = self.worker.render_jev_report()
        self.worker.routing = {}
        body = self.worker.render_pending_comment(
            {
                "ai": "Codex",
                "model": "gpt-test",
                "effort": "high",
                "commit_sha": "c" * 40,
                "commit_message": "Omit context relevance from comments (#404)",
                "ai_usage_report": " ",
                "ai_output": "Completed.",
            }
        )

        self.assertIn("Workflow Decisions:", report)
        self.assertIn("- **Rag Scope:** Repository — 91%", report)
        self.assertIn("- **Completion:** Complete — 95%", report)
        self.assertNotIn("Context Relevance", report)
        self.assertNotIn("Context Relevance", body)
        self.assertIn(report, body)
        self.assertEqual(summary["jevCalls"], 8)
        self.assertIn(f"**Jev calls:** {summary['jevCalls']}", report)
        self.assertIn(f"**Total decision latency:** {summary['totalLatencyMs'] / 1000:.1f}s", report)
        self.assertIn(f"**Estimated cost:** ${summary['estimatedCost']:.4f}", report)
        self.assertIn(
            f"**Estimated LLM decision calls avoided:** {summary['llmCallsAvoided']}",
            report,
        )

        with ExecutionHistoryRepository(db).connect() as database:
            kinds = [
                row["decision_type"]
                for row in database.execute("SELECT decision_type FROM jev_decisions")
            ]
        self.assertEqual(kinds.count(DecisionType.CONTEXT_RELEVANCE.value), 6)
        self.assertIn(DecisionType.RAG_SCOPE.value, kinds)
        self.assertIn(DecisionType.COMPLETION.value, kinds)

    def test_only_context_relevance_omits_the_workflow_heading_on_the_comment(self) -> None:
        self.enable_history()
        self.bind_issue(number=404)
        self.install_jev_engine(lambda *_args, **_kwargs: None)
        engine = self.worker.decision_engine()
        engine.records = [
            self.decision(
                decision_type=DecisionType.CONTEXT_RELEVANCE.value,
                decision="KEEP",
                confidence=0.5,
                source=Source.LOW_CONFIDENCE.value,
                latency_ms=9,
            )
            for _ in range(4)
        ]

        report = self.worker.render_jev_report()
        self.worker.routing = {}
        body = self.worker.render_pending_comment(
            {
                "ai": "Claude",
                "model": "claude-test",
                "effort": "low",
                "commit_sha": "d" * 40,
                "commit_message": "No empty Workflow Decisions heading (#404)",
                "ai_usage_report": " ",
                "ai_output": "Completed.",
            }
        )
        self.assertIn("### Jev Decision Engine", report)
        self.assertNotIn("Workflow Decisions:", report)
        self.assertNotIn("Context Relevance", report)
        self.assertNotIn("Keep — 50%", report)
        self.assertIn("**Jev calls:**", report)
        self.assertNotIn("Workflow Decisions:", body)
        self.assertNotIn("Context Relevance", body)


class ContextRelevanceScoringUnchangedTests(JevWorkerFixture, unittest.TestCase):
    def test_low_scores_still_keep_a_fallback_pack(self) -> None:
        self.bind_issue(number=404)

        def evaluate(kind, _context):
            if kind == DecisionType.RAG_SCOPE.value:
                return self.decision(decision_type=kind, decision="REPOSITORY", confidence=0.4)
            return self.decision(
                decision_type=DecisionType.CONTEXT_RELEVANCE.value,
                decision="LOW",
                confidence=0.2,
                scores={"relevance": 0.08},
            )

        self.install_jev_engine(evaluate)
        pack = SimpleNamespace(
            items=[
                {"title": "Architecture document", "summary": "pipeline"},
                {"title": "Historical security incident", "summary": "incident"},
                {"title": "Unrelated README", "summary": "readme"},
            ],
            metadata={},
        )
        kept = self.worker.score_knowledge_pack(pack)
        self.assertGreaterEqual(len(kept.items), 1)
        kinds = [item.decision_type for item in self.worker.decision_engine().records]
        self.assertGreaterEqual(kinds.count(DecisionType.CONTEXT_RELEVANCE.value), 1)
        self.assertNotIn("Context Relevance", self.worker.render_jev_report())


if __name__ == "__main__":
    unittest.main()
