"""Adversarial checks for checkout declarations in workflow step syntax."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# The action reference is a YAML scalar. Accept quoted scalars and trailing
# comments so a harmless formatting change cannot hide a stale checkout pin.
USES_STEP = re.compile(
    r"^\s*-\s+uses:\s*(?:'([^']*)'|\"([^\"]*)\"|([^#\s]+))\s*(?:#.*)?$"
)
CHECKOUT_REF = re.compile(r"^actions/checkout@v([0-9]+)$")


def checkout_refs(workflow_text: str) -> list[str]:
    refs = []
    for line in workflow_text.splitlines():
        match = USES_STEP.match(line)
        if not match:
            continue
        ref = next(value for value in match.groups() if value is not None)
        if ref.split("@", 1)[0] == "actions/checkout":
            refs.append(ref)
    return refs


class CheckoutYamlFormsTests(unittest.TestCase):
    def test_supported_checkout_refs_are_found_across_yaml_scalar_forms(self) -> None:
        fixture = """\
steps:
  # - uses: actions/checkout@v4
  - uses: actions/setup-node@v20
  - uses: 'actions/checkout@v5' # quoted ref
  - uses: \"actions/checkout@v6\"
  - run: echo actions/checkout@v4
"""
        self.assertEqual(checkout_refs(fixture), [
            "actions/checkout@v5",
            "actions/checkout@v6",
        ])

    def test_real_workflow_has_two_explicit_supported_checkout_pins(self) -> None:
        refs = checkout_refs(WORKFLOW.read_text(encoding="utf-8"))
        self.assertEqual(len(refs), 2, f"expected both CI checkout steps: {refs!r}")
        majors = []
        for ref in refs:
            match = CHECKOUT_REF.fullmatch(ref)
            self.assertIsNotNone(match, f"checkout ref must be explicit: {ref!r}")
            majors.append(int(match.group(1)))
        self.assertTrue(
            all(major >= 5 for major in majors),
            f"checkout v4 uses deprecated Node.js 20: {refs!r}",
        )


if __name__ == "__main__":
    unittest.main()
