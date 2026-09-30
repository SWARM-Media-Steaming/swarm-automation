"""Issue #372 acceptance: an effort-only change must not reset a live session.

Oracle (from the issue, before reading the diff): with work in progress and the
same model, a differing effort keeps the pinned route unless the new pick needs
higher capability or the pinned model is no longer offered; with no work it is
free to switch. "Materially cheaper" only applies to a genuinely different
model, and no verdict reason may read "X is cheaper than X".
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ISSUE_WORKER_DIR = Path(__file__).resolve().parents[2] / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import test_swarm_issue_worker as fixtures  # noqa: E402
from swarm_issue_worker import ProviderChoice  # noqa: E402


class EffortOnlyRerouteTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    M = "claude-sonnet-5-5"

    def verdict(self, old_effort, new_effort, profiles, has_work=True, offered=True,
                model=None, new_model=None):
        pinned = ProviderChoice("Claude", model or self.M, old_effort, "s")
        fresh = ProviderChoice("Claude", new_model or model or self.M, new_effort, "s")
        with mock.patch("swarm_issue_worker.model_route_profile",
                        side_effect=lambda a, m, e: profiles[(m, e)]), \
             mock.patch.object(self.worker, "model_still_offered", return_value=offered):
            return self.worker.reroute_verdict(pinned, fresh, has_work)

    def test_cheaper_effort_only_is_kept_with_work(self) -> None:
        switch, why = self.verdict("high", "medium", {(self.M, "high"): (3, 0.048), (self.M, "medium"): (3, 0.028)})
        self.assertFalse(switch, why)
        self.assertNotIn("materially cheaper", why)

    def test_much_cheaper_and_lower_capability_effort_only_is_kept(self) -> None:
        switch, why = self.verdict("high", "low", {(self.M, "high"): (4, 0.10), (self.M, "low"): (2, 0.01)})
        self.assertFalse(switch, why)

    def test_costlier_effort_same_capability_is_kept(self) -> None:
        switch, why = self.verdict("low", "high", {(self.M, "low"): (3, 0.01), (self.M, "high"): (3, 0.09)})
        self.assertFalse(switch, why)

    def test_higher_capability_effort_only_switches(self) -> None:
        switch, why = self.verdict("medium", "high", {(self.M, "medium"): (3, 0.03), (self.M, "high"): (4, 0.05)})
        self.assertTrue(switch)
        self.assertIn("more capable", why)

    def test_no_work_switches_on_effort_only(self) -> None:
        switch, why = self.verdict("high", "medium", {(self.M, "high"): (3, 0.048), (self.M, "medium"): (3, 0.028)},
                                   has_work=False)
        self.assertTrue(switch)
        self.assertIn("nothing has been done", why)

    def test_unoffered_model_switches_on_effort_only(self) -> None:
        switch, why = self.verdict("high", "medium", {(self.M, "high"): (3, 0.048), (self.M, "medium"): (3, 0.028)},
                                   offered=False)
        self.assertTrue(switch)
        self.assertIn("no longer offered", why)

    def test_identical_route_is_unchanged(self) -> None:
        switch, why = self.verdict("high", "high", {})
        self.assertFalse(switch)
        self.assertIn("unchanged", why)

    def test_missing_profiles_keep_effort_only(self) -> None:
        switch, _ = self.verdict("high", "medium", {(self.M, "high"): None, (self.M, "medium"): None})
        self.assertFalse(switch)

    def test_different_model_materially_cheaper_still_switches(self) -> None:
        other = "claude-haiku-4-5"
        switch, why = self.verdict("high", "high", {(self.M, "high"): (3, 0.05), (other, "high"): (3, 0.01)},
                                   new_model=other)
        self.assertTrue(switch)
        self.assertIn("materially cheaper", why)
        self.assertIn(other, why)

    def test_different_model_and_effort_cheaper_still_switches(self) -> None:
        other = "claude-haiku-4-5"
        switch, why = self.verdict("high", "low", {(self.M, "high"): (3, 0.05), (other, "low"): (3, 0.01)},
                                   new_model=other)
        self.assertTrue(switch)

    def test_reroute_reason_never_compares_a_model_to_itself(self) -> None:
        for old, new in (("high", "medium"), ("medium", "high"), ("low", "high")):
            profiles = {(self.M, old): (3, 0.05), (self.M, new): (3, 0.01)}
            _, why = self.verdict(old, new, profiles)
            self.assertNotIn(f"{self.M} is materially cheaper than {self.M}", why)


if __name__ == "__main__":
    unittest.main()
