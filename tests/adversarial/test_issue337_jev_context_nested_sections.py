"""Issue #337: sub-headings inside a recognised section must not end it.

Real issue templates nest content (``## Acceptance criteria`` > ``### Functional``).
A long description whose acceptance or security details sit under such
sub-headings, away from the beginning/end, must still reach Jev.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER = ROOT / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

from issue_context import IssueContextSettings, build_issue_context  # noqa: E402

SETTINGS = IssueContextSettings(max_raw_chars=1000, max_summary_chars=400, max_excerpt_chars=1200)
FILLER = "Background prose that is not a decision input. " * 60
CLOSING = "Closing remarks with no requirements. " * 40


def long_body(section: str) -> str:
    return f"## Objective\nChange the router.\n\n## Background\n{FILLER}\n\n{section}\n\n## Notes\n{CLOSING}"


class NestedSectionTests(unittest.TestCase):
    def assert_kept(self, section: str, *markers: str) -> None:
        body = long_body(section)
        package = build_issue_context(body, SETTINGS)
        self.assertTrue(package["metadata"]["truncated"])
        # Guard the fixture: the markers are not in the head/tail edges.
        edges = body[:400] + body[-400:]
        for marker in markers:
            self.assertNotIn(marker, edges)
            self.assertIn(marker, package["text"], f"{marker} lost from a long description")

    def test_acceptance_items_under_subheadings_are_retained(self) -> None:
        self.assert_kept(
            "## Acceptance criteria\nAll of the following:\n### Functional\n- FUNCTIONAL_ITEM_MARK\n"
            "### Non-functional\n- NONFUNCTIONAL_ITEM_MARK",
            "FUNCTIONAL_ITEM_MARK",
            "NONFUNCTIONAL_ITEM_MARK",
        )

    def test_security_details_under_subheadings_are_retained(self) -> None:
        self.assert_kept(
            "## Security implications\nSee below.\n### Token storage\nTOKEN_STORAGE_MARK must stay encrypted.",
            "TOKEN_STORAGE_MARK",
        )

    def test_reproduction_steps_under_subheadings_are_retained(self) -> None:
        self.assert_kept(
            "## Reproduction steps\n### Setup\nSETUP_STEP_MARK\n### Trigger\nTRIGGER_STEP_MARK",
            "SETUP_STEP_MARK",
            "TRIGGER_STEP_MARK",
        )


if __name__ == "__main__":
    unittest.main()
