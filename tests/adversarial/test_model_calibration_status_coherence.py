"""Issue #205: the AI Configuration status must preserve known-good truth.

The issue requires the last known-good calibration to remain immediately
usable and separately visible from refresh state.  Status metadata and the
short-lived progress snapshot are advisory files; damage to either must not
hide a valid active calibration, invent a reviewable proposal, or make the
desktop command fail.  These tests exercise the production CLI boundary that
the Rust commands invoke, using only deterministic local fixtures.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

import model_calibration as calibration  # noqa: E402


def model_entry(*, relative_cost: int = 3) -> dict:
    return {
        "provider": "fixture",
        "agent": "fixture",
        "model": "known-good",
        "model_id": "known-good",
        "active": True,
        "recommended": True,
        "deprecated": False,
        "superseded_by": None,
        "supported_efforts": ["medium"],
        "strengths": [],
        "weaknesses": [],
        "relative_capability": 3,
        "relative_cost": relative_cost,
        "relative_token_efficiency": 3,
        "relative_latency": 3,
        "benchmarks": {},
        "benchmark_source": None,
        "benchmark_date": None,
        "notes": "",
    }


class CalibrationStatusCoherenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        self.service = calibration.ModelCalibrationService(self.state_dir)
        result = self.service.refresh(
            fetch_fn=lambda: [model_entry()], now=1_800_000_000.0
        )
        self.assertTrue(result["activated"])
        self.active_version = self.service.load_active()["version"]

    def cli_status(self) -> tuple[subprocess.CompletedProcess[str], dict]:
        completed = subprocess.run(
            [
                sys.executable,
                str(ISSUE_WORKER / "model_calibration.py"),
                "--state-dir",
                str(self.state_dir),
                "status",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            self.fail(
                f"status CLI did not return JSON (exit {completed.returncode}): "
                f"stdout={completed.stdout!r}, stderr={completed.stderr!r}, error={error}"
            )
        return completed, payload

    def test_damaged_state_metadata_cannot_hide_the_valid_active_version(self) -> None:
        self.service.state_path.write_text("{truncated", encoding="utf-8")

        completed, status = self.cli_status()

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertEqual(status["active_version"], self.active_version)
        self.assertEqual(status["active_calibration"]["version"], self.active_version)
        self.assertTrue(status["healthy"])

    def test_damaged_progress_snapshot_does_not_take_down_status(self) -> None:
        self.service.progress_path.write_text("not-json", encoding="utf-8")

        completed, status = self.cli_status()

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertEqual(status["active_version"], self.active_version)
        self.assertFalse(status["refresh_running"])

    def test_damaged_proposal_cannot_advertise_a_phantom_update(self) -> None:
        proposed = self.service.refresh(
            fetch_fn=lambda: [model_entry(relative_cost=5)],
            now=1_800_000_100.0,
            force=True,
        )
        self.assertEqual(proposed["status"], "changed")
        self.assertFalse(proposed["activated"])
        self.service.proposed_path.write_text("{truncated", encoding="utf-8")

        completed, status = self.cli_status()

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertEqual(status["active_version"], self.active_version)
        self.assertIsNone(status["proposed_calibration"])
        self.assertFalse(
            status["has_newer_proposed"],
            "The UI must not offer activation when no readable proposal exists.",
        )


if __name__ == "__main__":
    unittest.main()
