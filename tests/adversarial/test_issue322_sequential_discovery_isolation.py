"""Exercise both real discovery roots in one interpreter, in sequence."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class SequentialDiscoveryIsolationTests(unittest.TestCase):
    def test_adversarial_collection_does_not_poison_worker_collection(self) -> None:
        script = r"""
import sys
import unittest
from pathlib import Path

root = Path(%r).resolve()
adversarial = root / "tests" / "adversarial"
worker = root / "issue_worker"
sys.path.insert(0, str(adversarial))
sys.path.insert(0, str(worker))
loader = unittest.TestLoader()

adversarial_suite = loader.discover(
    str(adversarial), pattern="test_adversarial_engineering_knowledge.py",
    top_level_dir=str(adversarial)
)
if loader.errors:
    raise SystemExit("adversarial discovery failed: " + "\n".join(loader.errors))
adversarial_module = sys.modules.get("test_adversarial_engineering_knowledge")
if adversarial_module is None or Path(adversarial_module.__file__).resolve().parent != adversarial:
    raise SystemExit("adversarial module did not resolve from its discovery root")

worker_suite = loader.discover(
    str(worker), pattern="test_engineering_knowledge.py", top_level_dir=str(worker)
)
if loader.errors:
    raise SystemExit("worker discovery failed: " + "\n".join(loader.errors))
worker_module = sys.modules.get("test_engineering_knowledge")
if worker_module is None or Path(worker_module.__file__).resolve().parent != worker:
    raise SystemExit("worker module did not resolve from issue_worker")
if adversarial_suite.countTestCases() == 0 or worker_suite.countTestCases() == 0:
    raise SystemExit("one of the real suites collected no tests")
print("adversarial", adversarial_suite.countTestCases())
print("issue_worker", worker_suite.countTestCases())
""" % str(ROOT)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertRegex(completed.stdout, r"adversarial [1-9][0-9]*")
        self.assertRegex(completed.stdout, r"issue_worker [1-9][0-9]*")


if __name__ == "__main__":
    unittest.main()
