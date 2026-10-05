"""Issue #205 AC 13, 16: version handles must keep their original meaning.

A user can keep a rollback version open while later automatic refreshes prune
history. Retiring an old version is acceptable; reusing its identifier for
different data can restore the wrong snapshot. Exercise real
source normalization, persistence and the normal history limit, offline.
"""

from calibration_uat_fixture import CalibrationUAT, NOW, calibration, price_row


class ImmutableVersionTests(CalibrationUAT):
    def setUp(self):
        super().setUp()
        self.service.ensure_bootstrap(now=NOW)

    def update(self, index):
        result = self.remote(
            {"models": [price_row(output_cost=index)]}, now=NOW + index,
        )
        self.assertEqual(result["status"], "changed")
        self.assertTrue(result["activated"])
        self.assertEqual(self.service.load_active()["models"][0]["output_cost"], index)
        return result["calibration_version"]

    def test_history_pruning_does_not_reissue_a_version_identifier(self):
        issued = {self.service.load_active()["version"]}
        for index in range(1, 41):
            version = self.update(index)
            self.assertTrue(
                version not in issued,
                f"New observation {index} reused calibration version {version}; "
                "rollback handles must be immutable even after pruning",
            )
            issued.add(version)

    def test_stale_rollback_handle_never_restores_a_different_snapshot(self):
        original_version = self.update(1)
        recorded = self.service.load_active()
        for index in range(2, 41):
            version = self.update(index)
            if version == original_version:
                break

        # A fresh command process has no in-memory knowledge of the old page.
        reader = calibration.ModelCalibrationService(self.service.state_dir)
        before = self.active_bytes()
        try:
            restored = reader.activate(original_version)
        except calibration.CalibrationError:
            # Explicitly reporting a pruned historical version is safe.
            self.assertEqual(self.active_bytes(), before)
        else:
            self.assertEqual(
                restored["models"][0]["output_cost"], recorded["models"][0]["output_cost"],
                "The rollback handle now restores a different snapshot than the one recorded",
            )
            self.assertEqual(restored["models"], recorded["models"])
            self.assertEqual(restored["routing"], recorded["routing"])


if __name__ == "__main__":
    import unittest
    unittest.main()
