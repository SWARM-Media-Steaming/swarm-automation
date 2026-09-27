"""Issue #205 AC 9, 13, 16: rollback cannot relabel a refresh's evidence.

After a price update is activated and rolled back, reopening AI Configuration
must not claim the rolled-back savings belong to the now-active older version.
Use real persisted service responses and the production JS presentation helpers;
the frontend cache is optional and must not determine historical truth.
"""

import json
import subprocess

from calibration_uat_fixture import CalibrationUAT, NOW, ROOT, calibration, model_entry, price_row


class RollbackSummaryIntegrationTests(CalibrationUAT):
    def setUp(self):
        super().setUp()
        self.local = [model_entry(input_cost=2, output_cost=8)]
        self.baseline = self.service.ensure_bootstrap(now=NOW)
        self.update = self.remote({"models": [price_row(output_cost=4)]}, now=NOW + 1)
        self.assertEqual(self.update["status"], "changed")
        self.assertLess(self.update["diff"]["estimated_cost_change_percent"], 0)
        self.service.activate(self.update["calibration_version"])

    def presentation(self, *, cached=None):
        # A newly opened page reads status through a fresh command process.
        reader = calibration.ModelCalibrationService(self.service.state_dir)
        script = """
const fs = require('node:fs');
const ui = require('./ui/model-calibration-ui.js');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const result = ui.refreshResult(input.status, input.cached);
process.stdout.write(JSON.stringify({result, lines: ui.resultSummaryLines(result)}));
"""
        command = subprocess.run(
            ["node", "-e", script], cwd=ROOT, text=True,
            input=json.dumps({"status": reader.status_report(), "cached": cached}),
            capture_output=True, timeout=15,
        )
        self.assertEqual(command.returncode, 0, command.stderr)
        return json.loads(command.stdout)

    def assert_rollback_summary_is_honest(self, presentation):
        lines = {row["label"]: row["value"] for row in presentation["lines"]}
        if "Calibration" not in lines:
            # Clearing a stale refresh summary is also a valid UI choice.
            return
        self.assertNotRegex(
            lines["Calibration"], r"(?i)^\s*Active\b",
            "The price-reduction summary describes the rolled-back update, not the active baseline",
        )
        self.assertIn(self.update["calibration_version"], lines["Calibration"])

    def test_reopened_page_does_not_attribute_rolled_back_changes_to_active_version(self):
        self.service.activate(self.baseline["version"])
        self.assertEqual(self.service.load_active()["models"][0]["output_cost"], 8)
        self.assert_rollback_summary_is_honest(self.presentation())

    def test_cached_page_also_describes_the_rolled_back_update_as_historical(self):
        self.service.activate(self.baseline["version"])
        self.assert_rollback_summary_is_honest(self.presentation(cached=self.update))

    def test_reopened_page_reports_the_update_as_active_before_rollback(self):
        shown = self.presentation()
        lines = {row["label"]: row["value"] for row in shown["lines"]}
        self.assertIn("Active", lines["Calibration"])
        self.assertIn(self.update["calibration_version"], lines["Calibration"])


if __name__ == "__main__":
    import unittest
    unittest.main()
