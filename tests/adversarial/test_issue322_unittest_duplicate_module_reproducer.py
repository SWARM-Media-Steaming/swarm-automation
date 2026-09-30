"""Reproduce unittest's duplicate-basename failure without changing the repo."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class DuplicateModuleDiscoveryTests(unittest.TestCase):
    def test_broad_discovery_collides_but_scoped_rustfmt_discovery_does_not(self) -> None:
        with tempfile.TemporaryDirectory(prefix="issue322-unittest-") as temp:
            root = Path(temp)
            adversarial = root / "tests" / "adversarial"
            worker = root / "issue_worker"
            adversarial.mkdir(parents=True)
            worker.mkdir()

            # This is the kind of import-time path adjustment that made a
            # later duplicate module resolve from issue_worker instead.
            (adversarial / "test_aaa_path_setup.py").write_text(
                "import sys\nfrom pathlib import Path\n"
                "sys.path.insert(0, str(Path(__file__).parents[2] / 'issue_worker'))\n",
                encoding="utf-8",
            )
            (adversarial / "test_engineering_knowledge.py").write_text(
                "import unittest\n"
                "class AdversarialTwin(unittest.TestCase):\n"
                "    def test_placeholder(self): pass\n",
                encoding="utf-8",
            )
            (worker / "test_engineering_knowledge.py").write_text(
                "import unittest\n"
                "class WorkerSuite(unittest.TestCase):\n"
                "    def test_placeholder(self): pass\n",
                encoding="utf-8",
            )
            (adversarial / "test_ci_rustfmt.py").write_text(
                "import unittest\n"
                "class RustfmtGate(unittest.TestCase):\n"
                "    def test_placeholder(self): pass\n",
                encoding="utf-8",
            )

            # Keep import caches and sys.path local to each fresh interpreter,
            # as they are in separate registered test-suite processes.
            broad = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", str(adversarial), "-p", "test_*.py"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(broad.returncode, 0, broad.stdout + broad.stderr)
            self.assertIn("incorrectly imported", broad.stdout + broad.stderr)

            scoped = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", str(adversarial), "-p", "test_ci_rustfmt.py"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(scoped.returncode, 0, scoped.stdout + scoped.stderr)
            self.assertIn("Ran 1 test", scoped.stderr)


if __name__ == "__main__":
    unittest.main()
