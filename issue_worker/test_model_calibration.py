"""Tests for Model Routing Calibration (issue #205).

Deterministic and offline: remote sources use an injected ``fetch_fn`` or a
mocked JSON transport (never a live network call), and the local source path
reads the real bundled ``skills/model-router/models.yaml`` so these tests also
double as a smoke test that file still parses.

Run with ``python3 -m unittest test_model_calibration`` from this directory
(not pytest — see test_swarm_issue_worker.py's module docstring for why).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import model_calibration as calib
import model_data_sources as sources
import model_router as mr


def _entry(
    model: str,
    *,
    provider: str = "fixture",
    agent: str = "fixture",
    capability: int = 3,
    cost: int = 3,
    token_efficiency: int = 3,
    latency: int = 3,
    active: bool = True,
    recommended: bool = True,
    deprecated: bool = False,
    efforts: tuple[str, ...] = ("medium",),
) -> dict:
    return {
        "provider": provider,
        "agent": agent,
        "model": model,
        "model_id": model,
        "active": active,
        "recommended": recommended,
        "deprecated": deprecated,
        "superseded_by": None,
        "supported_efforts": list(efforts),
        "strengths": [],
        "weaknesses": [],
        "relative_capability": capability,
        "relative_cost": cost,
        "relative_token_efficiency": token_efficiency,
        "relative_latency": latency,
        "benchmarks": {},
        "benchmark_source": None,
        "benchmark_date": None,
        "notes": "",
    }


class ModelCalibrationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = calib.ModelCalibrationService(Path(self._tmp.name))

    # ----- manual + startup refresh, no prior calibration -------------

    def test_manual_refresh_bootstraps_from_the_bundled_catalog(self) -> None:
        result = self.service.refresh(fetch_fn=lambda: [_entry("m1")], initiated_by="USER")
        self.assertIn(result["status"], ("changed", "no_change"))
        status = self.service.status_report()
        self.assertIsNotNone(status["active_version"])
        self.assertTrue(status["healthy"])
        # The bundled catalog is always usable immediately, before any
        # refresh has ever produced a diff.
        self.assertGreater(status["active_calibration"]["model_counts"][calib.STATUS_ACTIVE], 0)

    def test_startup_refresh_never_needs_network_and_leaves_a_usable_calibration(self) -> None:
        result = self.service.refresh(initiated_by="STARTUP")
        self.assertIn(result["status"], ("changed", "no_change"))
        active = self.service.load_active()
        self.assertIsNotNone(active)
        self.assertGreaterEqual(len(active["models"]), 1)

    # ----- throttling ---------------------------------------------------

    def test_refresh_throttling_skips_within_the_minimum_interval(self) -> None:
        base = 1_000_000.0
        first = self.service.refresh(fetch_fn=lambda: [_entry("m1")], now=base, min_interval_hours=6)
        self.assertNotEqual(first["status"], "skipped_interval")
        second = self.service.refresh(
            fetch_fn=lambda: [_entry("m1")], now=base + 60, min_interval_hours=6
        )
        self.assertEqual(second["status"], "skipped_interval")

    def test_manual_refresh_bypasses_the_interval_with_force(self) -> None:
        base = 1_000_000.0
        self.service.refresh(fetch_fn=lambda: [_entry("m1")], now=base, min_interval_hours=6)
        forced = self.service.refresh(
            fetch_fn=lambda: [_entry("m1", cost=4)],
            now=base + 60,
            min_interval_hours=6,
            force=True,
        )
        self.assertNotEqual(forced["status"], "skipped_interval")

    def test_refresh_interval_elapsed_runs_again(self) -> None:
        base = 1_000_000.0
        self.service.refresh(fetch_fn=lambda: [_entry("m1")], now=base, min_interval_hours=1)
        later = self.service.refresh(
            fetch_fn=lambda: [_entry("m1")], now=base + 3700, min_interval_hours=1
        )
        self.assertNotEqual(later["status"], "skipped_interval")

    # ----- source failure -------------------------------------------------

    def test_source_unavailable_leaves_the_existing_calibration_active(self) -> None:
        self.service.refresh(fetch_fn=lambda: [_entry("m1")], now=1.0)
        active_before = self.service.load_active()

        def _boom() -> list[dict]:
            raise calib.CalibrationSourceError("could not reach model data source: timed out")

        result = self.service.refresh(fetch_fn=_boom, now=100.0, force=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["source_status"], "unavailable")
        self.assertEqual(self.service.load_active(), active_before)
        status = self.service.status_report()
        self.assertEqual(status["last_attempted_status"], "failed")
        self.assertEqual(status["source_status"], "unavailable")

    def test_json_source_rejects_non_https_urls_without_raising_unhandled(self) -> None:
        result = self.service.refresh(
            source="json", source_url="http://example.com/models.json", force=True
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["source_status"], "unavailable")

    def test_remote_source_network_error_is_reported_as_unavailable(self) -> None:
        with mock.patch(
            "model_calibration._sources.fetch_source",
            side_effect=sources.SourceError("could not reach model source"),
        ):
            result = self.service.refresh(source="models_dev", force=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["source_status"], "unavailable")

    # ----- invalid data -----------------------------------------------

    def test_invalid_data_is_reported_and_does_not_touch_the_active_calibration(self) -> None:
        self.service.refresh(fetch_fn=lambda: [_entry("m1")], now=1.0)
        active_before = self.service.load_active()

        result = self.service.refresh(fetch_fn=lambda: [{"provider": "x"}], now=100.0, force=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["source_status"], "error")
        self.assertEqual(self.service.load_active(), active_before)

    def test_entries_with_unsafe_characters_are_dropped_not_fatal_when_others_are_valid(self) -> None:
        bad = _entry("m-bad")
        bad["notes"] = 'contains a "quote" that could break the stored catalog'
        result = self.service.refresh(fetch_fn=lambda: [_entry("m-good"), bad], now=1.0)
        self.assertIn(result["status"], ("changed", "no_change"))
        active = self.service.load_active()
        keys = {model["key"] for model in active["models"]}
        self.assertIn("fixture/m-good", keys)
        self.assertNotIn("fixture/m-bad", keys)

    def test_empty_source_data_is_a_validation_failure(self) -> None:
        result = self.service.refresh(fetch_fn=lambda: [], force=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["source_status"], "error")

    # ----- no-change vs meaningful-change -------------------------------

    def test_no_change_refresh_reports_no_change_and_no_new_proposed_version(self) -> None:
        # Activating the baseline first means the second identical refresh is
        # compared against a calibration that actually contains "m1", rather
        # than the still-active bootstrap.
        self.service.refresh(fetch_fn=lambda: [_entry("m1")], now=1.0, activation_policy="auto")
        result = self.service.refresh(fetch_fn=lambda: [_entry("m1")], now=100.0, force=True)
        self.assertEqual(result["status"], "no_change")
        status = self.service.status_report()
        self.assertFalse(status["has_newer_proposed"])

    def test_meaningful_change_produces_a_proposed_calibration_awaiting_review(self) -> None:
        self.service.refresh(fetch_fn=lambda: [_entry("m1", cost=2)], now=1.0, activation_policy="auto")
        result = self.service.refresh(fetch_fn=lambda: [_entry("m1", cost=4)], now=100.0, force=True)
        self.assertEqual(result["status"], "changed")
        self.assertTrue(result["diff"]["pricing_changes"])
        status = self.service.status_report()
        self.assertTrue(status["has_newer_proposed"])
        # Manual activation policy (the default) never swaps the active
        # calibration on its own.
        self.assertEqual(status["active_calibration"]["models"][0]["relative_cost"], 2)

    def test_a_newly_seen_model_is_discovered_not_immediately_routable(self) -> None:
        self.service.refresh(fetch_fn=lambda: [_entry("m1")], now=1.0, activation_policy="auto")
        result = self.service.refresh(
            fetch_fn=lambda: [_entry("m1"), _entry("m2")], now=100.0, force=True
        )
        self.assertEqual(result["status"], "changed")
        proposed = self.service.load_proposed()
        by_key = {m["key"]: m for m in proposed["models"]}
        self.assertEqual(by_key["fixture/m1"]["status"], calib.STATUS_ACTIVE)
        self.assertEqual(by_key["fixture/m2"]["status"], calib.STATUS_DISCOVERED)

    def test_auto_activation_policy_promotes_a_clean_refresh(self) -> None:
        self.service.refresh(fetch_fn=lambda: [_entry("m1", cost=2)], now=1.0)
        result = self.service.refresh(
            fetch_fn=lambda: [_entry("m1", cost=3)],
            now=100.0,
            force=True,
            activation_policy="auto",
        )
        self.assertTrue(result["activated"])
        status = self.service.status_report()
        self.assertEqual(status["active_version"], result["calibration_version"])
        self.assertFalse(status["has_newer_proposed"])

    # ----- rollback -----------------------------------------------------

    def test_activate_can_roll_back_to_an_earlier_version(self) -> None:
        first = self.service.refresh(fetch_fn=lambda: [_entry("m1", cost=2)], now=1.0)
        first_version = self.service.status_report()["active_version"]
        second = self.service.refresh(fetch_fn=lambda: [_entry("m1", cost=5)], now=100.0, force=True)
        self.service.activate(second["calibration_version"])
        self.assertEqual(self.service.status_report()["active_version"], second["calibration_version"])

        rolled_back = self.service.activate(first_version)
        self.assertEqual(rolled_back["version"], first_version)
        status = self.service.status_report()
        self.assertEqual(status["active_version"], first_version)
        self.assertEqual(status["active_calibration"]["models"][0]["relative_cost"], 2)

    def test_activate_rejects_an_unknown_or_malformed_version(self) -> None:
        self.service.refresh(fetch_fn=lambda: [_entry("m1")], now=1.0)
        with self.assertRaises(calib.CalibrationError):
            self.service.activate("2026-01-01-999")
        with self.assertRaises(calib.CalibrationError):
            self.service.activate("../../etc/passwd")

    def test_activation_writes_a_catalog_override_the_live_router_can_load(self) -> None:
        result = self.service.refresh(fetch_fn=lambda: [_entry("m1", efforts=("medium",))], now=1.0)
        version = self.service.status_report()["active_version"]
        self.service.activate(version)
        catalog = mr.load_model_catalog(self.service.catalog_override_path)
        self.assertEqual({model.model for model in catalog}, {"m1"})
        self.assertIsInstance(result, dict)


class ExternalEvaluationRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = calib.ModelCalibrationService(Path(tmp.name))

    def refresh_evaluations(self, evaluations: dict, *, now: float) -> dict:
        payload = {
            "data": [
                {
                    "model_creator": {"slug": "fixture"},
                    "slug": "m1",
                    "evaluations": evaluations,
                }
            ],
        }
        with mock.patch.dict("os.environ", {"ARTIFICIAL_ANALYSIS_API_KEY": "fixture-key"}):
            with mock.patch.object(calib, "fetch_local_source", return_value=[_entry("m1")]):
                with mock.patch.object(sources, "fetch_json", return_value=(payload, str(now))):
                    return self.service.refresh(
                        source="artificial_analysis", force=True, now=now
                    )

    def test_external_values_are_reviewable_with_history_activation_and_rollback(self) -> None:
        initial = self.refresh_evaluations(
            {"coding_agent_index": 60, "intelligence_index": 40}, now=1.0
        )
        # First external data is a proposal against the offline baseline too.
        self.assertFalse(initial["activated"])
        self.service.activate(initial["calibration_version"])
        active_before = self.service.active_path.read_bytes()
        catalog_before = self.service.catalog_override_path.read_bytes()
        updated = self.refresh_evaluations(
            {"coding_agent_index": "80", "intelligence_index": 50}, now=100.0
        )

        self.assertEqual(updated["status"], "changed")
        self.assertFalse(updated["activated"])
        self.assertIsNotNone(updated["simulation"])
        changes = updated["diff"]["benchmark_changes"]
        self.assertEqual(
            [(item["field"], item["previous"], item["new"]) for item in changes],
            [
                ("external_evaluations.coding_agent_index", 60, 80),
                ("external_evaluations.intelligence_index", 40, 50),
            ],
        )
        self.assertEqual(self.service.active_path.read_bytes(), active_before)
        self.assertEqual(self.service.catalog_override_path.read_bytes(), catalog_before)
        proposed = self.service.load_proposed()
        history = proposed["models"][0]["benchmark_history"]
        latest = [item for item in history if item["at"] == calib.iso_now(100.0)]
        self.assertEqual(
            latest,
            [
                {"at": calib.iso_now(100.0), "field": "external_evaluations.coding_agent_index",
                 "previous": 60, "new": 80},
                {"at": calib.iso_now(100.0), "field": "external_evaluations.intelligence_index",
                 "previous": 40, "new": 50},
            ],
        )
        explanation = self.service.analyze()
        self.assertTrue(any("60.0 → 80.0" in item["answer"] for item in explanation["answers"]))
        self.assertFalse(any("no meaningful" in item["answer"] for item in explanation["answers"]))

        self.service.activate(updated["calibration_version"])
        self.assertEqual(self.service.load_active(), proposed)
        self.service.activate(initial["calibration_version"])
        self.assertEqual(self.service.active_path.read_bytes(), active_before)
        self.assertEqual(self.service.catalog_override_path.read_bytes(), catalog_before)

    def test_equivalent_normalized_evaluations_do_not_create_an_update(self) -> None:
        initial = self.refresh_evaluations(
            {"coding_agent_index": "60", "intelligence_index": 0}, now=1.0
        )
        self.service.activate(initial["calibration_version"])
        history_before = self.service.list_history()
        result = self.refresh_evaluations(
            {"intelligence_index": 0.0, "coding_agent_index": 60.0,
             "missing": None, "invalid": "not a number"},
            now=100.0,
        )
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(result["diff"]["benchmark_changes"], [])
        self.assertIsNone(self.service.load_proposed())
        self.assertEqual(self.service.load_active()["version"], initial["calibration_version"])
        self.assertEqual(self.service.list_history(), history_before)

    def test_added_and_removed_external_evaluations_are_reported(self) -> None:
        initial = self.refresh_evaluations({"coding_agent_index": 60}, now=1.0)
        self.service.activate(initial["calibration_version"])
        result = self.refresh_evaluations({"new_benchmark": 0}, now=100.0)
        self.assertEqual(result["status"], "changed")
        self.assertEqual(
            [(item["field"], item["previous"], item["new"])
             for item in result["diff"]["benchmark_changes"]],
            [
                ("external_evaluations.coding_agent_index", 60, None),
                ("external_evaluations.new_benchmark", None, 0),
            ],
        )
        self.assertEqual(
            self.service.load_proposed()["models"][0]["external_evaluations"],
            {"new_benchmark": 0},
        )


class ExternalBootstrapTests(unittest.TestCase):
    def test_every_external_initiator_publishes_offline_data_before_fetching(self) -> None:
        for initiator in calib.ALLOWED_INITIATORS:
            with self.subTest(initiator=initiator), tempfile.TemporaryDirectory() as tmp:
                service = calib.ModelCalibrationService(Path(tmp))
                baseline = _entry("m1") | {"input_cost": 2, "output_cost": 8}
                active_during_fetch = []

                def fetch(*_args, **_kwargs):
                    # Read without status_report: that must not be what
                    # bootstraps the baseline or determines activation policy.
                    active_during_fetch.append(service.load_active())
                    catalog = mr.load_model_catalog(service.catalog_override_path)
                    self.assertEqual(catalog[0].output_cost, 8)
                    self.assertIsNone(service.load_state().get("last_successful_at"))
                    return {"models": [{"provider": "fixture", "model": "m1", "output_cost": 50}]}, "v1"

                with mock.patch.object(calib, "fetch_local_source", return_value=[baseline]):
                    with mock.patch.object(sources, "fetch_json", side_effect=fetch):
                        result = service.refresh(
                            source="json", source_url="https://example.invalid/models.json",
                            initiated_by=initiator, now=1.0,
                        )
                self.assertEqual(result["status"], "changed")
                self.assertFalse(result["activated"])
                self.assertEqual(service.load_active(), active_during_fetch[0])
                self.assertEqual(service.load_state()["active_version"], active_during_fetch[0]["version"])
                self.assertEqual(service.load_proposed()["models"][0]["output_cost"], 50)


class OverlayReviewLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = calib.ModelCalibrationService(Path(tmp.name))
        local = mock.patch.object(calib, "fetch_local_source", return_value=[_entry("m1")])
        local.start()
        self.addCleanup(local.stop)
        self.service.ensure_bootstrap(now=1.0)

    def refresh_rows(self, rows: list[dict], *, now: float, **options: object) -> dict:
        with mock.patch.object(sources, "fetch_json", return_value=({"models": rows}, "fixture")):
            return self.service.refresh(
                source="json", source_url="https://example.invalid/models.json",
                force=True, now=now, **options,
            )

    def test_repeated_pending_proposal_keeps_version_history_and_review_data(self) -> None:
        rows = [
            {"provider": "fixture", "model": "m1", "input_cost": 2, "output_cost": 8},
            {"provider": "fixture", "model": "new", "input_cost": 1, "output_cost": 4},
        ]
        active_before = self.service.active_path.read_bytes()
        first = self.refresh_rows(rows, now=100.0)
        proposal_before = self.service.proposed_path.read_bytes()
        history_before = self.service.list_history()
        result = self.refresh_rows(rows, now=200.0, initiated_by="STARTUP")
        self.assertEqual(result["status"], "no_change")
        self.assertFalse(result["notification"]["should_notify"])
        self.assertEqual(result["diff"]["newly_discovered_models"], [])
        self.assertEqual(self.service.active_path.read_bytes(), active_before)
        self.assertEqual(self.service.proposed_path.read_bytes(), proposal_before)
        self.assertEqual(self.service.list_history(), history_before)
        self.assertEqual(self.service.status_report()["discovered_model_count"], 1)
        self.assertEqual(self.service.analyze()["proposed_version"], first["calibration_version"])

    def test_benchmark_only_overlay_preserves_known_prices(self) -> None:
        self.refresh_rows([
            {"provider": "fixture", "model": "m1", "input_cost": 2, "output_cost": 8},
        ], now=100.0, activation_policy="auto")
        result = self.refresh_rows([
            {"provider": "fixture", "model": "m1", "evaluations": {"coding": 75}},
        ], now=200.0)
        self.assertEqual(result["status"], "changed")
        self.assertEqual(result["diff"]["pricing_changes"], [])
        proposed = self.service.load_proposed()["models"][0]
        self.assertEqual((proposed["input_cost"], proposed["output_cost"]), (2, 8))

    def test_discovered_model_updates_retain_values_and_history_without_approval(self) -> None:
        def rows(price: float, benchmark: float) -> list[dict]:
            return [
                {"provider": "fixture", "model": "m1", "output_cost": 8},
                {"provider": "fixture", "model": "new", "output_cost": price,
                 "evaluations": {"coding": benchmark}},
            ]
        self.refresh_rows(rows(4, 50), now=100.0, activation_policy="auto")
        result = self.refresh_rows(rows(3, 60), now=200.0)
        self.assertEqual(result["status"], "changed")
        self.assertEqual(result["diff"]["newly_discovered_models"], [])
        proposed = self.service.load_proposed()
        model = proposed["discovered_models"][0]
        self.assertEqual(model["status"], "DISCOVERED")
        self.assertTrue(any(item["previous"] == 4 and item["new"] == 3 for item in model["pricing_history"]))
        self.assertTrue(any(item["previous"] == 50 and item["new"] == 60 for item in model["benchmark_history"]))
        self.assertNotIn("new", {item["model"] for item in self.service._router_models(proposed)})

    def test_auto_activation_requires_a_completed_simulation(self) -> None:
        before = self.service.active_path.read_bytes()
        result = self.refresh_rows([
            {"provider": "fixture", "model": "m1", "input_cost": 2, "output_cost": 8},
        ], now=100.0, activation_policy="auto", run_simulation_flag=False)
        self.assertFalse(result["activated"])
        self.assertIsNone(result["simulation"])
        self.assertEqual(self.service.active_path.read_bytes(), before)


class SimulationAndDiffTests(unittest.TestCase):
    def test_diff_reports_no_meaningful_change_for_identical_catalogs(self) -> None:
        service = calib.ModelCalibrationService(Path(tempfile.mkdtemp()))
        service.refresh(fetch_fn=lambda: [_entry("m1")], now=1.0)
        active = service.load_active()
        diff = calib.diff_calibrations(active, active)
        self.assertFalse(diff["has_meaningful_change"])

    def test_simulation_flags_a_capability_regression(self) -> None:
        specs_before = [mr._parse_model(_entry("m1", capability=5, cost=3, efforts=("high",)))]
        specs_after = [mr._parse_model(_entry("m1", capability=1, cost=3, efforts=("low",)))]
        routing_before = calib.recalculate_routing(specs_before, routing_optimization="best")
        routing_after = calib.recalculate_routing(specs_after, routing_optimization="best")
        previous = {
            "models": [calib._model_spec_to_dict(specs_before[0]) | {"key": "fixture/m1"}],
            "routing": routing_before,
        }
        new = {
            "models": [calib._model_spec_to_dict(specs_after[0]) | {"key": "fixture/m1"}],
            "routing": routing_after,
        }
        diff = calib.diff_calibrations(previous, new)
        simulation = calib.run_simulation(previous, new, diff)
        self.assertFalse(simulation["regression_ok"])
        self.assertTrue(simulation["regressions"])


class CalibrationPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = calib.ModelCalibrationService(Path(tmp.name))
        self.service.refresh(fetch_fn=lambda: [_entry("m1", cost=2)], now=1.0)
        self.previous = self.service.load_active()
        update = self.service.refresh(
            fetch_fn=lambda: [_entry("m1", cost=4)], now=100.0, force=True,
        )
        self.version = update["calibration_version"]

    def test_readers_observe_the_committed_version_throughout_publication(self) -> None:
        reader = calib.ModelCalibrationService(self.service.state_dir)
        real_replace = calib.os.replace
        observations = []

        def inspect_before_commit(source, destination):
            if Path(destination) == self.service.catalog_override_path:
                report = reader.status_report()
                catalog = mr.load_model_catalog(reader.catalog_override_path)
                observations.append((report["active_version"], catalog[0].relative_cost))
            return real_replace(source, destination)

        with mock.patch.object(calib.os, "replace", side_effect=inspect_before_commit):
            self.service.activate(self.version)
        self.assertEqual(observations, [(self.previous["version"], 2)])
        self.assertEqual(reader.status_report()["active_version"], self.version)
        self.assertEqual(mr.load_model_catalog(reader.catalog_override_path)[0].relative_cost, 4)

    def test_failed_state_publication_keeps_active_bytes_and_releases_lock(self) -> None:
        before = (self.service.active_path.read_bytes(), self.service.catalog_override_path.read_bytes())
        real_replace = calib.os.replace

        def fail_state(source, destination):
            if Path(destination) == self.service.state_path:
                raise OSError("fixture: state storage unavailable")
            return real_replace(source, destination)

        with mock.patch.object(calib.os, "replace", side_effect=fail_state):
            with self.assertRaises(OSError):
                self.service.activate(self.version)
        self.assertEqual(
            (self.service.active_path.read_bytes(), self.service.catalog_override_path.read_bytes()), before,
        )
        self.assertFalse(self.service.lock_path.exists())
        self.service.activate(self.version)
        self.assertEqual(self.service.load_active()["version"], self.version)

    def test_invalid_lock_marker_cannot_allow_another_mutation(self) -> None:
        other = calib.ModelCalibrationService(self.service.state_dir)
        self.service.acquire_lock()
        try:
            self.service.lock_path.write_text("{truncated", encoding="utf-8")
            with self.assertRaises(calib.CalibrationBusyError):
                other.activate(self.version)
            with self.assertRaises(calib.CalibrationBusyError):
                other.approve_discovered_model("fixture/m2")
            self.assertEqual(other.load_active(), self.previous)
        finally:
            self.service.release_lock()
        other.activate(self.version)
        self.assertEqual(other.load_active()["version"], self.version)

    def test_catalog_publication_failure_is_reported_without_new_success_time(self) -> None:
        real_replace = calib.os.replace
        successful_at = self.service.status_report()["last_successful_refresh_at"]

        def fail_catalog(source, destination):
            if Path(destination) == self.service.catalog_override_path:
                raise OSError("fixture: catalog storage unavailable")
            return real_replace(source, destination)

        with mock.patch.object(calib.os, "replace", side_effect=fail_catalog):
            result = self.service.refresh(
                fetch_fn=lambda: [_entry("m1", cost=5)], now=200.0, force=True,
                activation_policy="auto",
            )
        self.assertEqual(result["status"], "failed")
        status = self.service.status_report()
        self.assertEqual(status["last_successful_refresh_at"], successful_at)
        self.assertEqual(status["last_attempted_status"], "failed")
        self.assertEqual(self.service.load_active(), self.previous)

    def test_status_repairs_a_mismatched_filtered_catalog_from_activated_data(self) -> None:
        self.service.activate(self.version)
        active_bytes = self.service.active_path.read_bytes()
        publication = json.loads(self.service.catalog_override_path.read_text())
        # Valid JSON and a valid embedded version are not enough: the router
        # must receive the model data that belongs to that activated version.
        publication["models"][0]["relative_cost"] = 1
        self.service.catalog_override_path.write_text(json.dumps(publication))
        report = calib.ModelCalibrationService(self.service.state_dir).status_report()
        self.assertEqual(report["active_version"], self.version)
        self.assertTrue(report["healthy"])
        self.assertEqual(self.service.active_path.read_bytes(), active_bytes)
        self.assertEqual(mr.load_model_catalog(self.service.catalog_override_path)[0].relative_cost, 4)


class DynamicRouterHookTests(unittest.TestCase):
    """Zero-behavior-change-by-default guarantee for the live routing hook."""

    def test_no_override_env_var_means_no_override_path(self) -> None:
        import dynamic_router

        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("SWARM_MODEL_CALIBRATION_CATALOG", None)
            self.assertIsNone(dynamic_router.active_calibration_catalog_path())

    def test_override_path_is_ignored_when_the_file_does_not_exist(self) -> None:
        import dynamic_router

        with mock.patch.dict("os.environ", {"SWARM_MODEL_CALIBRATION_CATALOG": "/no/such/file.json"}):
            self.assertIsNone(dynamic_router.active_calibration_catalog_path())

    def test_override_path_is_used_when_the_file_exists(self) -> None:
        import dynamic_router

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "active_catalog.json"
            path.write_text(json.dumps({"models": []}), encoding="utf-8")
            with mock.patch.dict("os.environ", {"SWARM_MODEL_CALIBRATION_CATALOG": str(path)}):
                self.assertEqual(dynamic_router.active_calibration_catalog_path(), path)


if __name__ == "__main__":
    unittest.main()
