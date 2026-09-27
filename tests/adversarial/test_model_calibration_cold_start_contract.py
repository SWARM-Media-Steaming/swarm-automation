"""Issue #205 AC 4–6, 15, 17: bootstrap must precede external startup work.

The bundled catalog is the pre-feature known-good routing configuration.
Opening AI Configuration before the startup request completes must not decide
whether an external proposal is automatically activated under manual policy.
Exercise the real desktop CLI boundary and a held transport, without HTTP.
"""

import contextlib
import io
import json
import threading
from unittest import mock

from calibration_uat_fixture import (
    CalibrationUAT, NOW, SOURCE_URL, calibration, model_entry, price_row, router, sources,
)


class ColdStartupContractTests(CalibrationUAT):
    def setUp(self):
        super().setUp()
        self.local = [model_entry(input_cost=2, output_cost=8)]
        clock = mock.patch.object(calibration.time, "time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def startup_cli(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = calibration.main([
                "--state-dir", str(self.service.state_dir), "refresh",
                "--source", "json", "--source-url", SOURCE_URL,
                "--initiated-by", "STARTUP", "--activation-policy", "manual",
                "--min-interval-hours", "6",
            ])
        self.assertEqual(code, 0, output.getvalue())
        return json.loads(output.getvalue())

    def assert_local_active_and_remote_pending(self, result):
        active = self.service.load_active()
        self.assertIsNotNone(active, "Startup must retain an immediately usable baseline")
        self.assertEqual(
            (active["models"][0]["input_cost"], active["models"][0]["output_cost"]),
            (2, 8),
            "External startup data silently replaced the bundled known-good prices under manual policy",
        )
        self.assertFalse(result.get("activated", False))
        proposal = self.service.load_proposed()
        self.assertIsNotNone(proposal, "Changed external prices must remain available for review")
        self.assertEqual(proposal["models"][0]["output_cost"], 200)

    def test_first_external_startup_preserves_manual_review_without_a_prior_status_read(self):
        self.assertIsNone(self.service.load_active())
        payload = {"models": [price_row(input_cost=50, output_cost=200)]}
        with mock.patch.object(sources, "fetch_json", return_value=(payload, "fixture-v1")):
            result = self.startup_cli()
        self.assertEqual(result["initiated_by"], "STARTUP")
        self.assert_local_active_and_remote_pending(result)

    def test_opening_ai_configuration_before_startup_does_not_change_activation_policy(self):
        # Control for the otherwise identical first-launch CLI operation.
        self.assertTrue(self.service.status_report()["healthy"])
        payload = {"models": [price_row(input_cost=50, output_cost=200)]}
        with mock.patch.object(sources, "fetch_json", return_value=(payload, "fixture-v1")):
            result = self.startup_cli()
        self.assert_local_active_and_remote_pending(result)

    def test_first_startup_exposes_known_good_status_while_the_source_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        errors, results = [], []

        def held_source(*_args, **_kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError("Fixture source was never released")
            return {"models": [price_row(input_cost=50, output_cost=200)]}, "fixture-v1"

        def refresh():
            try:
                results.append(self.service.refresh(
                    source="json", source_url=SOURCE_URL, initiated_by="STARTUP",
                    activation_policy="manual", now=NOW,
                ))
            except BaseException as error:
                errors.append(error)

        with mock.patch.object(sources, "fetch_json", side_effect=held_source):
            thread = threading.Thread(target=refresh)
            thread.start()
            try:
                self.assertTrue(entered.wait(5), "Must exercise an in-progress source fetch")
                reader = calibration.ModelCalibrationService(self.service.state_dir)
                status = reader.status_report()
                self.assertTrue(status["refresh_running"])
                self.assertTrue(status["healthy"],
                                "First startup reports no usable calibration while waiting on the external source")
                self.assertIsNotNone(status["active_version"])
                catalog = router.load_model_catalog(reader.catalog_override_path)
                decision = router.route(router.RouteRequest(task_type="architecture", complexity=8), catalog=catalog)
                self.assertEqual(decision.model, "established")
                self.assertEqual(catalog[0].output_cost, 8)
            finally:
                release.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)

    def test_first_startup_outage_still_exposes_a_usable_local_calibration(self):
        with mock.patch.object(sources, "fetch_json", side_effect=sources.SourceError("Fixture outage")):
            result = self.startup_cli()
        self.assertEqual(result["status"], "failed")
        status = self.service.status_report()
        self.assertTrue(status["healthy"])
        self.assertEqual(status["last_attempted_status"], "failed")
        self.assertEqual(status["active_calibration"]["models"][0]["output_cost"], 8)
