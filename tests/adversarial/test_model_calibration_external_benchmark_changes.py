"""Issue #205 — refreshed external benchmark changes must reach calibration.

Expected behavior is derived from the issue, independently of the current
implementation:

* A refresh retrieves and normalizes current benchmark information, detects
  meaningful changes, recalculates deterministic routing metrics, and creates
  a proposed calibration when appropriate.
* The result explicitly reports benchmark changes and routing impact
  (acceptance criteria 3 and 13).
* A refresh must not claim that model data is current while silently throwing
  away a large benchmark change returned by the configured source.

The fixtures below exercise the public ``ModelCalibrationService.refresh``
entry point with the documented Artificial Analysis adapter boundary mocked
locally.  There is no network access.  The bundled catalog read is also
replaced with one deterministic model so the only input difference between
the two refreshes is a 20-point increase in a named coding benchmark.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import model_calibration as calib  # noqa: E402


def model_entry() -> dict:
    return {
        "provider": "fixture",
        "agent": "fixture",
        "model": "model-one",
        "model_id": "model-one",
        "active": True,
        "recommended": True,
        "deprecated": False,
        "superseded_by": None,
        "supported_efforts": ["medium"],
        "strengths": ["coding"],
        "weaknesses": [],
        "relative_capability": 3,
        "relative_cost": 3,
        "relative_token_efficiency": 3,
        "relative_latency": 3,
        "benchmarks": {},
        "benchmark_source": None,
        "benchmark_date": None,
        "notes": "",
    }


def source_response(score: float, version: str) -> tuple[list[dict], dict]:
    return (
        [
            {
                "provider": "fixture",
                "model": "model-one",
                "evaluations": {"coding_agent_index": score},
            }
        ],
        {
            "kind": "artificial_analysis",
            "version": version,
            "status": "ok",
            "url": None,
        },
    )


class ExternalBenchmarkChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = calib.ModelCalibrationService(Path(self._tmp.name))

    def refresh_with_score(
        self,
        score: float,
        *,
        now: float,
        version: str,
        activation_policy: str = "manual",
    ) -> dict:
        with (
            mock.patch("model_calibration.fetch_local_source", return_value=[model_entry()]),
            mock.patch(
                "model_calibration._sources.fetch_source",
                return_value=source_response(score, version),
            ),
        ):
            return self.service.refresh(
                source="artificial_analysis",
                force=True,
                now=now,
                activation_policy=activation_policy,
            )

    def test_large_external_benchmark_delta_is_reported_as_meaningful(self) -> None:
        self.refresh_with_score(60.0, now=1.0, version="source-v1", activation_policy="auto")

        result = self.refresh_with_score(80.0, now=100.0, version="source-v2")

        self.assertEqual(
            result["status"],
            "changed",
            "Artificial Analysis returned a 60 -> 80 coding benchmark change, "
            "but refresh reported that model data was current. External "
            "evaluation values are fetched and copied to "
            "external_evaluations, yet the deterministic diff only compares "
            "the unchanged seed-catalog benchmark summary, so real benchmark "
            "updates can never produce a proposed calibration.",
        )
        self.assertTrue(
            result["diff"]["benchmark_changes"],
            "Acceptance criterion 13 requires refresh results to show "
            "benchmark changes; the configured benchmark source changed by "
            "20 points but the result summary recorded zero changes.",
        )

    def test_changed_external_benchmark_is_retained_for_review(self) -> None:
        self.refresh_with_score(60.0, now=1.0, version="source-v1", activation_policy="auto")
        self.refresh_with_score(80.0, now=100.0, version="source-v2")

        proposed = self.service.load_proposed()
        self.assertIsNotNone(
            proposed,
            "A meaningful benchmark update must produce a proposed "
            "calibration instead of being discarded as a no-change refresh.",
        )
        by_key = {entry["key"]: entry for entry in proposed["models"]}
        self.assertEqual(
            by_key["fixture/model-one"]["external_evaluations"]["coding_agent_index"],
            80.0,
            "The latest normalized benchmark value must be retained in the "
            "proposal users inspect; the active last-known-good value should "
            "remain untouched until activation.",
        )


if __name__ == "__main__":
    unittest.main()
