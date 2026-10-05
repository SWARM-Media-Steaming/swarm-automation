"""Issue #205 AC 6, 15-17: retain one coherent, usable active calibration.

Oracles: failed refreshes must not replace active data; a refresh cannot undo
an explicit rollback; the version active immediately before an automatic
promotion remains available for rollback after history churn.
Faults/interleavings are injected at I/O boundaries, without scheduling sleeps.
"""

import errno
from pathlib import Path
from unittest import mock

from calibration_uat_fixture import CalibrationUAT, NOW, SOURCE_URL, calibration, price_row, router, sources


class PersistenceContractTests(CalibrationUAT):
    def setUp(self):
        super().setUp()
        self.service.ensure_bootstrap(now=NOW)

    def test_failed_auto_activation_preserves_both_active_data_and_routing_catalog(self):
        before = self.active_bytes()
        version = self.service.load_active()["version"]
        real_replace = calibration.os.replace
        failures = []

        def disk_full_on_catalog(source, destination):
            if Path(destination) == self.service.catalog_override_path:
                failures.append(str(destination))
                raise OSError(errno.ENOSPC, "Fixture disk full during catalog publication")
            return real_replace(source, destination)

        with mock.patch.object(calibration.os, "replace", side_effect=disk_full_on_catalog):
            try:
                result = self.remote({"models": [price_row()]}, now=NOW + 1, activation_policy="auto")
            except (OSError, calibration.CalibrationError):
                # Either a structured failure or an exception surfaced by the
                # desktop command is acceptable; changing active data is not.
                pass
            else:
                self.assertEqual(result["status"], "failed")
        self.assertTrue(failures, "The fixture must reach the catalog publication boundary")
        self.assertEqual(self.active_bytes(), before,
                         "A failed auto-refresh partially replaced the previous active calibration")
        self.assertEqual(self.service.status_report()["active_version"], version)
        self.assertFalse(self.service.lock_path.exists())

    def test_activation_during_fetch_cannot_leave_status_pointing_to_an_old_version(self):
        proposal = self.remote({"models": [price_row(output_cost=9)]}, now=NOW + 1)
        second_client = calibration.ModelCalibrationService(self.service.state_dir)
        interleavings = []

        def activate_while_fetching(*_args, **_kwargs):
            # Represents the independent UI command while refresh awaits HTTP.
            # Serializing by explicitly rejecting it as busy is also valid.
            try:
                second_client.activate(proposal["calibration_version"])
            except calibration.CalibrationBusyError:
                interleavings.append("busy")
            else:
                interleavings.append("activated")
            return {"models": [price_row(output_cost=10)]}, "fixture-next"

        with mock.patch.object(sources, "fetch_json", side_effect=activate_while_fetching):
            result = self.service.refresh(source="json", source_url=SOURCE_URL,
                                          now=NOW + 2, force=True)
        self.assertEqual(len(interleavings), 1)
        self.assertIn(result["status"], ("changed", "failed", "no_change"))
        status = second_client.status_report()
        self.assertEqual(status["active_version"], second_client.load_active()["version"],
                         "Finishing refresh overwrote the activation's current-version pointer")
        active_price = second_client.load_active()["models"][0]["output_cost"]
        catalog = router.load_model_catalog(second_client.catalog_override_path)
        self.assertEqual(catalog[0].output_cost, active_price)

    def test_immediately_previous_active_version_survives_history_churn(self):
        previous_version = self.service.load_active()["version"]
        previous_bytes = self.active_bytes()
        for index in range(1, 36):
            previous_version = self.service.load_active()["version"]
            previous_bytes = self.active_bytes()
            result = self.remote({"models": [price_row(output_cost=8 + index)]}, now=NOW + index)
            self.assertEqual(result["status"], "changed")
            self.assertTrue(result["activated"])
        try:
            self.service.activate(previous_version)
        except calibration.CalibrationError as error:
            self.fail(f"The immediately previous active version was pruned before it could be rolled back: {error}")
        self.assertEqual(self.active_bytes(), previous_bytes)
