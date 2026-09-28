"""Issue #205: authoritative catalog removals are meaningful changes.

The refresh contract detects model-data changes, exposes them for review, and
publishes the reviewed model eligibility.  A model need not be selected by one
of the small representative workload simulations to remain routable elsewhere,
so removing an unselected model from the authoritative local catalog cannot be
collapsed into a no-change refresh.
"""

from calibration_uat_fixture import CalibrationUAT, NOW, calibration, model_entry, router


class CatalogRemovalTests(CalibrationUAT):
    def setUp(self):
        super().setUp()
        self.local = [model_entry("alpha"), model_entry("omega")]
        self.baseline = self.service.ensure_bootstrap(now=NOW)

        routed = {
            decision["model"]
            for decision in self.baseline["routing"].values()
            if decision.get("model")
        }
        all_models = {model["model"] for model in self.baseline["models"]}
        unused = all_models - routed
        self.assertTrue(unused, "Fixture needs an active model outside representative routes")
        self.removed = sorted(unused)[0]
        self.retained = sorted(all_models - {self.removed})
        self.local = [model_entry(name) for name in self.retained]

    def test_removing_an_unselected_active_model_creates_a_reviewable_calibration(self):
        active_before = self.service.load_active()
        result = self.service.refresh(source="local", force=True, now=NOW + 1)

        self.assertEqual(
            result["status"],
            "changed",
            "An active model disappeared from the authoritative catalog but refresh reported no change",
        )
        self.assertFalse(result["activated"], "Manual refresh must preserve the active version")
        self.assertEqual(self.service.load_active()["version"], active_before["version"])
        self.assertIn(self.removed, {model["model"] for model in self.service.load_active()["models"]})

        proposed = self.service.load_proposed()
        self.assertIsNotNone(proposed, "The removed model must be available for review before activation")
        proposed_routable = {
            model["model"]
            for model in proposed["models"]
            if model["status"] in (calibration.STATUS_ACTIVE, calibration.STATUS_CANDIDATE)
        }
        self.assertNotIn(self.removed, proposed_routable)

        self.service.activate(result["calibration_version"])
        published = router.load_model_catalog(self.service.catalog_override_path)
        self.assertNotIn(self.removed, {model.model for model in published})
        self.assertEqual({model.model for model in published}, set(self.retained))

    def test_safe_auto_activation_applies_a_non_regressing_catalog_removal(self):
        result = self.service.refresh(
            source="local", force=True, now=NOW + 1, activation_policy="auto"
        )

        self.assertEqual(result["status"], "changed")
        self.assertTrue(result["simulation"]["regression_ok"])
        self.assertTrue(result["activated"])
        published = router.load_model_catalog(self.service.catalog_override_path)
        self.assertNotIn(self.removed, {model.model for model in published})


if __name__ == "__main__":
    import unittest

    unittest.main()
