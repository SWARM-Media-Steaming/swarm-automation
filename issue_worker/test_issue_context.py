import time
import unittest

from decision_engine import build_jev_request
from issue_context import IssueContextSettings, build_issue_context, issue_context_settings_from

SMALL = IssueContextSettings(max_raw_chars=1000, max_summary_chars=400, max_excerpt_chars=1200)


def long_body(tail):
    return "## Objective\nDo the thing.\n\n## Background\n" + ("filler text. " * 400) + "\n\n" + tail


class IssueContextTests(unittest.TestCase):
    def test_short_issue_sent_whole(self):
        pkg = build_issue_context("## Objective\nSmall change.", SMALL)
        self.assertEqual(pkg["text"], "## Objective\nSmall change.")
        self.assertFalse(pkg["metadata"]["truncated"])
        self.assertTrue(pkg["metadata"]["complete"])

    def test_long_issue_keeps_late_acceptance_and_security(self):
        body = long_body("## Acceptance criteria\n- MUST_KEEP_ACCEPT\n\n## Security\nToken storage matters SEC_MARK\n")
        pkg = build_issue_context(body, SMALL)
        m = pkg["metadata"]
        self.assertTrue(m["truncated"] and m["summarized"] and not m["complete"])
        self.assertIn("MUST_KEEP_ACCEPT", pkg["text"])
        self.assertIn("SEC_MARK", pkg["text"])
        self.assertIn("acceptance_criteria", m["sections"])
        self.assertIn("security", m["excerpts"])
        self.assertIn("beginning", m["excerpts"])
        self.assertIn("end", m["excerpts"])
        self.assertEqual(m["originalLength"], len(body))
        self.assertLessEqual(len(pkg["text"]), 400 + 1200 + 200)

    def test_secret_straddling_cut_is_redacted(self):
        secret = "ghp_" + "a" * 36
        pkg = build_issue_context(long_body(f"## Security\nleaked {secret}\n"), SMALL)
        self.assertNotIn(secret, pkg["text"])

    def test_summarizer_failure_falls_back(self):
        def boom(sections, limit):
            raise RuntimeError("x")
        pkg = build_issue_context(long_body("## Testing\nrun tests\n"), SMALL, summarizer=boom)
        self.assertEqual(pkg["metadata"]["summarySource"], "deterministic_after_summary_failure")
        self.assertTrue(pkg["metadata"]["summarized"])

    def test_summarizer_timeout_falls_back(self):
        cfg = IssueContextSettings(1000, 400, 1200, summary_timeout_seconds=0.5, summary_retries=0)
        start = time.time()
        pkg = build_issue_context(long_body("## Testing\nx\n"), cfg, summarizer=lambda s, n: time.sleep(3) or "late")
        self.assertLess(time.time() - start, 2.5)
        self.assertEqual(pkg["metadata"]["summarySource"], "deterministic_after_summary_failure")

    def test_generated_summary_is_sanitized_and_bounded(self):
        pkg = build_issue_context(
            long_body("## Testing\nx\n"), SMALL, summarizer=lambda s, n: "ghp_" + "b" * 36 + "y" * 2000
        )
        self.assertEqual(pkg["metadata"]["summarySource"], "generated")
        self.assertNotIn("b" * 36, pkg["text"])

    def test_limits_are_clamped(self):
        s = issue_context_settings_from({"context_max_raw_chars": 10**9, "context_summary_retries": 99})
        self.assertLessEqual(s.max_raw_chars, 24000)
        self.assertEqual(s.summary_retries, 3)

    def test_request_carries_context_metadata_and_size_bound(self):
        pkg = build_issue_context(long_body("## Acceptance criteria\n- LATE_AC\n"), SMALL)
        state, _ = build_jev_request("task_classification", {"title": "t", "issue_context": pkg, "summary": "x" * 5})
        self.assertIn("LATE_AC", state["summary"])
        self.assertTrue(state["issueContext"]["truncated"])
        self.assertFalse(state["issueContext"]["complete"])

    def test_without_issue_context_keeps_legacy_cap(self):
        state, _ = build_jev_request("rag_scope", {"summary": "y" * 5000})
        self.assertEqual(len(state["summary"]), 1200)
        self.assertNotIn("issueContext", state)


if __name__ == "__main__":
    unittest.main()
