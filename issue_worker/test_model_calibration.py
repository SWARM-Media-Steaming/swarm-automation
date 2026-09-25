"""Tests for Model Routing Calibration (issue #205).

Deterministic and offline: the "remote" source path is exercised only through
an injected ``fetch_fn`` (never a live network call), and the "local" source
path reads the real bundled ``skills/model-router/models.yaml`` so these tests
also double as a smoke test that file still parses.

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
