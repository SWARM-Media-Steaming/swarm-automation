"""Issue #205 AC 3, 15, 18: validate a feed before reporting success.

A syntactically valid response with no usable model data is not a successful
refresh. It must not erase last-known-good prices or advance the successful
refresh clock. Exercise the real adapters, normalization, persistence, and
automatic activation; only HTTP transport and the bundled seed are fixtures.
"""

from calibration_uat_fixture import CalibrationUAT, NOW, price_row


class ExternalInputIntegrityTests(CalibrationUAT):
    def assert_rejected_without_replacement(self, payload, *, kind="json"):
        self.remote({"models": [price_row()]}, activation_policy="auto")
        before = self.active_bytes()
        success_time = self.service.status_report()["last_successful_refresh_at"]
        result = self.remote(payload, kind=kind, now=NOW + 60, activation_policy="auto")
        with self.subTest(contract="invalid feed is a failure"):
            self.assertEqual(result["status"], "failed", "Unusable external data must not count as a clean refresh")
        with self.subTest(contract="last known-good data remains byte-identical"):
            self.assertEqual(self.active_bytes(), before, "Invalid source data replaced the activated catalog")
        with self.subTest(contract="success clock is unchanged"):
            self.assertEqual(self.service.status_report()["last_successful_refresh_at"], success_time)

    def test_empty_custom_feed_cannot_erase_active_prices(self):
        self.assert_rejected_without_replacement({"models": []})

    def test_all_non_model_rows_are_not_a_successful_refresh(self):
        self.assert_rejected_without_replacement({"models": [None, 17, "unavailable", {}]})

    def test_all_invalid_prices_are_not_silently_replaced_with_bundled_unknowns(self):
        self.assert_rejected_without_replacement({"models": [price_row(input_cost=-1, output_cost="not-a-price")]})

    def test_empty_models_dev_response_cannot_erase_active_prices(self):
        self.assert_rejected_without_replacement({}, kind="models_dev")

    def test_empty_benchmark_source_cannot_erase_active_prices(self):
        self.assert_rejected_without_replacement({"data": []}, kind="artificial_analysis")
