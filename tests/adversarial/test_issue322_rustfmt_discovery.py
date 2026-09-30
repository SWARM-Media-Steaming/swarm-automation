"""Regression coverage for the scoped rustfmt suite and unittest module collision."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
MANIFEST = REPOSITORY / ".swarm" / "tests.json"


class RustfmtDiscoveryTests(unittest.TestCase):
    def test_registered_rustfmt_command_collects_only_its_gate(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        matches = [suite for suite in manifest["suites"] if suite.get("id") == "adversarial-ci-rustfmt"]
        self.assertEqual(len(matches), 1)
        suite = matches[0]
        self.assertEqual(suite.get("origin"), "adversarial")
        self.assertTrue(suite.get("enabled"))
        self.assertFalse(suite.get("disruptive"))
        self.assertEqual(
            suite.get("command"),
            ["python3", "-m", "unittest", "discover", "-s", "tests/adversarial", "-p", "test_ci_rustfmt.py"],
        )

        script = r"""
import sys
import unittest
from pathlib import Path
repo = Path(%r).resolve()
sys.path.insert(0, str(repo / "tests" / "adversarial"))
loader = unittest.TestLoader()
suite = loader.discover(str(repo / "tests" / "adversarial"), pattern="test_ci_rustfmt.py")
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
print("\n".join(ids))
""" % str(REPOSITORY)
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=REPOSITORY, capture_output=True, text=True, check=False, timeout=300
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertEqual(
            completed.stdout.splitlines(),
            ["test_ci_rustfmt.CiRustfmtGateTests.test_rust_sources_pass_the_exact_ci_format_check"],
        )

    def test_issue_worker_discovery_does_not_import_the_adversarial_twin(self) -> None:
        """Keep same-basename modules from confusing unittest's sys.modules check."""
        script = r"""
import sys
import unittest
from pathlib import Path
repo = Path(%r).resolve()
sys.path.insert(0, str(repo / "tests" / "adversarial"))
sys.path.insert(0, str(repo / "issue_worker"))
loader = unittest.TestLoader()
suite = loader.discover(str(repo / "issue_worker"), pattern="test_engineering_knowledge.py")
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
print("COUNT", len(ids))
""" % str(REPOSITORY)
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=REPOSITORY, capture_output=True, text=True, check=False, timeout=300
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertRegex(output, r"COUNT [1-9]\d*")
        self.assertNotIn("incorrectly imported", output)

    def test_engineering_knowledge_suite_uses_unique_module_name(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        suite = next(item for item in manifest["suites"] if item.get("id") == "adversarial-engineering-knowledge")
        self.assertEqual(
            suite["command"][-1], "test_adversarial_engineering_knowledge.py"
        )
        self.assertIn(
            "tests/adversarial/test_adversarial_engineering_knowledge.py",
            suite["requirements"]["files"],
        )
        self.assertNotIn(
            "tests/adversarial/test_engineering_knowledge.py",
            suite["requirements"]["files"],
        )


if __name__ == "__main__":
    unittest.main()
