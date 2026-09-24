"""Issue #218: the Overview "AI agents" panel must show every *enabled*
provider's install/auth status, live remaining quota, and working state in
one place — the whole point being that a user no longer has to hunt for one
provider's status.

``check_usage()`` (issue_worker/swarm_issue_worker.py) is the new probe this
panel is built on: it loops over every enabled provider and calls
``Worker.provider_usage()`` for each, collecting one JSON payload. But the
loop has no per-provider isolation:

    for spec in config.providers:
        if not spec.enabled:
            continue
        usage = worker.provider_usage(spec.key)   # <-- can raise
        providers.append({...})

``claude_usage``/``codex_usage``/``grok_usage`` each shell out via
``self.config.python_bin`` to a *different* helper than the provider's own
CLI (``codex_rate_limits.py`` / ``grok_rate_limits.py``), a codepath that
``command_available(<provider-cli>)`` does not gate at all. A misconfigured
``python_bin``, a helper script raising, or any other unexpected failure in
one provider's probe is not caught anywhere in this loop, so the exception
propagates straight out of ``check_usage()`` — taking down the *already
computed* usage for every other enabled provider with it.

On the desktop side, ``check_provider_usage`` (src/main.rs) treats any
nonzero exit / unparsable stdout from this script as a single hard error for
the whole panel (``run_capture_owned`` -> ``Err`` -> nothing rendered for any
provider). So one provider having a bad probe blanks the *entire* "AI
agents" panel, directly contradicting the acceptance criterion that it
"shows... every enabled AI provider" — a transient hiccup in one provider
should degrade to reporting just that provider as unavailable, not take
visibility into the healthy providers down with it.

This reuses the existing ``issue_worker/test_swarm_issue_worker.py`` worker
fixture (git/state scaffolding, ``_worker_argv``) the same way
``test_now_working_boundary_logs.py`` does, rather than duplicating it.
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
from swarm_issue_worker import Config, Worker, build_parser, check_usage  # noqa: E402


class AiAgentsUsageProbeIsolationTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def config_with_enabled(self, *providers: str) -> Config:
        args = build_parser().parse_args(
            self._worker_argv(auto=False)
            + [flag for provider in providers for flag in ("--enabled-provider", provider)]
        )
        return Config.from_args(args)

    def test_one_providers_probe_exception_does_not_blank_the_others(self) -> None:
        config = self.config_with_enabled("claude", "codex", "grok")
        with mock.patch.object(Worker, "codex_usage", side_effect=RuntimeError("codex probe exploded")):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = check_usage(config)
        self.assertEqual(exit_code, 0)
        payload = json.loads(buffer.getvalue().strip())
        reported = {entry["provider"]: entry for entry in payload["providers"]}
        self.assertEqual(
            set(reported),
            {"claude", "codex", "grok"},
            "claude and grok must still be reported even though codex's own probe raised",
        )
        # Claude/Grok CLIs are unset in the fixture, so they legitimately
        # report unavailable too — the point is codex's exception must not
        # have prevented them from being reported at all.
        self.assertEqual(reported["codex"]["status"], 2)
        self.assertIsNone(reported["codex"]["remaining_percent"])

    def test_a_failure_on_the_first_enabled_provider_does_not_hide_a_later_one(self) -> None:
        # Enumeration order follows config.providers (claude, codex, grok);
        # failing the first-enumerated enabled provider is the case most
        # likely to wipe out everything after it if the loop aborts early.
        config = self.config_with_enabled("claude", "grok")
        with mock.patch.object(Worker, "claude_usage", side_effect=RuntimeError("claude probe exploded")):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = check_usage(config)
        self.assertEqual(exit_code, 0)
        payload = json.loads(buffer.getvalue().strip())
        reported = {entry["provider"]: entry for entry in payload["providers"]}
        self.assertIn("grok", reported, "grok must still be reported after claude's probe raised first")
        self.assertEqual(reported["claude"]["status"], 2)


if __name__ == "__main__":
    unittest.main()
