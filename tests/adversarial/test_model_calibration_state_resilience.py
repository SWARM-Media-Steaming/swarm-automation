"""Issue #205 (Model Routing Calibration) — an unreadable calibration file on
disk takes the whole AI Configuration status panel down instead of degrading
to the last known-good calibration, and leaves no in-app way to recover.

Expected behaviour, derived from the issue before reading the implementation:

- "The application MUST NOT depend on the external model-data source being
  available in order to start. **Always load the last known-good calibration
  first.**"
- Acceptance criterion 5: "Startup does not depend on successful external API
  access." Acceptance criterion 6: "The previous known-good calibration is
  always immediately usable." Acceptance criterion 9: "AI Configuration
  displays last refresh time and refresh status." Acceptance criterion 15:
  "Failed refreshes never corrupt or replace the active calibration."
- "Never prevent users from using AI functionality because calibration
  refresh failed." / "If refresh fails ... Model data refresh failed.
  Existing routing configuration remains active. Include useful error
  information without exposing secrets."

The whole design of this feature is that calibration is advisory data laid
*beside* routing, so nothing about it may ever become a hard dependency. The
status read is the single entry point the AI Configuration page has (the Rust
`get_model_calibration_status` command shells out to `model_calibration.py
status`, and `ui/app.js`'s `refreshModelCalibration` renders whatever it
returns); if that read raises, the command returns `Err`, and the panel shows
no active version, no last-refresh time, no refresh status, and no routing
table at all — the user cannot even see that a known-good calibration exists,
let alone that a refresh failed.

`ModelCalibrationService.status_report` guards only `ensure_bootstrap` behind
`except CalibrationError: pass`. The `load_state()` / `load_active()` /
`load_proposed()` calls immediately after it are unguarded, and `_read_json`
raises `CalibrationError` for any file it cannot parse. So a single
unparseable byte in `calibration_state.json`, `calibration_active.json` or
`calibration_proposed.json` permanently blanks the panel.

Worse, for `calibration_state.json` there is no recovery path from inside the
app: `_refresh_locked` also opens with an unguarded `self.load_state()`, so
the manual "Refresh Model Data" button — the one action the UI offers when
something is wrong — raises out of `refresh()` too, exits the CLI non-zero and
can never rewrite the bad file. The user has to find and delete state on disk
by hand.

These files are not hypothetically corruptible. `_atomic_write_json` writes
every one of them through a *fixed* temp name (`<file>.json.tmp`), and this
feature deliberately runs several writers against one app-wide state
directory at once: the startup refresh subprocess (`src/main.rs`'s
`spawn_startup_model_calibration_refresh`) and the status-poll subprocesses
`ui/app.js` fires on every AI Configuration view switch, every 20s while that
view is open, and every 700ms during a manual refresh — and `status_report`
itself *writes* (via `ensure_bootstrap`) on that read path. Two of those
sharing one temp filename is a torn-write waiting to happen; nothing in the
module tolerates the result.

The assertions below therefore only require graceful degradation, not any
particular repair strategy: a status read must still return a dict, and a
forced manual refresh must still return a result rather than raise.
"""

from __future__ import annotations

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
    """A minimal but complete catalog entry `model_router._parse_model` accepts."""
    entry = {
        "provider": "fixture",
        "agent": "fixture",
        "model": model,
        "model_id": model,
        "active": True,
        "recommended": True,
        "deprecated": False,
        "superseded_by": None,
        "supported_efforts": ["medium"],
        "strengths": [],
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
    entry.update(overrides)
    return entry


class CalibrationStateResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = calib.ModelCalibrationService(Path(self._tmp.name))
        # One good calibration, activated, so a "last known-good" genuinely
        # exists on disk for every case below to fall back to.
        self.service.refresh(
            fetch_fn=lambda: [model_entry("m1")], now=1.0, activation_policy="auto"
        )
        self.assertIsNotNone(
            self.service.status_report()["active_version"],
            "fixture setup must leave an activated calibration behind",
        )

    def test_status_read_survives_an_unreadable_state_file(self) -> None:
        self.service.state_path.write_text("{ truncated", encoding="utf-8")
        try:
            report = self.service.status_report()
        except calib.CalibrationError as error:
            self.fail(
                "An unparseable calibration_state.json must not take the AI "
                "Configuration status panel down — the last known-good "
                f"calibration has to stay visible. Raised: {error}"
            )
        self.assertIsInstance(report, dict)

    def test_manual_refresh_can_still_run_with_an_unreadable_state_file(self) -> None:
        self.service.state_path.write_text("{ truncated", encoding="utf-8")
        try:
            result = self.service.refresh(
                fetch_fn=lambda: [model_entry("m1")], now=99.0, force=True
            )
        except calib.CalibrationError as error:
            self.fail(
                "The manual Refresh Model Data action is the only in-app "
                "recovery the UI offers; it must report a refresh outcome "
                f"instead of raising out of the service. Raised: {error}"
            )
        self.assertIn("status", result)

    def test_status_read_survives_an_unreadable_active_calibration(self) -> None:
        self.service.active_path.write_text('{"version": "2026-09', encoding="utf-8")
        try:
            report = self.service.status_report()
        except calib.CalibrationError as error:
            self.fail(
                "A torn calibration_active.json must degrade to 'not yet "
                "calibrated' plus the recorded refresh status, never blank "
                f"the whole panel. Raised: {error}"
            )
        self.assertIsInstance(report, dict)

    def test_status_read_survives_an_unreadable_proposed_calibration(self) -> None:
        # Produce a real proposed calibration first, then corrupt it: the
        # active one is still perfectly good and must still be reported.
        self.service.refresh(
            fetch_fn=lambda: [model_entry("m1", relative_cost=5)], now=99.0, force=True
        )
        self.service.proposed_path.write_text("not json at all", encoding="utf-8")
        try:
            report = self.service.status_report()
        except calib.CalibrationError as error:
            self.fail(
                "A torn calibration_proposed.json must not hide the active "
                f"calibration it was only ever proposed against. Raised: {error}"
            )
        self.assertIsInstance(report, dict)


if __name__ == "__main__":
    unittest.main()
