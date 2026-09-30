import subprocess
import sys
import unittest
from pathlib import Path

WORKER = Path(__file__).resolve().parents[3] / "issue_worker"

CODE = (
    "import issue_context\n"
    "body = '## Objective\\n# x' + ' ' * 2500 + 'y\\n'\n"
    "issue_context.extract_sections(body)\n"
)


class HeadingRedosTests(unittest.TestCase):
    def test_padded_heading_line_is_linear_time(self):
        try:
            subprocess.run([sys.executable, "-c", CODE], cwd=WORKER, timeout=5, check=True)
        except subprocess.TimeoutExpired:
            self.fail("extract_sections backtracks catastrophically on a padded '# x   y' line")


if __name__ == "__main__":
    unittest.main()
