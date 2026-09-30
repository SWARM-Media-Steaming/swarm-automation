"""Issue #369 UAT: worker lifecycle, CLI/concurrency and router-eligibility boundaries."""
from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "issue_worker"))

import dynamic_router as dr  # noqa: E402
import model_router as mr  # noqa: E402
import repository_complexity as rc  # noqa: E402
from complexity_worker import ComplexityWorkerMixin  # noqa: E402
from decision_engine import CompositeDecisionEngine, JevDecisionEngine  # noqa: E402
from jev_cli import JevSettings  # noqa: E402
from test_model_router import _fixture_model  # noqa: E402

EFFORTS = ("low", "medium", "high", "xhigh")


def model(name, agent, capability, cost, efforts=EFFORTS):
    return dataclasses.replace(
        _fixture_model(name, capability=capability, cost=cost, token_efficiency=3, latency=3, efforts=efforts),
        agent=agent, input_cost=float(cost), output_cost=float(cost))


def security_prediction():
    return rc.deterministic_prediction(rc.unavailable_profile(), "Fix authentication bypass",
                                       "Credentials are not validated.")


def apply(catalog, prediction, **kwargs):
    candidates = [SimpleNamespace(key=key, name=key.upper()) for key in sorted({m.agent for m in catalog})]
    with mock.patch.object(dr, "candidate_catalog", side_effect=lambda c, **k: [
            SimpleNamespace(model=m.model) for m in catalog if m.agent == c.key]), \
         mock.patch.object(dr, "_routing_catalog", return_value=tuple(catalog)):
        return dr.apply_complexity_requirements(
            {"task_type": "feature", "provider": catalog[0].agent}, prediction, candidates, **kwargs)


class RouterEligibility(unittest.TestCase):
    def test_inactive_model_is_never_chosen_even_if_cheapest_capable(self):
        catalog = [model("weak", "fx", 1, 1), model("mid", "fx", 4, 1), model("top", "fx", 5, 5)]
        catalog[1] = dataclasses.replace(catalog[1], active=False)
        result = apply(catalog, security_prediction())
        self.assertEqual(result["selected_model"], "top")
        self.assertFalse(result["complexity_requirements_unmet"])

    def test_unpriced_model_is_never_chosen(self):
        catalog = [model("cheap-mid", "fx", 4, 1), model("dear-mid", "fx", 4, 3)]
        with mock.patch.object(mr, "is_priced", side_effect=lambda m: m.model != "cheap-mid"):
            result = apply(catalog, security_prediction())
        self.assertEqual(result["selected_model"], "dear-mid")

    def test_kept_provider_stays_when_capable_but_yields_when_not(self):
        prediction = security_prediction()
        capable = [model("a-mid", "a", 4, 3), model("b-mid", "b", 4, 1)]
        self.assertEqual(apply(capable, prediction)["selected_model"], "b-mid")
        self.assertEqual(apply(capable, prediction, keep_provider="a")["selected_model"], "a-mid")
        weak_kept = [model("a-weak", "a", 1, 1), model("b-mid", "b", 4, 1)]
        self.assertEqual(apply(weak_kept, prediction, keep_provider="a")["selected_model"], "b-mid")

    def test_required_effort_no_model_supports_is_unmet_not_silently_lowered(self):
        vector = {"implementation_complexity": 50, "change_surface": 10, "architecture_risk": 10,
                  "security_risk": 10, "uncertainty": 95, "repository_complexity": 50,
                  "relevant_component_complexity": 10, "confidence": .9}
        req = rc.requirements(vector)
        self.assertEqual(req["recommended_reasoning"], "xhigh")
        catalog = [model("hi", "fx", 5, 3, ("low", "medium", "high")), model("lo", "fx", 5, 1, ("low",))]
        with self.assertRaises(mr.ModelRouterError):
            mr.route(mr.RouteRequest("feature", 5, complexity_vector=vector, capability_requirements=req),
                     catalog=catalog)

    def test_upgrade_gate_rejects_below_floor_model_or_effort(self):
        prediction = security_prediction()
        catalog = [model("weak", "fx", 1, 1), model("mid", "fx", 4, 2)]
        with mock.patch.object(dr, "_routing_catalog", return_value=tuple(catalog)):
            self.assertFalse(dr.complexity_model_meets("fx", "weak", "high", prediction))
            self.assertFalse(dr.complexity_model_meets("fx", "mid", "low", prediction))
            self.assertTrue(dr.complexity_model_meets("fx", "mid", "high", prediction))
            self.assertFalse(dr.complexity_model_meets("fx", "missing", "high", prediction))

    def test_other_decision_kinds_still_honor_their_jev_category_switch(self):
        settings = JevSettings(enabled=True, use_preflight=False)
        cli = SimpleNamespace(ask=mock.Mock(), settings=settings)
        result = CompositeDecisionEngine(settings, jev=JevDecisionEngine(cli)).evaluate("TASK_CLASSIFICATION", {})
        self.assertEqual(result.source, "disabled")
        cli.ask.assert_not_called()


class ProfilerProcess(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        (self.repo / "a.py").write_text("def f():\n    return 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "x")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def test_cli_profiles_good_repository_and_isolates_a_failing_one(self):
        payload = {"repositories": [
            {"repository": "o/good", "workspace": str(self.repo)},
            {"repository": "o/missing", "workspace": str(self.root / "nope")}]}
        done = subprocess.run([sys.executable, str(ROOT / "issue_worker" / "repository_complexity.py"),
                               "--database", str(self.root / "s.db")],
                              input=json.dumps(payload), capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        results = {item["repository"]: item for item in json.loads(done.stdout)}
        self.assertEqual(results["o/good"]["profile_version"], 1)
        self.assertIn("unavailable", results["o/missing"])
        self.assertIsNotNone(rc.ComplexityStore(self.root / "s.db").latest("o/good"))
        self.assertIsNone(rc.ComplexityStore(self.root / "s.db").latest("o/missing"))

    def test_concurrent_full_refreshes_never_error_or_duplicate_versions(self):
        store = rc.ComplexityStore(self.root / "c.db")
        errors = []

        def run():
            try:
                rc.RepositoryProfiler(store, self.repo, "o/r").refresh(force=True)
            except Exception as error:  # pragma: no cover - failure detail
                errors.append(repr(error))

        threads = [threading.Thread(target=run) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        with closing(store.connect()) as db:
            versions = [row[0] for row in db.execute(
                "SELECT version FROM repository_complexity_profiles WHERE repository='o/r' ORDER BY version")]
        self.assertEqual(versions, list(range(1, len(versions) + 1)))
        self.assertEqual(store.latest("o/r")["version"], versions[-1])


class WorkerLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.worker = ComplexityWorkerMixin()
        self.worker.config = SimpleNamespace(execution_history_db=root / "s.db", github_repository="o/r",
                                             jev=JevSettings(enabled=False), allow_usage_credit_models=False)
        self.worker.in_progress_file = root / "in-progress.json"
        self.worker.issue = SimpleNamespace(number=5, title="Fix ui typo", body="text")
        self.worker.history = SimpleNamespace(execution_id="exec-1")
        self.worker._complexity_profile = {"id": None, "version": 3, "commit_sha": "a" * 40, "unavailable": [],
                                           "metrics": {"complexity": 50}, "components": {}}
        self.worker.knowledge_service = lambda: None
        self.worker.run_router = mock.Mock(side_effect=RuntimeError("no AI"))
        self.host = SimpleNamespace(key="t", router_model="m", router_effort="low")

    def rows(self, sql):
        with closing(self.worker.complexity_store().connect()) as db:
            return db.execute(sql).fetchall()

    def test_resumed_attempt_keeps_saved_prediction_without_reevaluating(self):
        first = self.worker.prepare_issue_complexity(self.host)
        saved = dict(first, marker="saved-before-restart")
        self.worker.in_progress_file.write_text("{}")
        self.worker.read_state = lambda: {"routing_decision": {"complexity_analysis": saved}}
        del self.worker._issue_complexity
        self.worker.run_router.reset_mock()
        again = self.worker.prepare_issue_complexity(self.host)
        self.assertEqual(again["marker"], "saved-before-restart")
        self.worker.run_router.assert_not_called()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM issue_complexity_evaluations")[0][0], 1)

    def test_paused_statuses_record_no_outcome(self):
        self.worker.prepare_issue_complexity(self.host)
        self.worker.current_token_usage_events = lambda: []
        for status in ("quota_paused", "awaiting_input"):
            self.worker.finish_complexity_outcome(status, ["a.py"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM complexity_outcomes")[0][0], 0)

    def test_duplicate_usage_events_and_files_are_counted_once_and_missing_stays_null(self):
        prediction = self.worker.prepare_issue_complexity(self.host)
        self.worker.read_state = lambda: {"routing_decision": {"complexity_analysis": prediction}}
        events = [{"id": "x", "agent_type": "primary", "total_tokens": 5, "estimated_cost": .5,
                   "provider": "p", "model": "m"}] * 3
        events.append({"id": "y", "agent_type": "router", "total_tokens": None, "estimated_cost": None,
                       "provider": "p", "model": "m"})
        self.worker.current_token_usage_events = lambda: events
        self.worker.finish_complexity_outcome("completed", ["a.py", "a.py", "b/c.py"])
        row = self.rows("SELECT status, actual FROM complexity_outcomes")[0]
        actual = json.loads(row["actual"])
        self.assertEqual((row["status"], actual["total_tokens"], actual["estimated_cost"]), ("completed", 5, .5))
        self.assertEqual((actual["files_changed"], actual["worker_rounds"]), (2, 1))
        self.assertIsNone(actual["input_tokens"])

    def test_routing_persistence_stores_selection_but_not_router_prose(self):
        prediction = self.worker.prepare_issue_complexity(self.host)
        self.worker.routing = {"provider": "p", "selected_model": "m", "reasoning_effort": "high",
                               "tier_explanation": "FREE-FORM-PROSE", "complexity_analysis": prediction}
        self.worker.persist_complexity_routing()
        row = self.rows("SELECT routing, execution_id FROM issue_complexity_evaluations")[0]
        self.assertNotIn("FREE-FORM-PROSE", row["routing"])
        self.assertEqual(json.loads(row["routing"])["selected_model"], "m")
        self.assertEqual(row["execution_id"], "exec-1")


if __name__ == "__main__":
    unittest.main()
