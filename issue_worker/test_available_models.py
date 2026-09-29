"""Models reported by the provider CLIs reach the routers and the decision engine."""

from __future__ import annotations

import json
import unittest

import available_models
import decision_engine
import dynamic_router
import jev_cli
import model_pricing
import model_router
import swarm_issue_worker

CLAUDE = {
    "claude": [
        {"value": "claude-sonnet-5-5", "label": "Sonnet 5.5", "efforts": ["low", "medium", "high"]},
        {"value": "claude-opus-5-5", "label": "Opus 5.5", "efforts": ["high", "xhigh", "max"]},
        {"value": "claude-sonnet-5"},
        {"value": "claude-opus-5"},
        {"value": "claude-haiku-4-5-20251001"},
        {"value": "claude-opus-4-6"},
    ]
}


def _route(catalog, complexity):
    return model_router.route(
        model_router.RouteRequest(
            task_type="general_reasoning",
            complexity=complexity,
            cost_consideration_enabled=True,
            cost_sensitive=True,
            quality_requirement="normal",
        ),
        catalog=catalog,
        availability=model_router.RoutingAvailability(
            enabled_agents=frozenset({"claude"}), disabled_models=frozenset()
        ),
    )


class AvailableModelsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        available_models.reset()
        self.addCleanup(available_models.reset)


class ConfigureTests(AvailableModelsTestCase):
    def test_accepts_the_apps_json_and_skips_malformed_rows(self) -> None:
        raw = json.dumps({
            "Claude": [{"value": "claude-sonnet-5-5", "requiresUsageCredits": True}, 7, {}, "claude-x"],
            "grok": "not a list",
        })
        self.assertEqual(available_models.configure(raw), 2)
        self.assertEqual(available_models.agents(), ("claude",))
        self.assertTrue(available_models.discovered("claude")[0].requires_usage_credits)

    def test_bad_or_empty_input_clears_discovery(self) -> None:
        available_models.configure(CLAUDE)
        for value in ("", "not json", "[]", None):
            available_models.configure(CLAUDE)
            available_models.configure(value)
            self.assertEqual(available_models.discovered(), ())

    def test_versions_and_families(self) -> None:
        self.assertEqual(available_models.family_and_version("claude-sonnet-5-5"), (("sonnet",), (5, 5)))
        self.assertEqual(available_models.family_and_version("gpt-5.6-luna"), (("luna",), (5, 6)))
        self.assertEqual(available_models.family_and_version("grok-4.7"), ((), (4, 7)))
        self.assertEqual(available_models.canonical("claude-haiku-4-5-20251001"), "claude-haiku-4-5")


class RoutingCatalogTests(AvailableModelsTestCase):
    def test_without_discovery_the_checked_in_catalog_is_unchanged(self) -> None:
        models = {spec.model for spec in model_router.load_model_catalog()}
        self.assertNotIn("claude-sonnet-5-5", models)
        self.assertNotIn("claude-sonnet-5-5", dynamic_router.catalog_model_names(("claude",)))

    def test_a_new_release_is_routable_and_wins_ties_over_its_predecessor(self) -> None:
        available_models.configure(CLAUDE)
        catalog = model_router.load_model_catalog()
        self.assertEqual(_route(catalog, 5).model, "claude-sonnet-5-5")
        self.assertEqual(_route(catalog, 8).model, "claude-opus-5-5")
        self.assertIn("claude-sonnet-5-5", dynamic_router.catalog_model_names(("claude",)))
        self.assertIn("claude-opus-5-5", dynamic_router.catalog_model_names(("claude",)))

    def test_inferred_metadata_is_flagged_and_never_invents_benchmarks(self) -> None:
        available_models.configure(CLAUDE)
        spec = next(s for s in model_router.load_model_catalog() if s.model == "claude-sonnet-5-5")
        peer = next(s for s in model_router.load_model_catalog() if s.model == "claude-sonnet-5")
        self.assertEqual((spec.relative_capability, spec.relative_cost), (peer.relative_capability, peer.relative_cost))
        self.assertEqual(spec.supported_efforts, ("low", "medium", "high"))
        self.assertIn("inferred from claude-sonnet-5", spec.notes)
        for entry in spec.benchmarks.values():
            self.assertEqual(entry.data_quality, "HEURISTIC")
            self.assertIsNone(entry.coding_agent_index)
        self.assertIn("discovered from the claude CLI", dynamic_router.model_description("claude-sonnet-5-5"))

    def test_releases_the_catalog_has_moved_past_are_superseded_not_routed(self) -> None:
        available_models.configure(CLAUDE)
        spec = next(s for s in model_router.load_model_catalog() if s.model == "claude-opus-4-6")
        self.assertTrue(spec.deprecated)
        self.assertEqual(spec.superseded_by, "claude-opus-5")
        self.assertNotIn("claude-opus-4-6", dynamic_router.catalog_model_names(("claude",)))

    def test_a_dated_alias_does_not_duplicate_a_catalogued_model(self) -> None:
        available_models.configure(CLAUDE)
        names = [s.model for s in model_router.load_model_catalog()]
        self.assertFalse([n for n in names if n.startswith("claude-haiku-4-5") and n != "claude-haiku-4-5"])

    def test_a_family_with_no_relative_borrows_the_lightest_models_numbers(self) -> None:
        available_models.configure({"claude": [{"value": "claude-quartz-9"}]})
        spec = next(s for s in model_router.load_model_catalog() if s.model == "claude-quartz-9")
        lightest = min((s for s in model_router.load_model_catalog() if s.agent == "claude" and s.model != "claude-quartz-9"),
                       key=lambda s: (s.relative_capability, s.relative_cost))
        self.assertEqual((spec.relative_capability, spec.relative_cost), (lightest.relative_capability, lightest.relative_cost))
        self.assertFalse(spec.deprecated)

    def test_a_discovered_model_is_not_given_a_guessed_price(self) -> None:
        available_models.configure(CLAUDE)
        estimate = model_pricing.estimate_invocation_cost(
            model="claude-sonnet-5-5", input_tokens=1000, output_tokens=100
        )
        self.assertIsNone(estimate.cost)
        self.assertNotEqual(estimate.status, model_pricing.PRICING_STATUS_PRICED)

    def test_usage_credit_models_stay_filtered(self) -> None:
        available_models.configure({"claude": [{"value": "claude-fable-5-2"}]})
        self.assertNotIn("claude-fable-5-2", dynamic_router.catalog_model_names(("claude",)))
        self.assertIn(
            "claude-fable-5-2",
            dynamic_router.catalog_model_names(("claude",), allow_usage_credit_models=True),
        )


class DecisionEngineTests(AvailableModelsTestCase):
    def test_jev_sees_every_routable_model_and_is_asked_to_pick_one(self) -> None:
        available_models.configure(CLAUDE)
        state, questions = decision_engine.build_jev_request("TASK_CLASSIFICATION", {"title": "t"})
        listed = {item["model"]: item for item in state["availableModels"]["claude"]}
        self.assertEqual(listed["claude-sonnet-5-5"]["status"], "inferred")
        self.assertEqual(listed["claude-sonnet-5"]["status"], "catalogued")
        self.assertEqual(listed["claude-opus-4-6"]["status"], "superseded")
        criteria = questions["recommended_model"]["criteria"]
        self.assertIn("claude/claude-sonnet-5-5", criteria)
        self.assertNotIn("claude/claude-opus-4-6", criteria)

    def test_a_model_pick_is_kept_only_when_it_is_really_routable(self) -> None:
        available_models.configure(CLAUDE)
        self.assertEqual(decision_engine._recommended_model("claude/claude-sonnet-5-5"), "claude/claude-sonnet-5-5")
        for answer in ("claude/claude-made-up", "claude/claude-opus-4-6", "", None):
            self.assertEqual(decision_engine._recommended_model(answer), "")

    def test_response_carries_the_pick_as_advisory_metadata(self) -> None:
        available_models.configure(CLAUDE)
        response = jev_cli.JevResponse(
            answers={
                "task_type": {"type": "choice", "choice": "BUG", "confidence": 0.9},
                "complexity": {"type": "score", "score": 3, "confidence": 0.9},
                "recommended_model": {"type": "choice", "choice": "claude/claude-sonnet-5-5", "confidence": 0.8},
            },
            usage=jev_cli.JevUsage(),
            raw_shape="answers",
            model="jev-test",
        )
        result = decision_engine.interpret_jev_response("TASK_CLASSIFICATION", {}, response)
        self.assertEqual(result.metadata["recommendedModel"], "claude/claude-sonnet-5-5")


class WorkerFlagTests(AvailableModelsTestCase):
    def test_the_flag_configures_discovery_for_the_process(self) -> None:
        args = swarm_issue_worker.build_parser().parse_args(
            ["--available-models", json.dumps(CLAUDE), "--allow-usage-credit-models"]
        )
        swarm_issue_worker.available_models.configure(
            args.available_models, allow_usage_credit_models=args.allow_usage_credit_models
        )
        self.assertIn("claude-sonnet-5-5", [m.value for m in available_models.discovered("claude")])
        self.assertTrue(available_models.allow_usage_credit_models())


if __name__ == "__main__":
    unittest.main()
