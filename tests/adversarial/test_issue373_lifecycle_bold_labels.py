from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "issue_worker"))

from adversarial_security import SECURITY_STAGE  # noqa: E402
from adversarial_uat import UAT_STAGE  # noqa: E402
from token_usage import render_ai_usage_markdown  # noqa: E402
from swarm_issue_worker import (  # noqa: E402
    IssueContext,
    ProviderChoice,
    ProviderUsage,
    Worker,
)


class LifecycleCommentHarness:
    """Small local-only harness around the production comment renderers."""

    def __init__(
        self,
        provider: str = "Codex",
        *,
        issue_number: int = 373,
        session_id: str = "session-373",
    ) -> None:
        self.state: dict[str, object] = {}
        self.worker = Worker.__new__(Worker)
        self.worker.issue = IssueContext(
            issue_number,
            "Bold lifecycle labels",
            "",
            [],
            f"https://example.invalid/issues/{issue_number}",
        )
        self.worker.choice = ProviderChoice(
            provider,
            f"{provider.lower()}-test-model",
            "high",
            session_id,
        )
        self.worker.config = SimpleNamespace(
            github_repository="example/swarm",
            ready_label="Ready For Testing",
        )
        self.worker.github = SimpleNamespace(gh=mock.Mock(return_value=""))
        self.worker.start_usage = ProviderUsage(
            0,
            42.5,
            "session 42.5% / week 80% remaining",
        )
        self.worker.routing = None
        self.worker.quota_resume_ready = False
        self.worker.read_state = lambda: self.state
        self.worker.update_state = lambda **values: self.state.update(values)
        self.worker.comments = lambda _issue_number: []
        self.worker.expected_branch = lambda: f"ai/{provider.lower()}/issue-{issue_number}"
        self.worker.usage_snapshot = lambda _provider: {
            "remaining_percent": 42.5,
            "detail": "session 42.5% / week 80% remaining",
        }
        self.worker.load_resume_comments = lambda *_args: []
        self.worker.render_jev_report = lambda: ""

    def posted_body(self) -> str:
        comment_calls = [
            call
            for call in self.worker.github.gh.call_args_list
            if call.args and list(call.args[0][:2]) == ["issue", "comment"]
        ]
        if len(comment_calls) != 1:
            raise AssertionError(f"expected one comment call, got {len(comment_calls)}")
        return str(comment_calls[0].args[2])


class Issue373LifecycleBoldLabelTests(unittest.TestCase):
    @staticmethod
    def assert_bold_label(body: str, label: str, value_pattern: str = r".+") -> None:
        expected = rf"(?m)^- \*\*{re.escape(label)}:\*\* {value_pattern}$"
        if not re.search(expected, body):
            raise AssertionError(f"missing GitHub Markdown label {label!r} in:\n{body}")
        plain = rf"(?m)^- {re.escape(label)}:"
        if re.search(plain, body):
            raise AssertionError(f"found unbolded lifecycle label {label!r} in:\n{body}")

    def test_started_comment_bolds_dynamic_labels_for_every_provider(self) -> None:
        for provider in ("Claude", "Codex", "Grok"):
            with self.subTest(provider=provider):
                harness = LifecycleCommentHarness(provider)
                harness.worker.post_started_comment()
                body = harness.posted_body()

                self.assert_bold_label(body, "Model", rf"`{provider.lower()}-test-model`")
                self.assert_bold_label(
                    body,
                    "Branch",
                    rf"`ai/{provider.lower()}/issue-373`",
                )
                self.assert_bold_label(
                    body,
                    f"{provider} usage remaining",
                    r"42\.5% remaining \(session 42\.5% / week 80% remaining\)",
                )

    def test_resumed_comment_bolds_required_and_optional_labels(self) -> None:
        harness = LifecycleCommentHarness("Grok")
        harness.worker.quota_resume_ready = True
        harness.state.update(
            quota_resumed_at="2026-09-30T12:00:00-05:00",
            session_comment_id=10,
            rerouted_from={
                "from": "Claude old-model (medium)",
                "to": "Grok grok-test-model (high)",
                "reason": "capacity changed",
                "at": "2026-09-30T12:00:00-05:00",
            },
        )
        harness.worker.load_resume_comments = lambda *_args: [
            {"id": 11, "body": "First"},
            {"id": 12, "body": "Second"},
        ]

        harness.worker.post_resumed_comment()
        body = harness.posted_body()

        for label in (
            "Model",
            "Branch",
            "Session",
            "Grok usage remaining",
            "Re-routed",
            "Picking up new comments",
        ):
            self.assert_bold_label(body, label)
        self.assertIn("2 new trusted comments", body)

    def test_quota_pause_bolds_model_and_session_with_malformed_markdown_chars(self) -> None:
        harness = LifecycleCommentHarness("Claude", session_id="session_*_[373]")
        harness.state["quota_comment_posted"] = False

        harness.worker.post_quota_comment()
        body = harness.posted_body()

        self.assert_bold_label(body, "Model", r"`claude-test-model`")
        self.assert_bold_label(body, "Session", r"`session_\*_\[373\]`")

    def test_completion_bolds_all_scoped_labels_and_preserves_ai_output(self) -> None:
        harness = LifecycleCommentHarness("Codex")
        uat_summary = UAT_STAGE.summary_line(
            {"outcome": "resolved_after_n", "round": 2, "tests_added": 3}
        )
        security_summary = SECURITY_STAGE.summary_line(
            {
                "outcome": "clean_first_pass",
                "status": "PASS",
                "tests_added": 1,
                "findings": [],
                "fixed_findings": [],
                "open_findings": [],
                "advisory_findings": [],
                "filed_finding_details": [],
            }
        )
        authored = "## Summary\nModel: this line is authored content and must stay unchanged."
        pending = {
            "ai": "Codex",
            "ai_tool": "Codex",
            "model": "gpt-test",
            "effort": "high",
            "branch_name": "ai/codex/issue-373",
            "pull_request_url": "https://example.invalid/pull/373",
            "commit_sha": "a" * 40,
            "commit_message": "Bold lifecycle labels (#373)",
            "usage_at_start": {"remaining_percent": 80.0, "detail": "week 80%"},
            "usage_at_completion": {"remaining_percent": 72.5, "detail": "week 72.5%"},
            "adversarial_summary": uat_summary + security_summary,
            "ai_usage_report": " ",
            "jev_report": " ",
            "ai_output": authored,
            "work_type": "followup",
        }

        body = harness.worker.render_pending_comment(pending)

        for label in (
            "Model",
            "Effort",
            "Branch",
            "Commit",
            "Codex usage at start",
            "Codex usage at completion",
            "Approx. Codex usage for this issue",
            "Adversarial UAT",
            "Adversarial Cybersecurity",
        ):
            self.assert_bold_label(body, label)
        self.assertIn("Reworked by **Codex**.", body)
        self.assertIn(authored, body)
        self.assertNotIn("**Model:** this line is authored content", body)

    def test_completion_bolds_quota_reset_label_and_handles_missing_branch(self) -> None:
        harness = LifecycleCommentHarness("Claude")
        pending = {
            "ai": "Claude",
            "model": "claude-test",
            "effort": "medium",
            "commit_sha": "b" * 40,
            "commit_message": "Handle reset (#373)",
            "usage_at_start": {"remaining_percent": 5.0},
            "usage_at_completion": {"remaining_percent": 95.0},
            "ai_usage_report": " ",
            "jev_report": " ",
            "ai_output": "done",
        }

        body = harness.worker.render_pending_comment(pending)

        self.assert_bold_label(body, "Claude quota window reset during this run")
        self.assertNotIn("- **Branch:**", body)

    def test_completion_ai_usage_totals_bold_every_key_value_label(self) -> None:
        markdown = render_ai_usage_markdown(
            [
                {
                    "id": "usage-1",
                    "sequence": 1,
                    "agent_type": "primary",
                    "provider": "codex",
                    "model": "gpt-test",
                    "prompt_type": "implementation",
                    "reasoning_effort": "high",
                    "attempt_number": 1,
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "reasoning_tokens": 5,
                    "output_tokens": 10,
                    "total_tokens": 115,
                    "estimated_cost": 0.01,
                    "currency": "USD",
                    "started_at": "2026-09-30T12:00:00-05:00",
                    "completed_at": "2026-09-30T12:00:01-05:00",
                    "duration_ms": 1000,
                    "success": True,
                    "error_type": "",
                }
            ]
        )

        for label in (
            "Input",
            "Cached Input",
            "Reasoning",
            "Output",
            "Total Tokens",
            "Estimated Cost",
            "AI Invocations",
        ):
            expected = rf"(?m)^\*\*{re.escape(label)}:\*\* .+$"
            self.assertRegex(markdown, expected)
            self.assertNotRegex(markdown, rf"(?m)^{re.escape(label)}:")

    def test_needs_input_uses_markdown_sections_without_rewriting_ai_text(self) -> None:
        harness = LifecycleCommentHarness("Claude")
        worker = harness.worker
        worker.ensure_label = mock.Mock()
        worker.record_completed = mock.Mock()
        worker.history = SimpleNamespace(note=mock.Mock())
        worker.cleanup_no_code_branch = mock.Mock()
        worker.finish_execution_history = mock.Mock()
        worker.clear_in_progress = mock.Mock()
        authored = (
            "## Action required\nReply with the approved account.\n\n"
            "## Summary\nAccount: keep this AI-authored key:value text literal.\n\n"
            "## Recommendations\nUse a non-secret identifier.\n\n"
            "## Step-by-step guide\n- None.\n\n"
        )

        worker.finalize_needs_input(authored)
        body = harness.posted_body()

        for heading in (
            "## Action required",
            "## Summary",
            "## Recommendations",
            "## Step-by-step guide",
            "## How to resume",
        ):
            self.assertIn(heading, body)
        self.assertIn("Account: keep this AI-authored key:value text literal.", body)
        self.assertNotIn("**Account:**", body)

    def test_question_answer_uses_markdown_sections_without_rewriting_ai_text(self) -> None:
        harness = LifecycleCommentHarness("Grok")
        worker = harness.worker
        worker.record_completed = mock.Mock()
        worker.history = SimpleNamespace(note=mock.Mock())
        worker.cleanup_no_code_branch = mock.Mock()
        worker.finish_execution_history = mock.Mock()
        worker.clear_in_progress = mock.Mock()
        authored = (
            "## Answer\nModel: preserve the answer exactly.\n\n"
            "## Evidence\nRepository source.\n\n"
            "## Recommendations\n- None.\n"
        )

        worker.finalize_question_answer(authored)
        body = harness.posted_body()

        for heading in ("## Answer", "## Evidence", "## Recommendations"):
            self.assertIn(heading, body)
        self.assertIn("Model: preserve the answer exactly.", body)
        self.assertNotIn("**Model:** preserve the answer exactly.", body)


if __name__ == "__main__":
    unittest.main()
