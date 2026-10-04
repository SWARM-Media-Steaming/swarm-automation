"""Adversarial acceptance boundaries for issue #374.

These tests exercise the public calibration refresh and the shared lifecycle
policy with deterministic local feed/CLI fixtures. They intentionally avoid
the protected issue #205 tests: no dispute authorized rewriting those suites.
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
import model_lifecycle  # noqa: E402


class AutomaticCalibrationBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="issue374-adversarial-")
        self.addCleanup(temporary.cleanup)
        self.service = calibration.ModelCalibrationService(Path(temporary.name))
        environment = mock.patch.dict(
            os.environ,
            {
                "ARTIFICIAL_ANALYSIS_API_KEY": "deterministic-fixture-key",
                "SWARM_MODEL_CALIBRATION_CATALOG": str(
                    self.service.catalog_override_path
                ),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        available_models.reset()
        self.addCleanup(available_models.reset)

    def refresh(
        self,
        rows: list[dict[str, object]],
        available: dict[str, list[str]],
        *,
        now: float,
    ) -> dict[str, object]:
        available_models.configure(available)
        source = {"kind": "artificial_analysis", "status": "ok"}
        with mock.patch.object(
            calibration._sources,
            "fetch_source",
            return_value=(copy.deepcopy(rows), source),
        ):
            return self.service.refresh(
                source="artificial_analysis",
                available_models=available,
                initiated_by="SCHEDULED",
                force=True,
                now=now,
            )

    def test_retirement_requires_the_successor_from_its_own_provider_cli(self) -> None:
        """A same-named offer from another CLI is not provider availability."""
        rows = calibration.fetch_local_source()
        evidence = {
            "claude": ["claude-sonnet-5"],
            # Deliberately cross-provider evidence: Codex cannot make an
            # Anthropic successor available to Claude routing.
            "codex": ["claude-sonnet-5-5"],
            "grok": [],
        }

        policy = model_lifecycle.policy_snapshot(copy.deepcopy(rows), evidence)

        self.assertNotIn(
            "claude-sonnet-5",
            policy["retirements"],
            "The retirement gate must bind successor availability to the "
            "successor's provider CLI. Flattening all CLI names together can "
            "retire a still-routable predecessor based on an unrelated CLI.",
        )

    def test_malformed_feed_price_rejects_refresh_and_preserves_last_good(self) -> None:
        """Risk acceptance keeps structural validation; bad rates cannot go live."""
        available = {"claude": [], "codex": ["gpt-9-sol"], "grok": []}
        valid = [
            {
                "provider": "openai",
                "model": "gpt-9-sol",
                "input_cost": 2,
                "output_cost": 10,
                "evaluations": {calibration.INTELLIGENCE_KEY: 52},
            }
        ]
        first = self.refresh(valid, available, now=1_800_000_000.0)
        self.assertTrue(first["activated"], first)
        before = self.service.catalog_override_path.read_bytes()

        malformed = copy.deepcopy(valid)
        malformed[0]["input_cost"] = "two dollars"
        malformed[0]["output_cost"] = 11
        result = self.refresh(malformed, available, now=1_800_000_100.0)

        self.assertEqual(
            result["status"],
            "failed",
            "A malformed authoritative rate must fail validation, not be "
            "dropped and replaced by the previous calibration's input rate.",
        )
        self.assertEqual(
            self.service.catalog_override_path.read_bytes(),
            before,
            "A rejected feed must leave the atomic last-known-good publication unchanged.",
        )

    def test_availability_logs_once_per_transition_and_controls_routing_status(self) -> None:
        row = {
            "provider": "openai",
            "model": "gpt-9-nova",
            "input_cost": 1.5,
            "output_cost": 7.5,
            "evaluations": {calibration.INTELLIGENCE_KEY: 50},
        }
        absent = {"claude": [], "codex": [], "grok": []}
        present = {"claude": [], "codex": ["gpt-9-nova"], "grok": []}

        first = self.refresh([row], absent, now=1_800_001_000.0)
        self.assertEqual(
            [line for line in first["log_lines"] if "gpt-9-nova" in line],
            [
                "Model data: openai/gpt-9-nova is reported by Artificial Analysis "
                "but not offered by the codex CLI; not a routing candidate."
            ],
        )
        self.assertEqual(
            self.refresh([row], absent, now=1_800_001_100.0)["log_lines"], []
        )

        available = self.refresh([row], present, now=1_800_001_200.0)
        self.assertEqual(
            sum("gpt-9-nova is now available" in line for line in available["log_lines"]),
            1,
        )
        active = {
            entry["model"]: entry for entry in self.service.load_active()["models"]
        }
        self.assertIn(active["gpt-9-nova"]["status"], calibration.ROUTABLE_STATUSES)
        self.assertIn(
            "openai/gpt-9-nova",
            available["diff"]["newly_discovered_models"],
            "Automatic onboarding must remain visible in the activated diff.",
        )
        self.assertIn(
            "new model",
            available["notification"]["message"],
            "notification_for must surface the automatic onboarding event.",
        )
        self.assertEqual(
            self.refresh([row], present, now=1_800_001_300.0)["log_lines"], []
        )

        withdrawn = self.refresh([row], absent, now=1_800_001_400.0)
        self.assertEqual(
            sum("not offered by the codex CLI" in line for line in withdrawn["log_lines"]),
            1,
        )
        active = {
            entry["model"]: entry for entry in self.service.load_active()["models"]
        }
        self.assertNotIn(active["gpt-9-nova"]["status"], calibration.ROUTABLE_STATUSES)

    def test_derived_retirement_threshold_edges_are_inclusive(self) -> None:
        old = {
            "provider": "openai",
            "agent": "codex",
            "model": "gpt-10-edge",
            "active": True,
            "deprecated": False,
            "input_cost": 2.0,
            "output_cost": 10.0,
            "intelligence_by_effort": {"high": 50.0},
        }
        boundary = {
            **old,
            "model": "gpt-10-1-edge",
            "input_cost": 2.0 * model_lifecycle.UPGRADE_PRICE_TOLERANCE,
            "output_cost": 10.0 * model_lifecycle.UPGRADE_PRICE_TOLERANCE,
            "intelligence_by_effort": {
                "high": 50.0 - model_lifecycle.UPGRADE_SCORE_MARGIN
            },
        }
        offered = {"codex": [old["model"], boundary["model"]]}
        self.assertEqual(
            model_lifecycle.supersessions([old, boundary], offered),
            {old["model"]: boundary["model"]},
        )

        too_expensive = {**boundary, "input_cost": boundary["input_cost"] + 0.001}
        too_weak = {
            **boundary,
            "intelligence_by_effort": {
                "high": boundary["intelligence_by_effort"]["high"] - 0.001
            },
        }
        self.assertEqual(model_lifecycle.supersessions([old, too_expensive], offered), {})
        self.assertEqual(model_lifecycle.supersessions([old, too_weak], offered), {})


if __name__ == "__main__":
    unittest.main()
