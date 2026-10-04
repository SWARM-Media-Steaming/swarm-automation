"""Issue #374 migration of the former #205 DISCOVERED approval-gate suite.

#374 requirement 1 deliberately removes sticky DISCOVERED state and approval.
This suite proves that a valid source model becomes routable in the same
validated refresh and that onboarding remains visible in the immutable diff.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import model_calibration as calib  # noqa: E402


def model_entry(model: str, **overrides: object) -> dict:
    entry = {
        "provider": "fixture", "agent": "fixture", "model": model,
        "model_id": model, "active": True, "recommended": True,
        "deprecated": False, "superseded_by": None,
        "supported_efforts": ["medium"], "strengths": [], "weaknesses": [],
        "relative_capability": 3, "relative_cost": 3,
        "relative_token_efficiency": 3, "relative_latency": 3,
        "benchmarks": {}, "benchmark_source": None,
        "benchmark_date": None, "notes": "",
    }
    entry.update(overrides)
    return entry


class AutomaticOnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.service = calib.ModelCalibrationService(Path(temporary.name))
        self.service.refresh(fetch_fn=lambda: [model_entry("established")], now=1.0)
        self.result = self.service.refresh(
            fetch_fn=lambda: [model_entry("established"), model_entry("brand-new")],
            now=100.0, force=True,
        )

    def routable_models(self) -> set[str]:
        payload = json.loads(self.service.catalog_override_path.read_text(encoding="utf-8"))
        return {entry["model"] for entry in payload["models"]}

    def test_new_model_is_routable_after_one_validated_refresh(self) -> None:
        active = {entry["key"]: entry for entry in self.service.load_active()["models"]}
        self.assertIn(active["fixture/brand-new"]["status"], calib.ROUTABLE_STATUSES)
        self.assertIn("brand-new", self.routable_models())
        self.assertTrue(self.result["activated"])

    def test_onboarding_is_recorded_in_diff_and_notification(self) -> None:
        self.assertIn("fixture/brand-new", self.result["diff"]["newly_discovered_models"])
        self.assertEqual(self.result["diff"]["activation"]["policy"], "auto")
        self.assertTrue(self.result["notification"]["should_notify"])
        self.assertIn("new model", self.result["notification"]["message"])

    def test_unrelated_refresh_does_not_hide_or_repeat_onboarding(self) -> None:
        later = self.service.refresh(
            fetch_fn=lambda: [
                model_entry("established", relative_cost=4), model_entry("brand-new")
            ],
            now=200.0, force=True,
        )
        self.assertIn("brand-new", self.routable_models())
        self.assertEqual(later["diff"]["newly_discovered_models"], [])


if __name__ == "__main__":
    unittest.main()
