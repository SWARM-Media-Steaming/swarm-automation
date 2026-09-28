"""Issue #205 AC 6, 15, 17: safe auto-activation needs honest coverage checks.

If the last routable model is deprecated while an unreviewed model is found,
the latter cannot substitute for it in simulation. Simulating models that
will be excluded from the live catalog can certify a zero-model catalog as
healthy and replace the last known-good calibration.
"""

from calibration_uat_fixture import CalibrationUAT, NOW, model_entry


class SimulationEligibilityTests(CalibrationUAT):
    def setUp(self):
        super().setUp()
        self.service.ensure_bootstrap(now=NOW)
        baseline = self.service.load_active()
        self.assertTrue(all(d["model"] == "established" for d in baseline["routing"].values()))
        self.before = self.active_bytes()
        self.local = [model_entry(active=False, deprecated=True), model_entry("unreviewed-replacement")]

    def test_simulated_picks_are_all_eligible_in_the_proposed_catalog(self):
        result = self.service.refresh(source="local", force=True, now=NOW + 1)
        self.assertEqual(result["status"], "changed")
        proposed = self.service.load_proposed()
        eligible = {m["key"] for m in proposed["models"] if m["status"] in ("ACTIVE", "CANDIDATE")}
        selected = {f'{d["provider"]}/{d["model"]}' for d in proposed["routing"].values() if d.get("model")}
        self.assertLessEqual(selected, eligible, "Simulation selected a model excluded from the proposed live catalog")

    def test_loss_of_all_previously_covered_workloads_is_a_regression(self):
        result = self.service.refresh(source="local", force=True, now=NOW + 1)
        self.assertFalse(result["simulation"]["regression_ok"], "All five previously working routes lost every eligible model")
        self.assertTrue(result["simulation"]["regressions"])

    def test_auto_activation_cannot_replace_working_routes_with_an_empty_catalog(self):
        result = self.service.refresh(source="local", force=True, now=NOW + 1, activation_policy="auto")
        self.assertFalse(result["activated"], "Discovery must not make a coverage regression safe to auto-activate")
        self.assertEqual(self.active_bytes(), self.before)
