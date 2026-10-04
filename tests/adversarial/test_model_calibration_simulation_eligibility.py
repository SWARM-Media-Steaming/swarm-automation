"""Issue #374 migration of #205 simulation eligibility coverage.

Simulation may describe a regression but cannot gate a validated activation.
Every simulated route must still refer to a routable model in the publication.
"""

from unittest import mock

from calibration_uat_fixture import CalibrationUAT, NOW, calibration, model_entry


class SimulationEligibilityTests(CalibrationUAT):
    def setUp(self):
        super().setUp()
        self.service.ensure_bootstrap(now=NOW)
        self.local = [
            model_entry(active=False, deprecated=True),
            model_entry("automatic-replacement"),
        ]

    def test_simulated_picks_are_eligible_in_the_activated_catalog(self):
        result = self.service.refresh(source="local", force=True, now=NOW + 1)
        self.assertTrue(result["activated"])
        active = self.service.load_active()
        eligible = {
            row["key"] for row in active["models"]
            if row["status"] in calibration.ROUTABLE_STATUSES
        }
        selected = {
            f'{decision["provider"]}/{decision["model"]}'
            for decision in active["routing"].values() if decision.get("model")
        }
        self.assertLessEqual(selected, eligible)
        self.assertIn("fixture/automatic-replacement", eligible)

    def test_recorded_regression_never_blocks_activation(self):
        regression = {"regression_ok": False, "regressions": ["fixture coverage loss"]}
        with mock.patch.object(calibration, "run_simulation", return_value=regression):
            result = self.service.refresh(source="local", force=True, now=NOW + 1)
        self.assertTrue(result["activated"])
        self.assertTrue(result["diff"]["regression"])
        self.assertEqual(result["simulation"], regression)
        self.assertIn("regression", result["notification"]["message"])
        self.assertEqual(self.service.load_active()["version"], result["calibration_version"])


if __name__ == "__main__":
    import unittest
    unittest.main()
