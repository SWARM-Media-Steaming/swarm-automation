"""Issue #321: GitHub Actions CI on ai-main failed one of 622 issue-worker tests.

The failing pipeline is `.github/workflows/ci.yml` job `test`, step
`Issue-worker Python tests`:

    working-directory: issue_worker
    python3 -m unittest discover -p 'test_*.py'

That is the same collection as the scheduled `issue-worker` suite. The
adversarial-UAT rules (not the then-current assertion) define the expected
cap-hit shape:

* Round 0 is the uncounted independent assessment (one tester phase).
* Each counted round is one fixer phase plus one re-test.
* At most MAX_ROUNDS counted rounds follow the assessment.

A continually failing stage therefore performs (1 + MAX_ROUNDS) tester
phases and MAX_ROUNDS fixer phases, then delivers as best effort with
the unresolved notes handed to a follow-up issue. After PR #306 set MAX_ROUNDS to 3, that is 4 tester
phases — not the leftover 7 that belonged to MAX_ROUNDS=6.

These tests replay the CI collector, refuse a skip/disable/gut of that
step, and scan every CI-collected module for the stale tester-count
literal that broke ai-main. They do not treat a passing implementation as
the spec.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SUITE_DIR = Path(__file__).resolve().parent
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
for _path in (SUITE_DIR, ISSUE_WORKER_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from issue321_ci_subrun import assert_verbose_subrun_passed  # noqa: E402

import adversarial_core  # noqa: E402
import adversarial_uat as uat  # noqa: E402

CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
TESTS_JSON = REPO_ROOT / ".swarm" / "tests.json"
CORE_FILE = ISSUE_WORKER_DIR / "adversarial_core.py"
UAT_RULES = REPO_ROOT / ".claude" / "rules" / "adversarial-uat-testing.md"
CAP_HOLDS_FILE = ISSUE_WORKER_DIR / "test_adversarial_uat.py"
CAP_HOLDS_CLASS = "AdversarialUatTests"
CAP_HOLDS_METHOD = "test_cap_holds_automation_and_asks_a_trusted_author_to_adjudicate"
STALE_TESTER_COUNT = 7  # 1 + previous MAX_ROUNDS=6; the assertion that failed CI
STALE_FIXER_COUNT = 6


def _is_count_of_phase(node: ast.AST, phase: str) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "count"):
        return False
    if len(node.args) != 1:
        return False
    arg = node.args[0]
    return isinstance(arg, ast.Constant) and arg.value == phase


def _assert_equal_calls(tree: ast.AST) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "assertEqual" and len(node.args) >= 2:
            calls.append(node)
    return calls


def _is_max_rounds_name(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and node.id == "MAX_ROUNDS":
        return True
    return isinstance(node, ast.Attribute) and node.attr == "MAX_ROUNDS"


def _phase_count_expectation(node: ast.AST) -> object | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if _is_max_rounds_name(node):
        return "MAX_ROUNDS"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        sides = (node.left, node.right)
        if any(_is_max_rounds_name(side) for side in sides) and any(
            isinstance(side, ast.Constant) and side.value == 1 for side in sides
        ):
            return "1+MAX_ROUNDS"
    return None


def _phase_count_values(tree: ast.AST, phase: str) -> list[object]:
    values: list[object] = []
    for node in _assert_equal_calls(tree):
        for left, right in ((node.args[0], node.args[1]), (node.args[1], node.args[0])):
            if not _is_count_of_phase(left, phase):
                continue
            expected = _phase_count_expectation(right)
            if expected is not None:
                values.append(expected)
    return values


def _method(tree: ast.Module, class_name: str, method_name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return item
    raise AssertionError(f"{class_name}.{method_name} is missing")


def _decorator_names(method: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for decorator in method.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


class Issue321CiPythonDiscoverAlignmentTests(unittest.TestCase):
    def test_spec_requires_assessment_plus_max_rounds_retests(self) -> None:
        rules = UAT_RULES.read_text(encoding="utf-8")
        self.assertIn("Round zero is the independent assessment of the normal implementation.", rules)
        self.assertIn("One counted round is one implementer fix plus one adversarial re-test.", rules)
        self.assertIn("At most three counted rounds follow the assessment", rules)
        self.assertEqual(adversarial_core.MAX_ROUNDS, 3)
        self.assertEqual(uat.MAX_ROUNDS, adversarial_core.MAX_ROUNDS)
        self.assertEqual(1 + uat.MAX_ROUNDS, 4)
        self.assertNotEqual(1 + uat.MAX_ROUNDS, STALE_TESTER_COUNT)

    def test_ci_workflow_still_gates_ai_main_with_the_failing_python_step(self) -> None:
        """The issue forbids skipping, disabling, or deleting the failing workflow or tests."""
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^name:\s*CI\s*$")
        self.assertIn("push:\n    branches: [main, ai-main]", workflow)
        python_step = re.search(
            r"name:\s*Issue-worker Python tests\n"
            r"(?P<body>(?:[ \t]+[^\n]*\n)+)",
            workflow,
        )
        self.assertIsNotNone(python_step, "CI must still run Issue-worker Python tests")
        body = python_step.group("body")
        self.assertIn("working-directory: issue_worker", body)
        self.assertRegex(body, r"run:\s+python3 -m unittest discover -p 'test_\*\.py'")
        self.assertNotIn("continue-on-error", body)
        self.assertNotIn("|| true", body)
        self.assertNotIn("pytest", body)
        self.assertNotIn("-k ", body)
        self.assertNotIn("expectedFailure", body)

        test_job = re.search(
            r"^  test:\n(?P<body>.*?)(?=^  [A-Za-z][\w-]*:\n|\Z)",
            workflow,
            flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(test_job, "CI must keep a test job that can fail the workflow")
        test_body = test_job.group("body")
        self.assertNotRegex(test_body, r"(?m)^\s+if:\s+false\s*$")
        self.assertNotRegex(test_body, r"(?m)^\s+continue-on-error:\s+true\s*$")
        self.assertIn("working-directory: issue_worker", test_body)

    def test_ci_style_discover_still_collects_the_cap_holds_case(self) -> None:
        """Replay the GitHub Actions collector in a clean process; a renamed test is not a fix."""
        script = r"""
import json
import sys
import unittest
from pathlib import Path
issue_worker = Path(%r).resolve()
sys.path.insert(0, str(issue_worker))
loader = unittest.TestLoader()
suite = loader.discover(str(issue_worker), pattern="test_*.py")
if loader.errors:
    raise SystemExit("".join(loader.errors))
ids = []
stack = [suite]
while stack:
    item = stack.pop()
    if isinstance(item, unittest.TestSuite):
        stack.extend(item)
    else:
        ids.append(item.id())
print(json.dumps({"count": len(ids), "ids": ids}))
""" % (str(ISSUE_WORKER_DIR),)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ISSUE_WORKER_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        payload = json.loads(completed.stdout.splitlines()[-1])
        expected = f"test_adversarial_uat.{CAP_HOLDS_CLASS}.{CAP_HOLDS_METHOD}"
        self.assertIn(expected, payload["ids"])
        self.assertGreater(
            payload["count"],
            500,
            "CI discovery collapsed; gutting the issue-worker suite is not a CI fix",
        )
        self.assertTrue(
            CAP_HOLDS_FILE.name.startswith("test_") and CAP_HOLDS_FILE.suffix == ".py",
            "the failing module must remain a unittest-discoverable test_*.py file",
        )

    def test_no_ci_collected_module_still_expects_the_stale_six_round_counts(self) -> None:
        """Any leftover count('test')==7 would fail the same way ai-main did."""
        leftovers: list[str] = []
        for path in sorted(ISSUE_WORKER_DIR.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for value in _phase_count_values(tree, "test"):
                if value == STALE_TESTER_COUNT:
                    leftovers.append(f"{path.name}: count('test') == {value}")
            for value in _phase_count_values(tree, "fix"):
                if value == STALE_FIXER_COUNT:
                    leftovers.append(f"{path.name}: count('fix') == {value}")
        self.assertEqual(leftovers, [], "stale MAX_ROUNDS=6 phase counts remain in CI-collected tests")

    def test_cap_holds_method_is_an_executable_gate_at_one_plus_max_rounds(self) -> None:
        source = CAP_HOLDS_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(CAP_HOLDS_FILE))
        method = _method(tree, CAP_HOLDS_CLASS, CAP_HOLDS_METHOD)
        self.assertTrue(method.name.startswith("test_"))
        self.assertFalse(
            _decorator_names(method) & {"skip", "skipIf", "skipUnless", "expectedFailure"},
            f"{CAP_HOLDS_METHOD} must remain an executable CI gate",
        )
        test_values = _phase_count_values(method, "test")
        fix_values = _phase_count_values(method, "fix")
        self.assertTrue(test_values, f"{CAP_HOLDS_METHOD} must still assert the tester-phase count")
        self.assertTrue(fix_values, f"{CAP_HOLDS_METHOD} must still assert the fixer-phase count")
        allowed_test = {1 + uat.MAX_ROUNDS, "1+MAX_ROUNDS"}
        allowed_fix = {uat.MAX_ROUNDS, "MAX_ROUNDS"}
        self.assertTrue(
            set(test_values) <= allowed_test,
            f"tester-count assertions {test_values!r} must equal 1+MAX_ROUNDS={1 + uat.MAX_ROUNDS}",
        )
        self.assertTrue(
            set(fix_values) <= allowed_fix,
            f"fixer-count assertions {fix_values!r} must equal MAX_ROUNDS={uat.MAX_ROUNDS}",
        )
        self.assertNotIn(STALE_TESTER_COUNT, test_values)
        self.assertNotIn("self.skipTest", ast.dump(method))

    def test_shared_loop_caps_on_max_rounds_not_a_stale_literal(self) -> None:
        """The durable loop is the spec implementation; a leftover >= 6 would desync history from tests."""
        source = CORE_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(CORE_FILE))
        cap_uses_max_rounds = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or len(node.ops) != 1:
                continue
            if not isinstance(node.ops[0], ast.GtE):
                continue
            left, right = node.left, node.comparators[0]
            left_is_round = (
                isinstance(left, ast.Subscript)
                and isinstance(left.value, ast.Name)
                and left.value.id == "loop"
                and isinstance(left.slice, ast.Constant)
                and left.slice.value == "round"
            )
            right_is_cap = isinstance(right, ast.Name) and right.id == "MAX_ROUNDS"
            if left_is_round and right_is_cap:
                cap_uses_max_rounds = True
        self.assertTrue(
            cap_uses_max_rounds,
            "cap_hit must be loop['round'] >= MAX_ROUNDS so the loop and CI tests cannot drift",
        )
        self.assertIn('loop["outcome"] = "cap_hit"', source)
        self.assertNotRegex(source, r"""loop\["round"\]\s*>=\s*6\b""")
        self.assertNotRegex(source, r"""loop\["round"\]\s*>=\s*7\b""")
        self.assertRegex(
            source,
            r"""loop\["phase"\] = "fix"\s*\n\s*loop\["round"\] \+= 1""",
        )

    def test_scheduled_issue_worker_suite_still_matches_github_actions(self) -> None:
        definition = json.loads(TESTS_JSON.read_text(encoding="utf-8"))
        worker_suites = [suite for suite in definition["suites"] if suite.get("id") == "issue-worker"]
        self.assertEqual(len(worker_suites), 1)
        suite = worker_suites[0]
        self.assertEqual(
            suite["command"],
            ["python3", "-m", "unittest", "discover", "-s", "issue_worker", "-p", "test_*.py"],
        )
        self.assertNotEqual(suite.get("enabled"), False)
        self.assertNotEqual(suite.get("origin"), "adversarial-security")
        self.assertLessEqual(int(suite.get("timeoutSeconds", 0) or 0), 1800)

    def test_github_actions_cwd_runs_the_case_that_failed_on_ai_main(self) -> None:
        """A failing assertion in the CI-collected case must fail this suite."""
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "-v",
                f"test_adversarial_uat.{CAP_HOLDS_CLASS}.{CAP_HOLDS_METHOD}",
            ],
            cwd=ISSUE_WORKER_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        assert_verbose_subrun_passed(
            self,
            completed,
            method=CAP_HOLDS_METHOD,
            class_qualname=f"test_adversarial_uat.{CAP_HOLDS_CLASS}",
        )


if __name__ == "__main__":
    unittest.main()
