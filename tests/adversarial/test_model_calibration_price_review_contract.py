"""Issue #205 AC 13 and Analyze Routing Update: describe the price that changed.

The backend and AI Configuration's real formatter must explain input/output/
reasoning price changes using stored values. Describing an unchanged output
price as a 0% change conceals the actual input or reasoning price increase.
This crosses Python refresh -> JSON -> the frontend's production formatter.
"""

import json
import subprocess

from calibration_uat_fixture import CalibrationUAT, NOW, ROOT, price_row


class PriceReviewContractTests(CalibrationUAT):
    def assert_visible_change(self, field, before, after, label):
        self.remote({"models": [price_row(**{field: before})]}, activation_policy="auto")
        result = self.remote({"models": [price_row(**{field: after})]}, now=NOW + 1)
        self.assertEqual(result["status"], "changed")
        self.assertEqual(len(result["diff"]["pricing_changes"]), 1)
        rendering = subprocess.run([
            "node", "-e",
            "const fs = require('node:fs');"
            "const ui = require('./ui/model-calibration-ui.js');"
            "const diff = JSON.parse(fs.readFileSync(0, 'utf8'));"
            "process.stdout.write(JSON.stringify(ui.changeDetailLines(diff)));",
        ], input=json.dumps(result["diff"]), cwd=ROOT, text=True,
            capture_output=True, timeout=10, check=False)
        self.assertEqual(rendering.returncode, 0, rendering.stderr)
        details = json.loads(rendering.stdout)
        model_details = " ".join(item["detail"] for item in details if item["heading"] == "fixture/established")
        analysis = self.service.analyze()
        model_answers = " ".join(item["answer"] for item in analysis["answers"]
                                 if "fixture/established" in item["question"])
        for surface, text in (("AI Configuration change details", model_details),
                              ("stored-data analysis", model_answers)):
            with self.subTest(surface=surface):
                self.assertIn(label, text.lower(), f"{surface} described the wrong price: {text}")
                self.assertIn(str(before), text, f"{surface} omitted the previous {label} price")
                self.assertIn(str(after), text, f"{surface} omitted the new {label} price")

    def test_input_only_price_change_is_explained_in_review_and_analysis(self):
        self.assert_visible_change("input_cost", 2.125, 3.625, "input")

    def test_reasoning_only_price_change_is_explained_in_review_and_analysis(self):
        self.assert_visible_change("reasoning_cost", 6.5, 9.25, "reasoning")

    def test_output_only_price_change_is_explained_in_review_and_analysis(self):
        self.assert_visible_change("output_cost", 8.5, 10.75, "output")
