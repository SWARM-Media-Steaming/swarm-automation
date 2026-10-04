"""Issue #205/#374: activate benchmark updates even when the winner stays.

The benchmark data and measured task costs are calibration inputs, not just
the three display scores. A single available model is intentional: its same
identity/effort must not disguise a material change to measured data.
"""

from calibration_uat_fixture import CalibrationUAT, NOW, model_entry


class EffortBenchmarkRetentionTests(CalibrationUAT):
    def setUp(self):
        super().setUp()
        self.local = [model_entry(supported_efforts=["high"], benchmarks={"high": {
            "coding_agent_index": 80, "deep_swe": 80, "swe_atlas_qna": 80,
            "terminal_bench": 50, "benchmark_cost_per_task": 2,
            "data_quality": "MEASURED",
        }})]
        self.service.ensure_bootstrap(now=NOW)
        self.before = self.active_bytes()

    def assert_change_retained(self, field, value):
        self.local[0]["benchmarks"]["high"][field] = value
        result = self.service.refresh(source="local", force=True, now=NOW + 1)
        self.assertEqual(result["diff"]["routing_changes"], [], "Fixture keeps the only available model and effort")
        self.assertEqual(result["status"], "changed", f"A changed {field} is meaningful even without a different route winner")
        self.assertTrue(result["diff"]["benchmark_changes"], f"The activation diff omitted {field}")
        self.assertTrue(result["activated"])
        active = self.service.load_active()
        self.assertEqual(active["models"][0]["benchmarks"]["high"][field], value)
        self.assertNotEqual(self.active_bytes(), self.before)

    def test_terminal_benchmark_change_is_activated(self):
        self.assert_change_retained("terminal_bench", 95)

    def test_measured_task_cost_change_is_activated(self):
        self.assert_change_retained("benchmark_cost_per_task", 7)

    def test_measured_runtime_change_is_activated(self):
        self.assert_change_retained("benchmark_runtime_minutes", 45)
