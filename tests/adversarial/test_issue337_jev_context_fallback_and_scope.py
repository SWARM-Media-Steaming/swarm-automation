"""Adversarial domain boundaries for Issue #337's tiered Jev context.

The fixtures are deterministic and stay below process/network boundaries.  They
exercise two requirements not implied by the implementation shape: every long
description still needs a summary when headings are absent, and rich issue
context belongs only to issue-level decisions rather than per-candidate RAG
scoring.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER = ROOT / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

from decision_engine import DecisionResult, DecisionType, Source  # noqa: E402
from issue_context import IssueContextSettings, SECTION_KEYS, build_issue_context  # noqa: E402
from jev_cli import JevSettings  # noqa: E402
from swarm_issue_worker import Worker  # noqa: E402


class LongIssueFallbackContractTests(unittest.TestCase):
    def test_long_unheaded_description_still_has_a_bounded_summary_and_edge_excerpts(self) -> None:
        """Plain prose must not silently degrade to just a new head/tail truncation."""
        body = (
            "Requested change: preserve useful issue context. "
            + ("Background prose without markdown headings. " * 300)
            + "Acceptance requires the bounded summary and the final constraint to survive."
        )
        settings = IssueContextSettings(
            max_raw_chars=300,
            max_summary_chars=240,
            max_excerpt_chars=600,
        )

        package = build_issue_context(body, settings)
        metadata = package["metadata"]

        self.assertTrue(metadata["truncated"])
        self.assertFalse(metadata["complete"])
        self.assertTrue(
            metadata["summarized"],
            "every over-limit issue needs a bounded summary even when headings are unavailable",
        )
        self.assertEqual(metadata["summarySource"], "deterministic")
        self.assertIn("[summary]\n", package["text"])
        self.assertIn("beginning", metadata["excerpts"])
        self.assertIn("end", metadata["excerpts"])
        self.assertIn("final constraint", package["text"])
        self.assertLessEqual(
            len(package["text"]),
            settings.max_summary_chars + settings.max_excerpt_chars + 200,
        )

    def test_every_required_structured_section_is_extracted_when_available(self) -> None:
        markers = {
            "requested_change": ("Requested change", "REQUEST_MARKER"),
            "acceptance_criteria": ("Acceptance criteria", "ACCEPTANCE_MARKER"),
            "reproduction_steps": ("Reproduction steps", "REPRO_MARKER"),
            "technical_constraints": ("Technical constraints", "CONSTRAINT_MARKER"),
            "affected_components": ("Affected components", "COMPONENT_MARKER"),
            "dependencies": ("Dependencies and cross-repository implications", "DEPENDENCY_MARKER"),
            "security": ("Security implications", "SECURITY_MARKER"),
            "testing": ("Testing requirements", "TESTING_MARKER"),
            "out_of_scope": ("Explicit out-of-scope items", "OUT_OF_SCOPE_MARKER"),
        }
        body = "\n\n".join(
            f"## {heading}\n{marker} must remain available to the decision."
            for heading, marker in markers.values()
        )
        package = build_issue_context(
            body,
            IssueContextSettings(
                max_raw_chars=200,
                max_summary_chars=2200,
                max_excerpt_chars=2200,
            ),
        )

        self.assertEqual(set(package["metadata"]["sections"]), set(SECTION_KEYS))
        for key, (_heading, marker) in markers.items():
            with self.subTest(section=key):
                self.assertIn(marker, package["text"])


class RagIsolationContractTests(unittest.TestCase):
    def test_worker_keeps_rich_issue_package_out_of_per_candidate_rag_calls(self) -> None:
        body = "B" * 5000 + " LATE_ISSUE_ONLY_CONTEXT"
        worker = object.__new__(Worker)
        worker.issue = SimpleNamespace(number=337, title="Long issue", labels=[], body=body)
        worker.config = SimpleNamespace(
            jev=JevSettings(enabled=True, context_max_raw_chars=300),
            github_repository="example/repository",
            adversarial_uat_enabled=False,
            adversarial_security_enabled=False,
        )

        class RecordingHistory:
            execution_id = "execution-337-rag"

            def record_jev_decision(self, _record):
                return None

        calls = []

        class RecordingEngine:
            def evaluate(self, decision_type, context):
                calls.append((decision_type, dict(context)))
                return DecisionResult(
                    decision_type=DecisionType.CONTEXT_RELEVANCE.value,
                    decision="KEEP",
                    confidence=0.95,
                    source=Source.JEV.value,
                    scores={"relevance": 0.9},
                )

        worker.history = RecordingHistory()
        worker._decision_engine = RecordingEngine()

        Worker.evaluate_decision(
            worker,
            DecisionType.CONTEXT_RELEVANCE.value,
            {"candidate": "one bounded candidate"},
        )

        self.assertEqual(len(calls), 1)
        sent = calls[0][1]
        self.assertNotIn("issue_context", sent)
        self.assertEqual(len(sent["summary"]), 1200)
        self.assertNotIn("LATE_ISSUE_ONLY_CONTEXT", sent["summary"])


if __name__ == "__main__":
    unittest.main()
