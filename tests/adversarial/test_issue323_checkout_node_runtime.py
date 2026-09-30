"""Acceptance checks for checkout action runtime modernization in CI."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CHECKOUT_STEP = re.compile(r"^\s*-\s+uses:\s*(\S+)\s*$")
CHECKOUT_REF = re.compile(r"^actions/checkout@v([0-9]+)$")


class CheckoutRuntimeTests(unittest.TestCase):
    def checkout_references(self) -> list[str]:
        text = WORKFLOW.read_text(encoding="utf-8")
        references = []
        for line in text.splitlines():
            match = CHECKOUT_STEP.match(line)
            if match and match.group(1).split("@", 1)[0] == "actions/checkout":
                references.append(match.group(1))
        return references

    def test_each_ci_checkout_uses_a_release_after_node20(self) -> None:
        references = self.checkout_references()
        self.assertEqual(
            len(references),
            2,
            "CI has two checkout steps (test and failure reporting); validate both",
        )

        majors = []
        for reference in references:
            match = CHECKOUT_REF.fullmatch(reference)
            self.assertIsNotNone(
                match,
                f"checkout must use an explicit versioned actions/checkout ref: {reference!r}",
            )
            majors.append(int(match.group(1)))

        self.assertTrue(
            all(major >= 5 for major in majors),
            f"actions/checkout v4 runs on deprecated Node.js 20: {references!r}",
        )
        self.assertEqual(
            len(set(majors)), 1,
            f"CI checkout steps should stay on the same supported major: {references!r}",
        )


if __name__ == "__main__":
    unittest.main()
