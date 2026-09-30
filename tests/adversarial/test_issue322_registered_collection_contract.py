"""Independent executable checks for the #322 unittest discovery contract."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / ".swarm" / "tests.json"


class RegisteredCollectionContractTests(unittest.TestCase):
    def test_rustfmt_entry_executes_exactly_the_rustfmt_gate(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        suites = [entry for entry in manifest["suites"] if entry.get("id") == "adversarial-ci-rustfmt"]
        self.assertEqual(len(suites), 1)
        suite = suites[0]
        self.assertEqual(suite.get("origin"), "adversarial")
        self.assertIs(suite.get("enabled"), True)
        self.assertIs(suite.get("disruptive"), False)
        self.assertEqual(suite.get("command"), [
            "python3", "-m", "unittest", "discover", "-s", "tests/adversarial",
            "-p", "test_ci_rustfmt.py",
        ])

        completed = subprocess.run(
            suite["command"], cwd=ROOT, capture_output=True, text=True, timeout=180, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("Ran 1 test", completed.stderr)

    def test_issue_worker_module_and_adversarial_counterpart_are_distinct(self) -> None:
        adversarial = ROOT / "tests" / "adversarial" / "test_adversarial_engineering_knowledge.py"
        worker = ROOT / "issue_worker" / "test_engineering_knowledge.py"
        self.assertTrue(adversarial.is_file())
        self.assertTrue(worker.is_file())
        self.assertNotEqual(adversarial.stem, worker.stem)

        # Use the same discovery API/roots as the scheduled issue-worker suite
        # while the adversarial tree is present on sys.path.
        probe = r"""
import sys, unittest
from pathlib import Path
root = Path(%r)
sys.path.insert(0, str(root / "tests" / "adversarial"))
sys.path.insert(0, str(root / "issue_worker"))
loader = unittest.TestLoader()
suite = loader.discover(str(root / "issue_worker"), pattern="test_engineering_knowledge.py")
if loader.errors:
    raise SystemExit("\n".join(loader.errors))
print("COUNT", suite.countTestCases())
""" % str(ROOT)
        completed = subprocess.run(
            [sys.executable, "-c", probe], cwd=ROOT, capture_output=True,
            text=True, timeout=180, check=False,
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        count_line = next((line for line in completed.stdout.splitlines() if line.startswith("COUNT ")), "")
        self.assertRegex(count_line, r"^COUNT [1-9][0-9]*$")
        self.assertNotIn("incorrectly imported", output)


if __name__ == "__main__":
    unittest.main()
