"""Issue #205 refresh validation: one source identity has one observation.

A refresh cannot deterministically normalize two contradictory rows for the
same provider/model by trusting row order.  Such a feed is ambiguous source
data, so it must fail without changing the last known-good calibration or
publishing a proposal.  Exercise the real JSON adapter, overlay merge,
calibration service, and persistence with an offline transport.
"""

from unittest import mock

from calibration_uat_fixture import CalibrationUAT, NOW, price_row, sources


class DuplicateSourceIdentityTests(CalibrationUAT):
    def setUp(self):
        super().setUp()
        self.service.ensure_bootstrap(now=NOW)
        initial = self.remote(
            {"models": [price_row(input_cost=2, output_cost=8)]},
            now=NOW + 1,
            activation_policy="auto",
        )
        self.assertEqual(initial["status"], "changed")
        self.assertTrue(initial["activated"])
        self.assertEqual(self.service.load_active()["models"][0]["output_cost"], 8)

    def conflicting_refresh(self, rows):
        with mock.patch.object(
            sources,
            "fetch_json",
            return_value=({"models": rows}, "conflicting-fixture"),
        ):
            return self.service.refresh(
                source="json",
                source_url="https://example.invalid/model-data.json",
                force=True,
                now=NOW + 2,
                activation_policy="auto",
            )

    def assert_rejected_without_publication(self, rows):
        before = self.active_bytes()
        proposed_before = (
            self.service.proposed_path.read_bytes()
            if self.service.proposed_path.exists()
            else None
        )
        status_before = self.service.status_report()
        successful_at = status_before["last_successful_refresh_at"]
        self.assertFalse(status_before["has_newer_proposed"])

        result = self.conflicting_refresh(rows)

        self.assertEqual(
            result["status"],
            "failed",
            "Contradictory rows for one model identity are invalid source data, not a safe proposal",
        )
        self.assertEqual(result["source_status"], "error")
        self.assertEqual(self.active_bytes(), before)
        proposed_after = (
            self.service.proposed_path.read_bytes()
            if self.service.proposed_path.exists()
            else None
        )
        self.assertEqual(proposed_after, proposed_before)
        status = self.service.status_report()
        self.assertFalse(status["has_newer_proposed"])
        self.assertEqual(status["last_successful_refresh_at"], successful_at)
        self.assertEqual(status["last_attempted_status"], "failed")

    def test_conflicting_rows_for_an_established_model_are_rejected(self):
        self.assert_rejected_without_publication(
            [
                price_row(input_cost=2, output_cost=4),
                price_row(input_cost=2, output_cost=40),
            ]
        )

    def test_conflicting_rows_for_a_newly_discovered_model_are_rejected(self):
        self.assert_rejected_without_publication(
            [
                price_row(input_cost=2, output_cost=8),
                price_row("new-model", input_cost=1, output_cost=3),
                price_row("new-model", input_cost=10, output_cost=30),
            ]
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
