"""Issue #205: identical external data is not a new discovery on every launch.

AC 18 requires a no-change refresh; the notification requirement excludes
routine successful startup checks. Approval is deliberately NOT performed:
keeping an already-seen model awaiting review is not discovering it again.
"""

from calibration_uat_fixture import CalibrationUAT, NOW, price_row


class ExternalDiscoveryIdempotenceTests(CalibrationUAT):
    def setUp(self):
        super().setUp()
        self.payload = {"models": [price_row(), price_row("new-model")]}
        first = self.remote(self.payload, activation_policy="auto")
        self.assertIn("fixture/new-model", first["diff"]["discovered_models"])
        self.before = self.active_bytes()
        self.history_before = self.service.list_history()

    def test_identical_feed_with_pending_discovery_does_not_create_another_version(self):
        result = self.remote(self.payload, now=NOW + 7 * 3600, initiated_by="STARTUP", activation_policy="auto")
        self.assertEqual(result["status"], "no_change", "Already-seen external discoveries must participate in comparison")
        self.assertEqual(self.service.list_history(), self.history_before)
        self.assertEqual(self.active_bytes(), self.before)

    def test_pending_discovery_does_not_re_notify_on_an_identical_startup_check(self):
        result = self.remote(self.payload, now=NOW + 7 * 3600, initiated_by="STARTUP", activation_policy="auto")
        self.assertFalse(result["notification"]["should_notify"], "An unchanged, already-discovered model is not a new startup event")
        self.assertEqual(result["diff"]["newly_discovered_models"], [])
