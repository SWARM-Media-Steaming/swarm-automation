"""Deterministic tests for the reusable Dynamic Model Router (issues #195, #198).

No live API calls: every test constructs a RouteRequest/RoutingAvailability
and asserts on model_router.route()'s structured output. Most tests use the
real shipped catalog (skills/model-router/models.yaml) so they also double as
a regression check on that data; a few isolate one scoring dimension with
small hand-built ModelSpec fixtures where the real catalog's models differ on
more than one axis at once.

Run with ``python3 -m unittest test_model_router`` from this directory (not
pytest — see test_swarm_issue_worker.py's module docstring for why).
"""

from __future__ import annotations

import unittest

import model_router as mr


def _fixture_model(
    model: str,
    *,
    capability: int,
    cost: int,
    token_efficiency: int,
    latency: int,
    efforts: tuple[str, ...] = ("medium",),
    strengths: tuple[str, ...] = (),
    benchmarks: dict[str, mr.BenchmarkEntry] | None = None,
    provider: str = "fixture",
    agent: str = "fixture",
) -> mr.ModelSpec:
    return mr.ModelSpec(
        provider=provider,
        agent=agent,
        model=model,
        model_id=None,
        active=True,
        recommended=True,
        deprecated=False,
        superseded_by=None,
        supported_efforts=efforts,
        strengths=frozenset(strengths),
        weaknesses=frozenset(),
        relative_capability=capability,
        relative_cost=cost,
        relative_token_efficiency=token_efficiency,
        relative_latency=latency,
        benchmarks=benchmarks or {},
        benchmark_source=None,
        benchmark_date=None,
        notes="",
    )


def _measured(**fields: float | None) -> mr.BenchmarkEntry:
    return mr.BenchmarkEntry(
        coding_agent_index=fields.get("coding_agent_index"),
        deep_swe=fields.get("deep_swe"),
        terminal_bench=fields.get("terminal_bench"),
        swe_atlas_qna=fields.get("swe_atlas_qna"),
        benchmark_cost_per_task=fields.get("benchmark_cost_per_task"),
        benchmark_tokens_per_task=fields.get("benchmark_tokens_per_task"),
        benchmark_runtime_minutes=fields.get("benchmark_runtime_minutes"),
        data_quality="MEASURED",
    )


class CatalogLoadingTests(unittest.TestCase):
    def test_loads_the_shipped_catalog_and_rules(self) -> None:
        catalog = mr.load_model_catalog()
        rules = mr.load_routing_rules()
        self.assertGreaterEqual(len(catalog), 10)
        self.assertEqual([b.level for b in rules.complexity_bands], list(mr.COMPLEXITY_LEVELS))
        models_by_slug = {model.model for model in catalog}
        # Slugs the rest of the app already invokes providers with
        # (dynamic_router._MODEL_CATALOG) must be present in the router catalog.
        for slug in ("claude-haiku-4-5", "claude-sonnet-5", "claude-fable-5-1", "gpt-5.6-sol", "gpt-6-astra", "grok-4.6"):
            self.assertIn(slug, models_by_slug)


class RealCatalogRoutingTests(unittest.TestCase):
    """One test per issue #195 scenario, against the real shipped catalog."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = mr.load_model_catalog()
        cls.rules = mr.load_routing_rules()

    def _route(self, request: mr.RouteRequest, availability: mr.RoutingAvailability = mr.RoutingAvailability()) -> mr.RoutingDecision:
        return mr.route(request, catalog=self.catalog, rules=self.rules, availability=availability)

    def test_trivial_documentation_edit_picks_the_cheapest_tier(self) -> None:
        decision = self._route(mr.RouteRequest("documentation", "TRIVIAL"))
        self.assertEqual(decision.complexity, "TRIVIAL")
        self.assertEqual(decision.task_type, "documentation")
        self.assertEqual(decision.effort, "low")
        self.assertIn(decision.model, {"claude-haiku-4-5", "gpt-5.6-luna", "grok-4.5"})

    def test_simple_bug_fix_stays_off_frontier_models(self) -> None:
        decision = self._route(mr.RouteRequest("simple_bug_fix", "SIMPLE"))
        self.assertEqual(decision.complexity, "SIMPLE")
        self.assertNotIn(decision.model, {"claude-fable-5-1", "claude-opus-5", "gpt-6-astra", "grok-4.7"})
        self.assertIn(decision.effort, {"low", "medium"})

    def test_standard_feature_lands_on_a_mid_tier_model(self) -> None:
        decision = self._route(mr.RouteRequest("feature", "STANDARD"))
        self.assertEqual(decision.complexity, "STANDARD")
        self.assertNotIn(decision.model, {"claude-fable-5-1", "claude-opus-5", "gpt-6-astra", "grok-4.7"})

    def test_complex_debugging_task_reaches_a_deep_debugging_specialist(self) -> None:
        decision = self._route(mr.RouteRequest("deep_debugging", "COMPLEX"))
        self.assertEqual(decision.complexity, "COMPLEX")
        self.assertEqual(decision.task_type, "deep_debugging")
        self.assertIn(decision.effort, {"high", "xhigh"})
        # deep_debugging is one of Astra's and Fable's declared strengths.
        self.assertIn(decision.model, {"gpt-6-astra", "claude-fable-5-1", "gpt-5.6-sol"})

    def test_terminal_heavy_devops_task_avoids_the_weak_terminal_bench_model(self) -> None:
        decision = self._route(mr.RouteRequest("devops", "STANDARD"))
        self.assertEqual(decision.task_type, "devops")
        # grok-4.6's measured Terminal-Bench score is the weakest in the catalog
        # (0.18); the terminal_heavy benchmark emphasis should not make it the
        # automatic pick once a comparably priced alternative exists, and in no
        # case should it pick something disproportionate to a STANDARD task.
        self.assertNotIn(decision.model, {"claude-fable-5-1", "claude-opus-5", "gpt-6-astra", "grok-4.7"})

    def test_repository_analysis_favors_context_and_repo_comprehension(self) -> None:
        decision = self._route(mr.RouteRequest("repository_analysis", "COMPLEX"))
        self.assertEqual(decision.task_type, "repository_analysis")
        self.assertIn(decision.effort, {"high", "xhigh"})

    def test_large_refactor_reaches_a_capable_but_not_maxed_out_model(self) -> None:
        decision = self._route(mr.RouteRequest("large_refactor", "COMPLEX"))
        self.assertEqual(decision.complexity, "COMPLEX")
        self.assertNotEqual(decision.effort, "max")

    def test_architecture_task_uses_deep_reasoning_emphasis(self) -> None:
        decision = self._route(mr.RouteRequest("architecture", "VERY_COMPLEX"))
        self.assertEqual(decision.complexity, "VERY_COMPLEX")
        self.assertIn(decision.effort, {"xhigh", "max"})
        self.assertIn(decision.model, {"gpt-6-astra", "claude-fable-5-1", "claude-opus-5"})

    def test_extreme_multi_repository_task_reaches_for_a_frontier_model(self) -> None:
        decision = self._route(mr.RouteRequest("multi_repository", "EXTREME"))
        self.assertEqual(decision.complexity, "EXTREME")
        self.assertIn(decision.model, {"gpt-6-astra", "claude-fable-5-1", "grok-4.7"})

    def test_unknown_model_in_availability_is_ignored_not_fatal(self) -> None:
        decision = self._route(
            mr.RouteRequest("documentation", "TRIVIAL"),
            mr.RoutingAvailability(enabled_models=frozenset({"totally-made-up-model-9000", "claude-haiku-4-5"})),
        )
        self.assertEqual(decision.model, "claude-haiku-4-5")

    def test_disabled_provider_is_never_selected(self) -> None:
        decision = self._route(
            mr.RouteRequest("architecture", "VERY_COMPLEX"),
            mr.RoutingAvailability(enabled_agents=frozenset({"codex"})),
        )
        self.assertEqual(decision.agent, "codex")

    def test_disabled_model_is_never_selected(self) -> None:
        decision = self._route(
            mr.RouteRequest("architecture", "VERY_COMPLEX"),
            mr.RoutingAvailability(disabled_models=frozenset({"gpt-6-astra"})),
        )
        self.assertNotEqual(decision.model, "gpt-6-astra")

    def test_unsupported_reasoning_effort_excludes_the_model_for_that_request(self) -> None:
        # gpt-5.6-terra only supports low/medium; a COMPLEX task (effort floor
        # "high") must skip it even though it is the only enabled model besides
        # gpt-5.6-luna, which does support high.
        decision = self._route(
            mr.RouteRequest("complex_feature", "COMPLEX"),
            mr.RoutingAvailability(
                enabled_agents=frozenset({"codex"}),
                disabled_models=frozenset({"gpt-5.6-sol", "gpt-6-astra"}),
            ),
        )
        self.assertEqual(decision.model, "gpt-5.6-luna")
        self.assertEqual(decision.effort, "high")

    def test_frontier_model_overqualification_is_avoided_for_a_trivial_task(self) -> None:
        decision = self._route(mr.RouteRequest("mechanical_edit", "TRIVIAL"))
        self.assertNotIn(decision.model, {"claude-fable-5-1", "claude-opus-5", "gpt-6-astra", "grok-4.7"})
        self.assertEqual(decision.effort, "low")

    def test_max_reasoning_overqualification_is_avoided_when_high_effort_suffices(self) -> None:
        # Only a max-capable model is available, but the task only needs
        # COMPLEX/"high" — the unnecessary_reasoning_penalty must keep the
        # engine from reaching for "max" anyway.
        decision = self._route(
            mr.RouteRequest("complex_feature", "COMPLEX"),
            mr.RoutingAvailability(enabled_models=frozenset({"claude-fable-5-1"})),
        )
        self.assertEqual(decision.model, "claude-fable-5-1")
        self.assertEqual(decision.effort, "high")

    def test_no_eligible_candidate_raises_instead_of_guessing(self) -> None:
        with self.assertRaises(mr.ModelRouterError):
            self._route(
                mr.RouteRequest("documentation", "TRIVIAL"),
                mr.RoutingAvailability(enabled_models=frozenset({"does-not-exist"})),
            )

    def test_unknown_task_type_is_rejected(self) -> None:
        with self.assertRaises(mr.ModelRouterError):
            self._route(mr.RouteRequest("not_a_real_task_type", "STANDARD"))

    def test_unknown_complexity_is_rejected(self) -> None:
        with self.assertRaises(mr.ModelRouterError):
            self._route(mr.RouteRequest("feature", "SUPER_DUPER_COMPLEX"))

    def test_decision_serializes_to_the_documented_shape(self) -> None:
        decision = self._route(mr.RouteRequest("feature", "STANDARD"))
        payload = decision.as_dict()
        for key in (
            "provider",
            "agent",
            "model",
            "effort",
            "complexity",
            "task_type",
            "confidence",
            "reason",
            "alternatives",
            "cost_consideration_enabled",
        ):
            self.assertIn(key, payload)
        self.assertFalse(payload["cost_consideration_enabled"])
        self.assertLessEqual(len(payload["alternatives"]), 2)
        for alt in payload["alternatives"]:
            self.assertIn("provider", alt)
            self.assertIn("model", alt)
            self.assertIn("effort", alt)
            self.assertIn("score", alt)


class SensitivityFlagTests(unittest.TestCase):
    """Isolated fixtures: each pair differs on exactly the flagged dimension
    (plus enough of a spread that the sensitivity_boost multiplier actually
    changes the winner), so these prove the flag has a real, not merely
    theoretical, effect independent of the seeded catalog's own numbers.
    """

    def setUp(self) -> None:
        self.rules = mr.load_routing_rules()

    def test_cost_sensitive_prefers_the_cheaper_model_when_it_flips_the_ranking(self) -> None:
        pricier_but_better_token = _fixture_model("pricier", capability=3, cost=3, token_efficiency=4, latency=3)
        cheap_but_so_so = _fixture_model("cheap", capability=3, cost=1, token_efficiency=1, latency=3)
        catalog = [pricier_but_better_token, cheap_but_so_so]

        default = mr.route(mr.RouteRequest("general_reasoning", "STANDARD"), catalog=catalog, rules=self.rules)
        cost_sensitive = mr.route(
            mr.RouteRequest("general_reasoning", "STANDARD", cost_sensitive=True), catalog=catalog, rules=self.rules
        )
        self.assertEqual(default.model, "pricier")
        self.assertEqual(cost_sensitive.model, "cheap")

    def test_token_sensitive_prefers_the_more_token_efficient_model_when_it_flips_the_ranking(self) -> None:
        cheap_but_wasteful = _fixture_model("cheap", capability=3, cost=1, token_efficiency=2, latency=2)
        pricier_but_efficient = _fixture_model("efficient", capability=3, cost=4, token_efficiency=5, latency=2)
        catalog = [cheap_but_wasteful, pricier_but_efficient]

        default = mr.route(mr.RouteRequest("general_reasoning", "STANDARD"), catalog=catalog, rules=self.rules)
        token_sensitive = mr.route(
            mr.RouteRequest("general_reasoning", "STANDARD", token_sensitive=True), catalog=catalog, rules=self.rules
        )
        self.assertEqual(default.model, "cheap")
        self.assertEqual(token_sensitive.model, "efficient")

    def test_latency_sensitive_prefers_the_faster_model_when_it_flips_the_ranking(self) -> None:
        cheap_but_slow = _fixture_model(
            "cheap",
            capability=3,
            cost=1,
            token_efficiency=3,
            latency=1,
            strengths=("simple_fixes",),
        )
        pricier_but_fast = _fixture_model("fast", capability=3, cost=3, token_efficiency=3, latency=4)
        catalog = [cheap_but_slow, pricier_but_fast]

        default = mr.route(mr.RouteRequest("simple_bug_fix", "STANDARD"), catalog=catalog, rules=self.rules)
        latency_sensitive = mr.route(
            mr.RouteRequest("simple_bug_fix", "STANDARD", latency_sensitive=True), catalog=catalog, rules=self.rules
        )
        self.assertEqual(default.model, "cheap")
        self.assertEqual(latency_sensitive.model, "fast")

    def test_large_context_token_sensitive_task_prefers_the_efficient_model_in_the_real_catalog(self) -> None:
        catalog = mr.load_model_catalog()
        decision = mr.route(
            mr.RouteRequest("multi_repository", "VERY_COMPLEX", token_sensitive=True),
            catalog=catalog,
            rules=self.rules,
        )
        # gpt-6-astra has the best relative_token_efficiency (5) of any model
        # capable enough for VERY_COMPLEX work; a token-sensitive large-context
        # request should not land on something less token efficient.
        winner = next(model for model in catalog if model.model == decision.model)
        self.assertGreaterEqual(winner.relative_token_efficiency, 4)


class OverqualificationPenaltyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = mr.load_routing_rules()

    def test_overqualified_model_loses_to_an_adequately_capable_one(self) -> None:
        adequate = _fixture_model("adequate", capability=3, cost=2, token_efficiency=3, latency=3, efforts=("medium",))
        overqualified = _fixture_model(
            "overqualified", capability=5, cost=5, token_efficiency=3, latency=3, efforts=("medium", "high", "xhigh", "max")
        )
        decision = mr.route(
            mr.RouteRequest("general_reasoning", "STANDARD"), catalog=[adequate, overqualified], rules=self.rules
        )
        self.assertEqual(decision.model, "adequate")

    def test_unnecessary_max_effort_loses_to_the_minimum_sufficient_effort(self) -> None:
        model = _fixture_model(
            "only-option", capability=4, cost=3, token_efficiency=3, latency=3, efforts=("high", "xhigh", "max")
        )
        decision = mr.route(mr.RouteRequest("general_reasoning", "COMPLEX"), catalog=[model], rules=self.rules)
        self.assertEqual(decision.effort, "high")


class CostConsiderationTests(unittest.TestCase):
    """Issue #198: the UI Cost Consideration setting changes scoring, not architecture."""

    def setUp(self) -> None:
        self.rules = mr.load_routing_rules()

    def test_shipped_rules_expose_two_weight_sets_and_the_cost_thresholds(self) -> None:
        self.assertAlmostEqual(self.rules.weights["expected_success"], 0.45)
        self.assertAlmostEqual(self.rules.weights["cost_efficiency"], 0.0)
        self.assertAlmostEqual(self.rules.weights["token_efficiency"], 0.0)
        self.assertAlmostEqual(self.rules.cost_consideration_weights["expected_success"], 0.32)
        self.assertAlmostEqual(self.rules.cost_consideration_weights["cost_efficiency"], 0.10)
        self.assertAlmostEqual(self.rules.cost_consideration_weights["token_efficiency"], 0.08)
        self.assertAlmostEqual(self.rules.minimum_expected_success, 0.80)
        self.assertAlmostEqual(self.rules.cost_optimization_quality_tolerance, 0.03)
        self.assertAlmostEqual(self.rules.cost_consideration_unnecessary_reasoning_multiplier, 2.0)

    def test_same_task_cost_off_picks_the_stronger_model(self) -> None:
        astra, sol = self._astra_and_sol()
        decision = mr.route(
            mr.RouteRequest("deep_debugging", "COMPLEX", cost_consideration_enabled=False),
            catalog=[astra, sol],
            rules=self.rules,
        )
        self.assertEqual(decision.model, "gpt-6-astra")
        self.assertEqual(decision.effort, "high")
        self.assertFalse(decision.cost_consideration_enabled)
        self.assertIn("cost was not considered", decision.reason)

    def test_same_task_cost_on_picks_the_cheaper_capable_model(self) -> None:
        astra, sol = self._astra_and_sol()
        decision = mr.route(
            mr.RouteRequest("deep_debugging", "COMPLEX", cost_consideration_enabled=True),
            catalog=[astra, sol],
            rules=self.rules,
        )
        self.assertEqual(decision.model, "gpt-5.6-sol")
        self.assertEqual(decision.effort, "high")
        self.assertTrue(decision.cost_consideration_enabled)
        self.assertIn("lower estimated cost", decision.reason)
        payload = decision.as_dict()
        self.assertTrue(payload["cost_consideration_enabled"])

    def test_cost_sensitive_alias_enables_the_same_weight_set(self) -> None:
        astra, sol = self._astra_and_sol()
        flagged = mr.route(
            mr.RouteRequest("deep_debugging", "COMPLEX", cost_sensitive=True),
            catalog=[astra, sol],
            rules=self.rules,
        )
        explicit = mr.route(
            mr.RouteRequest("deep_debugging", "COMPLEX", cost_consideration_enabled=True),
            catalog=[astra, sol],
            rules=self.rules,
        )
        self.assertEqual(flagged.model, explicit.model)
        self.assertEqual(flagged.effort, explicit.effort)
        self.assertTrue(flagged.cost_consideration_enabled)

    def test_cost_cannot_override_an_underpowered_model(self) -> None:
        cheap = _fixture_model(
            "haiku",
            capability=1,
            cost=1,
            token_efficiency=5,
            latency=5,
            efforts=("high",),
            strengths=("trivial_tasks",),
        )
        capable = _fixture_model(
            "fable",
            capability=5,
            cost=5,
            token_efficiency=2,
            latency=2,
            efforts=("high",),
            strengths=("deep_debugging", "difficult_coding", "cross_component_reasoning"),
        )
        for cost_on in (False, True):
            decision = mr.route(
                mr.RouteRequest("deep_debugging", "COMPLEX", cost_consideration_enabled=cost_on),
                catalog=[cheap, capable],
                rules=self.rules,
            )
            self.assertEqual(decision.model, "fable", f"cost_consideration_enabled={cost_on}")

    def test_quality_tolerance_rejects_a_large_capability_gap(self) -> None:
        strong = _fixture_model("astra", capability=5, cost=5, token_efficiency=5, latency=3, efforts=("high",))
        far_behind = _fixture_model("luna", capability=2, cost=1, token_efficiency=5, latency=5, efforts=("high",))
        decision = mr.route(
            mr.RouteRequest("general_reasoning", "COMPLEX", cost_consideration_enabled=True),
            catalog=[strong, far_behind],
            rules=self.rules,
        )
        self.assertEqual(decision.model, "astra")

    def test_cost_on_prefers_medium_effort_when_high_is_unnecessary(self) -> None:
        catalog = self._medium_vs_high_catalog()
        availability = mr.RoutingAvailability(enabled_models=frozenset({"only-option"}))
        off = mr.route(
            mr.RouteRequest("architecture", "STANDARD", cost_consideration_enabled=False),
            catalog=catalog,
            rules=self.rules,
            availability=availability,
        )
        on = mr.route(
            mr.RouteRequest("architecture", "STANDARD", cost_consideration_enabled=True),
            catalog=catalog,
            rules=self.rules,
            availability=availability,
        )
        self.assertEqual(off.model, "only-option")
        self.assertEqual(on.model, "only-option")
        self.assertEqual(off.effort, "high")
        self.assertEqual(on.effort, "medium")

    def test_lower_api_price_does_not_beat_worse_token_efficiency(self) -> None:
        wasteful = _fixture_model(
            "cheap-wasteful",
            capability=4,
            cost=2,
            token_efficiency=1,
            latency=3,
            efforts=("high",),
            benchmarks={"high": _measured(benchmark_tokens_per_task=14_300_000)},
        )
        efficient = _fixture_model(
            "pricier-efficient",
            capability=4,
            cost=4,
            token_efficiency=5,
            latency=3,
            efforts=("high",),
            benchmarks={"high": _measured(benchmark_tokens_per_task=3_300_000)},
        )
        decision = mr.route(
            mr.RouteRequest("general_reasoning", "COMPLEX", cost_consideration_enabled=True),
            catalog=[wasteful, efficient],
            rules=self.rules,
        )
        self.assertEqual(decision.model, "pricier-efficient")

    def test_unmeasured_effort_does_not_inherit_measured_token_totals(self) -> None:
        model = _fixture_model(
            "astra",
            capability=5,
            cost=5,
            token_efficiency=1,
            latency=3,
            efforts=("high", "max"),
            benchmarks={"max": _measured(benchmark_tokens_per_task=3_300_000)},
        )
        high_tokens = mr.estimated_tokens_per_task(model, "high")
        max_tokens = mr.estimated_tokens_per_task(model, "max")
        self.assertIsNone(high_tokens)
        self.assertEqual(max_tokens, 3_300_000)
        self.assertIsNone(mr.estimated_dollar_cost(model, "high"))

    def test_disabled_models_remain_excluded_when_cost_consideration_is_on(self) -> None:
        astra, sol = self._astra_and_sol()
        decision = mr.route(
            mr.RouteRequest("deep_debugging", "COMPLEX", cost_consideration_enabled=True),
            catalog=[astra, sol],
            rules=self.rules,
            availability=mr.RoutingAvailability(disabled_models=frozenset({"gpt-5.6-sol"})),
        )
        self.assertEqual(decision.model, "gpt-6-astra")
        with self.assertRaises(mr.ModelRouterError):
            mr.route(
                mr.RouteRequest("deep_debugging", "COMPLEX", cost_consideration_enabled=True),
                catalog=[astra, sol],
                rules=self.rules,
                availability=mr.RoutingAvailability(enabled_agents=frozenset({"claude"})),
            )

    def test_mechanical_extra_cost_weights_do_not_apply_when_cost_is_off(self) -> None:
        cheap = _fixture_model("cheap", capability=3, cost=1, token_efficiency=3, latency=3)
        capable = _fixture_model(
            "capable",
            capability=3,
            cost=5,
            token_efficiency=3,
            latency=3,
            strengths=("documentation", "lightweight_fixes"),
        )
        off = mr.route(
            mr.RouteRequest("documentation", "STANDARD", cost_consideration_enabled=False),
            catalog=[cheap, capable],
            rules=self.rules,
        )
        on = mr.route(
            mr.RouteRequest("documentation", "STANDARD", cost_consideration_enabled=True),
            catalog=[cheap, capable],
            rules=self.rules,
        )
        self.assertEqual(off.model, "capable")
        self.assertEqual(on.model, "cheap")

    def _astra_and_sol(self) -> tuple[mr.ModelSpec, mr.ModelSpec]:
        astra = _fixture_model(
            "gpt-6-astra",
            capability=5,
            cost=5,
            token_efficiency=3,
            latency=3,
            efforts=("high",),
            strengths=("deep_debugging", "difficult_coding", "cross_component_reasoning"),
            provider="openai",
            agent="codex",
        )
        sol = _fixture_model(
            "gpt-5.6-sol",
            capability=4,
            cost=2,
            token_efficiency=4,
            latency=4,
            efforts=("high",),
            strengths=("debugging",),
            provider="openai",
            agent="codex",
        )
        return astra, sol

    def _medium_vs_high_catalog(self) -> list[mr.ModelSpec]:
        return [
            _fixture_model(
                "only-option",
                capability=3,
                cost=3,
                token_efficiency=3,
                latency=3,
                efforts=("medium", "high"),
                strengths=("architecture", "sustained_reasoning"),
                benchmarks={"high": _measured(deep_swe=1.0)},
            ),
            _fixture_model(
                "range-anchor",
                capability=3,
                cost=3,
                token_efficiency=3,
                latency=3,
                efforts=("medium",),
                benchmarks={"medium": _measured(deep_swe=0.0)},
            ),
        ]


if __name__ == "__main__":
    unittest.main()
