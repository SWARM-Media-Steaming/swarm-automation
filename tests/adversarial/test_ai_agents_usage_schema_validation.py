"""Issue #218: malformed fields in one usage row must not invalidate the
consolidated Python-to-Rust response.

The Overview command emits JSON from Python and deserializes it into Rust
fields whose schema is stricter than Python's runtime type hints.  In
particular, ``bool`` and ``float`` values are not valid Rust ``i32`` status
codes, and an object/array is not a valid ``Option<String>`` detail.  Python's
``json.dumps`` accepts all of those values.  If ``check_usage`` forwards one,
``serde_json`` rejects the complete response and the panel loses the healthy
providers along with the malformed one.

These tests exercise the real consolidated probe with deterministic provider
results.  A malformed provider must degrade to the canonical unavailable row
while a healthy sibling and its session/week breakdown remain intact.
"""

from __future__ import annotations

import contextlib
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
from swarm_issue_worker import (  # noqa: E402
    Config,
    ProviderUsage,
    Worker,
    build_parser,
    check_usage,
)


class AiAgentsUsageSchemaValidationTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def enabled_config(self) -> Config:
        args = build_parser().parse_args(
            self._worker_argv(auto=False)
            + [
                "--enabled-provider",
                "claude",
                "--enabled-provider",
                "codex",
            ]
        )
        return Config.from_args(args)

    def probe_with_claude_usage(self, malformed: ProviderUsage) -> dict[str, dict]:
        healthy = ProviderUsage(
            0,
            82.0,
            "session 82% / week 95% remaining",
        )

        def usage_for(provider: str) -> ProviderUsage:
            return malformed if provider == "claude" else healthy

        output = io.StringIO()
        with (
            mock.patch.object(Worker, "provider_usage", side_effect=usage_for),
            contextlib.redirect_stdout(output),
        ):
            exit_code = check_usage(self.enabled_config())

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue().strip())
        return {entry["provider"]: entry for entry in payload["providers"]}

    def assert_only_claude_degraded(self, reported: dict[str, dict]) -> None:
        self.assertEqual(set(reported), {"claude", "codex"})
        self.assertIs(
            type(reported["claude"]["status"]),
            int,
            "the Rust boundary requires an integer status, not bool/float",
        )
        self.assertEqual(reported["claude"]["status"], 2)
        self.assertIsNone(reported["claude"]["remaining_percent"])
        self.assertIsNone(reported["claude"]["detail"])
        self.assertEqual(reported["codex"]["status"], 0)
        self.assertEqual(reported["codex"]["remaining_percent"], 82.0)
        self.assertEqual(
            reported["codex"]["detail"],
            "session 82% / week 95% remaining",
        )

    def test_non_integer_status_degrades_only_its_provider(self) -> None:
        # bool is an int subclass in Python, and 0.0 compares equal to 0; a
        # membership-only status check therefore accepts both even though
        # serde_json cannot deserialize either into Rust's i32 field.
        for malformed_status in (True, False, 0.0, 1.0):
            with self.subTest(malformed_status=malformed_status):
                reported = self.probe_with_claude_usage(
                    ProviderUsage(malformed_status, 50.0, "session 50% remaining")
                )
                self.assert_only_claude_degraded(reported)

    def test_non_string_detail_degrades_only_its_provider(self) -> None:
        for malformed_detail in (
            {"session": 50},
            ["session 50% remaining"],
            True,
            50,
        ):
            with self.subTest(malformed_detail=malformed_detail):
                reported = self.probe_with_claude_usage(
                    ProviderUsage(0, 50.0, malformed_detail)
                )
                self.assert_only_claude_degraded(reported)


if __name__ == "__main__":
    unittest.main()
