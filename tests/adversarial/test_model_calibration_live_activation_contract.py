"""Issue #205: activation must govern the real dynamic-router choice path.

Oracle: proposed changes need review, deprecated models are excluded from
routing, and rollback restores the prior usable calibration (AC 6, 16, 17).
Use public source refresh, persistence, activation and resolve_routing_decision;
do not substitute simulation output for the model actually dispatched.
"""

from unittest import mock

from calibration_uat_fixture import CalibrationUAT, NOW, model_entry, router
import dynamic_router


class LiveActivationContractTests(CalibrationUAT):
    def setUp(self):
        super().setUp()
        # Exact supported CLI names, with controlled capability/price fixtures.
        self.retiring = "gpt-5.6-luna"
        self.replacement = "gpt-6-astra"
        self.local = [
            model_entry(name, provider="openai", agent="codex")
            for name in (self.retiring, self.replacement)
        ]
        baseline = self.service.ensure_bootstrap(now=NOW)
        self.baseline_version = baseline["version"]
        env = mock.patch.dict("os.environ", {
            "SWARM_MODEL_CALIBRATION_CATALOG": str(self.service.catalog_override_path),
        })
        env.start()
        self.addCleanup(env.stop)
        # Reference tiers are computed from the active catalog.  The former
        # default_routing_tiers helper was a static table and was deliberately
        # removed; using it here prevented these tests from reaching the
        # activation behavior they are meant to exercise.
        tiers = dynamic_router.derived_routing_tiers(
            "codex", allow_usage_credit_models=True
        )
        self.assertTrue(tiers, "the live calibration fixture must yield routing tiers")
        self.candidate = dynamic_router.RouterCandidate(
            key="codex", name="Codex", tiers=tiers,
        )

    def propose_deprecation(self):
        result = self.remote({"models": [{
            "provider": "openai", "model": self.retiring, "deprecated": True,
        }]}, now=NOW + 1)
        self.assertEqual(result["status"], "changed")
        self.assertFalse(result["activated"])
        return result["calibration_version"]

    def resolve(self, model):
        return dynamic_router.resolve_routing_decision({
            "task_type": "mechanical_edit", "complexity": 2, "risk": "low",
            "context_requirement": "small", "selected_provider": "codex",
            "selected_model": model, "reasoning_effort": "low", "confidence": 0.9,
            "prompt_grade": "A", "grade_reason": "Complete fixture request",
            "complexity_reason": "Small deterministic edit",
        }, [self.candidate], default_provider="codex", router_provider="codex",
            router_model="fixture-router", router_effort="low",
            allow_usage_credit_models=True, allow_tier_fallback=True)

    def test_proposal_does_not_change_the_running_workers_catalog(self):
        self.propose_deprecation()
        self.assertEqual(self.resolve(self.retiring)["selected_model"], self.retiring)
        self.assertEqual(self.service.load_active()["version"], self.baseline_version)

    def test_explicit_router_choice_cannot_bypass_activated_deprecation(self):
        self.service.activate(self.propose_deprecation())
        eligible = {m.model for m in router.load_model_catalog(self.service.catalog_override_path)}
        self.assertEqual(eligible, {self.replacement})
        # Control: the fallback path can already read the activated catalog.
        self.assertEqual(self.resolve("not-in-the-catalog")["selected_model"], self.replacement)
        decision = self.resolve(self.retiring)
        self.assertIn(decision["selected_model"], eligible,
                      "The normal router-choice path dispatched a model excluded by the active calibration")

    def test_rollback_restores_prior_explicit_model_eligibility(self):
        self.service.activate(self.propose_deprecation())
        self.service.activate(self.baseline_version)
        self.assertEqual(self.resolve(self.retiring)["selected_model"], self.retiring)
        self.assertEqual(self.service.status_report()["active_version"], self.baseline_version)
