"""Issue #374 migration of the #205 notification-significance suite.

#374 requirement 11 is more specific than the earlier suppression rule: every
automatic activation is visible through ``notification_for``. A small change
with no routing winner change must still be recorded and announced once when
its validated calibration is activated.
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


class NotificationSignificanceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = calib.ModelCalibrationService(Path(self._tmp.name))

    def test_a_one_unit_price_rank_bump_with_zero_routing_impact_notifies_activation(
        self,
    ) -> None:
        preferred = model_entry(
            "winner", relative_capability=5, relative_cost=1, recommended=True
        )
        # Dominated on every axis the router scores (lower capability, higher
        # cost, worse efficiency, worse latency, not recommended): whatever
        # workload this catalog routes anything to, it must be "winner", not
        # this model, both before and after the bump below.
        bystander_v1 = model_entry(
            "bystander",
            relative_capability=1,
            relative_cost=2,
            relative_token_efficiency=1,
            relative_latency=1,
            recommended=False,
        )
        self.service.refresh(
            fetch_fn=lambda: [preferred, bystander_v1],
            now=1.0,
            activation_policy="auto",
        )
        baseline_routing = self.service.load_active()["routing"]

        # The smallest possible pricing change on this same, uninvolved
        # candidate: its 1-5 ordinal cost rank moves by exactly one step.
        bystander_v2 = model_entry(
            "bystander",
            relative_capability=1,
            relative_cost=3,
            relative_token_efficiency=1,
            relative_latency=1,
            recommended=False,
        )
        result = self.service.refresh(
            fetch_fn=lambda: [preferred, bystander_v2],
            now=100.0,
            force=True,
            activation_policy="auto",
        )
        diff = result["diff"]

        self.assertEqual(
            diff["routing_changes"],
            [],
            "sanity check: this fixture must not change any workload's "
            "routing decision, so the only possible signal is the pricing "
            "change itself",
        )
        self.assertEqual(
            self.service.load_active()["routing"],
            baseline_routing,
            "sanity check: routing decisions must be byte-identical before "
            "and after this refresh",
        )
        self.assertEqual(
            len(diff["pricing_changes"]),
            1,
            "sanity check: exactly one pricing change (the 1-unit rank "
            "bump) must be recorded",
        )

        notification = result["notification"]
        self.assertTrue(notification["should_notify"], notification)
        self.assertTrue(result["activated"])
        self.assertIn("applied automatically", notification["message"])


if __name__ == "__main__":
    unittest.main()
