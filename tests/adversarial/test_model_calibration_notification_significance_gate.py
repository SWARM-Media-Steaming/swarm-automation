"""Issue #205 (Model Routing Calibration) — the startup notification gate
fires on *any* recorded pricing change, however trivial, instead of only a
"significant" one, so a refresh that provably changed nothing about routing
still produces an intrusive notification on every affected startup.

Expected behaviour, derived from the issue before reading the implementation:

- "Do not generate intrusive notifications for every startup refresh. Only
  surface a meaningful notification when: a new model is discovered;
  significant pricing changes occur; routing behavior materially changes; a
  refresh repeatedly fails; manual review is required. Routine successful
  startup checks should simply update the AI Configuration status."
- This is specifically a *startup*-refresh concern: `ui/app.js`'s
  `onModelCalibrationRefreshed` (the only caller that reads
  `payload.notification` and calls `showToast`) is wired exclusively to the
  `model-calibration-refreshed` event that `src/main.rs`'s
  `spawn_startup_model_calibration_refresh` emits after every app launch — a
  manual click never consults this gate, so this suite is squarely about the
  "every restart" annoyance the requirement calls out.
- "significant" is the operative word gating pricing changes, standing beside
  "materially changes" for routing. A pricing change on a candidate that
  affects zero routing decisions, by any amount, is the textbook case of an
  *insignificant* change the requirement says must not page the user.

What the implementation does: `notification_for` treats a non-empty
`diff["pricing_changes"]` list as notification-worthy outright::

    pricing = diff.get("pricing_changes") or []
    if pricing:
        reasons.append(f"{len(pricing)} pricing change...")

and `diff_calibrations` populates that list from a bare inequality check with
no magnitude threshold at all::

    if any(prev.get(field) != model.get(field) for field in price_fields):
        pricing_changes.append(...)

Neither the size of the change nor its effect on any routing decision is
considered. The test below constructs a refresh where every workload's
routing decision is byte-for-byte identical before and after (asserted as a
sanity check), and the only difference anywhere in the calibration is the
smallest possible unit change to one candidate's ordinal cost rank
(`relative_cost` incremented by 1, on a 1-5 scale). That refresh still
reports `should_notify: True`.

The assertion does not dictate a specific significance threshold or
mechanism — filtering by magnitude, requiring the change to touch a model
that is actually in some workload's routing decision, or something else
would all satisfy it. It only requires that a refresh which changed no
routing behavior at all, by the smallest representable pricing delta, does
not by itself justify the notification the issue reserves for "significant"
changes.
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

    def test_a_one_unit_price_rank_bump_with_zero_routing_impact_does_not_notify(
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
        self.assertFalse(
            notification["should_notify"],
            "A refresh that changed zero routing decisions, via the "
            "smallest possible ordinal cost-rank bump on a model nothing "
            "routes to, still triggered a startup notification "
            f"({notification!r}). `notification_for` treats any non-empty "
            "`pricing_changes` list as notification-worthy with no "
            "magnitude or routing-relevance threshold, contradicting "
            "'Do not generate intrusive notifications for every startup "
            "refresh. Only surface a meaningful notification when ... "
            "significant pricing changes occur ... routing behavior "
            "materially changes.'",
        )


if __name__ == "__main__":
    unittest.main()
