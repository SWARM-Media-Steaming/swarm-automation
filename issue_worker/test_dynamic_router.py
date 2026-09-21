#!/usr/bin/env python3

from __future__ import annotations

import json
import unittest
from unittest import mock

from dynamic_router import (
    RouterError,
    build_router_prompt,
    default_routing_tiers,
    display_model_name,
    fallback_routing_decision,
    format_routing_notice,
    load_routing_tiers,
    parse_router_payload,
    resolve_routing_decision,
    run_provider_router,
)


ORIGINAL_BODY = "Keep this sentence exactly.\nAcceptance: the toggle persists."


def sample_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_type": "debugging",
        "complexity": 7,
        "risk": "medium",
        "context_requirement": "large",
        "selected_model": "gpt-5.6-luna",
        "reasoning_effort": "low",
        "confidence": 0.91,
        "prompt_grade": "B+",
        "grade_reason": "Clear objective and context, but acceptance criteria are incomplete.",
    }
    payload.update(overrides)
    return payload


class DynamicRouterTest(unittest.TestCase):
    def test_parse_accepts_fenced_json(self) -> None:
        raw = "```json\n" + json.dumps(sample_payload()) + "\n```"
        parsed = parse_router_payload(raw)
        self.assertEqual(parsed["prompt_grade"], "B+")

    def test_parse_rejects_prose_without_an_object(self) -> None:
        with self.assertRaises(RouterError):
            parse_router_payload("I would rewrite the issue as follows.")

    def test_complexity_selects_the_configured_tier_not_the_suggestion(self) -> None:
        decision = resolve_routing_decision(
            "codex",
            sample_payload(),
            default_routing_tiers(),
            router_model="gpt-5.6-luna",
            router_effort="low",
        )
        self.assertEqual(decision["selected_model"], "gpt-5.6-sol")
        self.assertEqual(decision["reasoning_effort"], "high")
        self.assertEqual(decision["router_suggested_model"], "gpt-5.6-luna")
        self.assertEqual(decision["prompt_grade"], "B+")
        self.assertEqual(decision["confidence"], 0.91)
        self.assertFalse(decision["fallback"])

    def test_tier_bands_cover_each_provider(self) -> None:
        expectations = {
            ("claude", 2): ("claude-haiku-4-5", "low"),
            ("claude", 5): ("claude-sonnet-5", "medium"),
            ("claude", 8): ("claude-opus-5", "high"),
            ("claude", 10): ("claude-opus-5", "max"),
            ("codex", 1): ("gpt-5.6-luna", "low"),
            ("codex", 6): ("gpt-5.6-terra", "medium"),
            ("codex", 7): ("gpt-5.6-sol", "high"),
            ("codex", 9): ("gpt-6-astra", "xhigh"),
            ("grok", 3): ("grok-4.3", "low"),
            ("grok", 4): ("grok-4.6", "medium"),
            ("grok", 8): ("grok-4.6", "high"),
            ("grok", 10): ("grok-4.6", "xhigh"),
        }
        tiers = default_routing_tiers()
        for (provider, complexity), (model, effort) in expectations.items():
            decision = resolve_routing_decision(
                provider,
                sample_payload(complexity=complexity),
                tiers,
                router_model="router",
                router_effort="low",
            )
            self.assertEqual(decision["selected_model"], model, f"{provider} {complexity}")
            self.assertEqual(decision["reasoning_effort"], effort, f"{provider} {complexity}")

    def test_custom_tiers_replace_the_built_in_mapping(self) -> None:
        raw = json.dumps(
            {
                "codex": [
                    {"min_complexity": 1, "max_complexity": 10, "model": "custom-worker", "effort": "low"}
                ]
            }
        )
        tiers = load_routing_tiers(raw)
        decision = resolve_routing_decision(
            "codex",
            sample_payload(complexity=9),
            tiers,
            router_model="gpt-5.6-luna",
            router_effort="low",
        )
        self.assertEqual(decision["selected_model"], "custom-worker")
        self.assertEqual(tiers["claude"][0].model, "claude-haiku-4-5")

    def test_invalid_grade_is_rejected(self) -> None:
        with self.assertRaises(RouterError):
            resolve_routing_decision(
                "grok",
                sample_payload(prompt_grade="E"),
                default_routing_tiers(),
                router_model="grok-4.3",
                router_effort="low",
            )

    def test_percent_confidence_is_scaled_into_the_unit_interval(self) -> None:
        decision = resolve_routing_decision(
            "grok",
            sample_payload(complexity=3, confidence="91%"),
            default_routing_tiers(),
            router_model="grok-4.3",
            router_effort="low",
        )
        self.assertEqual(decision["confidence"], 0.91)
        self.assertEqual(decision["selected_model"], "grok-4.3")

    def test_prompt_quotes_the_original_issue_and_does_not_rewrite_it(self) -> None:
        prompt = build_router_prompt(
            title="Persist the toggle",
            body=ORIGINAL_BODY,
            labels=["enhancement"],
            provider="codex",
            tiers=default_routing_tiers()["codex"],
        )
        self.assertIn(ORIGINAL_BODY, prompt)
        self.assertIn("Do not rewrite", prompt)
        self.assertNotIn("Rewritten issue", prompt)

    def test_notice_matches_the_ownership_comment_shape(self) -> None:
        decision = resolve_routing_decision(
            "codex",
            sample_payload(),
            default_routing_tiers(),
            router_model="gpt-5.6-luna",
            router_effort="low",
        )
        notice = format_routing_notice(decision)
        self.assertIn("SWARM AI Routing", notice)
        self.assertIn("Prompt Grade: B+", notice)
        self.assertIn("Complexity: 7/10", notice)
        self.assertIn("Selected Model: GPT-5.6 Sol", notice)
        self.assertIn("Reasoning: High", notice)
        self.assertIn("Routing Confidence: 91%", notice)
        self.assertIn("acceptance criteria are incomplete", notice)
        self.assertEqual(display_model_name("claude-haiku-4-5"), "Claude Haiku 4.5")
        self.assertEqual(display_model_name("grok-4.3"), "Grok 4.3")

    def test_fallback_notice_keeps_the_configured_model(self) -> None:
        decision = fallback_routing_decision(
            provider="codex",
            model="gpt-5.6-luna",
            effort="medium",
            reason="router returned no JSON",
            router_model="gpt-5.6-luna",
            router_effort="low",
        )
        notice = format_routing_notice(decision)
        self.assertIn("fell back", notice)
        self.assertIn("Selected Model: GPT-5.6 Luna", notice)
        self.assertIn("Reasoning: Medium", notice)
        self.assertNotIn("Prompt Grade:", notice)

    def test_router_invocation_failure_surfaces_as_router_error(self) -> None:
        with mock.patch("dynamic_router._run", side_effect=RouterError("router timed out")):
            with self.assertRaises(RouterError):
                run_provider_router(
                    provider="claude",
                    bin_path="/usr/bin/true",
                    model="claude-haiku-4-5",
                    effort="low",
                    prompt="grade this",
                    cwd=__import__("pathlib").Path("."),
                )

    def test_malformed_tier_json_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            load_routing_tiers("{")


if __name__ == "__main__":
    unittest.main()
