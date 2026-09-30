"""Issue #337: fenced-code boundaries follow CommonMark, so content after a
fence keeps its section and headings inside code never re-route content."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER = ROOT / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

from issue_context import IssueContextSettings, build_issue_context, extract_sections  # noqa: E402

SETTINGS = IssueContextSettings(max_raw_chars=1000, max_summary_chars=400, max_excerpt_chars=1200)
FILLER = "Background prose that is not a decision input. " * 60
CLOSING = "Closing remarks with no requirements. " * 40
AC = "## Acceptance criteria\n"


class FenceEdgeTests(unittest.TestCase):
    def assert_ac_keeps(self, body: str, marker: str = "AFTER_MARK") -> None:
        self.assertIn(marker, extract_sections(body).get("acceptance_criteria", ""))

    def test_longer_outer_fence_is_not_closed_by_inner_shorter_fence(self) -> None:
        self.assert_ac_keeps(AC + "````md\n```\n# inner comment\n```\n````\nAFTER_MARK")

    def test_tilde_line_inside_backtick_fence_does_not_close_it(self) -> None:
        self.assert_ac_keeps(AC + "```bash\n~~~\n# comment\n```\nAFTER_MARK")

    def test_backtick_line_inside_tilde_fence_does_not_close_it(self) -> None:
        self.assert_ac_keeps(AC + "~~~bash\n```\n# comment\n~~~\nAFTER_MARK")

    def test_closing_fence_with_info_string_does_not_close(self) -> None:
        self.assert_ac_keeps(AC + "```bash\n```python\n# comment\n```\nAFTER_MARK")

    def test_indented_fence_up_to_three_spaces(self) -> None:
        self.assert_ac_keeps(AC + "   ```\n# comment\n   ```\nAFTER_MARK")

    def test_crlf_line_endings(self) -> None:
        self.assert_ac_keeps(AC.replace("\n", "\r\n") + "```bash\r\n# comment\r\n```\r\nAFTER_MARK")

    def test_inline_triple_backticks_are_not_a_fence(self) -> None:
        # ```x``` on one line is inline code; the following "# real" heading is real.
        sections = extract_sections(AC + "```x``` inline\n## Security\nSEC_MARK\n")
        self.assertIn("SEC_MARK", sections.get("security", ""))

    def test_heading_lookalikes_inside_fence_do_not_reroute_content(self) -> None:
        sections = extract_sections(AC + "```\n## Out of scope\nIN_CODE_MARK\n```\nAFTER_MARK")
        self.assertIn("IN_CODE_MARK", sections["acceptance_criteria"])
        self.assertIn("AFTER_MARK", sections["acceptance_criteria"])
        self.assertNotIn("out_of_scope", sections)

    def test_fence_opened_before_any_section_hides_headings(self) -> None:
        sections = extract_sections("```\n## Security\nCODE\n```\n" + AC + "AFTER_MARK")
        self.assertNotIn("security", sections)
        self.assertIn("AFTER_MARK", sections["acceptance_criteria"])

    def test_unclosed_fence_never_raises_and_bounds_output(self) -> None:
        body = f"## Objective\nx\n\n## Background\n{FILLER}\n\n{AC}```bash\n# never closed\nUNCLOSED_MARK\n{CLOSING}"
        package = build_issue_context(body, SETTINGS)
        self.assertTrue(package["metadata"]["truncated"])
        self.assertLessEqual(len(package["text"]), 400 + 1200 + 200)
        self.assertIn("UNCLOSED_MARK", package["text"])

    def test_end_to_end_late_criteria_after_nested_fence(self) -> None:
        body = (
            f"## Objective\nChange it.\n\n## Background\n{FILLER}\n\n"
            f"{AC}````md\n```\n# inner\n```\n````\n- NESTED_LATE_AC\n\n## Notes\n{CLOSING}"
        )
        package = build_issue_context(body, SETTINGS)
        self.assertNotIn("NESTED_LATE_AC", body[:400] + body[-400:])
        self.assertIn("NESTED_LATE_AC", package["text"])


if __name__ == "__main__":
    unittest.main()
