"""Issue #205 (Model Routing Calibration) — the DISCOVERED status is only a
one-refresh delay, not a gate: a newly discovered model is silently promoted
to ACTIVE and written into the live routing catalog by the next refresh that
changes anything at all, and nothing in the change summary mentions it.

Expected behaviour, derived from the issue before reading the implementation:

- "Make clear that discovering a new model does not automatically make it
  available for routing." The five statuses the issue enumerates — ACTIVE,
  CANDIDATE, DISCOVERED, DEPRECATED, DISABLED — are a review workflow, so
  DISCOVERED has to be a state a model *stays* in until something decides
  otherwise, not a label that ages out on its own.
- "Do not silently replace the active production calibration when a refresh
  occurs unless the application is explicitly configured for safe automatic
  activation", and "Only surface a meaningful notification when a new model
  is discovered ... routing behavior materially changes ... manual review is
  required". Letting an unreviewed model into routing is the single most
  material routing change the feature can make.
- Acceptance criterion 13: "Refresh results show discovered models, price
  changes, benchmark changes, and routing impact." Whatever else it does, a
  model entering the routable set must be visible in the summary the user
  reviews before activating.

What the implementation does: `_status_for` derives DISCOVERED purely from
"this key was not in the *immediately preceding* calibration"::

    if has_previous and entry["key"] not in previous_keys:
        return STATUS_DISCOVERED

Once a calibration containing that DISCOVERED entry is activated, the entry
*is* in `previous_keys` for the following refresh, so `_status_for` classifies
it as ACTIVE/CANDIDATE — and `_router_models`, which filters to
`ROUTABLE_STATUSES = (ACTIVE, CANDIDATE)`, writes it straight into the
`active_catalog.json` that `dynamic_router.active_calibration_catalog_path()`
feeds to live routing.

The promotion is invisible. `diff_calibrations` compares pricing fields,
benchmark fields and routing decisions; it never compares `status`. So the
refresh that flips the model to routable reports only the unrelated change
that triggered it, and `ui/model-calibration-ui.js`'s `changeDetailLines`
lists "New model discovered" solely from `diff["discovered_models"]`, which is
empty by then. With `model_calibration_auto_activate` on, no human is in the
loop for any of it.

This suite asserts the gate itself, not a particular mechanism for holding
it: an explicit approval step, a sticky status carried forward, or surfacing
the promotion as a reviewable change in the diff would all satisfy it.
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


ESTABLISHED = "established"
BRAND_NEW = "brand-new"


class DiscoveredModelGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = calib.ModelCalibrationService(Path(self._tmp.name))

        # Baseline: one known model, activated.
        self.service.refresh(
            fetch_fn=lambda: [model_entry(ESTABLISHED)],
            now=1.0,
            activation_policy="auto",
        )
        # A source now lists a second, never-seen model. It must land as
        # DISCOVERED and must not be routable.
        self.service.refresh(
            fetch_fn=lambda: [model_entry(ESTABLISHED), model_entry(BRAND_NEW)],
            now=100.0,
            force=True,
            activation_policy="auto",
        )
        statuses = self._statuses()
        self.assertEqual(
            statuses.get(f"fixture/{BRAND_NEW}"),
            calib.STATUS_DISCOVERED,
            "sanity check: a never-seen model must first land as DISCOVERED",
        )
        self.assertNotIn(
            BRAND_NEW,
            self._routable_models(),
            "sanity check: a DISCOVERED model must not be in the live routing catalog",
        )

    def _statuses(self) -> dict:
        active = self.service.load_active() or {}
        return {entry["key"]: entry["status"] for entry in active.get("models", [])}

    def _routable_models(self) -> list:
        payload = json.loads(self.service.catalog_override_path.read_text(encoding="utf-8"))
        return [entry["model"] for entry in payload["models"]]

    def _unrelated_price_change_refresh(self) -> dict:
        """A refresh whose only real change is to a *different*, established
        model — nothing about the discovered model changes at all."""
        return self.service.refresh(
            fetch_fn=lambda: [
                model_entry(ESTABLISHED, relative_cost=4),
                model_entry(BRAND_NEW),
            ],
            now=200.0,
            force=True,
            activation_policy="auto",
        )

    def test_a_discovered_model_is_not_promoted_by_an_unrelated_refresh(self) -> None:
        self._unrelated_price_change_refresh()
        self.assertEqual(
            self._statuses().get(f"fixture/{BRAND_NEW}"),
            calib.STATUS_DISCOVERED,
            "A refresh that only changed another model's price silently "
            "promoted the discovered model out of DISCOVERED. `_status_for` "
            "treats 'present in the previous calibration' as approval, so the "
            "status ages out on its own after exactly one refresh.",
        )

    def test_a_discovered_model_does_not_enter_live_routing_on_its_own(self) -> None:
        self._unrelated_price_change_refresh()
        self.assertNotIn(
            BRAND_NEW,
            self._routable_models(),
            "The discovered model was written into active_catalog.json — the "
            "file dynamic_router.active_calibration_catalog_path() hands to "
            "live routing — without anyone ever approving it for routing. "
            "Discovery must not make a model available for routing.",
        )

    def test_promoting_a_discovered_model_is_reported_as_a_change(self) -> None:
        result = self._unrelated_price_change_refresh()
        diff = result["diff"]
        mentioned = json.dumps(
            {
                "discovered_models": diff.get("discovered_models"),
                "pricing_changes": diff.get("pricing_changes"),
                "benchmark_changes": diff.get("benchmark_changes"),
                "routing_changes": diff.get("routing_changes"),
            }
        )
        self.assertIn(
            BRAND_NEW,
            mentioned,
            "If a model's routing availability changes, the refresh summary "
            "the user reviews has to say so. `diff_calibrations` compares "
            "pricing, benchmarks and routing decisions but never `status`, so "
            "this promotion is reported nowhere: "
            f"{mentioned}",
        )


if __name__ == "__main__":
    unittest.main()
