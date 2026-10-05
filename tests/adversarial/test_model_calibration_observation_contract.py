"""Issue #205: validate and preserve meaning across model-data refreshes.

Oracles derive from AC 3, 13, 15, 18 and the model/performance detail contract:
out-of-range source values must fail gracefully; substantial performance data
must reach the activated calibration; missing observations cannot explicitly re-enable a retired
model; learning an unknown price is not a price decrease for unchanged work.
All sources below use real adapters with deterministic JSON transport doubles.
"""

from calibration_uat_fixture import CalibrationUAT, NOW, model_entry, price_row


class ObservationContractTests(CalibrationUAT):
    def test_extreme_json_number_is_a_reported_validation_failure(self):
        self.remote({"models": [price_row()]}, activation_policy="auto")
        before = self.active_bytes()
        success = self.service.status_report()["last_successful_refresh_at"]
        # JSON integers have no IEEE-754 bound. This is valid JSON but cannot
        # be a finite monetary observation. Never send it to a live endpoint.
        try:
            result = self.remote({"models": [price_row(input_cost=10 ** 400, output_cost=None)]},
                                 now=NOW + 1, activation_policy="auto")
        except Exception as error:
            self.fail(f"Invalid source price escaped instead of becoming a refresh failure: {type(error).__name__}: {error}")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.active_bytes(), before)
        status = self.service.status_report()
        self.assertEqual(status["last_attempted_status"], "failed")
        self.assertEqual(status["last_successful_refresh_at"], success)

    def test_fourfold_speed_change_is_activated_and_recorded(self):
        self.remote({"models": [price_row(speed=40)]}, activation_policy="auto")
        result = self.remote({"models": [price_row(speed=160)]}, now=NOW + 1)
        self.assertEqual(result["status"], "changed",
                         "The refreshed performance observation was silently discarded as no change")
        self.assertTrue(result["activated"])
        active = self.service.load_active()
        self.assertEqual(active["models"][0]["speed"], 160)
        self.assertTrue(result["diff"]["performance_changes"])
        self.assertIsNotNone(result["simulation"])

    def test_price_only_overlay_does_not_resurrect_a_deprecated_model(self):
        self.local.append(model_entry("surviving-model"))
        self.service.ensure_bootstrap(now=NOW)
        retired = self.remote({"models": [price_row(deprecated=True)]},
                              now=NOW + 1, activation_policy="auto")
        self.assertTrue(retired["activated"])
        initial = {m["model"]: m for m in self.service.load_active()["models"]}
        self.assertEqual(initial["established"]["status"], "DEPRECATED")
        self.remote({"models": [price_row(output_cost=7)]},
                    now=NOW + 2, activation_policy="auto")
        active = {m["model"]: m for m in self.service.load_active()["models"]}
        self.assertNotIn(active["established"]["status"], ("ACTIVE", "CANDIDATE"),
                         "An omitted retirement flag was treated as explicit re-enablement")

    def test_expanding_price_coverage_is_not_a_routing_cost_saving(self):
        self.local = [
            model_entry("light", relative_capability=1, relative_cost=1,
                        relative_latency=5, relative_token_efficiency=5,
                        strengths=["general_reasoning", "mechanical_edit"]),
            model_entry("strong", input_cost=20, output_cost=80),
        ]
        active = self.service.ensure_bootstrap(now=NOW)
        self.assertEqual({d["model"] for d in active["routing"].values()}, {"light", "strong"},
                         "Exercise mixed known/unknown prices across actual workload routes")
        result = self.remote({"models": [price_row("light", input_cost=1, output_cost=4)]}, now=NOW + 1)
        self.assertEqual(result["diff"]["routing_changes"], [])
        delta = result["diff"]["estimated_cost_change_percent"]
        self.assertTrue(delta is None or delta == 0,
                        f"No route or previously known price changed, but adding an unknown price claimed {delta}% impact")
