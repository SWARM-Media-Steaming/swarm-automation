"""Issue #218: a malformed usage-probe result must degrade only its row.

The consolidated Overview panel is required to keep every enabled provider
visible.  That requires provider isolation not only when a helper raises, but
also when it returns an object that does not satisfy the ``ProviderUsage``
runtime contract.  Type annotations are not runtime validation, and an
accidental ``None``/mapping/tuple result must not prevent a healthy sibling's
quota and session/week detail from reaching the panel.
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


class AiAgentsUsageResultShapeTests(unittest.TestCase):
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

    def test_wrong_shaped_result_only_degrades_its_provider(self) -> None:
        healthy = ProviderUsage(
            0,
            82.0,
            "session 82% / week 95% remaining",
        )

        for malformed in (None, {}, (0, 50.0, "session 50% remaining")):
            with self.subTest(malformed=malformed):
                def usage_for(provider: str) -> object:
                    return malformed if provider == "claude" else healthy

                output = io.StringIO()
                with (
                    mock.patch.object(Worker, "provider_usage", side_effect=usage_for),
                    contextlib.redirect_stdout(output),
                ):
                    exit_code = check_usage(self.enabled_config())

                self.assertEqual(exit_code, 0)
                payload = json.loads(output.getvalue().strip())
                reported = {
                    entry["provider"]: entry for entry in payload["providers"]
                }
                self.assertEqual(set(reported), {"claude", "codex"})
                self.assertEqual(
                    reported["claude"],
                    {
                        "provider": "claude",
                        "name": "Claude",
                        "status": 2,
                        "remaining_percent": None,
                        "detail": None,
                    },
                )
                self.assertEqual(reported["codex"]["status"], 0)
                self.assertEqual(reported["codex"]["remaining_percent"], 82.0)
                self.assertEqual(
                    reported["codex"]["detail"],
                    "session 82% / week 95% remaining",
                )


if __name__ == "__main__":
    unittest.main()
