"""Issue #369 UAT: repository-aware complexity scoring contracts from the spec."""
from __future__ import annotations

import itertools
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "issue_worker"))

import repository_complexity as rc  # noqa: E402
import model_router as mr  # noqa: E402


def profile(repo=50, ui=20):
    return {"id": None, "version": 7, "commit_sha": "b" * 40, "unavailable": [],
            "metrics": {"complexity": repo}, "components": {"ui": {"complexity": ui}}}


class ScoringInvariants(unittest.TestCase):
    def test_word_prefix_does_not_make_docs_change_security_sensitive(self):
        # "authors"/"authoring" are not authentication.
        for title, body in (("Update docs about authors", "Fix the authoring guide wording."),
                            ("Fix author name typo", "Correct the author's name in the README.")):
            p = rc.deterministic_prediction(profile(), title, body)
            self.assertLess(p["vector"]["security_risk"], 50, title)
            self.assertFalse(p["scope"]["security_sensitive_code_likely"], title)
            self.assertLess(p["requirements"]["recommended_capability_floor"], 50, title)

    def test_real_auth_work_is_flagged(self):
        p = rc.deterministic_prediction(profile(), "Fix authentication bypass", "Credentials are not validated.")
        self.assertGreaterEqual(p["vector"]["security_risk"], 70)
        self.assertGreaterEqual(p["requirements"]["recommended_capability_floor"], 70)

    def test_requirements_are_monotonic_in_every_risk_dimension(self):
        base = {"implementation_complexity": 30, "change_surface": 30, "architecture_risk": 30,
                "security_risk": 30, "uncertainty": 30, "repository_complexity": 50,
                "relevant_component_complexity": 30, "confidence": .8}
        order = ["low", "medium", "high", "xhigh"]
        for key in ("implementation_complexity", "change_surface", "architecture_risk",
                    "security_risk", "uncertainty", "relevant_component_complexity"):
            prev = rc.requirements(base)
            for value in (50, 70, 100):
                cur = rc.requirements(dict(base, **{key: value}))
                self.assertGreaterEqual(cur["recommended_capability_floor"], prev["recommended_capability_floor"], key)
                self.assertGreaterEqual(order.index(cur["recommended_reasoning"]), order.index(prev["recommended_reasoning"]), key)
                prev = cur

    def test_repository_size_alone_never_raises_requirements(self):
        vec = lambda repo: {"implementation_complexity": 10, "change_surface": 8, "architecture_risk": 8,
                            "security_risk": 10, "uncertainty": 15, "repository_complexity": repo,
                            "relevant_component_complexity": 20, "confidence": .9}
        self.assertEqual(rc.requirements(vec(0)), rc.requirements(vec(100)))

    def test_requirement_bounds(self):
        for values in itertools.product((0, 100), repeat=5):
            v = dict(zip(rc.AI_KEYS, values), repository_complexity=50, relevant_component_complexity=50, confidence=.9)
            r = rc.requirements(v)
            self.assertTrue(0 <= r["recommended_capability_floor"] <= 100)
            self.assertTrue(1 <= r["estimated_fix_rounds"] <= 6)

    def test_ai_scope_may_not_be_negative_or_boolean_or_unknown_typed(self):
        base = rc.deterministic_prediction(profile(), "Fix ui typo", "")
        vec = dict(base["vector"], confidence=.9)
        for bad in ({"estimated_files": -1}, {"estimated_files": True}, {"estimated_files": 1.5},
                    {"api_change_likely": "yes"}, {"estimated_modules": "3"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                rc.interpret_prediction(base, {"vector": vec, "scope": bad}, "ai")
        with self.assertRaises(ValueError):
            rc.interpret_prediction(base, {"vector": vec, "scope": []}, "ai")
        with self.assertRaises(ValueError):
            rc.interpret_prediction(base, {"vector": vec, "drivers": "text"}, "ai")
        with self.assertRaises(ValueError):
            rc.interpret_prediction(base, "not json", "ai")

    def test_ai_confidence_is_capped_by_missing_metrics(self):
        p = profile()
        p["unavailable"] = [f"m{i}" for i in range(20)]
        base = rc.deterministic_prediction(p, "Fix ui typo", "")
        out = rc.interpret_prediction(base, {"vector": dict(base["vector"], confidence=1.0)}, "ai")
        self.assertLess(out["vector"]["confidence"], .8)

    def test_fallback_records_itself_and_reduces_confidence(self):
        full = rc.deterministic_prediction(profile(), "Fix ui typo", "Fix wording")
        none = rc.deterministic_prediction(rc.unavailable_profile(), "Fix ui typo", "Fix wording")
        self.assertTrue(none["fallback_used"])
        self.assertLess(none["vector"]["confidence"], .7)
        self.assertLessEqual(none["vector"]["confidence"], full["vector"]["confidence"])
        self.assertIn("profile_unavailable", none["unavailable"])
        self.assertIn("deterministic fallback", rc.format_analysis(none))

    def test_github_section_has_spec_fields_in_order(self):
        text = rc.format_analysis(rc.deterministic_prediction(profile(), "Fix ui typo", "Fix wording"))
        # Issue #373 changes lifecycle key:value labels to **Label:**. Keep
        # #369's ordering and value contract while requiring valid bold Markdown.
        labels = ["## Complexity Analysis", "**Repository Complexity:**", "**Relevant Component Complexity:**",
                  "**Implementation Complexity:**", "**Change Surface:**", "**Architecture Risk:**", "**Security Risk:**",
                  "**Uncertainty:**", "**Confidence:**", "Estimated Scope:", "Routing Requirements:",
                  "**Minimum capability:**", "**Reasoning:**", "**Context requirement:**", "Primary complexity drivers:",
                  "**Complexity Scoring Version:** v1.0", "**Repository Profile Version:** 7", "**Repository Commit:** " + "b" * 40]
        pos = -1
        for label in labels:
            nxt = text.find(label, pos + 1)
            self.assertGreater(nxt, pos, label)
            pos = nxt
        self.assertRegex(text, r"\*\*Confidence:\*\* \d+% ")
        self.assertNotRegex(text, r"(?m)^(?:- )?(?:Repository Complexity|Confidence|Minimum capability):\s")

    def test_missing_profile_renders_unavailable_not_none(self):
        text = rc.format_analysis(rc.deterministic_prediction(rc.unavailable_profile(), "t", "b"))
        self.assertIn("**Repository Profile Version:** unavailable", text)
        self.assertNotIn("Repository Profile Version: unavailable", text)
        self.assertNotIn("None", text)

    def test_relevant_components_avoid_substring_false_positives(self):
        p = {"components": {"ui": {"complexity": 1}, "auth": {"complexity": 1}}}
        self.assertEqual(rc.relevant_components(p, "Fix build", "The build fails on quick runs"), [])
        self.assertEqual(rc.relevant_components(p, "Fix ui label", ""), ["ui"])


class ProfileSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "r"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "a@example.invalid")
        self.git("config", "user.name", "a")
        self.store = rc.ComplexityStore(Path(self.tmp.name) / "s.sqlite3")

    def git(self, *a):
        return subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True, text=True).stdout

    def commit(self, files):
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git("add", "-A")
        self.git("commit", "-qm", "x")

    def test_syntax_error_and_binary_and_empty_repo_pieces_do_not_raise(self):
        self.commit({"a/bad.py": "def (:\n", "a/ok.py": "def f():\n    return 1\n", "a/blob.py": "x\0y"})
        p = rc.RepositoryProfiler(self.store, self.repo, "o/r").refresh()
        self.assertEqual(p["metrics"]["total_files"], 3)
        self.assertTrue(any("python_ast:failed" in u for u in p["unavailable"]))
        self.assertIsNotNone(p["metrics"]["cyclomatic_complexity"])

    def test_missing_default_branch_raises_rather_than_measuring_other_ref(self):
        self.commit({"a.py": "x = 1\n"})
        self.git("checkout", "-qb", "feature")
        prof = rc.RepositoryProfiler(self.store, self.repo, "o/r", base="nonexistent")
        with self.assertRaises(Exception):
            prof.refresh()
        self.assertIsNone(self.store.latest("o/r"))

    def test_repositories_are_isolated_in_the_shared_store(self):
        self.commit({"a.py": "x = 1\n"})
        rc.RepositoryProfiler(self.store, self.repo, "o/one").refresh()
        rc.RepositoryProfiler(self.store, self.repo, "o/two").refresh()
        self.assertEqual(self.store.latest("o/one")["version"], 1)
        self.assertEqual(self.store.latest("o/two")["version"], 1)

    def test_profile_contains_no_source_text(self):
        self.commit({"a.py": "SECRET_SENTINEL_VALUE = 1\ndef f():\n    return 'SECRET_SENTINEL_VALUE'\n"})
        p = rc.RepositoryProfiler(self.store, self.repo, "o/r").refresh()
        self.assertNotIn("SECRET_SENTINEL_VALUE", rc.dumps(p))
        self.assertNotIn("SECRET_SENTINEL_VALUE", rc.dumps(self.store.files(p["id"])))

    def test_outcome_for_unknown_evaluation_is_ignored(self):
        self.store.outcome("missing", "completed", {"files_changed": 1})


class RouterFloor(unittest.TestCase):
    def test_unmeetable_floor_raises_instead_of_picking_weak_model(self):
        from test_model_router import _fixture_model
        import dataclasses
        weak = dataclasses.replace(_fixture_model("weak", capability=2, cost=1, token_efficiency=3, latency=3,
                                                  efforts=("low", "medium", "high", "xhigh")), input_cost=1.0, output_cost=1.0)
        vec = {"implementation_complexity": 95, "change_surface": 95, "architecture_risk": 95,
               "security_risk": 95, "uncertainty": 95, "repository_complexity": 50,
               "relevant_component_complexity": 90, "confidence": .9}
        req = mr.RouteRequest("feature", 9, complexity_vector=vec, capability_requirements=rc.requirements(vec))
        with self.assertRaises(mr.ModelRouterError):
            mr.route(req, catalog=[weak])

    def test_request_without_vector_is_backward_compatible(self):
        self.assertEqual(mr.RouteRequest("feature", 3).complexity_vector, {})
        self.assertEqual(mr.RouteRequest("feature", 3).capability_requirements, {})


class NoToggle(unittest.TestCase):
    def test_no_complexity_setting_in_worker_or_ui(self):
        pat = re.compile(r"complexity[_-]?(scoring|analysis)[_-]?(enabled|disabled)|repository_complexity_enabled", re.I)
        for path in [*(ROOT / "issue_worker").glob("*.py"), ROOT / "src" / "config.rs",
                     ROOT / "ui" / "index.html", ROOT / "ui" / "app.js"]:
            if path.name.startswith("test_") or not path.exists():
                continue
            self.assertIsNone(pat.search(path.read_text(errors="ignore")), str(path))


if __name__ == "__main__":
    unittest.main()
