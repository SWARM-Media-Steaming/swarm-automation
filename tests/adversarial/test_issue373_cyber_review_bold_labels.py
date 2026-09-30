"""Issue #373: nested cybersecurity lifecycle output uses bold key labels.

Oracle derived from the issue before exercising the implementation: all
worker-authored key:value output in a Reworked/Completed comment must render
its label as ``**Label:**``.  The optional Adversarial Cybersecurity line can
expand into a structured review, so that review is part of the lifecycle
comment contract just like its top-level summary line and usage totals.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "issue_worker"))

from adversarial_security import SECURITY_STAGE  # noqa: E402
from swarm_issue_worker import Worker  # noqa: E402


class Issue373CyberReviewBoldLabelTests(unittest.TestCase):
    @staticmethod
    def render_completion(security_summary: str) -> str:
        worker = Worker.__new__(Worker)
        worker.render_jev_report = lambda: ""
        return worker.render_pending_comment(
            {
                "ai": "Codex",
                "model": "gpt-test",
                "effort": "high",
                "commit_sha": "d" * 40,
                "commit_message": "Render cybersecurity review (#373)",
                "adversarial_summary": security_summary,
                "ai_usage_report": " ",
                "jev_report": " ",
                "ai_output": "Completed.",
            }
        )

    def assert_bold_key_value(self, body: str, label: str, *, bullet: bool) -> None:
        prefix = r"- " if bullet else ""
        expected = rf"(?m)^{prefix}\*\*{re.escape(label)}:\*\*\s+\S.*$"
        self.assertRegex(body, expected, f"{label!r} is not a bold Markdown key in:\n{body}")
        self.assertNotRegex(body, rf"(?m)^{prefix}{re.escape(label)}:\s+\S")

    def test_completed_comment_bolds_expanded_security_review_key_values(self) -> None:
        summary = SECURITY_STAGE.summary_line(
            {
                "outcome": "resolved_after_n",
                "status": "FIXED",
                "tests_added": 1,
                "discovered_findings": [
                    {
                        "title": "Unsafe path",
                        "severity": "High",
                        "confidence": "high",
                        "files": ["service.py"],
                    }
                ],
                "fixed_findings": [{"title": "Unsafe path", "severity": "High"}],
                "open_findings": [],
                "advisory_findings": [],
                "filed_finding_details": [
                    {"title": "Legacy weakness", "url": "https://example.invalid/issues/1"}
                ],
                "summary": "The in-scope finding was fixed.",
                "results": [
                    {"id": "adversarial-local-validation", "exit_code": 0, "output": "ok"}
                ],
            }
        )

        body = self.render_completion(summary)

        self.assert_bold_key_value(body, "Status", bullet=False)
        for label in (
            "In-scope discovered",
            "In-scope fixed",
            "In-scope unresolved",
            "Low-confidence advisory",
            "Out-of-scope issues created",
            "Critical",
            "High",
            "Medium",
            "Low",
            "adversarial-local-validation",
        ):
            self.assert_bold_key_value(body, label, bullet=True)

    def test_completed_comment_bolds_security_review_error_key(self) -> None:
        summary = SECURITY_STAGE.summary_line(
            {
                "outcome": "cap_hit",
                "status": "FAILED",
                "review_error": "deterministic fixture failure",
                "tests_added": 0,
                "discovered_findings": [],
                "fixed_findings": [],
                "open_findings": [],
                "advisory_findings": [],
                "filed_finding_details": [],
                "summary": "",
                "results": [],
            }
        )

        body = self.render_completion(summary)

        self.assert_bold_key_value(body, "Status", bullet=False)
        self.assert_bold_key_value(body, "Review error", bullet=False)


if __name__ == "__main__":
    unittest.main()
