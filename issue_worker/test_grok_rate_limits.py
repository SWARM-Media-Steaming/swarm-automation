#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import grok_rate_limits as helper

SCRIPT = Path(helper.__file__).resolve()

# A stand-in for `grok agent --no-leader stdio` that speaks just enough ACP:
# it answers initialize, sprinkles in a notification, then answers the billing
# request with whatever JSON the test put in FAKE_BILLING.
FAKE_GROK = """#!/usr/bin/env python3
import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    if request["method"] == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": 1}}), flush=True)
    elif request["method"] == "_x.ai/billing":
        print(json.dumps({"jsonrpc": "2.0", "method": "_x.ai/mcp/servers_updated", "params": {}}), flush=True)
        print("stray non-protocol output", flush=True)
        print(os.environ["FAKE_BILLING"], flush=True)
"""


class NormalizeTestCase(unittest.TestCase):
    def test_reads_the_weekly_credit_percentage(self) -> None:
        result = helper.normalize(
            {
                "config": {
                    "creditUsagePercent": 30.0,
                    "currentPeriod": {
                        "type": "USAGE_PERIOD_TYPE_WEEKLY",
                        "end": "2026-09-28T15:30:36+00:00",
                    },
                },
                "subscription_tier": "SuperGrok",
            }
        )
        self.assertEqual(
            result,
            {
                "usedPercent": 30.0,
                "period": "week",
                "resetsAt": "2026-09-28T15:30:36+00:00",
                "tier": "SuperGrok",
            },
        )

    def test_period_labels_and_clamping(self) -> None:
        self.assertEqual(helper.period_label("USAGE_PERIOD_TYPE_MONTHLY"), "month")
        self.assertEqual(helper.period_label(None), "period")
        over = helper.normalize({"config": {"creditUsagePercent": 140}})
        self.assertEqual(over["usedPercent"], 100.0)
        under = helper.normalize({"config": {"creditUsagePercent": -5}})
        self.assertEqual(under["usedPercent"], 0.0)

    def test_rejects_a_response_without_a_usage_percentage(self) -> None:
        for bad in ({}, {"config": {}}, {"config": {"creditUsagePercent": "30"}}, {"config": {"creditUsagePercent": True}}):
            with self.assertRaises(RuntimeError, msg=str(bad)):
                helper.normalize(bad)


class EndToEndTestCase(unittest.TestCase):
    def run_helper(self, billing: dict, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "grok"
            fake.write_text(FAKE_GROK, encoding="utf-8")
            fake.chmod(0o755)
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--grok-bin", str(fake), "--timeout", str(timeout)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={"FAKE_BILLING": json.dumps(billing), "PATH": "/usr/bin:/bin"},
            )

    def test_prints_normalized_usage_from_the_agent(self) -> None:
        result = self.run_helper(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "config": {
                        "creditUsagePercent": 42.5,
                        "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY"},
                    },
                    "subscription_tier": "SuperGrok",
                },
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["usedPercent"], 42.5)
        self.assertEqual(payload["period"], "week")

    def test_fails_loudly_when_the_agent_reports_an_error(self) -> None:
        result = self.run_helper({"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "Method not found"}})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Could not read Grok usage", result.stderr)

    def test_a_missing_binary_is_reported_not_raised(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--grok-bin", "/nonexistent/grok", "--timeout", "2"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not read Grok usage", result.stderr)


if __name__ == "__main__":
    unittest.main()
