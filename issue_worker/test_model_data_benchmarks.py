"""models.dev is the fixed source; Artificial Analysis benchmarks are an optional overlay."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import model_calibration as calib
import model_data_sources as sources
from test_model_calibration import _entry

MODELS_DEV_ROWS = [
    {"provider": "fixture", "model": "m1", "input_cost": 3.0, "output_cost": 15.0},
]
AA_ROWS = [
    {
        "provider": "fixture",
        "model": "m1",
        "source_id": "aa-1",
        # Conflicts with models.dev on purpose: prices must come from models.dev only.
        "input_cost": 99.0,
        "output_cost": 99.0,
        "evaluations": {"artificial_analysis_coding_index": 61.5},
        "speed": 80.0,
        "latency_seconds": 0.4,
        "deprecated": True,
    }
]


def _fetch(kind: str, url: str = ""):
    if kind == "models_dev":
        return [dict(row) for row in MODELS_DEV_ROWS], {"kind": kind, "status": "ok"}
    if kind == "artificial_analysis":
        return [dict(row) for row in AA_ROWS], {"kind": kind, "status": "ok"}
    raise AssertionError(kind)


class BenchmarkOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = calib.ModelCalibrationService(Path(tmp.name))
        local = mock.patch.object(calib, "fetch_local_source", return_value=[_entry("m1", provider="fixture")])
        local.start()
        self.addCleanup(local.stop)
        self.service.ensure_bootstrap(now=1.0)

    def refresh(self, fetch, *, key: str | None, now: float = 10.0) -> tuple[dict, list[str]]:
        calls: list[str] = []

        def recording(kind: str, url: str = ""):
            calls.append(kind)
            return fetch(kind, url)

        env = {k: v for k, v in os.environ.items() if k != sources.ARTIFICIAL_ANALYSIS_KEY_ENV}
        if key is not None:
            env[sources.ARTIFICIAL_ANALYSIS_KEY_ENV] = key
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(calib._sources, "fetch_source", side_effect=recording):
            return self.service.refresh(source="models_dev", force=True, now=now, activation_policy="auto"), calls

    def test_without_a_key_only_models_dev_is_queried(self) -> None:
        result, calls = self.refresh(_fetch, key=None)
        self.assertEqual(calls, ["models_dev"])
        self.assertEqual(result["source_warnings"], [])
        entry = next(m for m in self.service.load_active()["models"] if m["model"] == "m1")
        self.assertNotIn("external_evaluations", entry)

    def test_with_a_key_benchmarks_are_added_and_prices_stay_with_models_dev(self) -> None:
        result, calls = self.refresh(_fetch, key="aa-test-key")
        self.assertEqual(calls, ["models_dev", "artificial_analysis"])
        self.assertIn(result["status"], ("changed", "no_change"))
        entry = next(m for m in self.service.load_active()["models"] if m["model"] == "m1")
        self.assertEqual(entry["external_evaluations"], {"artificial_analysis_coding_index": 61.5})
        self.assertEqual(entry["speed"], 80.0)
        self.assertEqual(entry["input_cost"], 3.0)
        self.assertEqual(entry["output_cost"], 15.0)
        self.assertFalse(entry.get("deprecated"), "Artificial Analysis cannot retire a model")

    def test_a_benchmark_failure_keeps_the_models_dev_refresh_and_is_reported(self) -> None:
        def fetch(kind: str, url: str = ""):
            if kind == "artificial_analysis":
                raise sources.SourceError("Model source returned HTTP 401; existing calibration remains active.")
            return _fetch(kind, url)

        result, calls = self.refresh(fetch, key="bad-key")
        self.assertEqual(calls, ["models_dev", "artificial_analysis"])
        self.assertNotEqual(result["status"], "failed")
        self.assertEqual(len(result["source_warnings"]), 1)
        self.assertIn("Artificial Analysis benchmarks unavailable", result["source_warnings"][0])
        self.assertNotIn("bad-key", result["source_warnings"][0])
        entry = next(m for m in self.service.load_active()["models"] if m["model"] == "m1")
        self.assertEqual(entry["input_cost"], 3.0)

    def test_a_models_dev_failure_still_fails_the_refresh_and_keeps_the_active_data(self) -> None:
        before = self.service.load_active()

        def fetch(kind: str, url: str = ""):
            raise sources.SourceError("Could not read model source: check connectivity.")

        result, calls = self.refresh(fetch, key="aa-test-key")
        self.assertEqual(calls, ["models_dev"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["source_status"], "unavailable")
        self.assertEqual(self.service.load_active(), before)

    def test_the_key_is_only_ever_sent_to_artificial_analysis(self) -> None:
        with self.assertRaises(sources.SourceError):
            sources.fetch_json("https://models.dev/api.json", api_key="aa-test-key")


if __name__ == "__main__":
    unittest.main()
