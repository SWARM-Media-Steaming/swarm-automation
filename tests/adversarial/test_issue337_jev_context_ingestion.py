"""Adversarial acceptance coverage for issue #337's Jev issue context.

Fixtures are local and deterministic: no Jev executable, network, or provider is
used.  The tests exercise the structured request boundary that the CLI receives,
including deliberately small configured limits where fixed-size excerpts are
most likely to violate the operator's budget.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER = ROOT / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

from decision_engine import (  # noqa: E402
    DecisionResult,
    DecisionType,
    Source,
    build_jev_request,
)
from issue_context import IssueContextSettings, build_issue_context  # noqa: E402
from jev_cli import JevSettings  # noqa: E402
from swarm_issue_worker import Worker  # noqa: E402


def long_issue(*, suffix: str, filler_chars: int = 1800) -> str:
    return (
        "## Requested change\nKeep the complete decision context.\n\n"
        + ("background material " * ((filler_chars // 20) + 1))
        + "\n\n"
        + suffix
    )


class CompleteAndStructuredContextTests(unittest.TestCase):
    def test_short_issue_reaches_jev_complete_without_legacy_1200_character_cut(self) -> None:
        body = "S" * 1300 + " END_OF_COMPLETE_DESCRIPTION"
        package = build_issue_context(
            body,
            IssueContextSettings(
                max_raw_chars=2000,
                max_summary_chars=300,
                max_excerpt_chars=500,
            ),
        )
        state, _questions = build_jev_request(
            "task_classification", {"title": "Short issue", "issue_context": package}
        )

        self.assertEqual(state["summary"], body)
        self.assertTrue(state["issueContext"]["complete"])
        self.assertFalse(state["issueContext"]["truncated"])

    def test_long_issue_retains_late_acceptance_and_security_original_excerpts(self) -> None:
        body = long_issue(
            suffix=(
                "## Acceptance criteria\n- ACCEPTANCE_AT_THE_END must be enforced.\n\n"
                "## Security implications\n- SECURITY_AT_THE_END credentials stay redacted.\n"
            )
        )
        package = build_issue_context(
            body,
            IssueContextSettings(
                max_raw_chars=300,
                max_summary_chars=500,
                max_excerpt_chars=1600,
            ),
        )
        state, _questions = build_jev_request(
            "issue_triage", {"title": "Long issue", "issue_context": package}
        )

        self.assertIn("ACCEPTANCE_AT_THE_END", state["summary"])
        self.assertIn("SECURITY_AT_THE_END", state["summary"])
        self.assertIn("acceptance_criteria", state["issueContext"]["sections"])
        self.assertIn("security", state["issueContext"]["sections"])
        self.assertIn("acceptance_criteria", state["issueContext"]["excerpts"])
        self.assertIn("security", state["issueContext"]["excerpts"])
        self.assertTrue(state["issueContext"]["summarized"])
        self.assertFalse(state["issueContext"]["complete"])

    def test_legacy_rag_request_remains_bounded_and_has_no_issue_context(self) -> None:
        state, _questions = build_jev_request(
            "context_relevance",
            {"summary": "R" * 5000, "candidate": {"title": "candidate"}},
        )

        self.assertEqual(len(state["summary"]), 1200)
        self.assertNotIn("issueContext", state)


class ConfigurationAndBudgetContractTests(unittest.TestCase):
    def test_jev_state_identifies_the_context_configuration_used_for_its_decision(self) -> None:
        """The request, not only an internal return value, must describe its limits."""
        settings = IssueContextSettings(
            max_raw_chars=321,
            max_summary_chars=234,
            max_excerpt_chars=567,
            summary_timeout_seconds=1.5,
            summary_retries=2,
        )
        package = build_issue_context(
            long_issue(suffix="## Testing requirements\nRun deterministic UAT.\n"),
            settings,
        )
        state, _questions = build_jev_request(
            "task_classification", {"title": "Configured issue", "issue_context": package}
        )
        metadata = state["issueContext"]

        self.assertEqual(
            metadata.get("limits"),
            package["metadata"]["limits"],
            "Jev cannot tell which configured context limits produced this partial request",
        )
        self.assertEqual(metadata.get("summarySource"), "deterministic")
        self.assertEqual(metadata.get("sentLength"), len(state["summary"]))

    def test_maximum_excerpt_size_is_a_real_bound_at_minimum_configuration(self) -> None:
        """Fixed 400-character head/tail reservations must yield to the configured cap."""
        settings = IssueContextSettings(
            max_raw_chars=200,
            max_summary_chars=100,
            max_excerpt_chars=100,
        )
        package = build_issue_context(
            long_issue(
                filler_chars=2400,
                suffix=(
                    "## Acceptance criteria\n- LATE_ACCEPTANCE survives.\n\n"
                    "## Security\n- LATE_SECURITY survives.\n\n"
                    + ("tail padding " * 80)
                    + "TRUE_END"
                ),
            ),
            settings,
        )
        text = package["text"]
        summary_prefix, excerpt_text = text.split("[beginning]\n", 1)
        summary_text = summary_prefix.split("[summary]\n", 1)[1].rstrip()

        self.assertLessEqual(len(summary_text), settings.max_summary_chars)
        self.assertLessEqual(
            len(excerpt_text),
            settings.max_excerpt_chars,
            "configured maximum excerpt size was exceeded by fixed head/tail excerpts",
        )
        self.assertIn("TRUE_END", text, "metadata must not claim an end excerpt that was clipped away")


class FailureAndProvenanceTests(unittest.TestCase):
    def test_sanitization_precedes_bounding_and_timeout_uses_deterministic_fallback(self) -> None:
        credential = "ghp_" + ("s" * 36)

        def slow_summarizer(_sections, _limit):
            time.sleep(1.0)
            return credential

        started = time.monotonic()
        package = build_issue_context(
            long_issue(
                suffix=(
                    "## Security\n"
                    f"Never transmit credential {credential}.\n"
                    "## Acceptance criteria\nThe credential is redacted.\n"
                )
            ),
            IssueContextSettings(
                max_raw_chars=300,
                max_summary_chars=300,
                max_excerpt_chars=1200,
                summary_timeout_seconds=0.5,
                summary_retries=0,
            ),
            summarizer=slow_summarizer,
        )

        self.assertLess(time.monotonic() - started, 0.9)
        self.assertEqual(package["metadata"]["summarySource"], "deterministic_after_summary_failure")
        self.assertNotIn(credential, package["text"])
        self.assertIn("[REDACTED]", package["text"])

    def test_low_confidence_partial_context_gets_one_bounded_expansion(self) -> None:
        body = long_issue(suffix="## Acceptance criteria\nExpanded context is still bounded.\n")
        settings = JevSettings(
            enabled=True,
            context_max_raw_chars=300,
            context_max_summary_chars=200,
            context_max_excerpt_chars=400,
        )
        worker = object.__new__(Worker)
        worker.issue = SimpleNamespace(body=body)
        worker.config = SimpleNamespace(jev=settings)
        calls = []

        class RecordingEngine:
            def evaluate(self, decision_type, context):
                calls.append((decision_type, context["issue_context"]))
                return DecisionResult(
                    decision_type=DecisionType.TASK_CLASSIFICATION.value,
                    decision="FEATURE",
                    confidence=0.92,
                    source=Source.JEV.value,
                )

        worker._decision_engine = RecordingEngine()
        payload = {"issue_context": build_issue_context(body, IssueContextSettings(300, 200, 400))}
        original_result = DecisionResult(
            decision_type=DecisionType.TASK_CLASSIFICATION.value,
            decision="UNKNOWN",
            confidence=0.2,
            source=Source.LOW_CONFIDENCE.value,
        )

        result = Worker.expand_partial_context(
            worker, original_result, DecisionType.TASK_CLASSIFICATION.value, payload
        )

        self.assertEqual(result.source, Source.JEV.value)
        self.assertEqual(len(calls), 1)
        expanded = calls[0][1]
        self.assertTrue(expanded["metadata"]["expandedRetry"])
        self.assertEqual(expanded["metadata"]["limits"]["maxRawChars"], 600)
        self.assertLessEqual(len(expanded["text"]), 400 + 800 + 200)

    def test_worker_persists_complete_or_partial_context_provenance_without_raw_body(self) -> None:
        raw_marker = "RAW_BODY_MUST_NOT_BE_PERSISTED"
        body = long_issue(suffix=f"## Acceptance criteria\n{raw_marker}\n")
        worker = object.__new__(Worker)
        worker.issue = SimpleNamespace(number=337, title="Jev context", labels=[], body=body)
        worker.config = SimpleNamespace(
            jev=JevSettings(enabled=True, context_max_raw_chars=300),
            github_repository="example/repository",
            adversarial_uat_enabled=False,
            adversarial_security_enabled=False,
        )

        class RecordingHistory:
            execution_id = "execution-337"

            def __init__(self):
                self.records = []

            def record_jev_decision(self, record):
                self.records.append(record)

        class FixedEngine:
            def evaluate(self, _decision_type, _context):
                return DecisionResult(
                    decision_type=DecisionType.TASK_CLASSIFICATION.value,
                    decision="FEATURE",
                    confidence=0.96,
                    source=Source.JEV.value,
                    input_fingerprint="sanitized-fingerprint",
                )

        worker.history = RecordingHistory()
        worker._decision_engine = FixedEngine()
        outcome = Worker.evaluate_decision(worker, DecisionType.TASK_CLASSIFICATION.value)

        self.assertTrue(outcome["contextMetadata"]["truncated"])
        record = worker.history.records[0]
        persisted_metadata = record.get("context_metadata", record.get("contextMetadata"))
        self.assertIsInstance(
            persisted_metadata,
            dict,
            "the persisted decision does not record whether Jev used complete or partial context",
        )
        self.assertTrue(persisted_metadata["truncated"])
        self.assertNotIn(raw_marker, repr(record))


if __name__ == "__main__":
    unittest.main()
