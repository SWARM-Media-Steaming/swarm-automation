"""CLI adapter tests for Jev. No live network or real binary."""

from __future__ import annotations

import json
import subprocess
import unittest
from types import SimpleNamespace

from jev_cli import (
    JevCli,
    JevError,
    JevSettings,
    context_fingerprint,
    estimate_jev_cost,
    parse_jev_stdout,
    redact_cli_text,
    settings_from_mapping,
)


def _completed(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class ParseTests(unittest.TestCase):
    def test_answers_shape(self) -> None:
        raw = json.dumps({
            "answers": {"task_type": {"value": "BUG", "confidence": 0.92}},
            "usage": {"input_tokens": 40, "estimated_cost": 0.000002},
            "model": "jev-latest",
        })
        response = parse_jev_stdout(raw)
        self.assertEqual(response.raw_shape, "answers")
        self.assertEqual(response.answers["task_type"]["value"], "BUG")
        self.assertEqual(response.usage.input_tokens, 40)

    def test_decision_shape(self) -> None:
        response = parse_jev_stdout(json.dumps({"decision": "FIX_NOW", "confidence": 0.91}))
        self.assertEqual(response.raw_shape, "decision")

    def test_malformed_and_empty_fail(self) -> None:
        with self.assertRaises(JevError) as error:
            parse_jev_stdout("not json")
        self.assertEqual(error.exception.error_type, "malformed")
        with self.assertRaises(JevError):
            parse_jev_stdout("")
        with self.assertRaises(JevError):
            parse_jev_stdout("{}")

    def test_redaction_strips_keys(self) -> None:
        text = redact_cli_text("api_key=sk-secret-value-here-xxxxx")
        self.assertIn("[REDACTED]", text)
        self.assertNotIn("sk-secret", text)

    def test_fingerprint_is_stable_and_omits_prompt(self) -> None:
        a = context_fingerprint({"title": "Fix login", "prompt": "RAW SECRET"})
        b = context_fingerprint({"title": "Fix login", "prompt": "DIFFERENT"})
        self.assertEqual(a, b)
        self.assertEqual(len(a), 32)

    def test_cost_estimate_uses_published_input_price(self) -> None:
        self.assertAlmostEqual(estimate_jev_cost(1_000_000), 0.042)
        self.assertIsNone(estimate_jev_cost(None))


class CliTests(unittest.TestCase):
    def test_disabled_does_not_invoke(self) -> None:
        calls = []

        def runner(command, timeout, stdin):
            calls.append(command)
            return _completed()

        cli = JevCli(JevSettings(enabled=False, bin="/usr/bin/jev"), runner=runner)
        cli.bin_path = "/usr/bin/jev"
        with self.assertRaises(JevError) as error:
            cli.ask(state={"title": "x"}, questions={"q": {}})
        self.assertEqual(error.exception.error_type, "disabled")
        self.assertEqual(calls, [])

    def test_successful_ask_parses_json(self) -> None:
        def runner(command, timeout, stdin):
            self.assertIn("ask", command)
            self.assertIn("--json", command)
            return _completed(stdout=json.dumps({
                "answers": {"task_type": {"value": "FEATURE", "confidence": 0.88}},
                "usage": {"input_tokens": 12},
            }))

        cli = JevCli(JevSettings(enabled=True, bin="/usr/bin/jev", max_retries=0), runner=runner)
        cli.bin_path = "/usr/bin/jev"
        response = cli.ask(state={"title": "Add export"}, questions={"task_type": {"type": "choice"}})
        self.assertEqual(response.answers["task_type"]["value"], "FEATURE")
        self.assertGreaterEqual(response.latency_ms, 0)

    def test_timeout_retries_then_fails(self) -> None:
        calls = {"n": 0}

        def runner(command, timeout, stdin):
            calls["n"] += 1
            raise subprocess.TimeoutExpired(command, timeout)

        sleeps: list[float] = []
        cli = JevCli(
            JevSettings(enabled=True, bin="/usr/bin/jev", max_retries=2),
            runner=runner,
            sleeper=sleeps.append,
        )
        cli.bin_path = "/usr/bin/jev"
        with self.assertRaises(JevError) as error:
            cli.ask(state={}, questions={"q": {}})
        self.assertEqual(error.exception.error_type, "timeout")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(sleeps), 2)

    def test_authentication_failure_does_not_look_like_success(self) -> None:
        def runner(command, timeout, stdin):
            return _completed(stderr="unauthorized: missing api key", returncode=1)

        cli = JevCli(JevSettings(enabled=True, bin="/usr/bin/jev", max_retries=0), runner=runner)
        cli.bin_path = "/usr/bin/jev"
        with self.assertRaises(JevError) as error:
            cli.ask(state={}, questions={"q": {}})
        self.assertEqual(error.exception.error_type, "authentication")

    def test_missing_binary(self) -> None:
        cli = JevCli(JevSettings(enabled=True, bin=""))
        cli.bin_path = ""
        with self.assertRaises(JevError) as error:
            cli.ask(state={}, questions={"q": {}})
        self.assertEqual(error.exception.error_type, "not_installed")

    def test_settings_normalize_percent_and_unknown_fallback(self) -> None:
        settings = settings_from_mapping({
            "enabled": True,
            "confidenceAutomation": 90,
            "fallback": "nope",
            "timeout_seconds": 120,
        })
        self.assertAlmostEqual(settings.confidence_automation, 0.9)
        self.assertEqual(settings.fallback, "rules")
        self.assertEqual(settings.timeout_seconds, 60.0)


if __name__ == "__main__":
    unittest.main()
