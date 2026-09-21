#!/usr/bin/env python3

from __future__ import annotations

import json
import unittest
from unittest import mock

from dynamic_router import (
    REWORK_SAME_PROVIDER_MIN_CONFIDENCE,
    ROUTER_RESPONSE_SCHEMA,
    RouterCandidate,
    RouterError,
    build_router_prompt,
    default_provider_strengths,
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
PROVIDER_NAMES = {"claude": "Claude", "codex": "Codex", "grok": "Grok"}


def candidates(*keys: str, tiers: dict[str, object] | None = None) -> list[RouterCandidate]:
    table = tiers or default_routing_tiers()
    return [
        RouterCandidate(
            key=key,
            name=PROVIDER_NAMES[key],
            tiers=tuple(table[key]),
            strengths=default_provider_strengths(key),
        )
        for key in keys
    ]


def sample_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_type": "debugging",
        "complexity": 7,
        "risk": "medium",
        "context_requirement": "large",
        "selected_provider": "codex",
        "provider_reason": "Codex is best at test-driven bug fixes like this one.",
        "selected_model": "gpt-5.6-luna",
        "reasoning_effort": "low",
        "confidence": 0.91,
        "prompt_grade": "B+",
        "grade_reason": "Clear objective and context, but acceptance criteria are incomplete.",
        "complexity_reason": "Touches the parser and two callers, and needs new regression tests.",
    }
    payload.update(overrides)
    return payload


def resolve(payload, *keys, **kwargs):
    tools = kwargs.pop("candidates", None) or candidates(*keys)
    kwargs.setdefault("default_provider", tools[0].key)
    kwargs.setdefault("router_provider", tools[0].key)
    kwargs.setdefault("router_model", "router-model")
    kwargs.setdefault("router_effort", "low")
    return resolve_routing_decision(payload, tools, **kwargs)


class DynamicRouterTest(unittest.TestCase):
    def test_parse_accepts_fenced_json(self) -> None:
        raw = "```json\n" + json.dumps(sample_payload()) + "\n```"
        parsed = parse_router_payload(raw)
        self.assertEqual(parsed["prompt_grade"], "B+")

    def test_parse_rejects_prose_without_an_object(self) -> None:
        with self.assertRaises(RouterError):
            parse_router_payload("I would rewrite the issue as follows.")

    def test_complexity_selects_the_configured_tier_not_the_suggestion(self) -> None:
        decision = resolve(sample_payload(), "codex")
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
            ("grok", 3): ("grok-4.6", "low"),
            ("grok", 4): ("grok-4.6", "medium"),
            ("grok", 8): ("grok-4.6", "high"),
            ("grok", 10): ("grok-4.6", "xhigh"),
        }
        for (provider, complexity), (model, effort) in expectations.items():
            decision = resolve(
                sample_payload(complexity=complexity, selected_provider=provider),
                provider,
            )
            self.assertEqual(decision["selected_model"], model, f"{provider} {complexity}")
            self.assertEqual(decision["reasoning_effort"], effort, f"{provider} {complexity}")

    def test_router_may_hand_the_issue_to_another_available_tool(self) -> None:
        decision = resolve(
            sample_payload(selected_provider="grok", provider_reason="Grok is fastest here."),
            "claude",
            "grok",
            "codex",
        )
        self.assertEqual(decision["provider"], "grok")
        self.assertEqual(decision["provider_name"], "Grok")
        # Grok's own tier table, not the tool that ran the router.
        self.assertEqual(decision["selected_model"], "grok-4.6")
        self.assertEqual(decision["reasoning_effort"], "high")
        self.assertEqual(decision["provider_reason"], "Grok is fastest here.")
        self.assertEqual(decision["provider_candidates"], ["claude", "grok", "codex"])
        self.assertEqual(decision["router_provider"], "claude")
        self.assertEqual(decision["provider_override_reason"], "")

    def test_a_tool_that_is_not_available_falls_back_to_the_default(self) -> None:
        decision = resolve(sample_payload(selected_provider="grok"), "codex", "claude")
        self.assertEqual(decision["provider"], "codex")
        self.assertIn("not an available AI tool", decision["provider_override_reason"])
        self.assertEqual(decision["selected_model"], "gpt-5.6-sol")

    def test_rework_moves_off_the_previous_tool_without_clear_confidence(self) -> None:
        decision = resolve(
            sample_payload(selected_provider="codex", confidence=0.6),
            "grok",
            "claude",
            "codex",
            previous_provider="codex",
            rework=True,
        )
        self.assertEqual(decision["provider"], "grok")
        self.assertIn("Rework", decision["provider_override_reason"])
        self.assertIn("60% confidence", decision["provider_override_reason"])
        self.assertEqual(decision["selected_model"], "grok-4.6")

    def test_rework_keeps_the_previous_tool_when_the_router_is_sure(self) -> None:
        decision = resolve(
            sample_payload(
                selected_provider="codex",
                confidence=REWORK_SAME_PROVIDER_MIN_CONFIDENCE,
                provider_reason="Only Codex has the failing test reproduced.",
            ),
            "grok",
            "codex",
            previous_provider="codex",
            rework=True,
        )
        self.assertEqual(decision["provider"], "codex")
        self.assertEqual(decision["provider_override_reason"], "")

    def test_first_pass_is_not_second_guessed_by_the_rework_rule(self) -> None:
        decision = resolve(
            sample_payload(selected_provider="codex", confidence=0.4),
            "grok",
            "codex",
            previous_provider="codex",
            rework=False,
        )
        self.assertEqual(decision["provider"], "codex")
        self.assertEqual(decision["provider_override_reason"], "")

    def test_rework_keeps_the_previous_tool_when_it_is_the_only_one_left(self) -> None:
        decision = resolve(
            sample_payload(selected_provider="codex", confidence=0.3),
            "codex",
            previous_provider="codex",
            rework=True,
        )
        self.assertEqual(decision["provider"], "codex")
        self.assertEqual(decision["provider_override_reason"], "")

    def test_custom_tiers_replace_the_built_in_mapping(self) -> None:
        raw = json.dumps(
            {
                "codex": [
                    {"min_complexity": 1, "max_complexity": 10, "model": "custom-worker", "effort": "low"}
                ]
            }
        )
        tiers = load_routing_tiers(raw)
        decision = resolve(
            sample_payload(complexity=9),
            candidates=candidates("codex", tiers=tiers),
        )
        self.assertEqual(decision["selected_model"], "custom-worker")
        self.assertEqual(tiers["claude"][0].model, "claude-haiku-4-5")

    def test_invalid_grade_is_rejected(self) -> None:
        with self.assertRaises(RouterError):
            resolve(sample_payload(prompt_grade="E", selected_provider="grok"), "grok")

    def test_routing_without_any_available_tool_is_an_error(self) -> None:
        with self.assertRaises(RouterError):
            resolve_routing_decision(
                sample_payload(),
                [],
                default_provider="codex",
                router_provider="codex",
                router_model="gpt-5.6-luna",
                router_effort="low",
            )

    def test_percent_confidence_is_scaled_into_the_unit_interval(self) -> None:
        decision = resolve(
            sample_payload(complexity=3, confidence="91%", selected_provider="grok"),
            "grok",
        )
        self.assertEqual(decision["confidence"], 0.91)
        self.assertEqual(decision["selected_model"], "grok-4.6")

    def test_prompt_offers_every_tool_and_does_not_rewrite_the_issue(self) -> None:
        prompt = build_router_prompt(
            title="Persist the toggle",
            body=ORIGINAL_BODY,
            labels=["enhancement"],
            candidates=candidates("codex", "claude", "grok"),
        )
        self.assertIn(ORIGINAL_BODY, prompt)
        self.assertIn("Do not rewrite", prompt)
        self.assertNotIn("Rewritten issue", prompt)
        for key in ("codex", "claude", "grok"):
            self.assertIn(f"- {key} (", prompt)
            self.assertIn(default_provider_strengths(key).split(",")[0], prompt)
        self.assertIn("selected_provider", prompt)
        self.assertIn("gpt-5.6-sol", prompt)
        self.assertNotIn("being reworked", prompt)

    def test_prompt_tells_the_router_to_favor_another_tool_on_a_rework(self) -> None:
        prompt = build_router_prompt(
            title="Second pass",
            body=ORIGINAL_BODY,
            labels=[],
            candidates=candidates("claude", "grok", "codex"),
            previous_provider="codex",
            rework=True,
        )
        self.assertIn("being reworked", prompt)
        self.assertIn("codex completed the previous pass", prompt)
        self.assertIn("Favor a different AI tool", prompt)

    def test_prompt_reports_remaining_usage_for_each_tool(self) -> None:
        tools = candidates("claude", "grok")
        tools[0] = RouterCandidate(
            key=tools[0].key, name=tools[0].name, tiers=tools[0].tiers,
            strengths=tools[0].strengths, usage_remaining=42.0,
        )
        prompt = build_router_prompt(title="t", body="b", labels=[], candidates=tools)
        self.assertIn("usage remaining: 42%", prompt)

    def test_prompt_without_any_tool_is_an_error(self) -> None:
        with self.assertRaises(RouterError):
            build_router_prompt(title="t", body="b", labels=[], candidates=[])

    def test_notice_matches_the_ownership_comment_shape(self) -> None:
        decision = resolve(sample_payload(), "codex", "grok")
        notice = format_routing_notice(decision)
        self.assertIn("SWARM AI Routing", notice)
        self.assertIn("Prompt Grade: B+", notice)
        self.assertIn("Complexity: 7/10", notice)
        self.assertIn("Selected AI: Codex", notice)
        self.assertIn("Selected Model: GPT-5.6 Sol", notice)
        self.assertIn("Reasoning: High", notice)
        self.assertIn("Routing Confidence: 91%", notice)
        self.assertIn("AI Tools Considered: Codex, Grok", notice)
        self.assertIn("Why Codex: Codex is best at test-driven bug fixes", notice)
        self.assertIn("Why this grade (B+): Clear objective and context, but acceptance criteria are incomplete.", notice)
        self.assertIn("How complexity was determined (7/10): Touches the parser and two callers", notice)
        self.assertIn("Complexity 7/10 falls in Codex's 7–8 band, which maps to GPT-5.6 Sol at High reasoning.", notice)
        self.assertEqual(display_model_name("claude-haiku-4-5"), "Claude Haiku 4.5")
        self.assertEqual(display_model_name("grok-4.3"), "Grok 4.3")

    def test_prompt_asks_for_both_explanations(self) -> None:
        prompt = build_router_prompt(title="t", body="b", labels=[], candidates=candidates("codex"))
        self.assertIn("complexity_reason", prompt)
        self.assertIn("exactly why it earned", prompt)
        self.assertIn("complexity_reason", ROUTER_RESPONSE_SCHEMA["required"])

    def test_decision_without_a_complexity_reason_still_explains_the_tier(self) -> None:
        payload = sample_payload()
        del payload["complexity_reason"]
        decision = resolve(payload, "codex")
        self.assertEqual(decision["complexity_reason"], "")
        notice = format_routing_notice(decision)
        self.assertNotIn("How complexity was determined", notice)
        self.assertIn("falls in Codex's 7–8 band", notice)

    def test_notice_reports_when_the_router_was_overruled(self) -> None:
        decision = resolve(
            sample_payload(selected_provider="codex", confidence=0.5),
            "claude",
            "codex",
            previous_provider="codex",
            rework=True,
        )
        notice = format_routing_notice(decision)
        self.assertIn("Selected AI: Claude", notice)
        self.assertIn("Rework: Codex completed the previous pass", notice)

    def test_fallback_notice_keeps_the_configured_model(self) -> None:
        decision = fallback_routing_decision(
            provider="codex",
            provider_name="Codex",
            model="gpt-5.6-luna",
            effort="medium",
            reason="router returned no JSON",
            router_model="gpt-5.6-luna",
            router_effort="low",
        )
        notice = format_routing_notice(decision)
        self.assertIn("fell back", notice)
        self.assertIn("Selected AI: Codex", notice)
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
