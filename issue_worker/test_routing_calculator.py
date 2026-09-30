"""The routing calculator answers with what the worker's routing code would do."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

import available_models
import dynamic_router
import routing_calculator as calc

ALL = ["claude", "codex", "grok"]


def simulate(**inputs):
    return calc.simulate(
        inputs, providers=ALL,
        allow_usage_credit_models=False,
    )


class CalculatorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        available_models.reset()
        self.addCleanup(available_models.reset)


class DescribeTests(CalculatorTestCase):
    def test_describes_every_control_the_dialog_draws(self) -> None:
        info = calc.describe(providers=["claude", "grok"])
        self.assertEqual([p["key"] for p in info["providers"]], ["claude", "grok"])
        self.assertGreaterEqual(len(info["taskTypes"]), 20)
        self.assertTrue(all(t["label"] and "_" not in t["label"] for t in info["taskTypes"]))
        self.assertEqual((info["complexity"]["min"], info["complexity"]["max"]), (1, 10))
        covered = {n for band in info["complexity"]["bands"] for n in range(band["from"], band["to"] + 1)}
        self.assertEqual(covered, set(range(1, 11)), "every complexity value belongs to a band")
        self.assertEqual([r["value"] for r in info["risks"]], ["low", "medium", "high"])
        self.assertIn(info["catalog"]["source"], ("bundled", "calibration"))

    def test_unknown_or_missing_providers_fall_back_to_all_three(self) -> None:
        self.assertEqual([p["key"] for p in calc.describe(providers=[])["providers"]], ALL)
        self.assertEqual([p["key"] for p in calc.describe(providers=["nope"])["providers"]], ALL)


class SimulateTests(CalculatorTestCase):
    def test_matches_the_router_exactly(self) -> None:
        # Parity: the calculator must not be a second implementation.
        result = simulate(taskType="feature", complexity=7, risk="high", provider="claude")
        candidate = dynamic_router.RouterCandidate(
            key="claude", name="Claude", tiers=dynamic_router.derived_routing_tiers("claude"),
            strengths="", usage_remaining=None, excluded_models=(),
        )
        model, effort, _explanation = dynamic_router._scored_tier_decision(
            candidate, 7, "feature", "high", routing_optimization="cost", allow_usage_credit_models=False,
        )
        upgrade = dynamic_router.latest_release("claude", model, effort)
        [row] = result["results"]
        self.assertEqual((row["model"], row["effort"]), (upgrade.model if upgrade else model, effort))

    def test_without_a_chosen_tool_every_enabled_tool_is_answered(self) -> None:
        result = simulate(taskType="feature", complexity=5, risk="medium")
        self.assertEqual([r["provider"] for r in result["results"]], ALL)
        for row in result["results"]:
            self.assertTrue(row["model"] and row["effort"] and row["explanation"] and row["steps"])

    def test_a_chosen_tool_is_answered_alone(self) -> None:
        result = simulate(taskType="feature", complexity=5, risk="medium", provider="codex")
        self.assertEqual([r["provider"] for r in result["results"]], ["codex"])

    def test_higher_complexity_never_lowers_the_capability_of_the_pick(self) -> None:
        easy = simulate(taskType="feature", complexity=2, risk="low", provider="claude")["results"][0]
        hard = simulate(taskType="feature", complexity=10, risk="high", provider="claude")["results"][0]
        self.assertNotEqual((easy["model"], easy["effort"]), (hard["model"], hard["effort"]))

    def test_alternatives_are_ranked_and_capped(self) -> None:
        row = simulate(taskType="feature", complexity=6, risk="medium", provider="claude")["results"][0]
        scores = [a["score"] for a in row["alternatives"]]
        self.assertTrue(0 < len(scores) <= calc.ALTERNATIVES_SHOWN)
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_the_answer_says_which_band_the_complexity_falls_in(self) -> None:
        row = simulate(taskType="feature", complexity=9, risk="low", provider="claude")["results"][0]
        self.assertEqual(row["band"], "Very complex")
        self.assertIn("Very complex", row["steps"][0])

    def test_a_valid_suggested_model_is_honoured_like_the_real_router(self) -> None:
        row = simulate(taskType="feature", complexity=5, risk="medium", provider="claude",
                       suggestedModel="claude-opus-5-5", suggestedEffort="high")["results"][0]
        self.assertEqual(row["source"], "suggested")
        self.assertEqual(row["effort"], "high")
        self.assertTrue(any("used as suggested" in step for step in row["steps"]))

    def test_an_unusable_suggestion_is_ignored_with_a_reason(self) -> None:
        plain = simulate(taskType="feature", complexity=5, risk="medium", provider="claude")["results"][0]
        row = simulate(taskType="feature", complexity=5, risk="medium", provider="claude",
                       suggestedModel="gpt-5.6-sol")["results"][0]
        self.assertEqual((row["model"], row["effort"]), (plain["model"], plain["effort"]))
        self.assertTrue(any("not one Claude can run" in step for step in row["steps"]))

    def test_a_blacklisted_suggestion_is_ignored_like_the_real_router(self) -> None:
        available_models.configure({"claude": [{"value": v} for v in (
            "claude-sonnet-5", "claude-sonnet-5-5", "claude-opus-5", "claude-opus-5-5")]})
        plain = simulate(taskType="feature", complexity=5, risk="medium", provider="claude")["results"][0]
        for blacklisted in ("claude-sonnet-5", "claude-opus-5"):
            row = simulate(taskType="feature", complexity=5, risk="medium", provider="claude",
                           suggestedModel=blacklisted, suggestedEffort="medium")["results"][0]
            self.assertEqual((row["model"], row["effort"]), (plain["model"], plain["effort"]))
            self.assertNotEqual(row["source"], "suggested")
            self.assertTrue(any("not one Claude can run" in step for step in row["steps"]))

    def test_a_task_type_the_router_does_not_know_falls_back_like_the_router(self) -> None:
        result = simulate(taskType="something odd", complexity=4, risk="medium", provider="claude")
        self.assertEqual(result["inputs"]["taskType"], "general_reasoning")


class ValidationTests(CalculatorTestCase):
    def test_bad_inputs_are_explained_not_crashed_on(self) -> None:
        for bad in ({"complexity": 0}, {"complexity": 11}, {"complexity": "abc"}, {"complexity": None}):
            with self.assertRaisesRegex(calc.CalculatorError, "1 to 10"):
                simulate(taskType="feature", risk="medium", **bad)
        with self.assertRaisesRegex(calc.CalculatorError, "Risk"):
            simulate(taskType="feature", complexity=5, risk="extreme")
        with self.assertRaisesRegex(calc.CalculatorError, "not an enabled"):
            simulate(taskType="feature", complexity=5, risk="low", provider="mistral")

    def test_a_tool_that_is_not_enabled_is_rejected(self) -> None:
        with self.assertRaisesRegex(calc.CalculatorError, "not an enabled"):
            calc.simulate({"complexity": 5, "provider": "grok"}, providers=["claude"],
                          allow_usage_credit_models=False)


class CommandLineTests(CalculatorTestCase):
    def run_cli(self, *argv: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = calc.main(list(argv))
        return code, json.loads(buffer.getvalue())

    def test_simulate_prints_json(self) -> None:
        code, payload = self.run_cli(
            "simulate", "--providers", "claude", "--input",
            json.dumps({"taskType": "feature", "complexity": 5, "risk": "medium"}))
        self.assertEqual(code, 0)
        self.assertEqual(payload["results"][0]["provider"], "claude")

    def test_errors_are_json_with_a_non_zero_exit(self) -> None:
        for argv in (
            ("simulate", "--input", "not json"),
            ("simulate", "--input", "[]"),
            ("simulate", "--input", json.dumps({"complexity": 99})),
        ):
            code, payload = self.run_cli(*argv)
            self.assertEqual(code, 1)
            self.assertTrue(payload["error"])

    def test_the_cli_reported_models_are_used(self) -> None:
        code, payload = self.run_cli(
            "simulate", "--providers", "claude", "--available-models",
            json.dumps({"claude": [{"value": "claude-sonnet-5"}, {"value": "claude-sonnet-5-5"}]}),
            "--input", json.dumps({"taskType": "feature", "complexity": 5, "risk": "medium",
                                   "suggestedModel": "claude-sonnet-5"}))
        self.assertEqual(code, 0)
        self.assertEqual(payload["results"][0]["model"], "claude-sonnet-5-5")

    def test_the_usage_credit_setting_widens_what_can_be_chosen(self) -> None:
        args = ("simulate", "--providers", "claude", "--input",
                json.dumps({"taskType": "feature", "complexity": 10, "risk": "high"}))
        _, without = self.run_cli(*args)
        _, allowed = self.run_cli(*args, "--allow-usage-credit-models")
        self.assertNotIn("fable", without["results"][0]["model"])
        self.assertTrue(allowed["results"][0]["model"])


if __name__ == "__main__":
    unittest.main()
