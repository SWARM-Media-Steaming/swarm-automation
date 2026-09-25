"""Issue #218: the app's enabled-provider set must be authoritative for the
Overview usage probe.

The desktop command passes one ``--enabled-provider`` flag for each provider
enabled in saved configuration.  ``SWARM_ENABLED_PROVIDERS`` is only a
standalone-worker fallback; an inherited shell/launch-agent value must not be
merged into those explicit flags.  Otherwise a provider disabled by the user
is still invoked by every periodic Overview refresh.  The frontend happens to
filter the extra response row, but the unwanted CLI invocation still violates
both the enable/disable contract and the requirement to avoid excessive
probing.

These tests exercise the real command-line entrypoint with deterministic
provider probes.  They pin both sides of the precedence rule: explicit argv
wins when present, while the environment remains a valid fallback when argv
does not select a provider.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import test_swarm_issue_worker as fixtures  # noqa: E402
from swarm_issue_worker import ProviderUsage, Worker, main as worker_main  # noqa: E402


class AiAgentsEnabledProviderAuthorityTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def run_usage_probe(self, provider_args: list[str]) -> tuple[list[str], list[str]]:
        probed: list[str] = []

        def usage_for(provider: str) -> ProviderUsage:
            probed.append(provider)
            return ProviderUsage(0, 75.0, "test window 75% remaining")

        output = io.StringIO()
        argv = self._worker_argv(auto=False) + ["--check-usage", *provider_args]
        with (
            mock.patch.object(Worker, "provider_usage", side_effect=usage_for),
            contextlib.redirect_stdout(output),
        ):
            exit_code = worker_main(argv)

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue().strip())
        reported = [entry["provider"] for entry in payload["providers"]]
        return probed, reported

    def test_explicit_enabled_provider_replaces_conflicting_environment_default(self) -> None:
        # This is the desktop command shape when Claude is disabled and Codex
        # is enabled.  A stale launch environment still naming Claude must not
        # cause the periodic panel refresh to execute Claude's CLI.
        with mock.patch.dict(os.environ, {"SWARM_ENABLED_PROVIDERS": "claude"}):
            probed, reported = self.run_usage_probe(["--enabled-provider", "codex"])

        self.assertEqual(probed, ["codex"], "disabled Claude must not be probed")
        self.assertEqual(reported, ["codex"], "the response must mirror saved app configuration")

    def test_environment_provider_remains_the_fallback_without_explicit_flags(self) -> None:
        # Preserve the documented environment-only worker configuration; the
        # regression is precedence, not removal of SWARM_ENABLED_PROVIDERS.
        with mock.patch.dict(os.environ, {"SWARM_ENABLED_PROVIDERS": "grok"}):
            probed, reported = self.run_usage_probe([])

        self.assertEqual(probed, ["grok"])
        self.assertEqual(reported, ["grok"])


if __name__ == "__main__":
    unittest.main()
