"""Issue #205 — refreshed external benchmark changes must reach calibration.

Expected behavior is derived from the issue, independently of the current
implementation:

* A configured refresh retrieves and normalizes benchmark information,
  detects meaningful changes, and activates the validated calibration.
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
        "provider": "openai",
        "agent": "codex",
        "model": "gpt-5.6-sol",
        "model_id": "gpt-5.6-sol",
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
                "provider": "openai",
                "model": "gpt-5.6-sol",
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
        activation_policy: str = "auto",
    ) -> dict:
        with (
            mock.patch.dict("os.environ", {"ARTIFICIAL_ANALYSIS_API_KEY": "fixture-key"}),
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
                available_models={"codex": ["gpt-5.6-sol"]},
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
            "updates can never produce an activated calibration diff.",
        )
        self.assertTrue(
            result["diff"]["benchmark_changes"],
            "Acceptance criterion 13 requires refresh results to show "
            "benchmark changes; the configured benchmark source changed by "
            "20 points but the result summary recorded zero changes.",
        )

    def test_changed_external_benchmark_is_retained_in_the_active_calibration(self) -> None:
        self.refresh_with_score(60.0, now=1.0, version="source-v1", activation_policy="auto")
        self.refresh_with_score(80.0, now=100.0, version="source-v2")

        active = self.service.load_active()
        self.assertIsNotNone(active)
        by_key = {entry["key"]: entry for entry in active["models"]}
        self.assertEqual(
            by_key["openai/gpt-5.6-sol"]["external_evaluations"]["coding_agent_index"],
            80.0,
            "The latest normalized benchmark value must be retained in the "
            "automatically activated calibration.",
        )


if __name__ == "__main__":
    unittest.main()
