"""Issue #369 UAT: prediction-vs-actual calibration, version isolation and persistence.

Derived from the issue: "Never silently reinterpret historical scores using a newer
scoring algorithm", "Avoid allowing a small number of historical samples to
dominate routing decisions", "Store prediction vs actual differences" and the
database being the authoritative structured record.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "issue_worker"))

import model_router as mr  # noqa: E402
import repository_complexity as rc  # noqa: E402
from test_model_router import _fixture_model  # noqa: E402

PROFILE = {"id": None, "version": 7, "commit_sha": "b" * 40, "unavailable": [],
           "metrics": {"complexity": 70}, "components": {"auth": {"complexity": 80}, "ui": {"complexity": 20}}}


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = rc.ComplexityStore(Path(self.temp.name) / "state.sqlite3")

    def record(self, number, title="auth login change", status="completed", repairs=2, routing=None):
        prediction = rc.deterministic_prediction(PROFILE, title, "authentication flow")
        identifier = self.store.record_prediction("o/r", number, "fp", prediction)
        self.store.routing(identifier, routing or {"selected_model": "m", "provider": "p"})
        self.store.outcome(identifier, status, {"files_changed": 4, "modules_changed": 1, "repair_rounds": repairs,
                                                "components": ["auth"]})
        return identifier, prediction


class PredictionVersusActual(StoreCase):
    def test_differences_are_signed_actual_minus_predicted(self):
        identifier, prediction = self.record(1, repairs=0)
        with closing(self.store.connect()) as db:
            diff = json.loads(db.execute("SELECT differences FROM complexity_outcomes WHERE evaluation_id=?",
                                         (identifier,)).fetchone()[0])
        self.assertEqual(diff["repair_rounds"], 0 - prediction["requirements"]["estimated_fix_rounds"])
        self.assertEqual(diff["files"], 4 - prediction["scope"]["estimated_files"])
        self.assertLessEqual(diff["repair_rounds"], -1)  # fewer rounds than predicted is negative

    def test_prediction_row_carries_versions_and_full_vector(self):
        identifier, prediction = self.record(2)
        with closing(self.store.connect()) as db:
            row = db.execute("SELECT * FROM issue_complexity_evaluations WHERE id=?", (identifier,)).fetchone()
        stored = json.loads(row["prediction"])
        self.assertEqual(row["scoring_version"], rc.SCORING_VERSION)
        self.assertEqual(stored["repo_commit"], "b" * 40)
        self.assertEqual(stored["profile_version"], 7)
        for key in rc.VECTOR_KEYS + ("confidence",):
            self.assertIn(key, stored["vector"])
        self.assertIn("estimated_files", stored["scope"])
        self.assertEqual(json.loads(row["routing"])["selected_model"], "m")

    def test_outcome_for_failed_and_best_effort_are_kept_verbatim(self):
        for number, status in enumerate(("failed", "best_effort"), start=10):
            identifier, _ = self.record(number, status=status)
            with closing(self.store.connect()) as db:
                self.assertEqual(db.execute("SELECT status FROM complexity_outcomes WHERE evaluation_id=?",
                                            (identifier,)).fetchone()[0], status)

    def test_outcome_serialisation_never_writes_nan(self):
        identifier, _ = self.record(20)
        with self.assertRaises((ValueError, TypeError)):
            self.store.outcome(identifier, "completed", {"total_tokens": float("nan")})
        with closing(self.store.connect()) as db:
            actual = json.loads(db.execute("SELECT actual FROM complexity_outcomes WHERE evaluation_id=?",
                                           (identifier,)).fetchone()[0])
        self.assertNotIn("total_tokens", actual)


class HistoricalSimilarity(StoreCase):
    def test_other_scoring_versions_are_never_reused(self):
        self.record(1)
        self.assertEqual(len(self.store.similar("o/r", "auth login change", ["auth"], 99)), 1)
        with mock.patch.object(rc, "SCORING_VERSION", "2.0"):
            self.assertEqual(self.store.similar("o/r", "auth login change", ["auth"], 99), [])

    def test_unrelated_history_is_not_similar(self):
        self.record(1)
        self.assertEqual(self.store.similar("o/r", "zzz qqq", ["ui"], 99), [])

    def test_history_is_capped_and_deduplicated_per_issue(self):
        for number in range(1, 30):
            self.record(number)
        self.record(5)  # a second attempt at issue 5
        similar = self.store.similar("o/r", "auth login change", ["auth"], 999)
        self.assertLessEqual(len(similar), 12)
        numbers = [item["issue_number"] for item in similar]
        self.assertEqual(len(numbers), len(set(numbers)))

    def test_incomplete_outcomes_are_not_history(self):
        self.record(1, status="quota_paused")
        prediction = rc.deterministic_prediction(PROFILE, "auth login change", "x")
        self.store.record_prediction("o/r", 2, "fp", prediction)  # no outcome yet
        self.assertEqual(self.store.similar("o/r", "auth login change", ["auth"], 99), [])

    def test_two_or_fewer_samples_never_move_the_score(self):
        base = rc.deterministic_prediction(PROFILE, "auth login change", "authentication flow")
        sample = {"vector": base["vector"], "requirements": base["requirements"], "actual": {"repair_rounds": 50}}
        for count in (1, 2):
            scored = rc.deterministic_prediction(PROFILE, "auth login change", "authentication flow", [sample] * count)
            self.assertEqual(scored["vector"]["implementation_complexity"],
                             base["vector"]["implementation_complexity"])
            self.assertEqual(scored["requirements"]["estimated_fix_rounds"],
                             base["requirements"]["estimated_fix_rounds"])

    def test_many_extreme_samples_stay_within_documented_bounds(self):
        base = rc.deterministic_prediction(PROFILE, "auth login change", "authentication flow")
        sample = {"vector": base["vector"], "requirements": base["requirements"], "actual": {"repair_rounds": 500}}
        scored = rc.deterministic_prediction(PROFILE, "auth login change", "authentication flow", [sample] * 100)
        self.assertLessEqual(abs(scored["vector"]["implementation_complexity"]
                                 - base["vector"]["implementation_complexity"]), 8)
        self.assertLessEqual(scored["requirements"]["estimated_fix_rounds"], 6)
        self.assertLessEqual(scored["vector"]["confidence"], 1)


class RouterHistoryBoundaries(unittest.TestCase):
    def setUp(self):
        self.catalog = [dataclasses.replace(
            _fixture_model(name, capability=cap, cost=cost, token_efficiency=3, latency=3,
                           efforts=("low", "medium", "high", "xhigh")),
            input_cost=float(cost), output_cost=float(cost))
            for name, cap, cost in (("weak", 1, 1), ("capable", 4, 2), ("frontier", 5, 5))]
        self.vector = {"implementation_complexity": 60, "change_surface": 40, "architecture_risk": 40,
                       "security_risk": 20, "uncertainty": 30, "repository_complexity": 50,
                       "relevant_component_complexity": 50, "confidence": .9}

    def request(self, history=(), vector=None):
        vector = vector or self.vector
        return mr.RouteRequest("feature", 6, complexity_vector=vector,
                               capability_requirements=rc.requirements(vector), historical_performance=tuple(history))

    def test_glowing_history_cannot_lift_a_model_below_the_floor(self):
        history = ({"model": "weak", "success": True},) * 200
        result = mr.route(self.request(history), catalog=self.catalog)
        self.assertNotEqual(result.model, "weak")

    def test_history_for_another_model_is_ignored(self):
        bad = ({"model": "other", "success": False},) * 50
        self.assertEqual(mr.route(self.request(bad), catalog=self.catalog).model,
                         mr.route(self.request(), catalog=self.catalog).model)

    def test_security_risk_forces_a_strong_model_even_for_a_trivial_edit(self):
        vector = dict(self.vector, implementation_complexity=5, change_surface=5, architecture_risk=5,
                      security_risk=90, uncertainty=5)
        result = mr.route(self.request(vector=vector), catalog=self.catalog)
        self.assertIn(result.model, {"capable", "frontier"})

    def test_high_uncertainty_alone_raises_reasoning_effort(self):
        calm = mr.route(self.request(vector=dict(self.vector, uncertainty=10)), catalog=self.catalog)
        unsure = mr.route(self.request(vector=dict(self.vector, uncertainty=95)), catalog=self.catalog)
        order = ["low", "medium", "high", "xhigh"]
        self.assertGreater(order.index(unsure.effort), order.index(calm.effort))

    def test_empty_catalog_raises_router_error_not_index_error(self):
        with self.assertRaises(mr.ModelRouterError):
            mr.route(self.request(), catalog=[])


class ProfileStorePersistence(StoreCase):
    def test_component_and_file_rows_belong_to_their_own_profile_version(self):
        base = {"repository": "o/r", "commit_sha": "c" * 40, "schema_version": rc.SCHEMA_VERSION,
                "scoring_version": rc.SCORING_VERSION, "generated_at": 100.0, "full_at": 100.0,
                "changes_since_full": 0, "refresh_kind": "full", "analyzer_versions": {}, "metrics": {"complexity": 1},
                "components": {"a": {"complexity": 1}}, "unavailable": []}
        first = self.store.publish(dict(base), {"a/x.py": {"component": "a"}})
        second = self.store.publish(dict(base, generated_at=200.0, components={"b": {"complexity": 2}}),
                                    {"b/y.py": {"component": "b"}})
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(set(self.store.files(first["id"])), {"a/x.py"})
        self.assertEqual(set(self.store.files(second["id"])), {"b/y.py"})
        self.assertEqual(set(self.store.latest("o/r")["components"]), {"b"})

    def test_stale_writer_cannot_replace_a_newer_profile(self):
        base = {"repository": "o/r", "commit_sha": "c" * 40, "schema_version": rc.SCHEMA_VERSION,
                "scoring_version": rc.SCORING_VERSION, "generated_at": 500.0, "full_at": 500.0,
                "changes_since_full": 0, "refresh_kind": "full", "analyzer_versions": {}, "metrics": {},
                "components": {}, "unavailable": []}
        newest = self.store.publish(dict(base), {})
        stale = self.store.publish(dict(base, generated_at=100.0), {})
        self.assertEqual(stale["id"], newest["id"])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM repository_complexity_profiles").fetchone()[0], 1)

    def test_likely_files_escape_like_wildcards_in_component_names(self):
        base = {"repository": "o/r", "commit_sha": "c" * 40, "schema_version": rc.SCHEMA_VERSION,
                "scoring_version": rc.SCORING_VERSION, "generated_at": 1.0, "full_at": 1.0,
                "changes_since_full": 0, "refresh_kind": "full", "analyzer_versions": {}, "metrics": {},
                "components": {}, "unavailable": []}
        profile = self.store.publish(base, {"a_b/one.py": {}, "axb/two.py": {}, "%/three.py": {}})
        self.assertEqual(self.store.likely_files(profile["id"], ["a_b"]), ["a_b/one.py"])
        self.assertEqual(self.store.likely_files(profile["id"], ["%"]), ["%/three.py"])

    def test_store_reopens_existing_database_idempotently(self):
        self.record(1)
        again = rc.ComplexityStore(self.store.path)
        with closing(sqlite3.connect(again.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM issue_complexity_evaluations").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
