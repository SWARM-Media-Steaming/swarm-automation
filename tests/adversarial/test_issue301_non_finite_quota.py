"""
Adversarial tests for issue #301: Non-finite Codex usage values are treated as usable.

Tests verify that non-finite quota percentages (NaN, Inf, -Inf) are properly rejected
and treated as unavailable, not usable.
"""
from __future__ import annotations

import json
import math
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "issue_worker"))

from swarm_issue_worker import Worker, build_parser, Config, ProviderUsage
import ai_test_assist as assist


class NonFiniteQuotaTestCase(unittest.TestCase):
    """Test non-finite quota handling across both Codex quota probes."""

    def test_codex_usage_from_limits_rejects_nan_in_primary(self) -> None:
        """codex_usage_from_limits must reject NaN in primary usedPercent."""
        limits = {"primary": {"usedPercent": float("nan")}, "secondary": {"usedPercent": 20.0}}

        # Create minimal worker
        worker = object.__new__(Worker)
        worker.config = mock.MagicMock()
        worker.config.minimum_remaining_percent_for.return_value = 0

        method = Worker.codex_usage_from_limits.__get__(worker, Worker)
        result = method(limits, log_result=False)

        self.assertIsNone(result, "Should return None for NaN in primary")

    def test_codex_usage_from_limits_rejects_nan_in_secondary(self) -> None:
        """codex_usage_from_limits must reject NaN in secondary usedPercent."""
        limits = {"primary": {"usedPercent": 50.0}, "secondary": {"usedPercent": float("nan")}}

        worker = object.__new__(Worker)
        worker.config = mock.MagicMock()
        worker.config.minimum_remaining_percent_for.return_value = 0

        method = Worker.codex_usage_from_limits.__get__(worker, Worker)
        result = method(limits, log_result=False)

        self.assertIsNone(result, "Should return None for NaN in secondary")

    def test_codex_usage_from_limits_rejects_inf(self) -> None:
        """codex_usage_from_limits must reject positive Infinity in usedPercent."""
        limits = {"primary": {"usedPercent": float("inf")}, "secondary": {"usedPercent": 20.0}}

        worker = object.__new__(Worker)
        worker.config = mock.MagicMock()
        worker.config.minimum_remaining_percent_for.return_value = 0

        method = Worker.codex_usage_from_limits.__get__(worker, Worker)
        result = method(limits, log_result=False)

        self.assertIsNone(result, "Should return None for +Inf")

    def test_codex_usage_from_limits_rejects_negative_inf(self) -> None:
        """codex_usage_from_limits must reject negative Infinity in usedPercent."""
        limits = {"primary": {"usedPercent": float("-inf")}, "secondary": {"usedPercent": 20.0}}

        worker = object.__new__(Worker)
        worker.config = mock.MagicMock()
        worker.config.minimum_remaining_percent_for.return_value = 0

        method = Worker.codex_usage_from_limits.__get__(worker, Worker)
        result = method(limits, log_result=False)

        self.assertIsNone(result, "Should return None for -Inf")

    def test_codex_usage_from_limits_rejects_both_windows_nan(self) -> None:
        """codex_usage_from_limits must reject when both windows have NaN."""
        limits = {"primary": {"usedPercent": float("nan")}, "secondary": {"usedPercent": float("nan")}}

        worker = object.__new__(Worker)
        worker.config = mock.MagicMock()
        worker.config.minimum_remaining_percent_for.return_value = 0

        method = Worker.codex_usage_from_limits.__get__(worker, Worker)
        result = method(limits, log_result=False)

        self.assertIsNone(result, "Should return None when both have NaN")

    def test_codex_usage_from_limits_accepts_finite_values(self) -> None:
        """codex_usage_from_limits must accept valid finite usedPercent values."""
        limits = {"primary": {"usedPercent": 30.0}, "secondary": {"usedPercent": 20.0}}

        worker = object.__new__(Worker)
        worker.config = mock.MagicMock()
        worker.config.minimum_remaining_percent_for.return_value = 0

        method = Worker.codex_usage_from_limits.__get__(worker, Worker)
        result = method(limits, log_result=False)

        self.assertIsNotNone(result, "Should accept finite values")
        self.assertIsInstance(result, ProviderUsage)
        self.assertEqual(result.remaining_percent, 70.0, "remaining should be 100-30")

    def test_codex_capacity_rejects_nan_used_percent(self) -> None:
        """ai_test_assist.codex_capacity must reject NaN in usedPercent."""
        limits = {"primary": {"usedPercent": float("nan")}, "secondary": {"usedPercent": 20.0}}

        with tempfile.TemporaryDirectory() as scripts:
            (Path(scripts) / "codex_rate_limits.py").write_text("# helper\n", encoding="utf-8")
            with mock.patch.object(assist, "command_available", return_value=True), \
                 mock.patch.object(assist, "_run", return_value=(0, json.dumps(limits, allow_nan=True))):
                result = assist.codex_capacity("codex", 0, "python3", scripts)

        self.assertFalse(result["available"], "Should mark unavailable for NaN")
        self.assertIn("invalid", result["detail"].lower(), "Should indicate invalid response")

    def test_codex_capacity_rejects_inf_used_percent(self) -> None:
        """ai_test_assist.codex_capacity must reject Infinity in usedPercent."""
        limits = {"primary": {"usedPercent": float("inf")}, "secondary": {"usedPercent": 20.0}}

        with tempfile.TemporaryDirectory() as scripts:
            (Path(scripts) / "codex_rate_limits.py").write_text("# helper\n", encoding="utf-8")
            with mock.patch.object(assist, "command_available", return_value=True), \
                 mock.patch.object(assist, "_run", return_value=(0, json.dumps(limits, allow_nan=True))):
                result = assist.codex_capacity("codex", 0, "python3", scripts)

        self.assertFalse(result["available"], "Should mark unavailable for +Inf")
        self.assertIn("invalid", result["detail"].lower(), "Should indicate invalid response")

    def test_codex_capacity_rejects_negative_inf_used_percent(self) -> None:
        """ai_test_assist.codex_capacity must reject negative Infinity in usedPercent."""
        limits = {"primary": {"usedPercent": float("-inf")}, "secondary": {"usedPercent": 20.0}}

        with tempfile.TemporaryDirectory() as scripts:
            (Path(scripts) / "codex_rate_limits.py").write_text("# helper\n", encoding="utf-8")
            with mock.patch.object(assist, "command_available", return_value=True), \
                 mock.patch.object(assist, "_run", return_value=(0, json.dumps(limits, allow_nan=True))):
                result = assist.codex_capacity("codex", 0, "python3", scripts)

        self.assertFalse(result["available"], "Should mark unavailable for -Inf")
        self.assertIn("invalid", result["detail"].lower(), "Should indicate invalid response")

    def test_codex_capacity_accepts_finite_values(self) -> None:
        """ai_test_assist.codex_capacity must accept finite usedPercent values."""
        limits = {"primary": {"usedPercent": 30.0}, "secondary": {"usedPercent": 20.0}}

        with tempfile.TemporaryDirectory() as scripts:
            (Path(scripts) / "codex_rate_limits.py").write_text("# helper\n", encoding="utf-8")
            with mock.patch.object(assist, "command_available", return_value=True), \
                 mock.patch.object(assist, "_run", return_value=(0, json.dumps(limits))):
                result = assist.codex_capacity("codex", 0, "python3", scripts)

        self.assertTrue(result["available"], "Should accept finite values")
        self.assertIn("remaining", result["detail"].lower(), "Should show remaining %")

    def test_codex_usage_from_limits_single_window_nan(self) -> None:
        """codex_usage_from_limits must reject NaN even with only one active window."""
        limits = {"primary": {"usedPercent": float("nan")}}

        worker = object.__new__(Worker)
        worker.config = mock.MagicMock()
        worker.config.minimum_remaining_percent_for.return_value = 0

        method = Worker.codex_usage_from_limits.__get__(worker, Worker)
        result = method(limits, log_result=False)

        self.assertIsNone(result, "Should reject NaN even with single window")

    def test_codex_usage_from_limits_edge_case_zero_window(self) -> None:
        """codex_usage_from_limits must handle empty window list correctly."""
        limits = {}

        worker = object.__new__(Worker)
        worker.config = mock.MagicMock()
        worker.config.minimum_remaining_percent_for.return_value = 0

        method = Worker.codex_usage_from_limits.__get__(worker, Worker)
        result = method(limits, log_result=False)

        self.assertIsNone(result, "Should handle empty limits")

    def test_codex_capacity_both_windows_invalid(self) -> None:
        """ai_test_assist.codex_capacity must reject when both windows have invalid values."""
        limits = {"primary": {"usedPercent": float("nan")}, "secondary": {"usedPercent": float("inf")}}

        with tempfile.TemporaryDirectory() as scripts:
            (Path(scripts) / "codex_rate_limits.py").write_text("# helper\n", encoding="utf-8")
            with mock.patch.object(assist, "command_available", return_value=True), \
                 mock.patch.object(assist, "_run", return_value=(0, json.dumps(limits, allow_nan=True))):
                result = assist.codex_capacity("codex", 0, "python3", scripts)

        self.assertFalse(result["available"], "Should mark unavailable when both are invalid")


if __name__ == "__main__":
    unittest.main()
