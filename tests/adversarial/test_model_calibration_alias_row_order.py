"""Issue #205 AC 3, 13, 15 and the trusted amendment: a feed is a set, not a list.

Derived before reading the implementation. A refresh "fetches the latest
model/pricing/benchmark information", "validates", "normalizes" and "detects
changes"; the amendment adds that contradictory rows for one established model
identity must be rejected as failed without publishing a calibration update.
None of that gives the transport's array order any meaning: JSON array position
is not something a public model feed publishes on purpose, and a provider may
legitimately list one model under both its display name and its API id.

Two invariants follow for any single refresh:

1. Permuting one feed's rows cannot change the verdict, the published prices, or
   the identity a newly discovered model is published under.
2. One real model is published once. An alias row tying a feed name to an
   established catalog entry must not also leave that same model in the
   DISCOVERED list as its own candidate.

Invariant 1 is asserted as an invariance, not as one blessed resolution:
rejecting every permutation of an alias group would satisfy it too, provided it
does so consistently. What must not happen is the same bytes succeeding or
failing depending on which row the source happened to emit first.
"""

from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from calibration_uat_fixture import (
    NOW,
    SOURCE_URL,
    CalibrationUAT,
    calibration,
    model_entry,
    price_row,
    sources,
)

# The bundled catalog entry every test treats as the established identity, and
# the second name a real feed would also publish the same model under.
ESTABLISHED = "established"
ESTABLISHED_ALIAS = "established-api"

# Every observation a feed row can contribute, so a coalesced value that lands
# on the wrong model — or gets picked by row order — is visible in the outcome.
OBSERVED_FIELDS = (
    "input_cost", "output_cost", "reasoning_cost", "speed", "latency_seconds",
    "release_date", "external_evaluations", "deprecated",
)


def merge_outcome(rows):
    """`merge_overlay`'s observable result as a comparable, printable string.

    A rejection is compared on the verdict alone: which of two equally valid
    validation messages a permutation happens to raise is not a contract, so
    only accepted-vs-rejected and the published data are held invariant.
    """
    try:
        merged, discovered = calibration.merge_overlay([model_entry()], copy.deepcopy(list(rows)))
    except calibration.CalibrationValidationError:
        return json.dumps({"verdict": "rejected"}, sort_keys=True)

    def summarize(entries):
        return sorted(
            json.dumps(
                {"key": f"{entry.get('provider')}/{entry.get('model')}",
                 **{field: entry.get(field) for field in OBSERVED_FIELDS}},
                sort_keys=True,
            )
            for entry in entries
        )

    return json.dumps(
        {"verdict": "accepted", "catalog": summarize(merged), "candidates": summarize(discovered)},
        sort_keys=True,
    )


def candidate_keys(outcome):
    """The DISCOVERED keys an accepted `merge_outcome` published."""
    return [json.loads(entry)["key"] for entry in json.loads(outcome).get("candidates", [])]


class AliasRowOrderMergeTests(CalibrationUAT):
    """Normalization level: every permutation of one feed must agree."""

    def permutation_outcomes(self, rows):
        outcomes: dict[str, list[tuple[int, ...]]] = {}
        for order in itertools.permutations(range(len(rows))):
            outcomes.setdefault(merge_outcome([rows[index] for index in order]), []).append(order)
        return outcomes

    def assert_permutations_agree(self, rows, *, contract):
        outcomes = self.permutation_outcomes(rows)
        self.assertEqual(
            len(outcomes), 1,
            f"{contract}: row order changed the result ({len(outcomes)} distinct outcomes) — "
            + " || ".join(f"orders {orders}: {outcome}" for outcome, orders in outcomes.items()),
        )
        return next(iter(outcomes))

    def test_an_alias_row_for_an_established_model_is_order_independent(self):
        rows = [
            {"provider": "fixture", "model": ESTABLISHED_ALIAS, "input_cost": 3, "output_cost": 9},
            {"provider": "fixture", "model": ESTABLISHED, "model_id": ESTABLISHED_ALIAS,
             "input_cost": 3, "output_cost": 9},
        ]
        self.assert_permutations_agree(
            rows, contract="one established model listed under two consistent names",
        )

    def test_an_established_model_is_never_also_published_as_its_own_candidate(self):
        rows = [
            {"provider": "fixture", "model": ESTABLISHED_ALIAS, "input_cost": 3, "output_cost": 9},
            {"provider": "fixture", "model": ESTABLISHED, "model_id": ESTABLISHED_ALIAS,
             "input_cost": 3, "output_cost": 9},
        ]
        for outcome, orders in self.permutation_outcomes(rows).items():
            if json.loads(outcome)["verdict"] == "rejected":
                continue
            self.assertEqual(
                candidate_keys(outcome), [],
                f"row orders {orders} published an alias of the established model as its own "
                f"candidate: {outcome}",
            )

    def test_two_feed_names_for_one_new_model_collapse_to_one_candidate(self):
        # Complementary observations split across a new model's display name and
        # its API id, plus the row stating that both names are the same model.
        rows = [
            {"provider": "fixture", "model": "new-alpha", "input_cost": 2},
            {"provider": "fixture", "model": "new-alpha-api", "output_cost": 8},
            {"provider": "fixture", "model": "new-alpha", "model_id": "new-alpha-api",
             "release_date": "2026-01-05"},
        ]
        outcome = self.assert_permutations_agree(
            rows, contract="one new model listed under two consistent names",
        )
        payload = json.loads(outcome)
        self.assertEqual(payload["verdict"], "accepted",
                         "a feed naming one new model twice, consistently, is usable data")
        self.assertEqual(len(payload["candidates"]), 1,
                         f"one new model must yield one candidate: {outcome}")
        candidate = json.loads(payload["candidates"][0])
        self.assertEqual(candidate["input_cost"], 2.0)
        self.assertEqual(candidate["output_cost"], 8.0)
        self.assertEqual(candidate["release_date"], "2026-01-05")
        self.assertIn(candidate["key"], {"fixture/new-alpha", "fixture/new-alpha-api"})

    def test_contradictory_values_reached_through_an_alias_are_rejected_in_every_order(self):
        # The amendment via a two-hop identity: the third row is the only thing
        # making the first two rows two input prices for one established model.
        rows = [
            {"provider": "fixture", "model": ESTABLISHED_ALIAS, "input_cost": 3},
            {"provider": "fixture", "model": ESTABLISHED, "input_cost": 40},
            {"provider": "fixture", "model": ESTABLISHED, "model_id": ESTABLISHED_ALIAS,
             "output_cost": 9},
        ]
        outcome = self.assert_permutations_agree(
            rows, contract="contradictory input prices for one established identity",
        )
        self.assertEqual(json.loads(outcome)["verdict"], "rejected",
                         "two different input prices for one model identity are not usable data")


class AliasRowOrderRefreshTests(CalibrationUAT):
    """Service level: the verdict, the publication, and the active calibration."""

    def refresh_outcome(self, rows):
        """A fresh bootstrapped service, one priced baseline, then `rows` once."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        service = calibration.ModelCalibrationService(Path(temporary.name))
        service.ensure_bootstrap(now=NOW)

        def remote(payload, now, **options):
            with mock.patch.object(
                sources, "fetch_json", return_value=(copy.deepcopy(payload), "fixture-version")
            ):
                return service.refresh(
                    source="json", source_url=SOURCE_URL, now=now, force=True, **options
                )

        remote({"models": [price_row(input_cost=2, output_cost=8)]}, NOW + 1, activation_policy="auto")
        baseline = service.active_path.read_bytes()
        result = remote({"models": copy.deepcopy(list(rows))}, NOW + 2)

        def priced(document):
            return sorted(
                (entry["key"], entry.get("input_cost"), entry.get("output_cost"))
                for entry in (document or {}).get("models") or []
            )

        def candidates(document):
            return sorted(entry["key"] for entry in (document or {}).get("discovered_models") or [])

        # Only a proposal the status report actually offers for review counts.
        # An auto-activated refresh leaves its superseded proposed.json behind
        # on purpose; the state pointer, not the file, is the user-visible one.
        report = service.status_report()
        pending = report["proposed_calibration"] if report["has_newer_proposed"] else None
        return {
            "status": result["status"],
            "source_status": result.get("source_status"),
            "active_replaced": service.active_path.read_bytes() != baseline,
            "active_prices": priced(service.load_active()),
            "active_candidates": candidates(service.load_active()),
            "pending_prices": priced(pending),
            "pending_candidates": candidates(pending),
        }

    def test_row_order_decides_neither_the_verdict_nor_what_is_published(self):
        rows = [
            {"provider": "fixture", "model": ESTABLISHED_ALIAS, "input_cost": 3, "output_cost": 9},
            {"provider": "fixture", "model": ESTABLISHED, "model_id": ESTABLISHED_ALIAS,
             "input_cost": 3, "output_cost": 9},
        ]
        forward = self.refresh_outcome(rows)
        reverse = self.refresh_outcome(rows[::-1])
        self.assertEqual(
            forward, reverse,
            "refreshing the same feed with its two rows swapped produced different outcomes",
        )

    def test_a_rejected_alias_feed_leaves_the_active_calibration_untouched(self):
        # Whichever orders this implementation rejects, rejection must take the
        # amendment's shape: failed, with the last known-good data still live
        # and no proposed calibration offered for review.
        rows = [
            {"provider": "fixture", "model": ESTABLISHED_ALIAS, "input_cost": 3},
            {"provider": "fixture", "model": ESTABLISHED, "model_id": ESTABLISHED_ALIAS,
             "input_cost": 40},
        ]
        for label, order in (("forward", rows), ("reverse", rows[::-1])):
            with self.subTest(order=label):
                outcome = self.refresh_outcome(order)
                self.assertEqual(outcome["status"], "failed")
                self.assertEqual(outcome["source_status"], "error")
                self.assertFalse(outcome["active_replaced"],
                                 "a rejected feed replaced the active calibration")
                self.assertEqual(outcome["active_prices"], [("fixture/established", 2.0, 8.0)])
                self.assertEqual(outcome["pending_prices"], [],
                                 "a rejected feed offered a proposed calibration for review")
