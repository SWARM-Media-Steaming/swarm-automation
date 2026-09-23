#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import codex_rate_limits as helper


SCRIPT = Path(helper.__file__).resolve()
FAKE_CODEX = """#!/usr/bin/env python3
import json, os, signal, sys, time
from pathlib import Path

def terminate(*_):
    Path(os.environ["TERM_FILE"]).write_text("terminated")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, terminate)
mode = os.environ["FAKE_MODE"]
for line in sys.stdin:
    request = json.loads(line)
    if request["method"] == "initialize":
        if mode == "initialize_timeout":
            print("initialize-stalled", file=sys.stderr, flush=True)
            time.sleep(60)
        print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}), flush=True)
    elif request["method"] == "account/rateLimits/read":
        if mode == "read_timeout":
            print("EARLY" + "x" * 3000 + "STDERR_TAIL", file=sys.stderr, flush=True)
            time.sleep(60)
        print(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"rateLimits": {
            "primary": {"usedPercent": 20}, "secondary": {"usedPercent": 30},
            "rateLimitReachedType": None, "spendControlReached": False
        }}}), flush=True)
"""


class CodexRateLimitsTestCase(unittest.TestCase):
    def run_helper(self, mode: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        fake = root / "codex"
        fake.write_text(FAKE_CODEX, encoding="utf-8")
        fake.chmod(0o755)
        terminated = root / "terminated"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--codex-bin", str(fake), "--timeout", "1"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "FAKE_MODE": mode, "TERM_FILE": str(terminated)},
            timeout=5,
        )
        return result, terminated

    def test_reads_rate_limits(self) -> None:
        result, terminated = self.run_helper("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["secondary"]["usedPercent"], 30)
        self.assertTrue(terminated.exists())

    def test_initialization_timeout_names_stage_and_reaps_process(self) -> None:
        result, terminated = self.run_helper("initialize_timeout")
        self.assertEqual(result.returncode, 1)
        self.assertIn("initialize response", result.stderr)
        self.assertIn("initialize-stalled", result.stderr)
        self.assertTrue(terminated.exists())

    def test_rate_limit_timeout_names_stage_and_bounds_stderr(self) -> None:
        result, terminated = self.run_helper("read_timeout")
        self.assertEqual(result.returncode, 1)
        self.assertIn("account/rateLimits/read response", result.stderr)
        self.assertIn("STDERR_TAIL", result.stderr)
        self.assertNotIn("EARLY", result.stderr)
        self.assertLess(len(result.stderr), helper.STDERR_TAIL_CHARS + 250)
        self.assertTrue(terminated.exists())


if __name__ == "__main__":
    unittest.main()
