#!/usr/bin/env python3
"""Routing cap (issue #401): a per-provider ceiling over what routing selects."""

from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import adversarial_core
import ai_execution_history
import dynamic_router
import routing_cap
import swarm_issue_worker as worker

CLAUDE_CAP = {"claude": {"model": "claude-sonnet-5-5", "effort": "high"}}


class CalibrationFree(unittest.TestCase):
    def setUp(self) -> None:
        # The worker environment points at a calibration catalog; use the bundled one.
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        os.environ.pop("SWARM_MODEL_CALIBRATION_CATALOG", None)
        self.addCleanup(patcher.stop)


class ClampTests(CalibrationFree):
    def clamp(self, model, effort, caps=CLAUDE_CAP, provider="claude", **kw):
        return routing_cap.clamp(provider, model, effort, caps, **kw)

    def test_a_dearer_pick_is_replaced_by_the_caps_exact_pair(self):
        record = self.clamp("claude-opus-5-5", "xhigh")
        self.assertTrue(record["applied"])
        self.assertEqual((record["final_model"], record["final_effort"]), ("claude-sonnet-5-5", "high"))
        self.assertEqual((record["router_model"], record["router_effort"]), ("claude-opus-5-5", "xhigh"))
        self.assertGreater(record["router_cost"], record["cap_cost"])
        self.assertEqual(record["final_cost"], record["cap_cost"])

    def test_a_cheaper_pick_stands_even_at_a_higher_effort(self):
        record = self.clamp("claude-haiku-4-5", "xhigh")
        self.assertFalse(record["applied"])
        self.assertEqual((record["final_model"], record["final_effort"]), ("claude-haiku-4-5", "xhigh"))
        self.assertIn("Claude Sonnet 5.5 high not applied (selected Claude Haiku 4.5 xhigh is lower cost)", routing_cap.describe(record))

    def test_the_same_model_at_a_lower_effort_stands(self):
        self.assertFalse(self.clamp("claude-sonnet-5-5", "medium")["applied"])

    def test_the_cap_pair_itself_is_not_reported_as_a_clamp(self):
        self.assertFalse(self.clamp("claude-sonnet-5-5", "high")["applied"])

    def test_the_same_model_at_a_higher_effort_is_clamped_to_the_cap_effort(self):
        record = self.clamp("claude-sonnet-5-5", "xhigh")
        self.assertTrue(record["applied"])
        self.assertEqual(record["final_effort"], "high")

    def test_caps_are_per_provider(self):
        caps = {"codex": {"model": "gpt-5.6-luna", "effort": "low"}}
        self.assertIsNone(self.clamp("claude-opus-5-5", "max", caps))
        self.assertIsNone(self.clamp("claude-opus-5-5", "max", {}))
        self.assertTrue(self.clamp("gpt-6-astra", "max", caps, provider="codex")["applied"])

    def test_describe_matches_the_issue_wording(self):
        line = routing_cap.describe(self.clamp("claude-opus-5-5", "xhigh"))
        self.assertIn("Router selected Claude Opus 5.5 xhigh; capped at Claude Sonnet 5.5 high "
                      "(repository cap) → using Claude Sonnet 5.5 high", line)

    def test_a_cap_below_the_capability_floor_is_recorded_prominently(self):
        record = self.clamp("claude-opus-5-5", "xhigh", requirements={"recommended_capability_floor": 100})
        self.assertTrue(record["applied"])
        gap = record["below_capability_floor"]
        self.assertEqual(gap["floor"], 100)
        self.assertGreater(gap["expected_success_gap"], 0)
        self.assertIn("Capped below capability floor", routing_cap.describe(record))

    def test_unpriced_or_unknown_cap_models_are_rejected(self):
        for caps in ({"claude": {"model": "claude-not-a-model", "effort": "high"}},
                     {"claude": {"model": "claude-sonnet-5-5", "effort": "ultra"}}):
            record = self.clamp("claude-opus-5-5", "xhigh", caps)
            self.assertFalse(record["applied"])
            self.assertTrue(record["invalid"])
            self.assertEqual(record["final_model"], "claude-opus-5-5")
            self.assertIn("not applied", routing_cap.describe(record))

    def test_a_retired_cap_model_is_repaired_to_its_successor(self):
        with mock.patch.object(routing_cap._available_models, "replace_blacklisted",
                               side_effect=lambda m: "claude-sonnet-5-5" if m == "claude-sonnet-5-0" else m):
            record = self.clamp("claude-opus-5-5", "xhigh",
                                {"claude": {"model": "claude-sonnet-5-0", "effort": "high"}})
        self.assertTrue(record["applied"])
        self.assertEqual(record["cap_model"], "claude-sonnet-5-5")
        self.assertEqual(record["repaired_from"], "claude-sonnet-5-0")
        self.assertIn("retired", record["note"])

    def test_parse_caps_ignores_junk_and_half_caps(self):
        raw = json.dumps({"claude": {"model": "m", "effort": "High"}, "codex": {"model": "m"},
                          "other": {"model": "m", "effort": "low"}})
        self.assertEqual(routing_cap.parse_caps(raw), {"claude": {"model": "m", "effort": "high"}})
        for junk in ("", "{", "[]", None, 5):
            self.assertEqual(routing_cap.parse_caps(junk), {})


class UpgradeTests(CalibrationFree):
    def test_a_release_upgrade_never_exceeds_the_cap(self):
        ceiling = routing_cap.upgrade_ceiling("claude", CLAUDE_CAP)
        self.assertIsNotNone(ceiling)
        free = dynamic_router.latest_release("claude", "claude-sonnet-4-5", "high")
        capped = dynamic_router.latest_release("claude", "claude-sonnet-4-5", "high", max_cost=0.0001)
        self.assertIsNone(capped)
        if free:
            within = dynamic_router.latest_release("claude", "claude-sonnet-4-5", "high", max_cost=ceiling)
            if within:
                self.assertLessEqual(routing_cap.route_cost("claude", within.model, "high"), ceiling)

    def test_an_uncapped_provider_has_no_upgrade_ceiling(self):
        self.assertIsNone(routing_cap.upgrade_ceiling("codex", CLAUDE_CAP))


class FakeWorker:
    """Just enough of Worker for the cap hooks."""
    def __init__(self, caps, dynamic=True, key="claude", model="claude-opus-5-5", effort="xhigh"):
        self.config = types.SimpleNamespace(routing_caps=caps, dynamic_model_routing=dynamic,
                                            allow_usage_credit_models=False, dry_run=False)
        self.issue = types.SimpleNamespace(number=401)
        self.choice = worker.ProviderChoice("Claude", model, effort, "sid")


class WorkerCapTests(CalibrationFree):
    def test_the_primary_pick_is_clamped_and_the_router_pick_is_kept(self):
        fake = FakeWorker(CLAUDE_CAP)
        decision = {"selected_model": "claude-opus-5-5", "reasoning_effort": "xhigh"}
        with mock.patch.object(worker, "log") as log:
            self.assertTrue(worker.Worker.apply_routing_cap(fake, decision))
        self.assertEqual((fake.choice.model, fake.choice.effort), ("claude-sonnet-5-5", "high"))
        self.assertEqual((decision["selected_model"], decision["reasoning_effort"]), ("claude-sonnet-5-5", "high"))
        self.assertEqual(decision["routing_cap"]["router_model"], "claude-opus-5-5")
        self.assertEqual(log.call_args_list[0].args[0],
                         "Routing cap for issue #401: capped claude claude-opus-5-5 at xhigh effort to "
                         "claude-sonnet-5-5 at high effort (repository cap).")

    def test_a_cheaper_pick_is_recorded_as_not_applied(self):
        fake = FakeWorker(CLAUDE_CAP, model="claude-haiku-4-5", effort="medium")
        decision = {"selected_model": "claude-haiku-4-5", "reasoning_effort": "medium"}
        with mock.patch.object(worker, "log") as log:
            self.assertFalse(worker.Worker.apply_routing_cap(fake, decision))
        log.assert_not_called()
        self.assertFalse(decision["routing_cap"]["applied"])
        self.assertEqual(fake.choice.model, "claude-haiku-4-5")

    def test_no_caps_leaves_the_decision_untouched(self):
        decision = {"selected_model": "claude-opus-5-5", "reasoning_effort": "xhigh"}
        self.assertFalse(worker.Worker.apply_routing_cap(FakeWorker({}), decision))
        self.assertNotIn("routing_cap", decision)

    def test_the_issue_output_names_the_cap_under_the_routing_decision(self):
        fake = FakeWorker(CLAUDE_CAP)
        decision = {"selected_model": "claude-opus-5-5", "reasoning_effort": "xhigh", "prompt_grade": "B",
                    "complexity": 5, "confidence": 0.9, "provider": "claude", "provider_name": "Claude",
                    "dynamic_model_routing": True, "model_source": "router"}
        with mock.patch.object(worker, "log"):
            worker.Worker.apply_routing_cap(fake, decision)
        notice = dynamic_router.format_routing_notice(decision)
        self.assertIn("**Cap:** Router selected Claude Opus 5.5 xhigh; capped at Claude Sonnet 5.5 high", notice)
        self.assertIn("capped at", dynamic_router.routing_history_message(decision, "Claude", "m", "high")[1])
        self.assertNotIn("**Cap:**", dynamic_router.format_routing_notice(
            {k: v for k, v in decision.items() if k != "routing_cap"}))

    def test_the_history_row_carries_the_cap_fields(self):
        fake = FakeWorker(CLAUDE_CAP)
        decision = {"selected_model": "claude-opus-5-5", "reasoning_effort": "xhigh"}
        with mock.patch.object(worker, "log"):
            worker.Worker.apply_routing_cap(fake, decision)
        with tempfile.TemporaryDirectory() as directory:
            repository = ai_execution_history.ExecutionHistoryRepository(Path(directory) / "h.db")
            execution = repository.create(ai_execution_history.ExecutionStart(
                repository="o/r", issue_number=401, issue_url="", issue_title="t", issue_body="",
                provider="Claude", model="claude-sonnet-5-5", effort="high", branch_name="b",
                application_version="t", routing_decision=decision), "2026-10-05T00:00:00+00:00")
            with repository.connect() as database:
                stored = ai_execution_history.row_to_dict(database.execute(
                    "SELECT * FROM ai_executions WHERE execution_id = ?", (execution,)).fetchone())
        cap = stored["routing_decision"]["routing_cap"]
        for key in ("cap_model", "cap_effort", "cap_cost", "router_model", "router_effort", "router_cost",
                    "applied", "final_model", "final_effort", "final_cost"):
            self.assertIn(key, cap)
        self.assertTrue(cap["applied"])


class StageCapTests(CalibrationFree):
    def cap(self, fake, choice, escalating=False):
        stage = types.SimpleNamespace(label="Adversarial UAT")
        with mock.patch.object(worker, "log") as log:
            result = adversarial_core.AdversarialStageMixin.cap_stage_choice(
                fake, stage, {"epoch": 2}, choice, escalating=escalating)
        return result, log

    def test_escalation_stops_at_the_cap_and_is_stable_across_epochs(self):
        fake = FakeWorker(CLAUDE_CAP)
        choice = worker.ProviderChoice("Claude", "claude-opus-5-5", "max", "sid")
        first, log = self.cap(fake, choice, escalating=True)
        self.assertEqual((first.model, first.effort), ("claude-sonnet-5-5", "high"))
        self.assertTrue(any("blocks further escalation" in c.args[0] for c in log.call_args_list))
        # Every later (ever stronger) escalation lands on the same pair: it terminates.
        for effort in ("xhigh", "max"):
            again, _ = self.cap(fake, worker.ProviderChoice("Claude", "claude-opus-5-5", effort, "sid"), True)
            self.assertEqual((again.model, again.effort), (first.model, first.effort))

    def test_the_tester_floor_cannot_lift_a_choice_above_the_cap(self):
        raised = worker.ProviderChoice("Claude", "claude-opus-5-5", "high", "sid")
        result, _ = self.cap(FakeWorker(CLAUDE_CAP), raised)
        self.assertEqual(result.model, "claude-sonnet-5-5")

    def test_a_cheaper_stage_choice_and_other_providers_are_untouched(self):
        cheap = worker.ProviderChoice("Claude", "claude-haiku-4-5", "medium", "sid")
        self.assertIs(self.cap(FakeWorker(CLAUDE_CAP), cheap)[0], cheap)
        other = worker.ProviderChoice("Codex", "gpt-6-astra", "max", "sid")
        self.assertIs(self.cap(FakeWorker(CLAUDE_CAP, key="codex"), other)[0], other)

    def test_dynamic_routing_off_or_a_started_session_ignores_the_cap(self):
        big = worker.ProviderChoice("Claude", "claude-opus-5-5", "max", "sid")
        self.assertIs(self.cap(FakeWorker(CLAUDE_CAP, dynamic=False), big)[0], big)
        resumed = worker.ProviderChoice("Claude", "claude-opus-5-5", "max", "sid", True)
        self.assertIs(self.cap(FakeWorker(CLAUDE_CAP), resumed)[0], resumed)


if __name__ == "__main__":
    unittest.main()
