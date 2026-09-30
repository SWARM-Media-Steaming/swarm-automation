"""Issue #337: shell/Python comments inside fenced code are not issue headings.

Reproduction steps and acceptance criteria routinely embed fenced commands whose
``# comment`` lines look like Markdown headings. Content that follows such a
block, deep in a long description, must still reach Jev.
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


class FencedCodeTests(unittest.TestCase):
    def assert_kept(self, section: str, *markers: str) -> None:
        body = long_body(section)
        package = build_issue_context(body, SETTINGS)
        self.assertTrue(package["metadata"]["truncated"])
        edges = body[:400] + body[-400:]
        for marker in markers:
            self.assertNotIn(marker, edges)
            self.assertIn(marker, package["text"], f"{marker} lost after a fenced code block")

    def test_acceptance_after_shell_comment_in_fence_is_retained(self) -> None:
        self.assert_kept(
            "## Acceptance criteria\n- FIRST_AC_MARK\n```bash\n# prepare workspace\nnpm ci\n```\n- LATE_AC_MARK must hold",
            "FIRST_AC_MARK",
            "LATE_AC_MARK",
        )

    def test_reproduction_steps_after_comment_in_fence_are_retained(self) -> None:
        self.assert_kept(
            "## Reproduction steps\n```bash\n# start the worker\n./run.sh\n```\nREPRO_AFTER_FENCE_MARK",
            "REPRO_AFTER_FENCE_MARK",
        )

    def test_security_details_after_comment_in_fence_are_retained(self) -> None:
        self.assert_kept(
            "## Security implications\n~~~python\n# never log tokens\nprint('x')\n~~~\nSEC_AFTER_FENCE_MARK",
            "SEC_AFTER_FENCE_MARK",
        )


if __name__ == "__main__":
    unittest.main()
