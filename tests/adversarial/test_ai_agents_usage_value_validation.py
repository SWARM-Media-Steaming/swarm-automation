"""Issue #218: usage JSON must remain valid and useful when one provider
returns a malformed percentage.

The Overview command crosses a Python/Rust JSON boundary.  Python's JSON
encoder permits ``NaN`` and infinities by default, while Rust's
``serde_json`` decoder rejects them.  A provider helper can also return a
finite value outside the domain of a percentage.  None of those malformed
values may invalidate the consolidated response and hide healthy providers;
the bad row should degrade to the existing "unavailable" state instead.

These tests exercise ``check_usage`` with deterministic provider results and
use a strict JSON decoder, matching the desktop command's parsing contract.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
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


def reject_non_finite_json(token: str) -> None:
    raise ValueError(f"non-standard JSON number: {token}")


class AiAgentsUsageValueValidationTests(unittest.TestCase):
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
                "--enabled-provider",
                "grok",
            ]
        )
        return Config.from_args(args)

    def test_malformed_percentage_only_degrades_its_provider(self) -> None:
        healthy = {
            "claude": ProviderUsage(
                0,
                82.0,
                "session 82% / week 95% remaining",
            ),
            "grok": ProviderUsage(0, 64.0, "weekly 64% remaining"),
        }

        # ProviderUsage documents None as the unavailable representation; a
        # usable status without a percentage is therefore inconsistent too.
        for malformed in (None, math.nan, math.inf, -0.1, 100.1):
            with self.subTest(malformed=malformed):
                def usage_for(provider: str) -> ProviderUsage:
                    if provider == "codex":
                        return ProviderUsage(0, malformed, "primary malformed% remaining")
                    return healthy[provider]

                output = io.StringIO()
                with (
                    mock.patch.object(Worker, "provider_usage", side_effect=usage_for),
                    contextlib.redirect_stdout(output),
                ):
                    exit_code = check_usage(self.enabled_config())

                self.assertEqual(exit_code, 0)
                payload = json.loads(
                    output.getvalue().strip(),
                    parse_constant=reject_non_finite_json,
                )
                reported = {
                    entry["provider"]: entry for entry in payload["providers"]
                }
                self.assertEqual(set(reported), {"claude", "codex", "grok"})
                self.assertEqual(reported["codex"]["status"], 2)
                self.assertIsNone(reported["codex"]["remaining_percent"])
                self.assertIsNone(reported["codex"]["detail"])
                self.assertEqual(reported["claude"]["remaining_percent"], 82.0)
                self.assertEqual(
                    reported["claude"]["detail"],
                    "session 82% / week 95% remaining",
                )
                self.assertEqual(reported["grok"]["remaining_percent"], 64.0)


if __name__ == "__main__":
    unittest.main()
