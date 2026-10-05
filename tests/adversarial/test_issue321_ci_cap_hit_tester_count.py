"""Issue #321: CI failed because a cap-hit unit test still expected the old
MAX_ROUNDS=6 tester count.

The adversarial-UAT rules define round 0 as the uncounted independent
assessment. Each counted round is one fixer phase plus one re-test. With
MAX_ROUNDS counted repair rounds, a continually failing stage therefore
performs (1 + MAX_ROUNDS) tester phases and MAX_ROUNDS fixer phases, then
delivers as best effort with unresolved notes handed to a follow-up issue.

PR #306 lowered MAX_ROUNDS from 6 to 3 and updated the fixer-count
assertion, but left
`test_cap_holds_automation_and_asks_a_trusted_author_to_adjudicate`
expecting 7 tester calls (1 + 6). GitHub Actions then failed on ai-main
while running `python3 -m unittest discover -p 'test_*.py'` from
`issue_worker/`. The durable loop was already correct; the CI-collected
unit test must stay aligned with 1 + MAX_ROUNDS, remain unskipped, and
still be discovered by that workflow step.
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
CAP_HOLDS_FILE = ISSUE_WORKER_DIR / "test_adversarial_uat.py"
CAP_HOLDS_CLASS = "AdversarialUatTests"
CAP_HOLDS_METHOD = "test_cap_holds_automation_and_asks_a_trusted_author_to_adjudicate"
ISSUE294_FILE = REPO_ROOT / "tests" / "adversarial" / "test_issue294_three_round_cap.py"
SKILL_FILE = REPO_ROOT / ".claude" / "skills" / "swarm-automation-dev" / "SKILL.md"
UAT_RULES = REPO_ROOT / ".claude" / "rules" / "adversarial-uat-testing.md"
CARDINALS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}


def _function_node(path: Path, class_name: str, method_name: str) -> ast.FunctionDef:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return item
    raise AssertionError(f"{path} has no {class_name}.{method_name}")


def _count_assertion_values(method: ast.FunctionDef, counted_phase: str) -> list[object]:
    """Integer or MAX_ROUNDS-derived values compared against calls.count(phase)."""
    values: list[object] = []
    for node in ast.walk(method):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "assertEqual"):
            continue
        if len(node.args) < 2:
            continue
        for left, right in ((node.args[0], node.args[1]), (node.args[1], node.args[0])):
            if not _is_phase_count(left, counted_phase):
                continue
            values.append(_literal_or_max_rounds_expr(right))
    return values


def _is_phase_count(node: ast.AST, phase: str) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "count"):
        return False
    if len(node.args) != 1:
        return False
    arg = node.args[0]
    return isinstance(arg, ast.Constant) and arg.value == phase


def _literal_or_max_rounds_expr(node: ast.AST) -> object:
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
    return ast.dump(node)


def _is_max_rounds_name(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and node.id == "MAX_ROUNDS":
        return True
    return isinstance(node, ast.Attribute) and node.attr == "MAX_ROUNDS"


def _has_skip_decorator(method: ast.FunctionDef) -> bool:
    for decorator in method.decorator_list:
        name = ""
        if isinstance(decorator, ast.Name):
            name = decorator.id
        elif isinstance(decorator, ast.Attribute):
            name = decorator.attr
        elif isinstance(decorator, ast.Call):
            func = decorator.func
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
        if name in {"skip", "skipIf", "skipUnless", "expectedFailure"}:
            return True
    return False


class Issue321CiCapHitTesterCountTests(unittest.TestCase):
    def test_spec_counts_fix_retest_rounds_after_round_zero(self) -> None:
        """Round 0 is uncounted; MAX_ROUNDS is the subsequent repair budget."""
        self.assertEqual(adversarial_core.MAX_ROUNDS, 3)
        self.assertEqual(uat.MAX_ROUNDS, adversarial_core.MAX_ROUNDS)
        rules = UAT_RULES.read_text(encoding="utf-8")
        self.assertIn(
            "Round zero is the independent assessment of the normal implementation.",
            rules,
        )
        self.assertIn(
            "One counted round is one implementer fix plus one adversarial re-test.",
            rules,
        )
        self.assertIn(
            "At most three counted rounds follow the assessment",
            rules,
        )

    def test_ci_collected_cap_holds_test_is_aligned_with_max_rounds(self) -> None:
        """The assertion that broke CI must track 1+MAX_ROUNDS, not a leftover 7."""
        method = _function_node(CAP_HOLDS_FILE, CAP_HOLDS_CLASS, CAP_HOLDS_METHOD)
        self.assertFalse(
            _has_skip_decorator(method),
            f"{CAP_HOLDS_METHOD} must remain an executable CI gate, not a skip",
        )
        fix_values = _count_assertion_values(method, "fix")
        test_values = _count_assertion_values(method, "test")
        self.assertTrue(
            fix_values,
            f"{CAP_HOLDS_METHOD} must still assert the fixer-phase count",
        )
        self.assertTrue(
            all(value in {uat.MAX_ROUNDS, "MAX_ROUNDS"} for value in fix_values),
            f"fixer-count assertions {fix_values!r} must equal MAX_ROUNDS={uat.MAX_ROUNDS}",
        )
        self.assertTrue(
            test_values,
            f"{CAP_HOLDS_METHOD} must still assert the tester-phase count",
        )
        allowed_test = {1 + uat.MAX_ROUNDS, "1+MAX_ROUNDS"}
        self.assertTrue(
            all(value in allowed_test for value in test_values),
            f"tester-count assertions {test_values!r} must equal 1+MAX_ROUNDS="
            f"{1 + uat.MAX_ROUNDS} (the stale CI failure expected 7)",
        )
        self.assertNotIn(
            7,
            test_values,
            "7 was the MAX_ROUNDS=6 leftover that failed CI on ai-main",
        )

    def test_issue294_sequence_still_expects_assessment_plus_three_repairs(self) -> None:
        """Issue #321 must not quietly weaken the #294 cap sequence."""
        source = ISSUE294_FILE.read_text(encoding="utf-8")
        self.assertIn(
            '[("test", 0), ("fix", 1), ("test", 1), ("fix", 2), ("test", 2), '
            '("fix", 3), ("test", 3)]',
            source,
        )
        test_phases = source.count('("test",')
        fix_phases = source.count('("fix",')
        self.assertGreaterEqual(test_phases, 4)
        self.assertGreaterEqual(fix_phases, 3)

    def test_github_actions_still_discovers_issue_worker_unit_tests(self) -> None:
        """The failing pipeline must keep collecting test_adversarial_uat.py."""
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(
            workflow,
            r"working-directory:\s*issue_worker\n"
            r"\s+run:\s+python3 -m unittest discover -p 'test_\*\.py'",
        )
        python_step = re.search(
            r"name:\s*Issue-worker Python tests\n"
            r"(?P<body>(?:[ \t]+[^\n]*\n)+)",
            workflow,
        )
        self.assertIsNotNone(python_step, "CI must still have an Issue-worker Python tests step")
        body = python_step.group("body")
        self.assertNotIn("continue-on-error", body)
        self.assertRegex(CAP_HOLDS_FILE.name, r"^test_.*\.py$")

        definition = json.loads(TESTS_JSON.read_text(encoding="utf-8"))
        worker_suites = [suite for suite in definition["suites"] if suite.get("id") == "issue-worker"]
        self.assertEqual(len(worker_suites), 1, "retain the scheduled issue-worker suite")
        self.assertEqual(
            worker_suites[0]["command"],
            ["python3", "-m", "unittest", "discover", "-s", "issue_worker", "-p", "test_*.py"],
        )
        self.assertNotEqual(worker_suites[0].get("enabled"), False)
        self.assertNotEqual(worker_suites[0].get("origin"), "adversarial-security")

    def test_ci_unittest_discover_collects_the_cap_holds_method(self) -> None:
        matches = list(ISSUE_WORKER_DIR.glob("test_*.py"))
        self.assertIn(CAP_HOLDS_FILE.resolve(), [path.resolve() for path in matches])
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromName(
            f"test_adversarial_uat.{CAP_HOLDS_CLASS}.{CAP_HOLDS_METHOD}",
        )

        def cases(item: unittest.TestSuite | unittest.TestCase) -> list[unittest.TestCase]:
            if isinstance(item, unittest.TestSuite):
                found: list[unittest.TestCase] = []
                for child in item:
                    found.extend(cases(child))
                return found
            return [item]

        expected = f"test_adversarial_uat.{CAP_HOLDS_CLASS}.{CAP_HOLDS_METHOD}"
        self.assertEqual([case.id() for case in cases(suite)], [expected])

    def test_the_ci_failing_case_now_passes_as_a_real_unittest(self) -> None:
        """Re-run the exact case GitHub Actions collected; a failing assertion must fail this suite."""
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

    def test_cap_hit_operator_copy_uses_the_current_round_budget(self) -> None:
        word = CARDINALS[uat.MAX_ROUNDS]
        notice = uat.CAP_HIT_PR_NOTICE
        self.assertIn(f"after {word} fix/re-test rounds", notice)
        self.assertNotIn("after seven fix/re-test rounds", notice)
        output = uat.UAT_STAGE.cap_hit_output(
            {
                "results": [],
                "outcome": "cap_hit",
                "round": uat.MAX_ROUNDS,
                "tests_added": 0,
            }
        )
        self.assertIn(f"after {word} fix/re-test rounds", output)
        self.assertIn(f"still failing after {uat.MAX_ROUNDS} rounds", output)

    def test_dev_skill_documents_tester_count_as_one_plus_max_rounds(self) -> None:
        skill = SKILL_FILE.read_text(encoding="utf-8")
        self.assertRegex(skill, r"1\s*\+\s*`?MAX_ROUNDS`?")
        self.assertIn("test_cap_holds", skill)
        self.assertIn("test_issue294_three_round_cap.py", skill)
        self.assertIn(
            f"today: {1 + uat.MAX_ROUNDS} tests, {uat.MAX_ROUNDS} fixes",
            skill,
        )


if __name__ == "__main__":
    unittest.main()
