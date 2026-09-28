"""Offline fixtures shared by issue #205 acceptance tests, never product code."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "issue_worker"))

import model_calibration as calibration  # noqa: E402
import model_data_sources as sources  # noqa: E402
import model_router as router  # noqa: E402

NOW = 1_800_000_000.0
SOURCE_URL = "https://example.invalid/model-data.json"


def model_entry(name="established", **changes):
    entry = {
        "provider": "fixture", "agent": "fixture", "model": name,
        "model_id": name, "active": True, "recommended": True,
        "deprecated": False, "superseded_by": None,
        "supported_efforts": ["low", "medium", "high"],
        "strengths": ["coding"], "weaknesses": [],
        "relative_capability": 5, "relative_cost": 3,
        "relative_token_efficiency": 3, "relative_latency": 3,
        "benchmarks": {}, "benchmark_source": None,
        "benchmark_date": None, "notes": "",
    }
    entry.update(changes)
    return entry


def price_row(name="established", **changes):
    row = {"provider": "fixture", "model": name, "input_cost": 2, "output_cost": 8}
    row.update(changes)
    return row


class CalibrationUAT(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = calibration.ModelCalibrationService(Path(tmp.name))
        self.local = [model_entry()]
        local_patch = mock.patch.object(calibration, "fetch_local_source", side_effect=lambda: copy.deepcopy(self.local))
        local_patch.start()
        self.addCleanup(local_patch.stop)
        # Every test must explicitly supply its response. Even an accidental
        # new network path through the adapter fails instead of going online.
        network_patch = mock.patch.object(sources, "fetch_json", side_effect=AssertionError("Unexpected network request"))
        network_patch.start()
        self.addCleanup(network_patch.stop)

    def remote(self, payload, *, now=NOW, kind="json", **options):
        with mock.patch.object(sources, "fetch_json", return_value=(copy.deepcopy(payload), "fixture-version")):
            with mock.patch.dict("os.environ", {"ARTIFICIAL_ANALYSIS_API_KEY": "offline-fixture"}):
                return self.service.refresh(
                    source=kind, source_url=SOURCE_URL, now=now, force=True, **options
                )

    def active_bytes(self):
        return (self.service.active_path.read_bytes(), self.service.catalog_override_path.read_bytes())


def ui_snapshots():
    """Real backend responses for the Node DOM integration suite."""
    with TemporaryDirectory() as tmp:
        service = calibration.ModelCalibrationService(Path(tmp))
        with mock.patch.object(calibration, "fetch_local_source", return_value=[model_entry()]):
            service.ensure_bootstrap(now=NOW)
            initial_payload = {"models": [price_row(input_cost=3.125, output_cost=17.75, evaluations={"fixture_coding": 63.125})]}
            with mock.patch.object(sources, "fetch_json", return_value=(initial_payload, "fixture-v1")):
                service.refresh(source="json", source_url=SOURCE_URL, force=True, now=NOW + 1, activation_policy="auto")
            payload = {"models": [price_row(input_cost=5.25, output_cost=23.5, evaluations={"fixture_coding": 70.875}), price_row("newly-discovered")]}
            with mock.patch.object(sources, "fetch_json", return_value=(payload, "fixture-v2")):
                result = service.refresh(source="json", source_url=SOURCE_URL, force=True, now=NOW + 2)
            proposal = service.status_report()
            service.activate(result["calibration_version"])
            with mock.patch.object(sources, "fetch_json", side_effect=sources.SourceError("Fixture source unavailable")):
                failure = service.refresh(source="json", source_url=SOURCE_URL, force=True, now=NOW + 3, initiated_by="STARTUP")
            failed_status = service.status_report()
            return {"result": result, "proposal": proposal, "failure": failure, "failed_status": failed_status}


if __name__ == "__main__":
    print(json.dumps(ui_snapshots()))
