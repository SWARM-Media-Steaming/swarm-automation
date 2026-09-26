"""Issue #205 (Model Routing Calibration) — the "Estimated cost impact" the
refresh summary leads with is computed by averaging two incompatible units
together, so the first refresh that attaches real published pricing reports a
~99% cost saving that did not happen.

Expected behaviour, derived from the issue before reading the implementation:

- The refresh result summary is specified in dollars and as a like-for-like
  comparison::

      Estimated average routing cost:
      $0.041 -> $0.036

      Estimated change:
      -12.2%

      Capability:
      No meaningful reduction detected

  "-12.2%" is only meaningful if both endpoints measure the same quantity in
  the same unit. "These values should come from the simulation engine", and a
  simulation that compares dollars against a 1-5 ordinal rank is not
  simulating anything.
- Acceptance criterion 13: "Refresh results show discovered models, price
  changes, benchmark changes, and routing impact." A routing impact figure
  that moves purely because the *unit* changed is not routing impact.
- "The AI must not invent pricing, benchmark values, or routing metrics" —
  the deterministic pipeline that feeds it must not invent them either.

What the implementation does: `model_calibration._average_routing_cost` walks
the workload categories' chosen models and appends, per model, *either* a
per-task dollar figure (`model_router.estimated_dollar_cost`, dollars, when
the entry carries `input_cost`/`output_cost`) *or* the entry's
`relative_cost` — the catalog's 1-5 ordinal rank, documented in
`skills/model-router/models.yaml` as "1 = cheapest, 5 = most expensive". Those
two go into one `costs` list and are averaged as if interchangeable.

This is not a corner case, it is the default first real refresh. The bundled
`skills/model-router/models.yaml` carries no `input_cost`/`output_cost` for
any of its entries, so every baseline calibration built from it averages
ordinal ranks (~1-5). `model_data_sources.fetch_source` exists precisely to
overlay published per-million-token prices from models.dev or Artificial
Analysis onto those same entries. The moment that lands, the *same* model at
the *same* effort for the *same* workload is re-measured in dollars, and
`diff_calibrations` divides one by the other:

    estimated_cost_before: 3.0      (rank)
    estimated_cost_after:  0.042    (dollars/task)
    estimated_cost_change_percent: -98.6

`ui/model-calibration-ui.js`'s `costImpactText` renders that verbatim as
"Estimated cost impact: -98.6%", and `explain_update` repeats it in prose
("Estimated average routing cost decreased by 98.6%"). Nothing changed about
routing; only the unit did.

The assertion below deliberately does not dictate a fix. Reporting no
percentage at all when the two endpoints are not comparable (`None`), or
re-deriving both endpoints in one unit, both pass. Only claiming a large
saving that did not occur fails.
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
import model_router as _model_router  # noqa: E402

# Published list prices, USD per million tokens, in the same shape
# `model_data_sources.fetch_source` hands back for models.dev /
# Artificial Analysis. The exact numbers are irrelevant to the defect.
INPUT_PRICE_PER_MILLION = 3.0
OUTPUT_PRICE_PER_MILLION = 15.0


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


class EstimatedCostImpactUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = calib.ModelCalibrationService(Path(self._tmp.name))

    def test_adding_published_pricing_is_not_reported_as_a_cost_saving(self) -> None:
        # Baseline: a catalog shaped like the bundled models.yaml, which
        # carries relative ranks but no per-token prices at all.
        self.service.refresh(
            fetch_fn=lambda: [model_entry("m1")], now=1.0, activation_policy="auto"
        )

        # The next refresh only *adds* published pricing for that same model.
        # Its rank, capability, benchmarks and supported efforts are all
        # untouched, so the router still picks exactly the same model at
        # exactly the same effort for every workload.
        result = self.service.refresh(
            fetch_fn=lambda: [
                model_entry(
                    "m1",
                    input_cost=INPUT_PRICE_PER_MILLION,
                    output_cost=OUTPUT_PRICE_PER_MILLION,
                )
            ],
            now=100.0,
            force=True,
            activation_policy="auto",
        )
        diff = result["diff"]

        self.assertEqual(
            diff["routing_changes"],
            [],
            "sanity check: no workload should have been re-routed, so any "
            "reported cost movement is not routing impact",
        )

        percent = diff["estimated_cost_change_percent"]
        if percent is not None:
            self.assertLessEqual(
                abs(percent),
                5.0,
                "Learning a model's published price is not a routing cost "
                "saving. `_average_routing_cost` averaged a 1-5 relative_cost "
                "rank for the baseline against per-task dollars afterwards, "
                f"so the summary claims {percent}% "
                f"({diff['estimated_cost_before']} -> "
                f"{diff['estimated_cost_after']}) while routing is unchanged. "
                "Report no percentage when the endpoints are not comparable, "
                "or measure both in one unit.",
            )

    def test_one_calibration_never_averages_ranks_and_dollars_together(self) -> None:
        # Two models the router will split workloads between: one with
        # published pricing, one without — exactly what a partial overlay
        # from an external source produces, since a source only ever knows
        # the models it happens to list.
        priced = model_entry(
            "priced-strong",
            relative_capability=5,
            relative_cost=5,
            supported_efforts=["medium", "high"],
            input_cost=INPUT_PRICE_PER_MILLION,
            output_cost=OUTPUT_PRICE_PER_MILLION,
        )
        unpriced = model_entry("unpriced-light", relative_capability=1, relative_cost=1)

        self.service.refresh(
            fetch_fn=lambda: [priced, unpriced], now=1.0, activation_policy="auto"
        )
        active = self.service.load_active()
        models_by_key = {entry["key"]: entry for entry in active["models"]}

        average = calib._average_routing_cost(active["routing"], models_by_key)
        self.assertIsNotNone(average, "sanity check: workloads must have routed somewhere")

        priced_costs = []
        saw_unpriced_choice = False
        for decision in active["routing"].values():
            key = f"{decision.get('provider')}/{decision.get('model')}"
            entry = models_by_key.get(key)
            if entry is None:
                continue
            if entry.get("input_cost") is None:
                saw_unpriced_choice = True
                continue
            priced_costs.append(
                _model_router.estimated_dollar_cost(
                    _model_router._parse_model(entry), str(decision.get("effort") or "medium")
                )
            )
        self.assertTrue(
            priced_costs and saw_unpriced_choice,
            "sanity check: this fixture must route some workloads to the "
            "priced model and at least one to the unpriced model",
        )

        # Every genuine per-task dollar figure in this calibration is bounded
        # by the most expensive one. An average over a subset of those values
        # can never exceed that bound — unless something that is not a dollar
        # figure was folded in.
        dearest = max(priced_costs)
        self.assertLessEqual(
            average,
            dearest,
            "`_average_routing_cost` folded the unpriced model's 1-5 "
            f"relative_cost rank into an average of dollar costs: it returned "
            f"{average}, above the dearest real per-task cost in the whole "
            f"calibration ({dearest}). The Refresh Change Summary presents "
            "this value as 'Estimated average routing cost: $...', so a rank "
            "must never be averaged into it — leave unpriced models out of "
            "the dollar estimate instead.",
        )


if __name__ == "__main__":
    unittest.main()
