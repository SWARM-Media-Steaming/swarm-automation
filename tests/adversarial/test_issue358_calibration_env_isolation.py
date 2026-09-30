"""Issue #358: routing/worker tests must not depend on the host calibration.

A worker launched by the desktop app inherits SWARM_MODEL_CALIBRATION_CATALOG.
The bundled-contract test modules must pass even when that variable points at
a real, materially different catalog.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "issue_worker"
MODELS = ROOT / "skills" / "model-router" / "models.yaml"


class CalibrationEnvIsolationTests(unittest.TestCase):
    def run_module(self, module: str, catalog: str) -> None:
        env = dict(os.environ, SWARM_MODEL_CALIBRATION_CATALOG=catalog)
        result = subprocess.run(
            [sys.executable, "-m", "unittest", module],
            cwd=WORKER, env=env, capture_output=True, text=True, timeout=1500,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])
        self.assertNotIn("NO TESTS RAN", result.stderr.upper())

    def hostile_catalog(self, directory: str) -> str:
        # Valid catalog in which nothing is routable: any test that reads the
        # host calibration instead of the bundled catalog finds no candidates.
        text = MODELS.read_text().replace("active: true", "active: false")
        self.assertNotEqual(text, MODELS.read_text())
        path = Path(directory) / "active_catalog.json"
        path.write_text(text)
        return str(path)

    def test_routing_calculator_ignores_host_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.run_module("test_routing_calculator", self.hostile_catalog(d))

    def test_worker_suite_ignores_host_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.run_module("test_swarm_issue_worker", self.hostile_catalog(d))


if __name__ == "__main__":
    unittest.main()
