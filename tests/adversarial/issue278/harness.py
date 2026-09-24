"""Local integration fixtures for issue #278; no provider or network execution.

The acceptance oracle is ownership: a reader may access only their own record.
The provider double reads the fixture and reports evidence, while real child
processes test both allowed and denied access. Only tests mutate this fixture.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
from pathlib import Path
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "issue_worker"))

import adversarial_security as security
import adversarial_uat as uat
import test_swarm_issue_worker as fixtures
from ai_execution_history import ExecutionHistoryService
from swarm_issue_worker import IssueContext, ProviderChoice, ProviderUsage

VULNERABLE = "def can_read(user, owner):\n    return True\n"
HARDENED = "def can_read(user, owner):\n    return user == owner\n"
SECURITY_ID = "adversarial-security-fixture-access"
UAT_ID = "adversarial-fixture-owner"
UNRELATED_ID = "adversarial-fixture-legacy"


def finding(**overrides):
    value = {
        "title": "Record access bypasses owner authorization",
        "description": "The record reader grants access to a different owner.",
        "files": ["access.py"], "severity": "High", "confidence": "high",
        "attack_scenario": "A signed-in reader requests another user's record.",
        "impact": "Disclosure of another user's private record.",
        "evidence": "can_read('alice', 'bob') returns True in access.py.",
        "remediation": "Compare the authenticated user to the record owner.",
        "suite_ids": [],
    }
    value.update(overrides)
    return value


def report(**overrides):
    value = {"summary": "Reviewed owner authorization and denied cross-owner access.",
             "dispute_resolution": "", "in_scope": [], "out_of_scope": []}
    value.update(overrides)
    return security.RESULT_MARKER + " " + json.dumps(value)


class LocalReviewFixture:
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def prepare(self, *, vulnerable=False, with_uat=False):
        self.worker.config = dataclasses.replace(
            self.worker.config, adversarial_security_enabled=True,
            adversarial_uat_enabled=with_uat, ai_execution_history_enabled=True,
            execution_history_db=self.state / "history.sqlite3",
            auto_approve=False, auto_merge=False, auto_promote=False,
            dynamic_model_routing=False,
        )
        self.worker.history = ExecutionHistoryService(True, self.state / "history.sqlite3")
        self.worker.issue = IssueContext(
            278, "Restrict record reads to the record owner",
            "Owners can read their records; other authenticated users must be denied.",
            [], "https://example.invalid/issues/278")
        self.worker.choice = ProviderChoice("Claude", "fixture-model", "high", "fixture-session")
        self.git("switch", "-c", "ai/claude/issue-278")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()
        (self.repo / "access.py").write_text(VULNERABLE if vulnerable else HARDENED)
        (self.repo / ".swarm").mkdir(exist_ok=True)
        self.definition = {"version": 1, "adversarialBootstrap": {
            "framework": "existing", "instructions": "Use unittest with local fixtures.",
            "suites": []}, "suites": []}
        self.write_definition()
        self.git("add", "--force", ".swarm/tests.json", "access.py")
        self.git("commit", "-qm", "[claude] Implement owner-only access (#278)")
        self.completion = self.git("rev-parse", "HEAD")
        self.calls = []
        self.api = []
        self.output = io.StringIO()

    def write_definition(self):
        (self.repo / ".swarm/tests.json").write_text(json.dumps(self.definition))

    def add_suite(self, suite_id, *, origin="adversarial-security", assertion=None):
        security_suite = origin == "adversarial-security"
        directory = self.repo / "tests/adversarial" / ("security" if security_suite else "")
        directory.mkdir(parents=True, exist_ok=True)
        name = "test_" + suite_id.replace("-", "_") + ".py"
        assertion = assertion or "self.assertFalse(access['can_read']('alice', 'bob'))"
        (directory / name).write_text(
            "import runpy\nimport unittest\n"
            "class Authorization(unittest.TestCase):\n"
            "    def test_access_boundary(self):\n"
            "        access = runpy.run_path('access.py')\n"
            f"        {assertion}\n")
        if not any(s["id"] == suite_id for s in self.definition["suites"]):
            self.definition["suites"].append({
                "id": suite_id, "name": suite_id, "origin": origin,
                "enabled": True, "disruptive": False, "timeoutSeconds": 20,
                "command": [sys.executable, "-m", "unittest", "discover", "-s",
                            str(directory.relative_to(self.repo)), "-p", name],
            })
        self.write_definition()

    def start(self, stage=security.SECURITY_STAGE):
        self.worker.initialize_stage(stage, self.completion, "## Summary\nOwner access implemented.")

    def loop(self):
        return self.worker.read_state()[security.SECURITY_STAGE.key]

    def row(self):
        return self.worker.history.repository.for_repository(self.worker.config.github_repository)[0]

    def gh(self, args, provider=None, body=None):
        self.api.append((args, body))
        if args[:2] == ["issue", "list"]:
            return "[]"
        if args[:2] == ["issue", "create"]:
            return "https://example.invalid/issues/901"
        return ""

    def reviewer(self, prompt, activity=""):
        loop = self.loop()
        self.calls.append((loop["phase"], self.worker.choice.session_id))
        if loop["phase"] == "fix":
            (self.repo / "access.py").write_text(HARDENED)
            self.worker.ai_output_file.write_text("Compared authenticated user and owner.")
        else:
            if not any(s["id"] == SECURITY_ID for s in self.definition["suites"]):
                self.add_suite(SECURITY_ID)
            vulnerable = (self.repo / "access.py").read_text() == VULNERABLE
            self.worker.ai_output_file.write_text(report(in_scope=[finding()] if vulnerable else []))
        return 0

    def patches(self, reviewer=None):
        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.redirect_stdout(self.output))
        stack.enter_context(mock.patch.object(self.worker, "provider_usage", return_value=ProviderUsage(0, 80)))
        stack.enter_context(mock.patch.object(self.worker, "ensure_bot_auth"))
        stack.enter_context(mock.patch.object(self.worker, "run_ai", side_effect=reviewer or self.reviewer))
        stack.enter_context(mock.patch.object(self.worker.github, "gh", side_effect=self.gh))
        # Stop at the delivery boundary: no real comments, PRs, or pushes.
        self.deliver = stack.enter_context(mock.patch.object(self.worker, "finalize_issue"))
        return stack
