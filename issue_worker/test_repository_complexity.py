"""Always-on complexity lifecycle, safety, scoring, and persistence contracts (#369)."""
from contextlib import closing
import copy
import dataclasses
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import repository_complexity as rc
import model_router as mr
import dynamic_router as dr
from complexity_worker import ComplexityWorkerMixin
from decision_engine import (CompositeDecisionEngine, DecisionType, JevDecisionEngine,
                             build_jev_request, interpret_jev_response)
from jev_cli import JevError, JevResponse, JevSettings, JevUsage
from test_model_router import _fixture_model


def profile():
    return {"id": None, "version": 143, "commit_sha": "a"*40, "unavailable": [],
            "metrics": {"complexity": 92, "total_files": 10000, "lines_of_code": 1000000},
            "components": {"ui": {"complexity": 25}, "auth": {"complexity": 89},
                           "services/billing": {"complexity": 83}, "services/reporting": {"complexity": 65},
                           "infra": {"complexity": 76}}}


class GitProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "complexity@example.invalid")
        self.git("config", "user.name", "Complexity test")
        self.write("services/auth/package.json", '{"dependencies":{"express":"*"}}')
        self.write("services/auth/auth.py", "def authenticate(user):\n    if user:\n        return True\n    return False\n")
        self.write("ui/package.json", '{"dependencies":{"react":"*"}}')
        self.write("ui/view.ts", "import {x} from '../services/auth/auth';\nfunction render() {return 'hi';}\n")
        self.write("tests/test_auth.py", "from services.auth.auth import authenticate\ndef test_auth():\n    assert authenticate('user')\n")
        self.write("infra/main.tf", "resource \"example\" \"app\" {}\n")
        self.commit()
        self.store = rc.ComplexityStore(self.root / "state.sqlite3")
        self.profiler = rc.RepositoryProfiler(self.store, self.repo, "owner/repo")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True).stdout.strip()

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def test_initial_profile_contains_modules_facts_and_versions(self):
        p = self.profiler.refresh(timestamp=1000)
        self.assertEqual(p["commit_sha"], self.git("rev-parse", "HEAD"))
        self.assertEqual(p["version"], 1)
        self.assertEqual(p["scoring_version"], "1.0")
        self.assertEqual(p["metrics"]["total_files"], 6)
        self.assertIn("services/auth", p["components"])
        self.assertEqual(p["metrics"]["frameworks"], ["express", "react"])
        self.assertEqual(p["metrics"]["test_count"], 1)
        self.assertEqual(p["metrics"]["cyclomatic_complexity"]["max"], 2)
        self.assertGreater(p["metrics"]["cross_module_dependencies"], 0)
        self.assertIn("python_ast", p["analyzer_versions"])
        self.assertIsNone(p["metrics"]["test_coverage"])
        self.assertIn("test_coverage", p["unavailable"])
        self.assertEqual(self.store.latest("owner/repo")["id"], p["id"])

    def test_unchanged_profile_is_reused_without_tree_or_blob_scan(self):
        first = self.profiler.refresh(timestamp=1000)
        with mock.patch.object(self.profiler, "command", wraps=self.profiler.command) as git:
            second = self.profiler.refresh(timestamp=1001)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(all(call.args[0] == "rev-parse" for call in git.call_args_list))

    def test_incremental_reuses_unchanged_and_handles_deletion_and_new_package(self):
        first = self.profiler.refresh(timestamp=1000)
        self.write("ui/view.ts", "export const text = 'hello';\n")
        (self.repo / "infra/main.tf").unlink()
        self.write("packages/new/package.json", '{"dependencies":{"vue":"*"}}')
        self.commit()
        with mock.patch.object(rc, "measure_file", wraps=rc.measure_file) as scan:
            second = self.profiler.refresh(timestamp=1001)
        self.assertEqual({call.args[0] for call in scan.call_args_list}, {"ui/view.ts", "packages/new/package.json"})
        self.assertEqual(second["refresh_kind"], "incremental")
        self.assertNotIn("infra", second["components"])
        self.assertIn("packages/new", second["components"])
        self.assertEqual(second["version"], 2)
        self.assertIn("infra/main.tf", self.store.files(first["id"]))
        self.assertNotIn("infra/main.tf", self.store.files(second["id"]))

    def test_weekly_full_verification_and_schema_invalidation(self):
        p = self.profiler.refresh(timestamp=1000)
        weekly = self.profiler.refresh(timestamp=1000+rc.FULL_INTERVAL)
        self.assertEqual(weekly["version"], 2)
        self.assertEqual(weekly["refresh_kind"], "full")
        with mock.patch.object(rc, "SCHEMA_VERSION", 2):
            changed = self.profiler.refresh(timestamp=1001+rc.FULL_INTERVAL)
        self.assertEqual(changed["version"], 3)
        self.assertEqual(changed["schema_version"], 2)
        self.assertEqual(p["schema_version"], 1)

    def test_substantial_change_threshold_recalculates(self):
        self.profiler.refresh(timestamp=1000)
        self.write("ui/view.ts", "// updated\n")
        self.commit()
        with mock.patch.object(rc, "SUBSTANTIAL_CHANGES", 1):
            self.assertEqual(self.profiler.refresh(timestamp=1001)["refresh_kind"], "full")

    def test_dirty_and_issue_branch_code_never_enters_default_profile(self):
        first = self.profiler.refresh(timestamp=1000)
        self.git("checkout", "-qb", "ai/issue")
        self.write("ui/view.ts", "x\n"*500)
        self.commit()
        self.assertEqual(self.profiler.refresh(timestamp=1001)["id"], first["id"])
        self.write("ui/view.ts", "y\n"*1000)
        self.assertEqual(self.profiler.refresh(force=True)["metrics"]["lines_of_code"], first["metrics"]["lines_of_code"])

    def test_remote_default_branch_takes_precedence_over_integration(self):
        sha = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/trunk", sha)
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        self.profiler.base = "ai-main"
        self.assertEqual(self.profiler.commit(), (sha, "refs/remotes/origin/HEAD"))

    def test_failed_unsupported_analyzers_and_limits_are_partial(self):
        analyzer = SimpleNamespace(name="missing", version="1", supports=lambda p: True,
                                   analyze=mock.Mock(side_effect=RuntimeError("secret should never persist")))
        self.profiler.analyzers = [analyzer]
        p = self.profiler.refresh()
        self.assertIn("missing:failed", p["unavailable"])
        self.assertIsNone(p["metrics"]["cyclomatic_complexity"])
        self.assertGreater(p["metrics"]["lines_of_code"], 0)
        self.assertNotIn("secret should", rc.dumps(p))
        with mock.patch.object(rc, "MAX_BLOB", 5):
            limited = self.profiler.refresh(force=True)
        self.assertIn("file_size_limit", limited["unavailable"])
        self.assertEqual(limited["metrics"]["analyzed_files"], 0)

    def test_secrets_symlinks_and_git_filters_are_not_read_or_executed(self):
        secret = self.root / "outside.py"
        secret.write_text("ULTRASECRET\n"*100)
        (self.repo / "link.py").symlink_to(secret)
        self.write(".env", "PASSWORD=ULTRASECRET")
        self.write(".gitattributes", "*.py filter=evil\n")
        self.git("config", "filter.evil.smudge", "touch SHOULD_NOT_EXIST")
        self.commit()
        p = self.profiler.refresh()
        files = self.store.files(p["id"])
        self.assertIn("symlink_or_submodule", files["link.py"]["unavailable"])
        self.assertIn("excluded_path", files[".env"]["unavailable"])
        self.assertNotIn("ULTRASECRET", rc.dumps(files))
        self.assertFalse((self.repo / "SHOULD_NOT_EXIST").exists())

    def test_coverage_reports_are_measured_not_invented(self):
        self.write("coverage-summary.json", '{"total":{"lines":{"pct":81.2}}}')
        self.commit()
        self.assertEqual(self.profiler.refresh()["metrics"]["test_coverage"], 81.2)

    def test_outcomes_similar_history_and_profile_immutability(self):
        p = self.profiler.refresh()
        prediction = rc.deterministic_prediction(p, "auth change", "authentication behavior")
        identifier = self.store.record_prediction("owner/repo", 1, "hash", prediction)
        self.store.routing(identifier, {"provider": "test", "selected_model": "worker", "reasoning_effort": "high"})
        actual = {"files_changed": 9, "modules_changed": 2, "repair_rounds": 5, "components": ["services/auth"]}
        self.store.outcome(identifier, "completed", actual)
        self.store.outcome(identifier, "completed", actual)
        history = self.store.similar("owner/repo", "auth change", ["services/auth"], exclude_issue=2)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["routing"]["selected_model"], "worker")
        self.assertFalse(self.store.similar("other/repo", "auth change", ["services/auth"]))
        self.assertFalse(self.store.similar("owner/repo", "auth change", ["services/auth"], exclude_issue=1))
        with closing(self.store.connect()) as db:
            diff = json.loads(db.execute("SELECT differences FROM complexity_outcomes").fetchone()[0])
            self.assertEqual(diff["files"], 9-prediction["scope"]["estimated_files"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM complexity_outcomes").fetchone()[0], 1)
        self.assertEqual(self.store.latest("owner/repo")["id"], p["id"])


class ScoringTests(unittest.TestCase):
    def test_representative_scenarios(self):
        trivial = rc.deterministic_prediction(profile(), "Fix ui typo", "Change the ui button text from Svae to Save.")
        medium = rc.deterministic_prediction(profile(), "Add reporting API", "Add a paginated API endpoint to reporting with tests and documented behavior.")
        large = rc.deterministic_prediction(profile(), "Redesign auth and billing", "End-to-end cross-service API architecture with database migration, authentication and distributed deployment.")
        infra = rc.deterministic_prediction(profile(), "infra Terraform change", "Update Kubernetes deployment configuration and resource limits with tests.")
        unclear = rc.deterministic_prediction(profile(), "Fix it", "")
        self.assertEqual(trivial["vector"]["repository_complexity"], 92)
        self.assertEqual(trivial["vector"]["relevant_component_complexity"], 25)
        self.assertLess(trivial["requirements"]["recommended_capability_floor"], 30)
        self.assertLess(trivial["vector"]["implementation_complexity"], medium["vector"]["implementation_complexity"])
        self.assertLess(medium["vector"]["implementation_complexity"], large["vector"]["implementation_complexity"])
        self.assertGreaterEqual(large["vector"]["security_risk"], 70)
        self.assertTrue(large["scope"]["database_change_likely"])
        self.assertTrue(infra["scope"]["infrastructure_change_likely"])
        self.assertFalse(infra["scope"]["database_change_likely"])
        self.assertGreaterEqual(unclear["vector"]["uncertainty"], 70)
        self.assertEqual(unclear["history_samples"], 0)

    def test_small_simple_repo_and_missing_measurements(self):
        small = profile()
        small["metrics"]["complexity"] = 5
        small["components"]["ui"]["complexity"] = 8
        prediction = rc.deterministic_prediction(small, "ui typo", "Fix the wording.")
        missing = rc.deterministic_prediction(rc.unavailable_profile(), "ui typo", "Fix the wording.")
        self.assertEqual(prediction["vector"]["repository_complexity"], 5)
        self.assertLess(missing["vector"]["confidence"], prediction["vector"]["confidence"])
        self.assertTrue(missing["fallback_used"])

    def test_complete_ai_vector_is_validated_and_cannot_replace_measured_scores(self):
        base = rc.deterministic_prediction(profile(), "auth", "Update authentication.")
        response = {"vector": dict(base["vector"], confidence=.9, repository_complexity=0)}
        interpreted = rc.interpret_prediction(base, response, "ai")
        self.assertEqual(interpreted["vector"]["repository_complexity"], 92)
        self.assertFalse(interpreted["fallback_used"])
        for invalid in (None, -1, 101, float("nan"), float("inf"), True, "80"):
            bad = copy.deepcopy(response)
            bad["vector"]["implementation_complexity"] = invalid
            with self.assertRaises(ValueError):
                rc.interpret_prediction(base, bad, "ai")
        response["vector"]["confidence"] = .69
        with self.assertRaisesRegex(ValueError, "low_confidence"):
            rc.interpret_prediction(base, response, "ai")

    def test_history_is_bounded_and_small_samples_do_not_change_policy(self):
        base = rc.deterministic_prediction(profile(), "reporting API", "Add a reporting endpoint with clear acceptance criteria and tests.")
        sample = {"vector": base["vector"], "requirements": base["requirements"], "actual": {"repair_rounds": 30}}
        small = rc.deterministic_prediction(profile(), "reporting API", "Add a reporting endpoint with clear acceptance criteria and tests.", [sample])
        many = rc.deterministic_prediction(profile(), "reporting API", "Add a reporting endpoint with clear acceptance criteria and tests.", [sample]*12)
        self.assertEqual(small["vector"], base["vector"])
        self.assertLessEqual(many["vector"]["implementation_complexity"]-base["vector"]["implementation_complexity"], 8)
        self.assertGreater(many["vector"]["confidence"], base["vector"]["confidence"])

    def test_compact_context_and_github_output_redact_and_escape(self):
        p = profile()
        base = rc.deterministic_prediction(p, "ui typo", "Fix wording")
        context = rc.compact_context(p, base, "ui typo", "password=secret-value\n"+"A"*100000, [], "token=hidden")
        self.assertNotIn("secret-value", rc.dumps(context))
        self.assertNotIn("hidden", rc.dumps(context))
        self.assertLess(len(rc.dumps(context)), 20000)
        base["drivers"] = ["<img src=x> @everyone [click](https://evil.invalid)\n## Forged"]
        rendered = rc.format_analysis(base)
        self.assertIn("## Complexity Analysis", rendered)
        self.assertIn("Repository Profile Version: 143", rendered)
        self.assertIn("Complexity Scoring Version: v1.0", rendered)
        self.assertNotIn("<img", rendered)
        self.assertNotIn("@everyone", rendered)
        self.assertNotIn("\n## Forged", rendered)

    def test_dependency_cycles_report_unknown_depth(self):
        self.assertEqual(rc.graph_depth(["a", "b", "c"], {("a", "b"), ("b", "c")}), (2, False))
        self.assertEqual(rc.graph_depth(["a", "b"], {("a", "b"), ("b", "a")}), (None, True))


class RoutingRequirementsTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [dataclasses.replace(_fixture_model(name, capability=cap, cost=cost,
                        token_efficiency=3, latency=3, efforts=("low", "medium", "high", "xhigh")),
                        input_cost=float(cost), output_cost=float(cost))
                        for name, cap, cost in (("cheap-weak", 1, 1), ("capable-cheap", 4, 2), ("frontier", 5, 5))]

    def request(self, **vector):
        base = {"implementation_complexity": 10, "change_surface": 15, "architecture_risk": 15,
                "security_risk": 10, "uncertainty": 20, "repository_complexity": 90,
                "relevant_component_complexity": 15, "confidence": .9}
        base.update(vector)
        return mr.RouteRequest("feature", 1, complexity_vector=base, capability_requirements=rc.requirements(base))

    def test_capability_floor_precedes_cost_even_when_every_model_is_underqualified(self):
        req = self.request(security_risk=80)
        result = mr.route(req, catalog=self.catalog)
        self.assertEqual(result.model, "capable-cheap")
        self.assertNotIn("cheap-weak", {item["model"] for item in result.candidates})
        with self.assertRaises(mr.ModelRouterError):
            mr.route(req, catalog=self.catalog[:1])

    def test_vectors_with_equal_implementation_route_differently(self):
        local = mr.route(self.request(), catalog=self.catalog)
        broad = mr.route(self.request(change_surface=90), catalog=self.catalog)
        ambiguous = mr.route(self.request(uncertainty=95), catalog=self.catalog)
        self.assertEqual(local.model, "cheap-weak")
        self.assertEqual(broad.model, "capable-cheap")
        self.assertEqual(ambiguous.effort, "xhigh")

    def test_observed_failure_changes_eligible_cost_pool_only_with_enough_samples(self):
        req = self.request(security_risk=80)
        bad = {"model": "capable-cheap", "success": False}
        sparse = mr.route(dataclasses.replace(req, historical_performance=(bad,)), catalog=self.catalog)
        many = mr.route(dataclasses.replace(req, historical_performance=(bad,)*12), catalog=self.catalog)
        self.assertEqual(sparse.model, "capable-cheap")
        self.assertEqual(many.model, "frontier")

    def test_explicit_no_candidate_fallback_is_strongest_and_audited(self):
        prediction = rc.deterministic_prediction(profile(), "auth redesign", "Cross-service authentication architecture and migration")
        offered = [SimpleNamespace(key="fixture", name="Fixture")]
        with mock.patch.object(dr, "candidate_catalog", return_value=[SimpleNamespace(model="cheap-weak")]), \
             mock.patch.object(dr, "_routing_catalog", return_value=tuple(self.catalog[:1])):
            result = dr.apply_complexity_requirements({"task_type": "feature"}, prediction, offered)
        self.assertTrue(result["complexity_requirements_unmet"])
        self.assertEqual(result["reasoning_effort"], "xhigh")
        self.assertEqual(result["selected_model"], "cheap-weak")


class JevComplexityTests(unittest.TestCase):
    def test_typed_jev_request_and_vector(self):
        context = {"complexity_context": {"repository": {"complexity": 80}, "components": {"ui": {"complexity": 20}}}}
        state, questions = build_jev_request(DecisionType.REPOSITORY_COMPLEXITY.value, context)
        self.assertEqual(state["evidence"], context["complexity_context"])
        self.assertEqual(set(questions), set(rc.AI_KEYS))
        response = JevResponse(answers={key: {"value": .7, "confidence": .9} for key in rc.AI_KEYS})
        result = interpret_jev_response(DecisionType.REPOSITORY_COMPLEXITY.value, context, response)
        self.assertEqual(result.scores["architecture_risk"], .7)
        for value in (float("nan"), 2, True, "invalid"):
            response.answers["security_risk"]["value"] = value
            with self.assertRaises(JevError):
                interpret_jev_response(DecisionType.REPOSITORY_COMPLEXITY.value, context, response)

    def test_jev_complexity_has_no_separate_category_toggle(self):
        settings = JevSettings(enabled=True, use_preflight=False, use_workflow=False)
        response = JevResponse(answers={key: {"value": "moderate", "confidence": .95} for key in rc.AI_KEYS})
        cli = SimpleNamespace(ask=mock.Mock(return_value=response), settings=settings)
        engine = CompositeDecisionEngine(settings, jev=JevDecisionEngine(cli))
        result = engine.evaluate("REPOSITORY_COMPLEXITY", {"complexity_context": {}})
        self.assertEqual(result.source, "jev")
        cli.ask.assert_called_once()

    def test_jev_failure_is_a_typed_fallback(self):
        settings = JevSettings(enabled=True)
        cli = SimpleNamespace(ask=mock.Mock(side_effect=JevError("quota unavailable", error_type="unavailable")), settings=settings)
        engine = CompositeDecisionEngine(settings, jev=JevDecisionEngine(cli))
        result = engine.evaluate("REPOSITORY_COMPLEXITY", {"complexity_context": {}})
        self.assertTrue(result.fallback_used)
        self.assertNotEqual(result.source, "jev")


class WorkerComplexityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.worker = ComplexityWorkerMixin()
        self.worker.config = SimpleNamespace(execution_history_db=root/"state.sqlite3", github_repository="owner/repo",
                                             jev=JevSettings(enabled=False), allow_usage_credit_models=False)
        self.worker.in_progress_file = root/"in-progress.json"
        self.worker.issue = SimpleNamespace(number=369, title="Fix ui typo", body="Change the ui text.")
        self.worker.history = SimpleNamespace(execution_id="")
        self.worker._complexity_profile = profile()
        self.worker.knowledge_service = lambda: None
        self.worker.run_router = mock.Mock(side_effect=RuntimeError("no AI"))
        self.host = SimpleNamespace(key="test", router_model="router", router_effort="low")

    def test_fallback_persists_even_without_execution_history_and_is_cached(self):
        p = self.worker.prepare_issue_complexity(self.host)
        again = self.worker.prepare_issue_complexity(self.host)
        self.assertIs(again, p)
        self.assertTrue(p["fallback_used"])
        self.assertTrue(p["evaluation_id"])
        self.worker.run_router.assert_called_once()
        with closing(self.worker.complexity_store().connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM issue_complexity_evaluations").fetchone()[0], 1)

    def test_jev_preferred_and_ai_used_if_jev_unavailable(self):
        self.worker.config.jev = JevSettings(enabled=True)
        self.worker.record_complexity_jev_usage = mock.Mock()
        self.worker.evaluate_decision = mock.Mock(return_value={"source": "jev", "scores": {key: .2 for key in rc.AI_KEYS}, "confidence": .95})
        p = self.worker.prepare_issue_complexity(self.host)
        self.assertEqual(p["source"], "jev")
        self.worker.run_router.assert_not_called()
        del self.worker._issue_complexity
        self.worker.evaluate_decision.return_value = {"source": "unavailable", "fallbackUsed": "rules"}
        self.worker.run_router.return_value = json.dumps({"vector": {**{key: 20 for key in rc.AI_KEYS}, "confidence": .9}})
        self.worker.run_router.side_effect = None
        p = self.worker.prepare_issue_complexity(self.host)
        self.assertEqual(p["source"], "ai")
        self.assertIn("jev_unavailable_or_low_confidence", p["evaluation_failures"])

    def test_actual_usage_rounds_and_best_effort_preserve_missing_tokens(self):
        prediction = self.worker.prepare_issue_complexity(self.host)
        self.worker.current_token_usage_events = lambda: [{"id": "one", "agent_type": "primary", "total_tokens": 10,
                                                          "cached_input_tokens": None, "model": "worker", "provider": "test"}]
        self.worker.finish_complexity_outcome("completed", ["ui/view.ts"])
        with closing(self.worker.complexity_store().connect()) as db:
            actual = json.loads(db.execute("SELECT actual FROM complexity_outcomes").fetchone()[0])
        self.assertEqual(actual["worker_rounds"], 1)
        self.assertEqual(actual["total_tokens"], 10)
        self.assertIsNone(actual["cached_input_tokens"])
        self.assertEqual(actual["files_changed"], 1)
        self.worker.in_progress_file.write_text("{}")
        self.worker.read_state = lambda: {"adversarial": {"rounds": [{"round_number": 0, "tests_failing_after": 2},
                                                    {"round_number": 1, "tests_failing_after": 1}], "outcome": "cap_hit"}}
        self.worker.finish_complexity_outcome("completed", ["ui/view.ts"])
        with closing(self.worker.complexity_store().connect()) as db:
            row = db.execute("SELECT status,actual FROM complexity_outcomes").fetchone()
        self.assertEqual(row["status"], "best_effort")
        self.assertEqual(json.loads(row["actual"])["repair_rounds"], 1)

    def test_broken_store_never_blocks_issue(self):
        self.worker.complexity_store = mock.Mock(side_effect=sqlite3.OperationalError("readonly"))
        p = self.worker.prepare_issue_complexity(self.host)
        self.assertTrue(p["fallback_used"])
        self.assertLess(p["vector"]["confidence"], .7)


if __name__ == "__main__":
    unittest.main()
