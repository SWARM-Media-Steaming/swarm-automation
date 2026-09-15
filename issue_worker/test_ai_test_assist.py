#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ai_test_assist as assist


class CapacityTestCase(unittest.TestCase):
    def test_claude_capacity_reports_unavailable_when_not_on_path(self) -> None:
        result = assist.claude_capacity("/nonexistent/claude", 10)
        self.assertFalse(result["available"])
        self.assertIn("not found", result["detail"])

    def test_claude_capacity_reads_session_and_week_remaining(self) -> None:
        usage = "Current session: 40% used\nCurrent week (rolling): 10% used\n"
        with mock.patch.object(assist, "command_available", return_value=True), mock.patch.object(
            assist, "_run", return_value=(0, json.dumps({"result": usage}))
        ):
            result = assist.claude_capacity("claude", 10)
        self.assertTrue(result["available"])
        self.assertIn("60%", result["detail"])

    def test_claude_capacity_is_unavailable_below_the_minimum(self) -> None:
        usage = "Current session: 95% used\nCurrent week: 10% used\n"
        with mock.patch.object(assist, "command_available", return_value=True), mock.patch.object(
            assist, "_run", return_value=(0, json.dumps({"result": usage}))
        ):
            result = assist.claude_capacity("claude", 10)
        self.assertFalse(result["available"])

    def test_grok_capacity_requires_sign_in(self) -> None:
        with mock.patch.object(assist, "command_available", return_value=True), mock.patch.dict(
            "os.environ", {"HOME": "/nonexistent-home"}, clear=False
        ):
            result = assist.grok_capacity("grok")
        self.assertFalse(result["available"])
        self.assertIn("signed in", result["detail"])

    def test_pick_provider_skips_disabled_and_reports_the_first_available(self) -> None:
        providers = [
            {"id": "claude", "bin": "claude", "enabled": False},
            {"id": "grok", "bin": "grok", "enabled": True},
        ]
        with mock.patch.object(assist, "grok_capacity", return_value={"available": True, "detail": "ok"}):
            result = assist.pick_provider(providers, 10, "python3", ".")
        self.assertEqual(result, {"available": True, "provider": "grok", "detail": "ok"})

    def test_pick_provider_reports_none_available_with_reasons(self) -> None:
        providers = [{"id": "grok", "bin": "grok", "enabled": True}]
        with mock.patch.object(assist, "grok_capacity", return_value={"available": False, "detail": "not signed in"}):
            result = assist.pick_provider(providers, 10, "python3", ".")
        self.assertFalse(result["available"])
        self.assertIsNone(result["provider"])
        self.assertIn("grok: not signed in", result["detail"])


class GenerateTestCase(unittest.TestCase):
    def test_generate_only_supports_claude_today(self) -> None:
        result = assist.generate("codex", "codex", "", "prompt", 5)
        self.assertFalse(result["ok"])
        self.assertIn("codex", result["error"])

    def test_generate_returns_the_models_text(self) -> None:
        with mock.patch.object(assist, "command_available", return_value=True), mock.patch.object(
            assist, "_run", return_value=(0, json.dumps({"result": "sample data"}))
        ):
            result = assist.generate("claude", "claude", "", "prompt", 5)
        self.assertEqual(result, {"ok": True, "text": "sample data"})


class DiscoverTestCase(unittest.TestCase):
    def test_shallow_listing_skips_ignored_directories(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            (root / "node_modules").mkdir()
            (root / "node_modules" / "left-pad").mkdir()
            (root / "Makefile").write_text("test:\n\techo hi\n")
            listing = assist.shallow_listing(root)
        self.assertIn("Makefile", listing)
        self.assertNotIn("node_modules", listing)

    def test_discover_validates_and_drops_malformed_suggestions(self) -> None:
        payload = {
            "suites": [
                {"id": "make", "name": "Make tests", "command": ["make", "test"]},
                {"id": "bad", "name": "No command", "command": []},
                {"id": "also-bad", "command": "not-a-list"},
            ],
            "notes": ["review before enabling"],
        }
        with mock.patch.object(assist, "generate", return_value={"ok": True, "text": json.dumps(payload)}), \
                tempfile.TemporaryDirectory() as workspace:
            (Path(workspace) / "Makefile").write_text("test:\n\techo hi\n")
            result = assist.discover(workspace, "claude", "claude", "", 5)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["suites"]), 1)
        self.assertEqual(result["suites"][0]["command"], ["make", "test"])
        self.assertEqual(result["notes"], ["review before enabling"])

    def test_discover_reports_invalid_json_instead_of_raising(self) -> None:
        with mock.patch.object(assist, "generate", return_value={"ok": True, "text": "not json"}), \
                tempfile.TemporaryDirectory() as workspace:
            (Path(workspace) / "Makefile").write_text("test:\n\techo hi\n")
            result = assist.discover(workspace, "claude", "claude", "", 5)
        self.assertFalse(result["ok"])
        self.assertIn("invalid JSON", result["error"])

    def test_discover_skips_the_ai_call_entirely_on_an_empty_workspace(self) -> None:
        with mock.patch.object(assist, "generate") as generate, tempfile.TemporaryDirectory() as workspace:
            result = assist.discover(workspace, "claude", "claude", "", 5)
        generate.assert_not_called()
        self.assertEqual(result, {"ok": True, "suites": [], "notes": []})


if __name__ == "__main__":
    unittest.main()
