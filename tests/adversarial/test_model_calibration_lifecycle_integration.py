"""Issue #205 lifecycle UAT using real persistence, sources and routing.

Oracles from AC 4-8, 14-18: the exact configured interval permits startup,
manual refresh bypasses it, outages preserve usable routing, concurrent
refreshes do not duplicate work, and activated versions can be rolled back.
Events control the concurrent test; it never depends on a scheduling sleep.
"""

import threading
from unittest import mock

from calibration_uat_fixture import (
    CalibrationUAT, NOW, SOURCE_URL, calibration, model_entry, price_row, router, sources,
)


class LifecycleIntegrationTests(CalibrationUAT):
    def setUp(self):
        super().setUp()
        self.service.ensure_bootstrap(now=NOW)

    def test_startup_interval_boundary_and_manual_bypass_use_the_same_adapter(self):
        payload = {"models": [price_row()]}
        with mock.patch.object(sources, "fetch_json", return_value=(payload, "v1")) as fetch:
            first = self.service.refresh(source="json", source_url=SOURCE_URL, initiated_by="STARTUP", now=NOW)
            self.assertEqual(first["status"], "changed")
            self.assertTrue(first["activated"])
            early = self.service.refresh(source="json", source_url=SOURCE_URL, initiated_by="STARTUP", now=NOW + 21599, min_interval_hours=6)
            self.assertEqual(early["status"], "skipped_interval")
            self.assertEqual(fetch.call_count, 1)
            due = self.service.refresh(source="json", source_url=SOURCE_URL, initiated_by="STARTUP", now=NOW + 21600, min_interval_hours=6)
            self.assertEqual(due["status"], "no_change")
            self.assertEqual(fetch.call_count, 2)
            manual = self.service.refresh(source="json", source_url=SOURCE_URL, initiated_by="USER", force=True, now=NOW + 21601, min_interval_hours=6)
            self.assertEqual(manual["status"], "no_change")
            self.assertEqual(fetch.call_count, 3)

    def test_source_failure_preserves_routing_and_obeys_retry_backoff(self):
        before = self.active_bytes()
        with mock.patch.object(sources, "fetch_json", side_effect=sources.SourceError("Offline fixture outage")) as fetch:
            failed = self.service.refresh(source="json", source_url=SOURCE_URL, initiated_by="STARTUP", now=NOW)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(self.active_bytes(), before)
            status = self.service.status_report()
            self.assertTrue(status["healthy"])
            self.assertEqual(status["last_attempted_status"], "failed")
            catalog = router.load_model_catalog(self.service.catalog_override_path)
            decision = router.route(router.RouteRequest(task_type="architecture", complexity=8), catalog=catalog)
            self.assertEqual(decision.model, "established")
            retry = self.service.refresh(source="json", source_url=SOURCE_URL, initiated_by="STARTUP", now=NOW + 1)
            self.assertEqual(retry["status"], "skipped_interval")
            self.assertEqual(fetch.call_count, 1)
            manual = self.service.refresh(source="json", source_url=SOURCE_URL, initiated_by="USER", force=True, now=NOW + 2)
            self.assertEqual(manual["status"], "failed")
            self.assertEqual(fetch.call_count, 2)

    def test_refresh_keeps_status_and_routing_usable_and_rejects_concurrent_work(self):
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        before = self.active_bytes()

        def held_transport(*_args, **_kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release the fixture response")
            return {"models": [price_row()]}, "fixture-v2"

        def refresh():
            try:
                results.append(self.service.refresh(source="json", source_url=SOURCE_URL, force=True, now=NOW + 1))
            except BaseException as error:
                errors.append(error)

        with mock.patch.object(sources, "fetch_json", side_effect=held_transport) as fetch:
            thread = threading.Thread(target=refresh)
            thread.start()
            try:
                self.assertTrue(entered.wait(5), "Refresh must reach the fixture adapter")
                status = self.service.status_report()
                self.assertTrue(status["refresh_running"])
                self.assertTrue(status["healthy"])
                self.assertEqual(self.active_bytes(), before)
                catalog = router.load_model_catalog(self.service.catalog_override_path)
                self.assertEqual(router.route(router.RouteRequest(complexity=8, task_type="architecture"), catalog=catalog).model, "established")
                another = calibration.ModelCalibrationService(self.service.state_dir)
                duplicate = another.refresh(source="json", source_url=SOURCE_URL, force=True, now=NOW + 2)
                self.assertEqual(duplicate["status"], "already_running")
                self.assertEqual(fetch.call_count, 1)
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive(), "Refresh must finish after the fixture is released")
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["status"], "changed")
        self.assertTrue(results[0]["activated"])
        self.assertNotEqual(self.active_bytes(), before)
        self.assertFalse(self.service.status_report()["refresh_running"])
        self.assertFalse(self.service.lock_path.exists())

    def test_all_initiators_activate_and_record_history_through_shared_refresh(self):
        for index, initiator in enumerate(("STARTUP", "USER", "SCHEDULED", "AI_AGENT"), start=1):
            with self.subTest(initiator=initiator):
                result = self.remote({"models": [price_row(output_cost=8 + index)]}, now=NOW + index, initiated_by=initiator)
                self.assertEqual(result["status"], "changed")
                self.assertTrue(result["activated"])
                history = {item["version"]: item for item in self.service.list_history()}
                self.assertEqual(history[result["calibration_version"]]["initiated_by"], initiator)
                self.assertEqual(self.service.load_active()["version"], result["calibration_version"])

    def test_automatic_activation_and_rollback_restore_the_live_catalog(self):
        first = self.service.load_active()["version"]
        before = self.active_bytes()
        result = self.remote({"models": [price_row()]}, now=NOW + 1)
        self.assertTrue(result["activated"])
        self.assertNotEqual(self.active_bytes(), before)
        loaded = router.load_model_catalog(self.service.catalog_override_path)
        self.assertEqual(loaded[0].input_cost, 2)
        self.assertEqual(loaded[0].output_cost, 8)
        self.service.activate(first)
        self.assertEqual(self.active_bytes(), before)
        self.assertEqual(self.service.status_report()["active_version"], first)
