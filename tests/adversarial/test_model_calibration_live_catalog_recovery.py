"""Issue #205 AC 6, 16–17: recovery must preserve actual activated routing.

A healthy compatibility copy of the last activated calibration remains on
disk. Losing/damaging its router publication must not silently route with an
older bundled catalog while AI Configuration reports the newer version.
"""

from unittest import mock

from calibration_uat_fixture import CalibrationUAT, NOW, calibration, model_entry
import dynamic_router


class LiveCatalogRecoveryTests(CalibrationUAT):
    def setUp(self):
        super().setUp()
        clock = mock.patch.object(calibration.time, "time", return_value=NOW + 60)
        clock.start()
        self.addCleanup(clock.stop)
        self.retired = "gpt-5.6-luna"
        self.survivor = "gpt-6-astra"
        self.local = [model_entry(name, provider="openai", agent="codex")
                      for name in (self.retired, self.survivor)]
        self.service.ensure_bootstrap(now=NOW)
        proposal = self.remote({"models": [{
            "provider": "openai", "model": self.retired, "deprecated": True,
        }]}, now=NOW + 1)
        self.service.activate(proposal["calibration_version"])
        self.version = proposal["calibration_version"]
        self.known_good_copy = self.service.active_path.read_bytes()
        env = mock.patch.dict("os.environ", {
            "SWARM_MODEL_CALIBRATION_CATALOG": str(self.service.catalog_override_path),
        })
        env.start()
        self.addCleanup(env.stop)
        self.candidate = dynamic_router.RouterCandidate(
            key="codex", name="Codex",
            tiers=tuple(dynamic_router.default_routing_tiers()["codex"]),
        )
        self.assertEqual(self.resolve()["selected_model"], self.survivor)

    def resolve(self):
        return dynamic_router.resolve_routing_decision({
            "task_type": "mechanical_edit", "complexity": 2, "risk": "low",
            "context_requirement": "small", "selected_provider": "codex",
            "selected_model": self.retired, "reasoning_effort": "low",
            "confidence": 0.9, "prompt_grade": "A",
            "grade_reason": "Complete deterministic request",
            "complexity_reason": "Small fixture edit",
        }, [self.candidate], default_provider="codex", router_provider="codex",
            router_model="fixture-router", router_effort="low",
            allow_usage_credit_models=True, allow_tier_fallback=True)

    def assert_recovered_routing(self):
        # A new status client models an application restart, not cached state.
        status = calibration.ModelCalibrationService(self.service.state_dir).status_report()
        self.assertTrue(status["healthy"])
        self.assertEqual(status["active_version"], self.version)
        self.assertEqual(self.service.active_path.read_bytes(), self.known_good_copy)
        self.assertEqual(
            self.resolve()["selected_model"], self.survivor,
            "Status reports the healthy activated version but the live router resurrected its retired model",
        )

    def test_missing_publication_recovers_last_activated_eligibility(self):
        self.service.catalog_override_path.unlink()
        self.assert_recovered_routing()

    def test_truncated_publication_recovers_last_activated_eligibility(self):
        self.service.catalog_override_path.write_text('{"models":', encoding="utf-8")
        self.assert_recovered_routing()

    def test_unusable_json_publication_recovers_last_activated_eligibility(self):
        self.service.catalog_override_path.write_text('{"models": null}', encoding="utf-8")
        self.assert_recovered_routing()
