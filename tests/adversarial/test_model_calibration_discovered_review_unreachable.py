"""Issue #374 migration of the former #205 approval reachability suite.

Approval is now forbidden surface area: a validated model is onboarded by the
refresh itself, while stale approval state and desktop affordances disappear.
"""

from __future__ import annotations

import re
import contextlib
import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import model_calibration as calib  # noqa: E402


def model_entry(model: str) -> dict:
    return {
        "provider": "fixture", "agent": "fixture", "model": model,
        "model_id": model, "active": True, "recommended": True,
        "deprecated": False, "superseded_by": None,
        "supported_efforts": ["medium"], "strengths": [], "weaknesses": [],
        "relative_capability": 3, "relative_cost": 3,
        "relative_token_efficiency": 3, "relative_latency": 3,
        "benchmarks": {}, "benchmark_source": None,
        "benchmark_date": None, "notes": "",
    }


class ApprovalStateRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.service = calib.ModelCalibrationService(Path(temporary.name))

    def test_valid_new_model_needs_no_approval_or_second_refresh(self) -> None:
        self.service.refresh(fetch_fn=lambda: [model_entry("established")], now=1.0)
        result = self.service.refresh(
            fetch_fn=lambda: [model_entry("established"), model_entry("brand-new")],
            now=2.0, force=True,
        )
        by_key = {row["key"]: row for row in self.service.load_active()["models"]}
        self.assertTrue(result["activated"])
        self.assertIn(by_key["fixture/brand-new"]["status"], calib.ROUTABLE_STATUSES)
        self.assertFalse(hasattr(self.service, "approve_discovered_model"))

    def test_legacy_approval_state_is_removed_on_save(self) -> None:
        self.service.save_state({
            "approved_models": ["fixture/brand-new"],
            "last_approved_at": "stale", "last_approved_by": "USER",
        })
        state = self.service.load_state()
        self.assertNotIn("approved_models", state)
        self.assertNotIn("last_approved_at", state)
        self.assertNotIn("last_approved_by", state)


class ApprovalSurfaceRetirementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main_rs = (REPO_ROOT / "src" / "main.rs").read_text(encoding="utf-8")
        cls.app_js = (REPO_ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        cls.index_html = (REPO_ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    def registered_commands(self) -> list[str]:
        match = re.search(r"tauri::generate_handler!\[(.*?)\]", self.main_rs, re.DOTALL)
        self.assertIsNotNone(match)
        return [name.strip() for name in match.group(1).split(",") if name.strip()]

    def test_backend_registers_no_model_approval_command(self) -> None:
        self.assertFalse(any("approve_discovered" in name for name in self.registered_commands()))

    def test_frontend_has_no_approval_invocation_or_affordance(self) -> None:
        invoked = set(re.findall(r'invoke\(\s*"([^"]+)"', self.app_js))
        self.assertFalse(any("approve_discovered" in name for name in invoked))
        self.assertNotIn("Approve for routing", self.app_js)
        self.assertNotIn("Activate proposed calibration", self.index_html)

    def test_cli_rejects_the_retired_approve_action(self) -> None:
        parser = calib.build_calibration_parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--state-dir", "/tmp/fixture", "approve", "fixture/model"])


if __name__ == "__main__":
    unittest.main()
