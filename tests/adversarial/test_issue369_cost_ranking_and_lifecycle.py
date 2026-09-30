"""Issue #369 UAT: cost ranking after capability floors, and profile lifecycle edges.

Expected behaviour is derived from the issue: "Rank remaining candidates using
capability, observed success, and cost" and "A highly complex repository should
not automatically make a trivial UI text change appear difficult". A candidate
whose dollar estimate is merely *unknown* is not infinitely expensive; the
catalog's relative cost tier must still order it.
"""
from __future__ import annotations

import dataclasses
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "issue_worker"))

import model_router as mr  # noqa: E402
import repository_complexity as rc  # noqa: E402
from test_model_router import _fixture_model  # noqa: E402

TRIVIAL = {"implementation_complexity": 10, "change_surface": 8, "architecture_risk": 8,
           "security_risk": 10, "uncertainty": 15, "repository_complexity": 90,
           "relevant_component_complexity": 20, "confidence": .9}


def model(name, *, capability, cost, dollars=None, efforts=("low", "medium")):
    spec = _fixture_model(name, capability=capability, cost=cost, token_efficiency=3,
                          latency=3, efforts=efforts)
    if dollars is not None:
        spec = dataclasses.replace(spec, input_cost=dollars, output_cost=dollars)
    return spec


def request(vector=TRIVIAL):
    return mr.RouteRequest("feature", 1, complexity_vector=vector,
                           capability_requirements=rc.requirements(vector))


class CostRankingAfterFloors(unittest.TestCase):
    def test_unmeasured_dollar_cost_does_not_lose_to_a_five_times_dearer_model(self):
        cheap = model("cheap-unmeasured", capability=4, cost=1)
        dear = model("dear-measured", capability=4, cost=5, dollars=50.0)
        self.assertIsNone(mr.estimated_dollar_cost(cheap, "low"))
        self.assertIsNotNone(mr.estimated_dollar_cost(dear, "low"))
        # The pre-existing (vector-free) path already prefers the cheap model.
        self.assertEqual(mr.route(mr.RouteRequest("feature", 1), catalog=[cheap, dear]).model,
                         "cheap-unmeasured")
        for catalog in ([cheap, dear], [dear, cheap]):
            self.assertEqual(mr.route(request(), catalog=catalog).model, "cheap-unmeasured")

    def test_a_measured_cheaper_model_still_beats_a_dearer_unmeasured_one(self):
        frugal = model("frugal-measured", capability=4, cost=1, dollars=0.01)
        pricey = model("pricey-unmeasured", capability=4, cost=5)
        self.assertEqual(mr.route(request(), catalog=[pricey, frugal]).model, "frugal-measured")

    def test_trivial_task_does_not_take_a_higher_effort_than_required(self):
        cheap = model("cheap-unmeasured", capability=4, cost=1, efforts=("low", "medium", "xhigh"))
        dear = model("dear-measured", capability=4, cost=5, dollars=50.0, efforts=("xhigh",))
        chosen = mr.route(request(), catalog=[cheap, dear])
        self.assertEqual((chosen.model, chosen.effort), ("cheap-unmeasured", "low"))

    def test_floor_still_precedes_cost_when_the_cheap_model_is_unmeasured(self):
        efforts = ("low", "medium", "high", "xhigh")
        weak = model("weak-unmeasured", capability=1, cost=1, efforts=efforts)
        strong = model("strong-measured", capability=5, cost=5, dollars=50.0, efforts=efforts)
        vector = dict(TRIVIAL, security_risk=90)
        self.assertEqual(mr.route(request(vector), catalog=[weak, strong]).model, "strong-measured")


class ProfileLifecycleEdges(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "uat@example.invalid")
        self.git("config", "user.name", "UAT")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def profiler(self):
        store = rc.ComplexityStore(self.root / "state.sqlite3")
        return store, rc.RepositoryProfiler(store, self.repo, "owner/repo")

    def test_new_package_root_reassigns_unchanged_files_incrementally(self):
        self.write("lib/a/x.py", "def f():\n    return 1\n")
        self.write("lib/b/y.py", "VALUE = 1\n")
        self.commit()
        store, profiler = self.profiler()
        profiler.refresh(timestamp=1)
        self.write("lib/a/package.json", '{"dependencies":{"react":"1"}}')
        self.commit()
        second = profiler.refresh(timestamp=2)
        self.assertEqual(second["refresh_kind"], "incremental")
        files = store.files(second["id"])
        self.assertEqual(files["lib/a/x.py"]["component"], "lib/a")
        self.assertEqual(files["lib/b/y.py"]["component"], "lib")
        self.assertIn("lib/a", second["components"])

    def test_unicode_and_newline_paths_do_not_break_profiling(self):
        self.write("naïve dir/é file.py", "def g():\n    pass\n")
        self.write("weird\nname.py", "def h():\n    pass\n")
        self.commit()
        _, profiler = self.profiler()
        profile = profiler.refresh(timestamp=1)
        self.assertEqual(profile["metrics"]["total_files"], 2)
        self.assertEqual(profile["metrics"]["analyzed_files"], 2)

    def test_empty_history_default_branch_without_commits_is_unavailable_not_silent(self):
        _, profiler = self.profiler()
        with self.assertRaises(ValueError):
            profiler.refresh(timestamp=1)


if __name__ == "__main__":
    unittest.main()
