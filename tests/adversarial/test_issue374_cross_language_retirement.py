"""#374 cross-language retirement contract and required static evidence."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class CrossLanguageRetirementUAT(unittest.TestCase):
    def test_python_and_rust_execute_the_same_dormant_and_active_cases(self) -> None:
        completed = subprocess.run(
            [
                "cargo", "test", "--locked",
                "retirement_dormancy_agrees_with_python_and_repairs_only_when_active",
                "--", "--nocapture",
            ],
            cwd=ROOT, text=True, capture_output=True, timeout=900, check=False,
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertRegex(output, r"test result: ok\.")
        self.assertRegex(output, r"1 passed; 0 failed")

    def test_required_gpt6_retirement_and_inactive_peer_are_retained(self) -> None:
        blacklist = json.loads(
            (ROOT / "skills/model-router/model-blacklist.json").read_text(encoding="utf-8")
        )
        rows = {row["model"]: row for row in blacklist["models"]}
        self.assertEqual(rows["gpt-6-sol"]["superseded_by"], "gpt-6-1-sol")
        self.assertEqual(
            rows["gpt-6-sol"]["reason"], "GPT-6.1 Sol supersedes GPT-6 Sol."
        )

        models = (ROOT / "skills/model-router/models.yaml").read_text(encoding="utf-8")
        marker = models.index("model: gpt-6-sol")
        peer = models[marker:marker + 500]
        self.assertIn("active: false", peer)
        self.assertIn("deprecated: true", peer)
        self.assertIn("superseded_by: gpt-6-1-sol", peer)


if __name__ == "__main__":
    unittest.main()
