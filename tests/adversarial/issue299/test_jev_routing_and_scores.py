"""Issue #299: Jev scores feed the existing router; cost-first stays Swarm-owned.

Jev must not pick the final worker model. Automatic routing prefers the lowest
estimated cost among models that already clear capability and expected-success
gates. Latency may only break a cost tie. Manual selections stay pinned when
Dynamic Model Routing is off. Legacy 'best' configuration is cost-first.
"""

from __future__ import annotations

import unittest
from unittest import mock

from harness import JevWorkerFixture
from dynamic_router import (
    apply_jev_signals_to_decision,
    cost_consideration_enabled,
    normalize_routing_optimization,
    pin_configured_routing_decision,
)
from jev_cli import JevCli, JevError
from model_calibration import _weights_summary, routing_mode_label
from model_router import RouteRequest, route
from test_dynamic_router import candidates, resolve, sample_payload
from test_model_router import _fixture_model


class CostFirstAutomaticRoutingTests(unittest.TestCase):
    def test_any_saved_optimization_mode_is_cost_first(self) -> None:
        for value in ("best", "quality", "cost", "cheapest", "", None):
            self.assertEqual(normalize_routing_optimization(value), "cost")
            self.assertTrue(cost_consideration_enabled(value))

    def test_calibration_example_routes_do_not_keep_a_best_fit_mode(self) -> None:
        self.assertEqual(routing_mode_label("best"), "cost_aware")
        self.assertEqual(routing_mode_label("quality"), "cost_aware")
        self.assertEqual(_weights_summary("best")["cost_efficiency"], "high")
        self.assertEqual(_weights_summary("quality")["cost_efficiency"], "high")

    def test_faster_expensive_model_cannot_beat_cheaper_capable_peer(self) -> None:
        cheap_slow = _fixture_model(
            "cheap-slow",
            capability=3,
            cost=1,
            token_efficiency=3,
            latency=1,
            efforts=("medium",),
        )
        expensive_fast = _fixture_model(
            "expensive-fast",
            capability=3,
            cost=5,
            token_efficiency=3,
            latency=5,
            efforts=("medium",),
        )
        decision = route(
            RouteRequest("general_reasoning", "STANDARD", cost_consideration_enabled=True),
            catalog=[cheap_slow, expensive_fast],
        )
        self.assertEqual(decision.model, "cheap-slow")

    def test_underpowered_cheap_model_is_not_selected(self) -> None:
        cheap = _fixture_model(
            "too-cheap",
            capability=1,
            cost=1,
            token_efficiency=5,
            latency=5,
            efforts=("high",),
        )
        capable = _fixture_model(
            "capable",
            capability=4,
            cost=4,
            token_efficiency=3,
            latency=3,
            efforts=("high",),
        )
        decision = route(
            RouteRequest("architecture", "COMPLEX", cost_consideration_enabled=True),
            catalog=[cheap, capable],
        )
        self.assertEqual(decision.model, "capable")


class JevSignalsDoNotReplaceRouterTests(JevWorkerFixture, unittest.TestCase):
    def test_disabled_attach_keeps_baseline_and_does_not_call_jev(self) -> None:
        self.bind_issue()
        self.enable_history()
        decision = resolve(sample_payload(), "codex")
        with mock.patch.object(JevCli, "ask", side_effect=AssertionError("disabled Jev must not be invoked")):
            updated = self.worker.attach_jev_routing(decision, candidates("codex"))
        jev = updated["jev"]
        self.assertEqual(jev["status"], "disabled")
        self.assertIsNone(jev["jev"])
        self.assertEqual(jev["baseline"]["model"], jev["modified"]["model"])
        self.assertFalse(jev["delta"]["routing_changed"])

    def test_baseline_score_is_preserved_when_jev_signals_are_applied(self) -> None:
        decision = resolve(sample_payload(complexity=4, selected_model="gpt-5.6-terra"), "codex")
        baseline_model = decision["selected_model"]
        baseline_complexity = decision["complexity"]
        payload = {
            "decision": "FEATURE",
            "confidence": 0.94,
            "scores": {"complexity": 0.2, "securityRisk": 0.1},
            "reasonCodes": ["FEATURE"],
            "metadata": {"routerTaskType": "feature"},
            "source": "jev",
            "model": "jev-latest",
        }
        updated = apply_jev_signals_to_decision(
            decision, payload, candidates=candidates("codex"), jev_status="enabled"
        )
        jev = updated["jev"]
        self.assertEqual(jev["baseline"]["complexity"], baseline_complexity)
        self.assertEqual(jev["baseline"]["model"], baseline_model)
        self.assertIsNotNone(jev["jev"])
        self.assertIn("modified", jev)
        self.assertIn("delta", jev)
        self.assertNotEqual(id(jev["baseline"]), id(jev["modified"]))

    def test_unavailable_jev_does_not_overwrite_the_baseline_with_a_zero_score(self) -> None:
        self.bind_issue()
        self.enable_history()
        self.enable_jev()
        self.worker._decision_engine = None
        decision = resolve(sample_payload(), "claude")
        with mock.patch.object(JevCli, "ask", side_effect=JevError("missing binary", error_type="not_installed")):
            with mock.patch("jev_cli.discover_jev_bin", return_value=""):
                updated = self.worker.attach_jev_routing(decision, candidates("claude"))
        jev = updated["jev"]
        self.assertIn(jev["status"], {"unavailable", "fallback"})
        self.assertIsNone(jev["jev"])
        self.assertEqual(jev["baseline"]["normalized_score"], jev["modified"]["normalized_score"])

    def test_dynamic_routing_off_keeps_the_operators_model(self) -> None:
        self.bind_issue()
        self.assertFalse(self.worker.config.dynamic_model_routing)
        decision = resolve(sample_payload(selected_model="gpt-5.6-sol"), "codex")
        payload = {
            "decision": "ARCHITECTURE_REFACTOR",
            "confidence": 0.96,
            "scores": {"complexity": 0.9, "securityRisk": 0.2},
            "metadata": {"routerTaskType": "architecture"},
            "source": "jev",
        }
        combined = apply_jev_signals_to_decision(
            decision,
            payload,
            candidates=candidates("codex"),
            jev_status="enabled",
            apply_model_change=False,
        )
        pinned = pin_configured_routing_decision(
            combined,
            provider="codex",
            provider_name="Codex",
            model="gpt-5.6-luna",
            effort="medium",
        )
        self.assertEqual(pinned["selected_model"], "gpt-5.6-luna")
        self.assertEqual(pinned["reasoning_effort"], "medium")
        self.assertEqual(pinned["model_source"], "configured")

    def test_jev_cannot_route_to_a_disabled_providers_model(self) -> None:
        decision = resolve(sample_payload(selected_provider="claude", selected_model="claude-sonnet-5"), "claude")
        payload = {
            "decision": "FEATURE",
            "confidence": 0.99,
            "scores": {"complexity": 0.4},
            "metadata": {"routerTaskType": "feature"},
            "source": "jev",
        }
        only_claude = candidates("claude")
        updated = apply_jev_signals_to_decision(
            decision, payload, candidates=only_claude, jev_status="enabled"
        )
        self.assertEqual(updated["jev"]["modified"]["provider"], "claude")
        self.assertNotEqual(updated.get("selected_model"), "gpt-5.6-sol")

    def test_typed_preflight_signals_are_stored_separately_from_swarm_grade(self) -> None:
        self.bind_issue()
        self.enable_history()
        self.install_jev_engine(
            lambda kind, context: self.decision(
                decision_type=kind,
                decision="ARCHITECTURE_REFACTOR",
                confidence=0.94,
                scores={
                    "complexity": 0.81,
                    "securityRisk": 0.34,
                    "crossRepoProbability": 0.88,
                    "ambiguity": 0.22,
                },
                metadata={
                    "ragScope": "ORGANIZATION",
                    "uatRecommended": True,
                    "cyberRecommended": False,
                    "routerTaskType": "architecture",
                },
            )
        )
        decision = resolve(sample_payload(complexity=5), "claude")
        updated = self.worker.attach_jev_routing(decision, candidates("claude"))
        jev = updated["jev"]
        self.assertEqual(jev["status"], "enabled")
        self.assertEqual(jev["jev"]["decision"], "ARCHITECTURE_REFACTOR")
        self.assertAlmostEqual(jev["jev"]["scores"]["complexity"], 0.81)
        self.assertEqual(jev["baseline"]["complexity"], 5)
        self.assertIsNotNone(jev["modified"])
