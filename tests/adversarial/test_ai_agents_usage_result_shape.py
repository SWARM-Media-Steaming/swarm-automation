"""Issue #259 acceptance: malformed provider usage results degrade gracefully.

check_usage() probes each provider's remaining quota for the Overview panel (#218).
When a provider returns a malformed result—a plain dict, tuple, or None instead of
a ProviderUsage instance—it must degrade only that provider, not crash and discard
the already-collected results for every other enabled provider.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import test_swarm_issue_worker as fixtures  # noqa: E402
from swarm_issue_worker import Config, ProviderUsage, check_usage  # noqa: E402


class UsageResultShapeTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def test_wrong_shaped_result_only_degrades_its_provider(self) -> None:
        """Malformed provider results degrade only that provider's row."""
        from swarm_issue_worker import Worker  # noqa: F811

        def claude_usage(self_: Worker) -> object:
            # Return a plain dict instead of ProviderUsage — a JSON boundary failure
            return {}

        def codex_usage(self_: Worker) -> object:
            # Return a tuple instead of ProviderUsage
            return (0, 50.0, "session 50% remaining")

        def grok_usage(self_: Worker) -> ProviderUsage:
            # Return valid ProviderUsage
            return ProviderUsage(0, 75.0, "session 75% remaining")

        config = self.worker.config
        with (
            mock.patch.object(Worker, "claude_usage", claude_usage),
            mock.patch.object(Worker, "codex_usage", codex_usage),
            mock.patch.object(Worker, "grok_usage", grok_usage),
        ):
            stdout_capture = io.StringIO()
            with contextlib.redirect_stdout(stdout_capture):
                exit_code = check_usage(config)

        self.assertEqual(exit_code, 0, "check_usage must return 0 on success")

        output_line = stdout_capture.getvalue().strip()
        self.assertTrue(
            output_line,
            "check_usage must output exactly one line of JSON",
        )

        result = json.loads(output_line)
        self.assertIn("providers", result)
        providers = {p["provider"]: p for p in result["providers"]}

        # Claude had a malformed result (empty dict) — should degrade to unavailable
        self.assertIn("claude", providers)
        claude_row = providers["claude"]
        self.assertEqual(
            claude_row["status"],
            2,
            "Malformed dict result must degrade Claude to unavailable (status=2)",
        )

        # Codex had a malformed result (tuple) — should degrade to unavailable
        self.assertIn("codex", providers)
        codex_row = providers["codex"]
        self.assertEqual(
            codex_row["status"],
            2,
            "Malformed tuple result must degrade Codex to unavailable (status=2)",
        )

        # Grok had a valid result — should be preserved
        self.assertIn("grok", providers)
        grok_row = providers["grok"]
        self.assertEqual(
            grok_row["status"],
            0,
            "Valid Grok result must be preserved with status=0",
        )
        self.assertEqual(
            grok_row["remaining_percent"],
            75.0,
            "Valid Grok result must preserve remaining_percent",
        )
        self.assertEqual(
            grok_row["detail"],
            "session 75% remaining",
            "Valid Grok result must preserve detail",
        )

    def test_provider_exception_degrades_gracefully(self) -> None:
        """Provider probe exceptions degrade only that provider."""
        from swarm_issue_worker import Worker  # noqa: F811

        def claude_usage(self_: Worker) -> ProviderUsage:
            raise RuntimeError("temporary quota endpoint failure")

        def codex_usage(self_: Worker) -> ProviderUsage:
            return ProviderUsage(1, 15.0, "session 15% remaining")

        def grok_usage(self_: Worker) -> ProviderUsage:
            return ProviderUsage(0, 90.0, "session 90% remaining")

        config = self.worker.config
        with (
            mock.patch.object(Worker, "claude_usage", claude_usage),
            mock.patch.object(Worker, "codex_usage", codex_usage),
            mock.patch.object(Worker, "grok_usage", grok_usage),
        ):
            stdout_capture = io.StringIO()
            with contextlib.redirect_stdout(stdout_capture):
                exit_code = check_usage(config)

        self.assertEqual(exit_code, 0)

        result = json.loads(stdout_capture.getvalue().strip())
        providers = {p["provider"]: p for p in result["providers"]}

        # Claude raised — should degrade to unavailable
        self.assertEqual(providers["claude"]["status"], 2)

        # Codex and Grok should be preserved
        self.assertEqual(providers["codex"]["status"], 1)
        self.assertEqual(providers["codex"]["remaining_percent"], 15.0)
        self.assertEqual(providers["grok"]["status"], 0)
        self.assertEqual(providers["grok"]["remaining_percent"], 90.0)

    def test_invalid_status_field_degrades(self) -> None:
        """Malformed status field (wrong type or invalid value) degrades."""
        from swarm_issue_worker import Worker  # noqa: F811

        def claude_usage(self_: Worker) -> ProviderUsage:
            # status is an int but not 0, 1, or 2 — invalid for this API
            usage = ProviderUsage(5, 50.0)  # type: ignore
            return usage

        def codex_usage(self_: Worker) -> ProviderUsage:
            # status is bool (Python bools are ints, but we must reject them)
            usage = ProviderUsage(True, 50.0)  # type: ignore
            return usage

        def grok_usage(self_: Worker) -> ProviderUsage:
            return ProviderUsage(0, 80.0)

        config = self.worker.config
        with (
            mock.patch.object(Worker, "claude_usage", claude_usage),
            mock.patch.object(Worker, "codex_usage", codex_usage),
            mock.patch.object(Worker, "grok_usage", grok_usage),
        ):
            stdout_capture = io.StringIO()
            with contextlib.redirect_stdout(stdout_capture):
                exit_code = check_usage(config)

        self.assertEqual(exit_code, 0)

        result = json.loads(stdout_capture.getvalue().strip())
        providers = {p["provider"]: p for p in result["providers"]}

        # Both Claude and Codex must degrade due to invalid status
        self.assertEqual(
            providers["claude"]["status"],
            2,
            "Out-of-range status (5) must degrade to unavailable",
        )
        self.assertEqual(
            providers["codex"]["status"],
            2,
            "Boolean status must degrade to unavailable",
        )

        # Grok should be valid
        self.assertEqual(providers["grok"]["status"], 0)

    def test_invalid_remaining_percent_degrades(self) -> None:
        """Malformed remaining_percent field (NaN, inf, out-of-range) degrades."""
        from swarm_issue_worker import Worker  # noqa: F811

        def claude_usage(self_: Worker) -> ProviderUsage:
            # remaining_percent is NaN
            usage = ProviderUsage(0, float("nan"))  # type: ignore
            return usage

        def codex_usage(self_: Worker) -> ProviderUsage:
            # remaining_percent is inf
            usage = ProviderUsage(0, float("inf"))  # type: ignore
            return usage

        def grok_usage(self_: Worker) -> ProviderUsage:
            # remaining_percent is out of valid range (>100)
            usage = ProviderUsage(0, 150.0)  # type: ignore
            return usage

        config = self.worker.config
        with (
            mock.patch.object(Worker, "claude_usage", claude_usage),
            mock.patch.object(Worker, "codex_usage", codex_usage),
            mock.patch.object(Worker, "grok_usage", grok_usage),
        ):
            stdout_capture = io.StringIO()
            with contextlib.redirect_stdout(stdout_capture):
                exit_code = check_usage(config)

        self.assertEqual(exit_code, 0)

        result = json.loads(stdout_capture.getvalue().strip())
        providers = {p["provider"]: p for p in result["providers"]}

        # All three must degrade due to invalid remaining_percent
        self.assertEqual(
            providers["claude"]["status"],
            2,
            "NaN remaining_percent must degrade to unavailable",
        )
        self.assertEqual(
            providers["codex"]["status"],
            2,
            "Infinite remaining_percent must degrade to unavailable",
        )
        self.assertEqual(
            providers["grok"]["status"],
            2,
            "Out-of-range remaining_percent (>100) must degrade to unavailable",
        )

    def test_invalid_detail_field_degrades(self) -> None:
        """Malformed detail field (non-string) degrades."""
        from swarm_issue_worker import Worker  # noqa: F811

        def claude_usage(self_: Worker) -> ProviderUsage:
            # detail is an int instead of str
            usage = ProviderUsage(0, 50.0, 12345)  # type: ignore
            return usage

        def codex_usage(self_: Worker) -> ProviderUsage:
            # detail is a dict instead of str
            usage = ProviderUsage(0, 50.0, {"info": "usage"})  # type: ignore
            return usage

        def grok_usage(self_: Worker) -> ProviderUsage:
            # detail is None (valid)
            return ProviderUsage(0, 80.0, None)

        config = self.worker.config
        with (
            mock.patch.object(Worker, "claude_usage", claude_usage),
            mock.patch.object(Worker, "codex_usage", codex_usage),
            mock.patch.object(Worker, "grok_usage", grok_usage),
        ):
            stdout_capture = io.StringIO()
            with contextlib.redirect_stdout(stdout_capture):
                exit_code = check_usage(config)

        self.assertEqual(exit_code, 0)

        result = json.loads(stdout_capture.getvalue().strip())
        providers = {p["provider"]: p for p in result["providers"]}

        # Both Claude and Codex must degrade due to invalid detail
        self.assertEqual(
            providers["claude"]["status"],
            2,
            "Non-string detail must degrade to unavailable",
        )
        self.assertEqual(
            providers["codex"]["status"],
            2,
            "Dict detail must degrade to unavailable",
        )

        # Grok should be valid
        self.assertEqual(providers["grok"]["status"], 0)
        self.assertIsNone(providers["grok"]["detail"])

    def test_json_output_is_valid_and_parseable(self) -> None:
        """The JSON output is valid serde_json and contains all required fields."""
        from swarm_issue_worker import Worker  # noqa: F811

        def claude_usage(self_: Worker) -> ProviderUsage:
            return ProviderUsage(0, 85.5, "session 85.5% remaining")

        def codex_usage(self_: Worker) -> ProviderUsage:
            return ProviderUsage(1, 8.0)

        def grok_usage(self_: Worker) -> ProviderUsage:
            return ProviderUsage(2)

        config = self.worker.config
        with (
            mock.patch.object(Worker, "claude_usage", claude_usage),
            mock.patch.object(Worker, "codex_usage", codex_usage),
            mock.patch.object(Worker, "grok_usage", grok_usage),
        ):
            stdout_capture = io.StringIO()
            with contextlib.redirect_stdout(stdout_capture):
                exit_code = check_usage(config)

        self.assertEqual(exit_code, 0)

        output_line = stdout_capture.getvalue().strip()

        # Should parse as valid JSON
        result = json.loads(output_line)
        self.assertIn("providers", result)

        # Each provider row must have all required fields
        for provider_row in result["providers"]:
            self.assertIn("provider", provider_row)
            self.assertIn("name", provider_row)
            self.assertIn("status", provider_row)
            self.assertIn("remaining_percent", provider_row)
            self.assertIn("detail", provider_row)

            # status must be 0, 1, or 2
            self.assertIn(provider_row["status"], (0, 1, 2))

            # remaining_percent must be a number or null
            if provider_row["remaining_percent"] is not None:
                self.assertIsInstance(provider_row["remaining_percent"], (int, float))
                self.assertTrue(0 <= provider_row["remaining_percent"] <= 100)

            # detail must be string or null
            if provider_row["detail"] is not None:
                self.assertIsInstance(provider_row["detail"], str)


if __name__ == "__main__":
    unittest.main()
