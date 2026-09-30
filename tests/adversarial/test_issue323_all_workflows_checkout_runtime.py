"""Repository-wide acceptance checks for the checkout Node runtime issue."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CHECKOUT_STEP = re.compile(r"^\s*-\s+uses:\s*(\S+)\s*$")
CHECKOUT_REF = re.compile(r"^actions/checkout@v([0-9]+)$")


def checkout_references(workflow_text: str) -> list[str]:
    """Return checkout action refs from `uses:` step declarations only."""
    refs = []
    for line in workflow_text.splitlines():
        match = CHECKOUT_STEP.match(line)
        if match and match.group(1).split("@", 1)[0] == "actions/checkout":
            refs.append(match.group(1))
    return refs


def has_supported_checkout_runtime(references: list[str]) -> bool:
    """Checkout v5+ is required because v4 is pinned to deprecated Node 20."""
    if not references:
        return False
    for reference in references:
        match = CHECKOUT_REF.fullmatch(reference)
        if match is None or int(match.group(1)) < 5:
            return False
    return True


class CheckoutRuntimeRepositoryTests(unittest.TestCase):
    def test_every_github_actions_workflow_checkout_is_on_v5_or_newer(self) -> None:
        workflow_paths = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        workflow_paths += sorted((ROOT / ".github" / "workflows").glob("*.yaml"))
        self.assertTrue(workflow_paths, "expected GitHub Actions workflow files")

        found = []
        for path in workflow_paths:
            refs = checkout_references(path.read_text(encoding="utf-8"))
            if refs:
                found.extend((path.relative_to(ROOT).as_posix(), ref) for ref in refs)

        self.assertTrue(found, "expected at least one checkout action in workflows")
        stale = [(path, ref) for path, ref in found if not has_supported_checkout_runtime([ref])]
        self.assertEqual(
            stale,
            [],
            "all actions/checkout references must use explicit v5+ refs; "
            f"Node.js 20 is deprecated: {found!r}",
        )

    def test_runtime_gate_rejects_legacy_malformed_and_floating_refs(self) -> None:
        cases = [
            ("actions/checkout@v4", False),
            ("actions/checkout@v3", False),
            ("actions/checkout@main", False),
            ("actions/checkout", False),
            ("actions/checkout@v5", True),
            ("actions/checkout@v6", True),
        ]
        for reference, expected in cases:
            with self.subTest(reference=reference):
                self.assertEqual(has_supported_checkout_runtime([reference]), expected)

    def test_scanner_ignores_non_checkout_actions_comments_and_run_commands(self) -> None:
        fixture = """\
steps:
  # - uses: actions/checkout@v4
  - uses: actions/setup-node@v20
  - run: echo actions/checkout@v4
  - uses: actions/checkout@v5
"""
        self.assertEqual(checkout_references(fixture), ["actions/checkout@v5"])


if __name__ == "__main__":
    unittest.main()
