#!/usr/bin/env python3
"""Provider normalization, cost estimation, and GitHub rendering for
per-prompt AI token usage (issue #280)."""

from __future__ import annotations

import json
import unittest

from token_usage import (
    AgentType,
    NormalizedUsage,
    PromptType,
    UsageRecord,
    estimate_cost,
    format_usage_log_line,
    normalize_claude_usage,
    normalize_codex_usage,
    normalize_grok_usage,
    normalize_usage,
    render_ai_usage_markdown,
)


def _claude_stream(usage: dict, *, extra_events: list[dict] | None = None) -> str:
    lines = [json.dumps({"type": "assistant", "message": {"content": []}})]
    for event in extra_events or []:
        lines.append(json.dumps(event))
    lines.append(json.dumps({"type": "result", "result": "done", "usage": usage}))
    return "\n".join(lines)


class ClaudeUsageTests(unittest.TestCase):
    def test_input_output_and_both_cache_counters_are_captured(self) -> None:
        raw = _claude_stream(
            {
                "input_tokens": 100,
                "output_tokens": 40,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 30,
            }
        )
        usage = normalize_claude_usage(raw)
        assert usage is not None
        self.assertEqual(usage.input_tokens, 100)
        self.assertEqual(usage.output_tokens, 40)
        # Anthropic bills both cache counters *in addition to* input_tokens.
        self.assertEqual(usage.cached_input_tokens, 50)
        self.assertIsNone(usage.reasoning_tokens)
        self.assertEqual(usage.total_tokens, 190)

    def test_missing_cache_fields_default_to_none_not_zero(self) -> None:
        raw = _claude_stream({"input_tokens": 10, "output_tokens": 5})
        usage = normalize_claude_usage(raw)
        assert usage is not None
        self.assertIsNone(usage.cached_input_tokens)
        self.assertEqual(usage.total_tokens, 15)

    def test_single_json_object_without_newlines_is_also_parsed(self) -> None:
        raw = json.dumps({"type": "result", "result": "done", "usage": {"input_tokens": 7, "output_tokens": 3}})
        usage = normalize_claude_usage(raw)
        assert usage is not None
        self.assertEqual(usage.total_tokens, 10)

    def test_last_result_event_wins_when_several_are_present(self) -> None:
        raw = "\n".join(
            [
                json.dumps({"type": "result", "usage": {"input_tokens": 1, "output_tokens": 1}}),
                json.dumps({"type": "result", "usage": {"input_tokens": 99, "output_tokens": 1}}),
            ]
        )
        usage = normalize_claude_usage(raw)
        assert usage is not None
        self.assertEqual(usage.input_tokens, 99)

    def test_no_result_event_returns_none(self) -> None:
        raw = json.dumps({"type": "assistant", "message": {}})
        self.assertIsNone(normalize_claude_usage(raw))

    def test_garbage_input_never_raises(self) -> None:
        self.assertIsNone(normalize_claude_usage("not json at all"))
        self.assertIsNone(normalize_claude_usage(""))


class CodexUsageTests(unittest.TestCase):
    def test_token_count_event_with_nested_details(self) -> None:
        events = [
            {"type": "thread.started", "thread_id": "abc"},
            {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 500,
                        "output_tokens": 120,
                        "total_tokens": 620,
                        "input_tokens_details": {"cached_tokens": 300},
                        "output_tokens_details": {"reasoning_tokens": 40},
                    }
                },
            },
        ]
        raw = "\n".join(json.dumps(event) for event in events)
        usage = normalize_codex_usage(raw)
        assert usage is not None
        self.assertEqual(usage.input_tokens, 500)
        self.assertEqual(usage.output_tokens, 120)
        self.assertEqual(usage.cached_input_tokens, 300)
        self.assertEqual(usage.reasoning_tokens, 40)
        # OpenAI-style totals already include cached/reasoning; use the
        # provider's own reported total rather than re-deriving it.
        self.assertEqual(usage.total_tokens, 620)

    def test_flat_field_names_are_also_accepted(self) -> None:
        events = [
            {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 4,
                        "output_tokens": 6,
                        "reasoning_output_tokens": 2,
                    }
                },
            }
        ]
        raw = "\n".join(json.dumps(event) for event in events)
        usage = normalize_codex_usage(raw)
        assert usage is not None
        self.assertEqual(usage.cached_input_tokens, 4)
        self.assertEqual(usage.reasoning_tokens, 2)
        # No reported total: fall back to input + output (cached/reasoning
        # are already subsets of those, not additional tokens).
        self.assertEqual(usage.total_tokens, 16)

    def test_last_token_count_event_wins(self) -> None:
        events = [
            {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 1, "output_tokens": 1}}},
            {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 50, "output_tokens": 8}}},
        ]
        raw = "\n".join(json.dumps(event) for event in events)
        usage = normalize_codex_usage(raw)
        assert usage is not None
        self.assertEqual(usage.input_tokens, 50)

    def test_no_token_count_event_returns_none(self) -> None:
        raw = json.dumps({"type": "thread.started", "thread_id": "abc"})
        self.assertIsNone(normalize_codex_usage(raw))


class GrokUsageTests(unittest.TestCase):
    def test_usage_key_alongside_text_and_session_id(self) -> None:
        raw = json.dumps(
            {
                "text": "done",
                "sessionId": "s-1",
                "usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 50,
                    "prompt_tokens_details": {"cached_tokens": 80},
                    "completion_tokens_details": {"reasoning_tokens": 12},
                },
            }
        )
        usage = normalize_grok_usage(raw)
        assert usage is not None
        self.assertEqual(usage.input_tokens, 200)
        self.assertEqual(usage.output_tokens, 50)
        self.assertEqual(usage.cached_input_tokens, 80)
        self.assertEqual(usage.reasoning_tokens, 12)
        self.assertEqual(usage.total_tokens, 250)

    def test_no_usage_key_returns_none(self) -> None:
        raw = json.dumps({"text": "done", "sessionId": "s-1"})
        self.assertIsNone(normalize_grok_usage(raw))


class NormalizeUsageDispatchTests(unittest.TestCase):
    def test_dispatches_by_provider_key_case_insensitively(self) -> None:
        raw = _claude_stream({"input_tokens": 1, "output_tokens": 1})
        self.assertIsNotNone(normalize_usage("Claude", raw))
        self.assertIsNotNone(normalize_usage("claude", raw))

    def test_unknown_provider_returns_none(self) -> None:
        self.assertIsNone(normalize_usage("unknown-provider", "{}"))

    def test_empty_raw_text_returns_none(self) -> None:
        self.assertIsNone(normalize_usage("claude", ""))


class EstimateCostTests(unittest.TestCase):
    def test_none_usage_returns_none(self) -> None:
        self.assertIsNone(estimate_cost("claude-sonnet-5", None))

    def test_unknown_model_returns_none_without_raising(self) -> None:
        usage = NormalizedUsage(input_tokens=1000, output_tokens=1000)
        self.assertIsNone(estimate_cost("not-a-real-model", usage))

    def test_higher_rank_model_costs_more_for_the_same_usage(self) -> None:
        usage = NormalizedUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        cheap = estimate_cost("claude-haiku-4-5", usage)  # rank 1
        expensive = estimate_cost("claude-opus-5", usage)  # rank 4
        assert cheap is not None and expensive is not None
        self.assertGreater(expensive, cheap)

    def test_cached_tokens_are_charged_less_than_fresh_input(self) -> None:
        base = NormalizedUsage(input_tokens=1_000_000, output_tokens=0)
        cached = NormalizedUsage(input_tokens=0, output_tokens=0, cached_input_tokens=1_000_000)
        base_cost = estimate_cost("claude-sonnet-5", base)
        cached_cost = estimate_cost("claude-sonnet-5", cached)
        assert base_cost is not None and cached_cost is not None
        self.assertLess(cached_cost, base_cost)


def _record(**overrides) -> dict:
    base = dict(
        id="id-1",
        sequence=1,
        agent_type=AgentType.PRIMARY.value,
        prompt_type=PromptType.INITIAL.value,
        provider="Claude",
        model="claude-sonnet-5",
        reasoning_effort="medium",
        attempt_number=1,
        input_tokens=1000,
        output_tokens=200,
        reasoning_tokens=None,
        cached_input_tokens=300,
        total_tokens=1500,
        estimated_cost=0.01,
        currency="USD",
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:01+00:00",
        duration_ms=1000,
        success=True,
        error_type="",
    )
    base.update(overrides)
    return base


class UsageRecordTests(unittest.TestCase):
    def test_round_trip_through_dict(self) -> None:
        record = UsageRecord(**_record())
        restored = UsageRecord.from_dict(record.to_dict())
        self.assertEqual(record, restored)

    def test_from_dict_ignores_unknown_keys(self) -> None:
        data = _record()
        data["some_future_column"] = "ignored"
        restored = UsageRecord.from_dict(data)
        self.assertEqual(restored.id, "id-1")


class RenderAiUsageMarkdownTests(unittest.TestCase):
    def test_empty_events_render_nothing(self) -> None:
        self.assertEqual(render_ai_usage_markdown([]), "")

    def test_table_includes_agent_labels_and_totals(self) -> None:
        events = [
            _record(
                sequence=1,
                agent_type=AgentType.ROUTER.value,
                prompt_type=PromptType.INITIAL.value,
                provider="Claude",
                model="claude-haiku-4-5",
                input_tokens=1000,
                output_tokens=200,
                cached_input_tokens=0,
                reasoning_tokens=None,
                total_tokens=1200,
                estimated_cost=0.0,
            ),
            _record(
                sequence=2,
                agent_type=AgentType.ADVERSARIAL_UAT.value,
                prompt_type=PromptType.ADVERSARIAL_SCAN.value,
                provider="Codex",
                model="gpt-5.6-terra",
                input_tokens=2000,
                output_tokens=400,
                cached_input_tokens=100,
                reasoning_tokens=50,
                total_tokens=2450,
                estimated_cost=0.05,
            ),
            _record(
                sequence=3,
                agent_type=AgentType.ADVERSARIAL_CYBERSECURITY.value,
                prompt_type=PromptType.REMEDIATION.value,
                provider="Grok",
                model="grok-4.6",
                input_tokens=None,
                output_tokens=None,
                cached_input_tokens=None,
                reasoning_tokens=None,
                total_tokens=None,
                estimated_cost=None,
            ),
        ]
        markdown = render_ai_usage_markdown(events)
        self.assertIn("### AI Usage", markdown)
        self.assertIn("Router", markdown)
        self.assertIn("UAT Adversarial", markdown)
        self.assertIn("Cyber Adversarial", markdown)
        # UAT and cybersecurity must be independently identifiable, never
        # merged under one generic "Adversarial" label (issue #280 item 9).
        self.assertNotIn("| Adversarial |", markdown)
        self.assertIn("3,000", markdown)  # total input across the three rows
        self.assertIn("AI Invocations: 3", markdown)
        self.assertIn("—", markdown)  # the row with no captured usage

    def test_totals_sum_every_row(self) -> None:
        events = [
            _record(input_tokens=100, output_tokens=10, cached_input_tokens=5, total_tokens=115, estimated_cost=0.001),
            _record(input_tokens=200, output_tokens=20, cached_input_tokens=15, total_tokens=235, estimated_cost=0.002),
        ]
        markdown = render_ai_usage_markdown(events)
        self.assertIn("Input: 300", markdown)
        self.assertIn("Cached Input: 20", markdown)
        self.assertIn("Output: 30", markdown)
        self.assertIn("Total Tokens: 350", markdown)
        self.assertIn("AI Invocations: 2", markdown)


class FormatUsageLogLineTests(unittest.TestCase):
    def test_includes_key_fields_and_nulls_missing_usage(self) -> None:
        line = format_usage_log_line(
            issue_number=42,
            agent_type=AgentType.ADVERSARIAL_CYBERSECURITY.value,
            provider="claude",
            model="claude-sonnet-5",
            usage=None,
            cost=None,
        )
        self.assertTrue(line.startswith("AI_USAGE_RECORDED "))
        self.assertIn("issue=42", line)
        self.assertIn("agent=adversarial_cybersecurity", line)
        self.assertIn("input_tokens=NULL", line)
        self.assertIn("cost=NULL", line)

    def test_reports_real_numbers_when_usage_is_present(self) -> None:
        usage = NormalizedUsage(input_tokens=10, output_tokens=5, total_tokens=15)
        line = format_usage_log_line(
            issue_number=1, agent_type="primary", provider="codex", model="gpt-5.6-terra", usage=usage, cost=0.5,
        )
        self.assertIn("input_tokens=10", line)
        self.assertIn("total_tokens=15", line)
        self.assertIn("cost=0.500000", line)


if __name__ == "__main__":
    unittest.main()
