"""Acceptance contract for Issue #272: CI tests remain, releases do not."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
WORKFLOWS = REPOSITORY / ".github" / "workflows"
CI_WORKFLOW = WORKFLOWS / "ci.yml"


class CiWithoutReleasesTests(unittest.TestCase):
    """Check the deploy boundary without needing GitHub credentials or runners."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    def test_ci_keeps_the_complete_test_gate_on_supported_events(self) -> None:
        """Removing release stages must not turn pushes or PRs into untested builds."""
        self.assertTrue(CI_WORKFLOW.is_file(), "CI must live at .github/workflows/ci.yml")
        for trigger in (
            "push:\n    branches: [main, ai-main]",
            "pull_request:\n    branches: [main]",
            "workflow_dispatch:",
        ):
            self.assertIn(trigger, self.workflow)

        test_job = re.search(
            r"^  test:\n(?P<body>.*?)(?=^  [A-Za-z][\w-]*:\n|\Z)",
            self.workflow,
            flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(test_job, "the workflow needs a dedicated test job")
        test_body = test_job.group("body")
        expected_commands = (
            "cargo fmt --all -- --check",
            "cargo clippy --all-targets -- -D warnings",
            "cargo test --locked",
            "python3 -m unittest discover -p 'test_*.py'",
            "npm test",
            "python3 -m unittest test_compute_version",
        )
        for command in expected_commands:
            self.assertIn(
                f"run: {command}",
                test_body,
                f"CI test job must execute {command!r}",
            )

        report_job = re.search(
            r"^  report-failure:\n(?P<body>.*)",
            self.workflow,
            flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(report_job, "test failures must still be reported")
        self.assertIn("needs: [test]", report_job.group("body"))

    def test_no_workflow_can_compute_or_publish_a_release(self) -> None:
        """A renamed deployment job must not survive as an untested release path."""
        self.assertFalse(
            (WORKFLOWS / "release.yml").exists(),
            "the retired release workflow must not remain alongside CI",
        )
        workflow_sources = {
            path.name: path.read_text(encoding="utf-8")
            for path in WORKFLOWS.glob("*.y*ml")
        }
        self.assertIn("ci.yml", workflow_sources)
        forbidden_patterns = (
            r"tauri-apps/tauri-action@",
            r"TAURI_SIGNING_PRIVATE_KEY",
            r"compute_version\.py\s+--channel",
            r"^  compute-version:\s*$",
            r"^  release:\s*$",
            r"\bgh\s+release\s+(?:create|upload|edit)\b",
            r"GITHUB_TOKEN:\s*\$\{\{\s*secrets\.GITHUB_TOKEN\s*\}\}",
        )
        combined = "\n".join(f"# {name}\n{source}" for name, source in workflow_sources.items())
        for pattern in forbidden_patterns:
            self.assertIsNone(
                re.search(pattern, combined, flags=re.MULTILINE),
                f"release behavior remains in a GitHub Actions workflow: {pattern}",
            )


if __name__ == "__main__":
    unittest.main()
