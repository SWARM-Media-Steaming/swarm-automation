"""#374 regression: one CLI observation must not poison later refresh cycles."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "issue_worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

import available_models  # noqa: E402
import decision_engine  # noqa: E402
import dynamic_router  # noqa: E402


class AvailableModelStateIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        available_models.reset()
        self.addCleanup(available_models.reset)

    def test_prior_discovery_read_cannot_hide_successors_from_the_next_cycle(self) -> None:
        # Mirrors the earlier existing test that reads a usage-credit-marked
        # discovery before a later scheduled cycle replaces the CLI report.
        available_models.configure({
            "claude": [
                {"value": "claude-sonnet-5-5", "requiresUsageCredits": True},
                "claude-x",
            ]
        })
        self.assertEqual(available_models.discovered("claude")[0].value, "claude-sonnet-5-5")

        available_models.reset()
        available_models.configure({
            "claude": [
                {"value": "claude-sonnet-5-5"},
                {"value": "claude-opus-5-5"},
                {"value": "claude-sonnet-5"},
                {"value": "claude-opus-5"},
                {"value": "claude-haiku-4-5-20251001"},
            ]
        })

        names = dynamic_router.catalog_model_names(("claude",))
        self.assertIn(
            "claude-sonnet-5-5", names,
            "A prior CLI observation poisoned the replacement report and hid an offered, priced successor.",
        )
        self.assertIn("claude-opus-5-5", names)
        self.assertEqual(
            decision_engine._recommended_model("claude/claude-sonnet-5-5"),
            "claude/claude-sonnet-5-5",
            "Jev's advisory model list diverged from the replacement CLI report.",
        )


if __name__ == "__main__":
    unittest.main()
