"""Issue #205 refresh steps 4-7, AC 13, 16, 18: retain meaningful inputs.

Representative simulation workloads cannot exhaust every possible request or
provider constraint. Changes to a routable model's supported reasoning levels
and task fit therefore need an activated, diff-visible calibration even when that model is
not picked by the five examples. The oracle is the refreshed model definition,
not the implementation's current list of diff fields.
"""

import copy

from calibration_uat_fixture import CalibrationUAT, NOW, model_entry, router


class RoutingInputFidelityTests(CalibrationUAT):
    def setUp(self):
        super().setUp()
        self.local = [model_entry("alpha"), model_entry("omega")]
        self.baseline = self.service.ensure_bootstrap(now=NOW)
        chosen = {d["model"] for d in self.baseline["routing"].values()}
        self.assertNotIn("omega", chosen, "Fixture needs an eligible model outside the examples")

    def assert_activation_and_round_trip(self, field, value):
        before = self.active_bytes()
        self.local[1][field] = copy.deepcopy(value)
        result = self.service.refresh(source="local", force=True, now=NOW + 1)

        self.assertEqual(
            result["status"], "changed",
            f"Changed {field} on a routable model was discarded because representative picks stayed the same",
        )
        self.assertTrue(result["activated"])
        self.assertNotEqual(self.active_bytes(), before)
        active = self.service.load_active()
        updated = next(m for m in active["models"] if m["model"] == "omega")
        self.assertEqual(updated[field], value)
        self.assertIsNotNone(result["simulation"])

        catalog = router.load_model_catalog(self.service.catalog_override_path)
        live = next(m for m in catalog if m.model == "omega")
        self.assertEqual(set(getattr(live, field)), set(value))
        self.service.activate(self.baseline["version"])
        self.assertEqual(self.active_bytes(), before)

    def test_removed_reasoning_levels_activate_without_a_sample_route_change(self):
        self.assert_activation_and_round_trip("supported_efforts", ["low"])

    def test_changed_task_strengths_activate_without_a_sample_route_change(self):
        # Documentation is a supported task-fit keyword outside the five
        # representative workloads; unlike a made-up tag it affects scoring.
        self.assert_activation_and_round_trip("strengths", ["documentation"])

    def test_changed_task_weaknesses_activate_without_a_sample_route_change(self):
        self.assert_activation_and_round_trip("weaknesses", ["documentation"])

    def test_identical_inputs_remain_a_no_change_refresh(self):
        before = self.active_bytes()
        history = self.service.list_history()
        result = self.service.refresh(source="local", force=True, now=NOW + 1)
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(self.active_bytes(), before)
        self.assertEqual(self.service.list_history(), history)
        self.assertIsNone(self.service.load_proposed())


if __name__ == "__main__":
    import unittest
    unittest.main()
