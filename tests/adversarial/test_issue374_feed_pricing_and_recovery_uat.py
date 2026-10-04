"""End-to-end #374 UAT for feed pricing, missing keys, and recovery.

All feeds and CLI lists are deterministic local fixtures. These tests exercise
the public refresh and spend-estimation boundaries used by scheduled work.
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "issue_worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

import available_models  # noqa: E402
import model_calibration as calibration  # noqa: E402
import model_pricing  # noqa: E402


class FeedPricingAndRecoveryUAT(unittest.TestCase):
    MODEL = "gpt-9-adversarial"
    AVAILABLE = {"claude": [], "codex": [MODEL], "grok": []}
    ROW = {
        "provider": "openai", "model": MODEL,
        "input_cost": 2.0, "output_cost": 10.0,
        "evaluations": {calibration.INTELLIGENCE_KEY: 52.0},
    }

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="issue374-pricing-")
        self.addCleanup(temporary.cleanup)
        self.service = calibration.ModelCalibrationService(Path(temporary.name))
        environment = mock.patch.dict(os.environ, {
            "ARTIFICIAL_ANALYSIS_API_KEY": "fixture-key",
            "SWARM_MODEL_CALIBRATION_CATALOG": str(self.service.catalog_override_path),
        })
        environment.start()
        self.addCleanup(environment.stop)
        available_models.reset()
        self.addCleanup(available_models.reset)

    def refresh(self, *, now: float = 1_800_010_000.0) -> dict:
        available_models.configure(self.AVAILABLE)
        with mock.patch.object(
            calibration._sources, "fetch_source",
            return_value=(copy.deepcopy([self.ROW]), {
                "kind": "artificial_analysis", "status": "ok",
            }),
        ):
            return self.service.refresh(
                source="artificial_analysis", available_models=self.AVAILABLE,
                initiated_by="SCHEDULED", force=True, now=now,
            )

    def test_feed_only_model_routes_and_records_spend_at_feed_rate(self) -> None:
        result = self.refresh()
        self.assertTrue(result["activated"])
        self.assertIn(
            self.MODEL,
            {row["model"] for row in self.service._router_models(self.service.load_active())},
        )

        resolution = model_pricing.resolve_price(self.MODEL, provider="codex")
        self.assertTrue(resolution.priced)
        self.assertEqual(resolution.price.input_per_million, 2.0)
        self.assertEqual(resolution.price.output_per_million, 10.0)
        estimate = model_pricing.estimate_invocation_cost(
            model=self.MODEL, provider="codex",
            input_tokens=1_000_000, output_tokens=1_000_000,
            cached_input_tokens=500_000, cached_tokens_included_in_input=True,
        )
        self.assertEqual(estimate.cost, 12.0)
        self.assertEqual(estimate.catalog_version, result["calibration_version"])
        self.assertTrue(estimate.rate_id.startswith("calibration/"))

    def test_static_catalog_price_wins_over_a_conflicting_feed_rate(self) -> None:
        feed = {
            "provider": "openai", "agent": "codex", "model": "gpt-5.6-sol",
            "input_cost": 999.0, "output_cost": 999.0,
        }
        resolution = model_pricing.resolve_price(
            "gpt-5.6-sol", provider="codex", calibration_entry=feed,
        )
        self.assertTrue(resolution.priced)
        self.assertFalse(resolution.price.rate_id.startswith("calibration/"))
        self.assertEqual(resolution.price.input_per_million, 5.0)

    def test_missing_key_skips_fetch_and_marks_last_good_as_not_current(self) -> None:
        self.refresh()
        before = self.service.catalog_override_path.read_bytes()
        with (
            mock.patch.dict(os.environ, {"ARTIFICIAL_ANALYSIS_API_KEY": ""}),
            mock.patch.object(calibration._sources, "fetch_source") as fetch,
        ):
            result = self.service.refresh(
                source="artificial_analysis", available_models=self.AVAILABLE,
                initiated_by="SCHEDULED", force=True, now=1_800_010_100.0,
            )
        fetch.assert_not_called()
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(result["source_status"], "not_configured")
        self.assertIn("not configured", result["log_lines"][0].lower())
        self.assertEqual(self.service.catalog_override_path.read_bytes(), before)
        self.assertEqual(self.service.status_report()["source_status"], "not_configured")

    def test_repeated_failures_alert_without_replacing_last_good(self) -> None:
        self.refresh()
        before = self.service.catalog_override_path.read_bytes()
        results = []
        with mock.patch.object(
            calibration._sources, "fetch_source",
            side_effect=calibration._sources.SourceError("fixture outage"),
        ):
            for offset in range(1, 4):
                results.append(self.service.refresh(
                    source="artificial_analysis", available_models=self.AVAILABLE,
                    initiated_by="SCHEDULED", force=True,
                    now=1_800_010_100.0 + offset,
                ))
        self.assertTrue(all(result["status"] == "failed" for result in results))
        self.assertFalse(results[0]["notification"]["should_notify"])
        self.assertTrue(results[-1]["notification"]["should_notify"])
        self.assertEqual(self.service.catalog_override_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
