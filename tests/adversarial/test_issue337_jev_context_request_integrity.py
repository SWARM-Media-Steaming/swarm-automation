"""Boundary UAT for Issue #337's final Jev request and limit parsing.

The extractor and request builder impose separate bounds.  These tests use only
local deterministic fixtures to verify that their composition cannot silently
discard excerpts which the package metadata says were included.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER = ROOT / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

from decision_engine import MAX_ISSUE_CONTEXT_CHARS, build_jev_request  # noqa: E402
from issue_context import IssueContextSettings, build_issue_context  # noqa: E402
from jev_cli import settings_from_mapping  # noqa: E402


def section(heading: str, marker: str) -> str:
    return f"## {heading}\n{marker} " + ("section detail " * 700) + "\n\n"


class FinalRequestIntegrityTests(unittest.TestCase):
    def test_legal_maximum_limits_do_not_erase_claimed_targeted_excerpts(self) -> None:
        body = (
            section("Requested change", "REQUESTED_CHANGE_MARKER")
            + section("Acceptance criteria", "ACCEPTANCE_MARKER")
            + section("Reproduction steps", "REPRODUCTION_MARKER")
            + section("Security implications", "SECURITY_MARKER")
            + section("Out of scope", "OUT_OF_SCOPE_MARKER")
            + "TRUE_DESCRIPTION_END"
        )
        settings = IssueContextSettings(
            max_raw_chars=200,
            max_summary_chars=24000,
            max_excerpt_chars=24000,
        )
        package = build_issue_context(
            body,
            settings,
            summarizer=lambda _sections, limit: "generated summary " * limit,
        )

        self.assertGreater(
            len(package["text"]),
            MAX_ISSUE_CONTEXT_CHARS,
            "fixture must cross the independent request-builder boundary",
        )
        state, _questions = build_jev_request(
            "task_classification",
            {"title": "Large configured context", "issue_context": package},
        )

        self.assertLessEqual(len(state["summary"]), MAX_ISSUE_CONTEXT_CHARS)
        for excerpt, marker in (
            ("acceptance_criteria", "ACCEPTANCE_MARKER"),
            ("reproduction_steps", "REPRODUCTION_MARKER"),
            ("security", "SECURITY_MARKER"),
            ("out_of_scope", "OUT_OF_SCOPE_MARKER"),
            ("end", "TRUE_DESCRIPTION_END"),
        ):
            with self.subTest(excerpt=excerpt):
                self.assertIn(excerpt, state["issueContext"]["excerpts"])
                self.assertTrue(
                    marker in state["summary"],
                    f"request metadata claims {excerpt!r} was included, but its original excerpt was dropped",
                )

    def test_non_finite_limit_values_fall_back_instead_of_crashing_configuration(self) -> None:
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                settings = settings_from_mapping(
                    {
                        "context_max_raw_chars": value,
                        "context_max_summary_chars": value,
                        "context_max_excerpt_chars": value,
                        "context_summary_timeout_seconds": value,
                        "context_summary_retries": value,
                    }
                )
                self.assertEqual(settings.context_max_raw_chars, 6000)
                self.assertEqual(settings.context_max_summary_chars, 1500)
                self.assertEqual(settings.context_max_excerpt_chars, 3000)
                self.assertEqual(settings.context_summary_timeout_seconds, 5.0)
                self.assertEqual(settings.context_summary_retries, 1)


if __name__ == "__main__":
    unittest.main()
