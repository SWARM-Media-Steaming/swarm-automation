import subprocess
import sys
import unittest
from pathlib import Path

WORKER = Path(__file__).resolve().parents[3] / "issue_worker"

# A go.mod that is only blank lines. The profiler reads it from the default
# branch (up to MAX_BLOB) with no in-process time limit.
CODE = (
    "import repository_complexity as rc\n"
    "rc.dependencies('go.mod', '\\n' * 20000)\n"
)


class ManifestRedosTests(unittest.TestCase):
    def test_blank_line_go_mod_is_linear_time(self):
        try:
            subprocess.run([sys.executable, "-c", CODE], cwd=WORKER, timeout=2, check=True)
        except subprocess.TimeoutExpired:
            self.fail("go.mod dependency regex is quadratic on blank lines; a 1 MiB go.mod hangs the profiler")


if __name__ == "__main__":
    unittest.main()
