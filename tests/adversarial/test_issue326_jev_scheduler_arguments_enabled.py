"""Issue #326: jev_scheduler_arguments must be covered when Jev is enabled."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST_NAME = "jev_scheduler_arguments_forwards_enabled_configuration"
VALUE_FLAGS = [
    "--jev-model",
    "--jev-timeout-seconds",
    "--jev-confidence-automation",
    "--jev-confidence-fallback",
    "--jev-confidence-security",
]


class JevSchedulerArgumentsEnabledCoverage(unittest.TestCase):
    def rust_test_body(self):
        source = (ROOT / "src" / "command_tests.rs").read_text()
        match = re.search(r"fn " + TEST_NAME + r"\(\)\s*\{(.*?)\n\}\n", source, re.S)
        self.assertIsNotNone(match, "enabled-Jev test is missing from src/command_tests.rs")
        return match.group(1)

    def test_rust_test_asserts_enabled_flag_and_every_value(self):
        body = self.rust_test_body()
        self.assertIn("jev_enabled: true", body)
        self.assertIn("jev_scheduler_arguments(", body)
        self.assertIn('"--jev-enabled"', body)
        self.assertIn('"--no-jev-enabled"', body)
        for flag in VALUE_FLAGS:
            self.assertIn(f'"{flag}"', body, flag)

    def test_rust_test_uses_non_default_values(self):
        body = self.rust_test_body()
        for default in ('"jev-latest"', '"8"', '"0.90"', '"0.70"', '"0.95"'):
            self.assertNotIn(default, body)

    def test_forwarded_flags_are_accepted_by_worker_parser(self):
        worker = (ROOT / "issue_worker" / "swarm_issue_worker.py").read_text()
        for flag in ["--jev-enabled"] + VALUE_FLAGS:
            self.assertIn(f'"{flag}"', worker, flag)

    def test_rust_test_actually_runs_and_passes(self):
        result = subprocess.run(
            ["cargo", "test", "--locked", TEST_NAME],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out[-3000:])
        self.assertRegex(out, r"test .*" + TEST_NAME + r" \.\.\. ok")
        self.assertRegex(out, r"[1-9]\d* passed")


if __name__ == "__main__":
    unittest.main()
