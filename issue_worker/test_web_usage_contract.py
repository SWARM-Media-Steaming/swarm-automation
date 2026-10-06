"""Cross-language contract between the worker and the hosted web backend.

The web backend (``web/``, issue #416) accounts API-key spend by ingesting the
worker's own ``token_usage.UsageRecord`` rows. ``web/tests/fixtures/
usage_record.json`` is real ``UsageRecord.to_dict()`` output; ``web/tests/
usage_contract.rs`` parses the same file in Rust. If a field the backend reads
is renamed or dropped here, or the fixture goes stale, one side fails.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import token_usage

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "web" / "tests" / "fixtures" / "usage_record.json"

# Exactly the fields ``web/src/usage.rs::UsageRecordIn`` deserializes.
CONSUMED_FIELDS = (
    "id",
    "provider",
    "estimated_cost",
    "currency",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_input_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


@unittest.skipUnless(FIXTURE.is_file(), "the web backend is not part of this checkout (worker image)")
class WebUsageContractTests(unittest.TestCase):
    def records(self) -> list[dict]:
        return json.loads(FIXTURE.read_text(encoding="utf-8"))["records"]

    def test_fixture_is_current_usage_record_output(self):
        for raw in self.records():
            self.assertEqual(
                token_usage.UsageRecord.from_dict(raw).to_dict(),
                raw,
                "regenerate web/tests/fixtures/usage_record.json from UsageRecord.to_dict()",
            )

    def test_every_field_the_backend_reads_is_on_the_dataclass(self):
        fields = {field.name for field in token_usage.UsageRecord.__dataclass_fields__.values()}
        self.assertEqual(sorted(set(CONSUMED_FIELDS) - fields), [])
        rust = (REPO / "web" / "src" / "usage.rs").read_text(encoding="utf-8")
        body = rust.split("pub struct UsageRecordIn", 1)[1].split("}", 1)[0]
        declared = set(re.findall(r"pub (\w+):", body))
        self.assertEqual(declared, set(CONSUMED_FIELDS), "update CONSUMED_FIELDS with UsageRecordIn")

    def test_fixture_covers_a_priced_and_an_unpriced_invocation(self):
        priced, unpriced = self.records()
        self.assertIsNotNone(priced["estimated_cost"])
        self.assertEqual(priced["currency"], "USD")
        self.assertIsNone(unpriced["estimated_cost"])
        self.assertIsNotNone(unpriced["total_tokens"])

    def test_model_data_key_variable_matches_the_desktop(self):
        desktop = (REPO / "src" / "secrets.rs").read_text(encoding="utf-8")
        web = (REPO / "web" / "src" / "model.rs").read_text(encoding="utf-8")
        name = re.search(r'ARTIFICIAL_ANALYSIS_ENV: &str = "(\w+)"', desktop).group(1)
        self.assertIn(f'"{name}"', web)


if __name__ == "__main__":
    unittest.main()
