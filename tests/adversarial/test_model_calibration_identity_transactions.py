"""Issue #205: contradictory identities fail before any calibration publication.

The trusted amendment requires rejection of contradictory established-model
rows. AC 15-17 additionally require preserved active routing and rollback.
Exercise aliases, real source adapters, pending review, and fresh readers;
only the offline catalog and HTTP transport are fixtures. Equal normalized
observations, complementary measurements, and different providers are valid.
"""

import copy
import itertools

from calibration_uat_fixture import (
    CalibrationUAT, NOW, calibration, model_entry, price_row, router,
)


class IdentityTransactionTests(CalibrationUAT):
    def setUp(self):
        super().setUp()
        self.local = [model_entry(model_id="established-api")]
        self.service.ensure_bootstrap(now=NOW)
        self.bootstrap_version = self.service.load_active()["version"]
        self.bootstrap_bytes = self.active_bytes()
        initial = self.remote({"models": [price_row()]}, now=NOW + 1,
                              activation_policy="auto")
        self.assertTrue(initial["activated"])
        pending = self.remote({"models": [price_row(output_cost=9)]}, now=NOW + 2)
        self.assertEqual(pending["status"], "changed")
        self.assertFalse(pending["activated"])

    def publications(self):
        paths = [self.service.active_path, self.service.catalog_override_path,
                 self.service.proposed_path, *self.service.history_dir.glob("*.json")]
        return {str(p.relative_to(self.service.state_dir)): p.read_bytes()
                for p in paths if p.exists()}

    def assert_failed_transaction(self, refresh):
        before = self.publications()
        status_before = self.service.status_report()
        catalog = router.load_model_catalog(self.service.catalog_override_path)
        request = router.RouteRequest(task_type="deep_debugging", complexity=7)
        decision = router.route(request, catalog=catalog)

        result = refresh()

        # Check publication first: an error status alone could conceal writes.
        after_publications = self.publications()
        changed_paths = sorted(path for path in before.keys() | after_publications.keys()
                               if before.get(path) != after_publications.get(path))
        self.assertEqual(changed_paths, [],
                         "Contradictory source data published a calibration update: "
                         f"status={result.get('status')}, activated={result.get('activated')}")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["source_status"], "error")
        self.assertTrue(result.get("error"), "Validation must explain the failure")
        reader = calibration.ModelCalibrationService(self.service.state_dir)
        status = reader.status_report()
        self.assertEqual(status["last_attempted_status"], "failed")
        self.assertEqual(status["last_successful_refresh_at"],
                         status_before["last_successful_refresh_at"])
        self.assertEqual(status["active_version"], status_before["active_version"])
        self.assertEqual(status["proposed_version"], status_before["proposed_version"])
        self.assertTrue(status["healthy"])
        self.assertTrue(status["has_newer_proposed"])
        self.assertFalse(status["refresh_running"])
        self.assertFalse(self.service.lock_path.exists())
        after = router.route(request, catalog=router.load_model_catalog(reader.catalog_override_path))
        self.assertEqual((after.model, after.effort), (decision.model, decision.effort))

    def test_conflicting_aliases_preserve_pending_review_for_every_initiator(self):
        # Intermediate aliases should not hide a conflict in a later row.
        for initiator, field, reverse in itertools.product(
            ("STARTUP", "USER", "SCHEDULED", "AI_AGENT"),
            ("input_cost", "output_cost", "reasoning_cost", "speed", "latency_seconds"),
            (False, True),
        ):
            with self.subTest(initiator=initiator, field=field, reverse=reverse):
                rows = [
                    {"provider": "fixture", "model": "established", field: 0},
                    {"provider": " FIXTURE ", "model": " Feed-Alias ",
                     "model_id": " ESTABLISHED-API ", field: 0},
                    {"provider": "fixture", "model": "established-api", field: 7},
                ]
                self.assert_failed_transaction(lambda: self.remote(
                    {"models": rows[::-1] if reverse else rows}, now=NOW + 3,
                    initiated_by=initiator, activation_policy="auto",
                ))

    def test_benchmark_and_retirement_conflicts_cannot_hide_behind_aliases(self):
        for field, values in (
            ("evaluations", ({"coding": 0}, {"coding": 90})),
            ("deprecated", (False, True)),
        ):
            for reverse in (False, True):
                with self.subTest(field=field, reverse=reverse):
                    rows = [
                        {"provider": "fixture", "model": "established", field: values[0]},
                        {"provider": "fixture", "model": "established-api", field: values[1]},
                    ]
                    self.assert_failed_transaction(lambda: self.remote(
                        {"models": rows[::-1] if reverse else rows}, now=NOW + 3,
                        activation_policy="auto",
                    ))

    def test_artificial_analysis_adapter_preserves_conflicting_observations(self):
        rows = [
            {"id": "source-row-one", "slug": "established",
             "model_creator": {"slug": "fixture"},
             "pricing": {"price_1m_input_tokens": 2, "price_1m_output_tokens": 4},
             "evaluations": {"coding": 60}},
            {"id": "source-row-two", "slug": "established-api",
             "model_creator": {"slug": "fixture"},
             "pricing": {"price_1m_input_tokens": 2, "price_1m_output_tokens": 40},
             "evaluations": {"coding": 80}},
        ]
        for order in (rows, rows[::-1]):
            with self.subTest(first=order[0]["id"]):
                self.assert_failed_transaction(lambda: self.remote(
                    {"data": order}, kind="artificial_analysis", now=NOW + 3,
                    activation_policy="auto",
                ))

    def test_valid_observations_coalesce_and_reordered_retry_is_no_change(self):
        active_before = self.active_bytes()
        rows = [
            {"provider": "fixture", "model": "established", "input_cost": "2.0",
             "evaluations": {"coding": "70.0"}},
            {"provider": " FIXTURE ", "model": " ESTABLISHED-API ", "input_cost": 2,
             "output_cost": 11, "evaluations": {"coding": 70, "reasoning": 80}},
            {"provider": "fixture", "model": "established-api", "speed": 0},
        ]
        result = self.remote({"models": rows}, now=NOW + 3)
        self.assertEqual(result["status"], "changed")
        self.assertFalse(result["activated"])
        proposed = self.service.load_proposed()
        self.assertEqual(len(proposed["models"]), 1)
        model = proposed["models"][0]
        self.assertEqual((model["input_cost"], model["output_cost"], model["speed"]), (2, 11, 0))
        self.assertEqual(model["external_evaluations"], {"coding": 70, "reasoning": 80})
        before = self.publications()
        for order in itertools.permutations(rows):
            retry = self.remote({"models": list(order)}, now=NOW + 4)
            self.assertEqual(retry["status"], "no_change")
            self.assertEqual(self.publications(), before)
        self.assertEqual(self.active_bytes(), active_before)

    def test_provider_namespace_separates_identical_model_slugs(self):
        result = self.remote({"models": [price_row(output_cost=4),
                            price_row(provider="another-provider", output_cost=40)]}, now=NOW + 3)
        self.assertEqual(result["status"], "changed")
        document = self.service.load_proposed()
        proposed = {m["key"]: m for m in document["models"] + document["discovered_models"]}
        self.assertEqual(proposed["fixture/established"]["output_cost"], 4)
        other = proposed["another-provider/established"]
        self.assertEqual(other["output_cost"], 40)
        self.assertEqual(other["status"], "DISCOVERED")

    def test_conflict_does_not_poison_retry_activation_or_rollback(self):
        self.assert_failed_transaction(lambda: self.remote(
            {"models": [price_row(output_cost=4), price_row(output_cost=40)]},
            now=NOW + 3, activation_policy="auto",
        ))
        # A new process must be able to apply the preserved, already-reviewed
        # proposal and then restore the previous offline calibration.
        reader = calibration.ModelCalibrationService(self.service.state_dir)
        pending = reader.load_proposed()
        reader.activate(pending["version"])
        self.assertEqual(router.load_model_catalog(reader.catalog_override_path)[0].output_cost, 9)
        reader.activate(self.bootstrap_version)
        self.assertEqual(self.active_bytes(), self.bootstrap_bytes)
        corrected = self.remote({"models": [price_row(output_cost=11)]}, now=NOW + 4)
        self.assertEqual(corrected["status"], "changed")
        self.assertFalse(corrected["activated"])

    def test_local_catalog_rejects_conflicting_performance_rows(self):
        # "local" is a selectable source on the same public service. Its
        # supported performance fields must receive the same validation as
        # remote observations; duplicate row order cannot select a winner.
        for field, values in (("speed", (20, 80)), ("latency_seconds", (0.1, 10))):
            for reverse in (False, True):
                with self.subTest(field=field, reverse=reverse):
                    rows = [model_entry(model_id="established-api", **{field: value})
                            for value in values]
                    self.local = copy.deepcopy(rows[::-1] if reverse else rows)
                    self.assert_failed_transaction(lambda: self.service.refresh(
                        source="local", force=True, now=NOW + 3,
                        activation_policy="auto",
                    ))

    def test_equal_normalized_local_performance_rows_remain_usable(self):
        self.local = [model_entry(model_id="established-api", speed=value,
                                  latency_seconds=0.5) for value in (20, "20.0")]
        before = self.active_bytes()
        result = self.service.refresh(source="local", force=True, now=NOW + 3)
        self.assertEqual(result["status"], "changed")
        self.assertFalse(result["activated"])
        self.assertEqual(self.active_bytes(), before)
        models = self.service.load_proposed()["models"]
        self.assertEqual(len(models), 1)
        self.assertEqual((models[0]["speed"], models[0]["latency_seconds"]), (20, 0.5))
        self.local.reverse()
        retry = self.service.refresh(source="local", force=True, now=NOW + 4)
        self.assertEqual(retry["status"], "no_change")


if __name__ == "__main__":
    import unittest
    unittest.main()
