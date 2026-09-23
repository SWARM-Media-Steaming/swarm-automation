"""Adversarial acceptance coverage for CI's Rust formatting gate."""

from __future__ import annotations

from pathlib import Path
import subprocess
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]


class CiRustfmtGateTests(unittest.TestCase):
    def test_rust_sources_pass_the_exact_ci_format_check(self) -> None:
        """Formatting regressions must fail before CI reaches later test jobs."""
        result = subprocess.run(
            ["cargo", "fmt", "--all", "--", "--check"],
            cwd=REPOSITORY,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            "cargo fmt --all -- --check failed:\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}",
        )
