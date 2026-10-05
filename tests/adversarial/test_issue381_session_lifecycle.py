"""Issue #381 acceptance: native CLI session continuity, recovery and independence.

Oracle (derived from the issue before reading the diff):

* A resumed session that the CLI cannot find/load must recover by itself with
  one fresh session and the full current context, using the failure text the
  real CLIs print (captured below from the installed Claude Code and Codex).
* A session is reused only for the same issue, repository, role, model, effort
  and instructions; independent reviewers never inherit another role's session.
* Recovery and fallbacks must not inflate the prompt, and routing must keep
  working when no session state exists or the state is damaged.
* Damaged session metadata is a fresh start, never a crash.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

ISSUE_WORKER_DIR = Path(__file__).resolve().parents[2] / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

from prompt_sessions import resume_failure  # noqa: E402
from swarm_issue_worker import Config, IssueContext, ProviderChoice, Worker, build_parser  # noqa: E402
from token_usage import NormalizedUsage, invocation_usage  # noqa: E402

# Verbatim output of `codex exec resume <unknown-uuid>` and of
# `claude -p --resume <unknown-uuid> --output-format stream-json`.
CODEX_MISSING_THREAD = (
    "Error: thread/resume: thread/resume failed: no rollout found for thread id "
    "11111111-2222-4333-8444-555555555555 (code -32600)\n"
)
CLAUDE_MISSING_SESSION = json.dumps({
    "type": "result", "subtype": "error_during_execution", "is_error": True,
    "session_id": "11111111-2222-4333-8444-555555555555", "total_cost_usd": 0,
    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0},
    "errors": ["No conversation found with session ID: 11111111-2222-4333-8444-555555555555"],
}) + "\n"
SPEC_MARKER = "UNIQUE-SPEC-MARKER-381-7f3a"


class ResumeFailureClassificationTests(unittest.TestCase):
    def test_real_cli_missing_session_messages_are_recognised(self) -> None:
        self.assertTrue(resume_failure(CLAUDE_MISSING_SESSION))
        self.assertTrue(resume_failure("No conversation found with session ID: x\n"))
        self.assertTrue(resume_failure(CODEX_MISSING_THREAD),
                        "Codex's real missing-thread failure must trigger a fresh retry")
        self.assertTrue(resume_failure(json.dumps({
            "type": "error", "message": CODEX_MISSING_THREAD.strip()})))
        self.assertTrue(resume_failure(json.dumps({
            "type": "turn.failed", "error": {"message": CODEX_MISSING_THREAD.strip()}})))

    def test_unrelated_failures_do_not_discard_a_good_session(self) -> None:
        for text in (
            "Rate limit exceeded, try again later",
            "You've hit your usage limit",
            "Not logged in. Please run /login",
            "unknown model id: foo",
            "test_session_cookie failed: assertion error",
            "",
        ):
            with self.subTest(text=text):
                self.assertFalse(resume_failure(text))
        # Ordinary assistant prose mentioning an expired session is not a CLI failure.
        self.assertFalse(resume_failure(json.dumps({
            "type": "assistant", "message": {"content": [
                {"type": "text", "text": "The login session expired; I fixed the test."}]}})))


class WorkerFixture(unittest.TestCase):
    provider = "Claude"
    model = "claude-sonnet-5"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "ai/claude/issue-381")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "source.py").write_text("value = 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        config = Config.from_args(build_parser().parse_args([
            "--repo-dir", str(self.repo), "--state-dir", str(self.root / "state"),
            "--github-repository", "acme/project", "--no-dynamic-model-routing",
            "--no-require-bot-auth", "--github-apps-config", str(self.root / "none.json"),
        ]))
        self.worker = Worker(config)
        self.worker.issue = IssueContext(
            381, "Caching", f"Required behaviour {SPEC_MARKER}", ["enhancement"], "https://example.invalid/381")
        session = str(uuid.uuid4()) if self.provider == "Claude" else ""
        self.worker.choice = ProviderChoice(self.provider, self.model, "high", session)
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def confirm_session(self) -> str:
        """Remember a successfully completed turn for the current role."""
        if not self.worker.choice.session_id:
            self.worker.choice.session_id = str(uuid.uuid4())
            self.worker.update_state(session_id=self.worker.choice.session_id)
        self.worker.prepare_cli_session()
        self.worker.update_state(session_started=True)
        self.worker.remember_cli_session(True)
        return self.worker.choice.session_id

    def next_turn(self, resume: bool = True) -> bool:
        """What a later execution sees: True when the earlier session is reused."""
        self.worker.choice.resume = resume
        self.worker.prepare_cli_session()
        return self.worker.choice.resume

    def loop(self, stage: str, phase: str, epoch: int = 1, round_number: int = 1) -> None:
        self.worker.update_state(**{stage: {
            "active": True, "phase": phase, "epoch": epoch, "round": round_number}})

    def end_loop(self, stage: str) -> None:
        self.worker.update_state(**{stage: {"active": False}})


class RecoveryTests(WorkerFixture):
    def run_ai(self, runner_name: str, runner, prompt: str = "current request") -> int:
        with mock.patch.object(self.worker, runner_name, side_effect=runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}):
            return self.worker.run_ai(prompt)

    def test_claude_failed_resume_retries_once_with_a_fresh_session(self) -> None:
        old = self.confirm_session()
        calls = []

        def runner(prompt, env, activity="working"):
            calls.append((self.worker.choice.resume, self.worker.choice.session_id))
            if len(calls) == 1:
                self.worker._last_ai_raw_output = CLAUDE_MISSING_SESSION
                return 1
            self.worker.update_state(session_started=True)
            self.worker._last_ai_raw_output = ""
            return 0

        self.assertEqual(self.run_ai("_run_claude", runner), 0)
        self.assertEqual([c[0] for c in calls], [True, False])
        self.assertEqual(calls[0][1], old)
        self.assertNotEqual(calls[1][1], old)

    def test_codex_failed_resume_retries_once_with_a_fresh_thread(self) -> None:
        # Real Codex wording; the thread id is minted by Codex, so a fresh run
        # starts with no id and without `resume`.
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-terra", "medium", str(uuid.uuid4()))
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))
        old = self.confirm_session()
        calls = []

        def runner(prompt, env, activity="working"):
            calls.append((self.worker.choice.resume, self.worker.choice.session_id, prompt))
            if len(calls) == 1:
                self.worker._last_ai_raw_output = CODEX_MISSING_THREAD
                return 1
            self.worker.choice.session_id = str(uuid.uuid4())
            self.worker.update_state(session_id=self.worker.choice.session_id, session_started=True)
            self.worker._last_ai_raw_output = ""
            return 0

        status = self.run_ai("_run_codex", runner)
        self.assertEqual(len(calls), 2, "a missing Codex thread must be retried fresh, exactly once")
        self.assertEqual(status, 0)
        self.assertTrue(calls[0][0])
        self.assertEqual(calls[0][1], old)
        self.assertFalse(calls[1][0])
        self.assertNotEqual(calls[1][1], old)

    def test_a_non_session_failure_does_not_trigger_a_fresh_retry(self) -> None:
        old = self.confirm_session()
        calls = []

        def runner(prompt, env, activity="working"):
            calls.append(self.worker.choice.resume)
            self.worker._last_ai_raw_output = "Rate limit exceeded"
            return 1

        self.assertEqual(self.run_ai("_run_claude", runner), 1)
        self.assertEqual(calls, [True])
        self.assertEqual(self.worker.choice.session_id, old)

    def test_model_rejection_retry_does_not_duplicate_the_issue_requirements(self) -> None:
        self.worker.choice = ProviderChoice("Claude", "no-such-model-381", "high", str(uuid.uuid4()))
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))
        original = self.worker.build_prompt(False, "", False)
        self.assertEqual(original.count(SPEC_MARKER), 1, "precondition: the prompt carries the spec once")
        seen = []

        def runner(prompt, env, activity="working"):
            seen.append(prompt)
            if len(seen) == 1:
                self.worker.ai_diagnostic_file.write_text("unknown model id: no-such-model-381", encoding="utf-8")
                return 1
            return 0

        self.assertEqual(self.run_ai("_run_claude", runner, original), 0)
        self.assertEqual(len(seen), 2, "the bounded model fallback must still run")
        self.assertEqual(seen[1].count(SPEC_MARKER), 1,
                         "a model-rejection retry re-sends the requirements once, not twice")
        self.assertLessEqual(len(seen[1]), len(seen[0]) + 400)

    def test_damaged_cumulative_baseline_is_unavailable_not_a_crash(self) -> None:
        usage = NormalizedUsage(input_tokens=500, output_tokens=50, usage_scope="session")
        for baseline in ("zz", [1], 7, {"input_tokens": "9"}):
            with self.subTest(baseline=baseline):
                result = invocation_usage(usage, True, baseline)
                self.assertIsNone(result.input_tokens)


class RerouteResilienceTests(WorkerFixture):
    PINNED, FRESH = "claude-sonnet-5-5", "claude-haiku-4-5"

    def verdict(self):
        pinned = ProviderChoice("Claude", self.PINNED, "high", "s")
        fresh = ProviderChoice("Claude", self.FRESH, "high", "s")
        profiles = {self.PINNED: (3, 0.10), self.FRESH: (3, 0.01)}
        with mock.patch("swarm_issue_worker.model_route_profile",
                        side_effect=lambda agent, model, effort: profiles[model]), \
             mock.patch.object(self.worker, "model_still_offered", return_value=True):
            return self.worker.reroute_verdict(pinned, fresh, True)

    def test_reroute_decision_works_without_in_progress_state(self) -> None:
        self.worker.in_progress_file.unlink()
        switch, why = self.verdict()
        self.assertTrue(switch, why)

    def test_reroute_decision_survives_a_damaged_state_file(self) -> None:
        self.worker.in_progress_file.write_text("{not json", encoding="utf-8")
        switch, why = self.verdict()
        self.assertTrue(switch, why)

    def test_capability_still_beats_cache_evidence(self) -> None:
        evidence = [{"provider": "claude", "model": self.PINNED, "effort": "high", "role": "primary",
                     "samples": 50, "issues": 10, "success_rate": 1.0, "api_cost_discount": 0.5}]
        pinned = ProviderChoice("Claude", self.PINNED, "high", "s")
        fresh = ProviderChoice("Claude", self.FRESH, "high", "s")
        profiles = {self.PINNED: (3, 0.10), self.FRESH: (6, 0.06)}
        with mock.patch("swarm_issue_worker.model_route_profile",
                        side_effect=lambda agent, model, effort: profiles[model]), \
             mock.patch.object(self.worker, "cache_routing_evidence", return_value=evidence), \
             mock.patch.object(self.worker, "model_still_offered", return_value=True):
            switch, why = self.worker.reroute_verdict(pinned, fresh, True)
        self.assertTrue(switch, why)


class IsolationTests(WorkerFixture):
    def test_reuse_requires_a_confirmed_compatible_session(self) -> None:
        session = self.confirm_session()
        self.assertTrue(self.next_turn())
        self.assertEqual(self.worker.choice.session_id, session)

    def test_source_edits_and_new_commits_do_not_discard_a_session(self) -> None:
        session = self.confirm_session()
        (self.repo / "source.py").write_text("value = 2\n")
        self.git("commit", "-qam", "progress")
        self.assertTrue(self.next_turn())
        self.assertEqual(self.worker.choice.session_id, session)

    def test_changed_issue_requirements_start_fresh(self) -> None:
        for field, value in (("body", "Different requirements"), ("title", "Another title"),
                             ("labels", ["Question"]), ("number", 382)):
            with self.subTest(field=field):
                old = self.confirm_session()
                setattr(self.worker.issue, field, value)
                self.assertFalse(self.next_turn())
                self.assertNotEqual(self.worker.choice.session_id, old)
                self.worker.issue = IssueContext(
                    381, "Caching", f"Required behaviour {SPEC_MARKER}", ["enhancement"], "https://example.invalid/381")

    def test_any_changed_instruction_file_starts_fresh(self) -> None:
        cases = {
            "root-agents": "AGENTS.md",
            "root-claude": "CLAUDE.md",
            "nested-agents": "pkg/deep/AGENTS.md",
            "claude-rule": ".claude/rules/new.md",
            "codex-config": ".codex/instructions.md",
        }
        for name, relative in cases.items():
            with self.subTest(name=name):
                old = self.confirm_session()
                path = self.repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"mandatory rule for {name}\n")
                self.assertFalse(self.next_turn(), f"{relative} is an instruction file")
                self.assertNotEqual(self.worker.choice.session_id, old)
                new = self.confirm_session()
                path.write_text(f"changed rule for {name}\n")
                self.assertFalse(self.next_turn(), f"edited {relative}")
                self.assertNotEqual(self.worker.choice.session_id, new)

    def test_unreadable_or_escaping_instruction_files_never_crash(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("outside rules")
        (self.repo / "AGENTS.md").symlink_to(outside)
        (self.repo / "CLAUDE.md").symlink_to(self.root / "does-not-exist.md")
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "AGENTS.md").mkdir()
        self.confirm_session()
        self.next_turn()  # must not raise

    def test_expired_session_is_not_reused(self) -> None:
        old = self.confirm_session()
        state = self.worker.read_state()
        state["cli_sessions"]["primary"]["updated_at"] = time.time() - 30 * 24 * 3600
        self.worker.write_state(state)
        self.assertFalse(self.next_turn())
        self.assertNotEqual(self.worker.choice.session_id, old)

    def test_a_new_issue_state_never_inherits_sessions(self) -> None:
        self.confirm_session()
        other = IssueContext(999, "Other", "Unrelated", [], "https://example.invalid/999")
        choice = ProviderChoice("Claude", self.model, "high", str(uuid.uuid4()))
        self.worker.issue = other
        self.worker.choice = choice
        self.worker.save_new_state(other, choice, self.git("rev-parse", "HEAD"))
        self.assertNotIn("primary", self.worker.read_state().get("cli_sessions") or {})
        self.assertFalse(self.next_turn())

    def test_a_non_native_provider_never_stores_or_reuses_a_session(self) -> None:
        self.worker.choice = ProviderChoice("Grok", "grok-4", "high", str(uuid.uuid4()))
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))
        self.worker.prepare_cli_session()
        self.worker.update_state(session_started=True)
        self.worker.remember_cli_session(True)
        self.assertEqual(self.worker.read_state().get("cli_sessions") or {}, {})


class AdversarialIndependenceTests(WorkerFixture):
    STAGES = ("adversarial", "adversarial_security")

    def test_independent_reviewers_never_resume_another_roles_session(self) -> None:
        primary = self.confirm_session()
        for stage in self.STAGES:
            with self.subTest(stage=stage):
                self.loop(stage, "test", round_number=0)
                # Even a state that (wrongly) asks to resume the implementer's
                # session id must produce a fresh, independent reviewer.
                self.worker.choice.session_id = primary
                self.assertFalse(self.next_turn(resume=True))
                self.assertNotEqual(self.worker.choice.session_id, primary)
                self.end_loop(stage)
                self.worker.choice.session_id = primary

    def test_every_new_assessment_is_fresh_even_for_the_same_provider_and_model(self) -> None:
        for stage in self.STAGES:
            with self.subTest(stage=stage):
                self.loop(stage, "test", round_number=0)
                self.worker.choice.session_id = str(uuid.uuid4())
                first = self.confirm_session()
                self.assertFalse(self.next_turn(resume=False))
                self.assertNotEqual(self.worker.choice.session_id, first)
                self.loop(stage, "test", round_number=1)
                self.worker.choice.session_id = first
                self.assertFalse(self.next_turn(resume=True), "round 1 must not inherit round 0's review")
                self.assertNotEqual(self.worker.choice.session_id, first)
                self.end_loop(stage)

    def test_uat_and_security_reviewers_are_independent_of_each_other(self) -> None:
        self.loop("adversarial", "test", round_number=0)
        uat = self.confirm_session()
        self.end_loop("adversarial")
        self.loop("adversarial_security", "test", round_number=0)
        self.worker.choice.session_id = uat
        self.assertFalse(self.next_turn(resume=True))
        self.assertNotEqual(self.worker.choice.session_id, uat)

    def test_an_interrupted_review_resumes_only_itself(self) -> None:
        self.loop("adversarial", "test", round_number=2)
        review = self.confirm_session()
        self.assertTrue(self.next_turn(resume=True))
        self.assertEqual(self.worker.choice.session_id, review)

    def test_fixer_continuity_is_per_stage_and_epoch(self) -> None:
        self.loop("adversarial", "fix", epoch=1)
        uat_fix = self.confirm_session()
        self.assertTrue(self.next_turn())
        self.assertEqual(self.worker.choice.session_id, uat_fix)
        self.loop("adversarial", "fix", epoch=2)
        self.assertFalse(self.next_turn(), "an escalated epoch starts a fresh fixer")
        self.end_loop("adversarial")
        self.loop("adversarial_security", "fix", epoch=1)
        self.worker.choice.session_id = uat_fix
        self.assertFalse(self.next_turn(resume=True), "security fixer must not take the UAT fixer's session")

    def test_fixer_does_not_change_model_or_effort_silently(self) -> None:
        self.loop("adversarial", "fix")
        fixer = self.confirm_session()
        self.worker.choice.effort = "max"
        self.assertFalse(self.next_turn())
        self.assertNotEqual(self.worker.choice.session_id, fixer)
        self.worker.choice.effort = "high"
        again = self.confirm_session()
        self.worker.choice.model = "claude-opus-5-5"
        self.assertFalse(self.next_turn())
        self.assertNotEqual(self.worker.choice.session_id, again)


class DamagedStateTests(WorkerFixture):
    def test_malformed_session_metadata_is_a_fresh_start(self) -> None:
        self.confirm_session()
        good = dict(self.worker.read_state()["cli_sessions"]["primary"])
        variants = {
            "map-is-list": [],
            "map-is-string": "x",
            "entry-is-string": {"primary": "x"},
            "id-is-int": {"primary": {**good, "id": 5}},
            "id-is-flag": {"primary": {**good, "id": "--last"}},
            "timestamp-text": {"primary": {**good, "updated_at": "later"}},
            "timestamp-nan": {"primary": {**good, "updated_at": float("nan")}},
            "head-is-int": {"primary": {**good, "head": 5}},
            "head-is-option": {"primary": {**good, "head": "--help"}},
            "head-unknown": {"primary": {**good, "head": "0" * 40}},
        }
        for name, value in variants.items():
            with self.subTest(name=name):
                state = self.worker.read_state()
                state["cli_sessions"] = value
                self.worker.write_state(state)
                self.worker.choice.session_id = str(uuid.uuid4())
                self.assertFalse(self.next_turn())
                self.assertNotEqual(self.worker.choice.session_id, good["id"])

    def test_damaged_usage_baseline_in_a_stored_session_does_not_break_accounting(self) -> None:
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-terra", "medium", str(uuid.uuid4()))
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))
        self.confirm_session()
        state = self.worker.read_state()
        state["cli_sessions"]["primary"]["usage_totals"] = "corrupt"
        self.worker.write_state(state)
        self.assertTrue(self.next_turn())
        self.worker._last_ai_raw_output = json.dumps({"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 150, "output_tokens": 20, "cached_input_tokens": 120}}})
        # The AI has already run; a damaged stored baseline must degrade to
        # "unavailable" instead of raising and losing the work-round.
        self.worker.record_ai_usage(attempt_number=1, started_at="2026-10-05T00:00:00+00:00", success=True)
        event = self.worker.read_state()["token_usage_events"][-1]
        self.assertIsNone(event["input_tokens"])


if __name__ == "__main__":
    unittest.main()
