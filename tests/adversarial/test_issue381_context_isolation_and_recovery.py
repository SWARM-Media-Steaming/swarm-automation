"""Issue #381: remaining session, context and CLI-boundary oracles.

Derived from the issue before treating the implementation as correct:

* Provider CLIs read repository instructions from disk, including ignored
  directories (``.claude/``, ``.codex/``) and nested ``AGENTS.md``. Changing
  those files must start a fresh session.
* Rewound Git history and a different branch are incompatible execution
  context. Source edits that fast-forward HEAD may continue.
* Independent assessments (``_independent_ai_pass`` / documentation review)
  must not resume or remember the implementer's session even if the caller
  left ``resume=True`` and the implementer's UUID in place. Session selection
  is ``Worker.run_ai`` via ``PromptSessionMixin``.
* Successful native compaction is not a resume failure. ``--last`` is never a
  session identifier. Unconfirmed Codex turns are not reusable.
* Fix continuations may omit an unchanged issue body only while actually
  resuming; they must still receive the current patch, suite results and
  findings. A recovered fixer gets the full current specification.
* Quota-paused in-progress state keeps session metadata. Session records hold
  identities and digests only. Caching has no product UI/config toggle.
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

ISSUE_WORKER_DIR = Path(__file__).resolve().parents[2] / "issue_worker"
ROOT = Path(__file__).resolve().parents[2]
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

from prompt_sessions import resume_failure, valid_session_id  # noqa: E402
from swarm_issue_worker import (  # noqa: E402
    ADVERSARIAL_STAGES,
    Config,
    IssueContext,
    ProviderChoice,
    Worker,
    build_parser,
)

SPEC_MARKER = "UNIQUE-SPEC-MARKER-381-CTX-9c2e"
SESSION_IDENTITY_KEYS = {
    "id", "provider", "model", "effort", "context", "updated_at",
    "usage_totals", "head", "started",
}


class ContextFixture(unittest.TestCase):
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
        (self.repo / "source.py").write_text("value = 1\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        config = Config.from_args(build_parser().parse_args([
            "--repo-dir", str(self.repo), "--state-dir", str(self.root / "state"),
            "--github-repository", "acme/project", "--no-dynamic-model-routing",
            "--no-require-bot-auth", "--github-apps-config", str(self.root / "none.json"),
        ]))
        self.worker = Worker(config)
        self.worker.issue = IssueContext(
            381, "Intelligent Prompt Caching", f"Required behaviour {SPEC_MARKER}",
            ["enhancement"], "https://example.invalid/381",
        )
        self.worker.choice = ProviderChoice(
            self.provider, self.model, "high", str(uuid.uuid4()))
        self.worker.save_new_state(
            self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def confirm_session(self) -> str:
        if not self.worker.choice.session_id:
            self.worker.choice.session_id = str(uuid.uuid4())
            self.worker.update_state(session_id=self.worker.choice.session_id)
        self.worker.prepare_cli_session()
        self.worker.update_state(session_started=True)
        self.worker.remember_cli_session(True)
        return self.worker.choice.session_id

    def next_turn(self, resume: bool = True) -> bool:
        self.worker.choice.resume = resume
        self.worker.prepare_cli_session()
        return self.worker.choice.resume


class IgnoredInstructionDirectoryTests(ContextFixture):
    def test_ignored_claude_and_codex_directories_invalidate_reuse(self) -> None:
        (self.repo / ".gitignore").write_text(".claude/\n.codex/\npkg/\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore local instruction trees")
        claude_rule = self.repo / ".claude" / "rules" / "caching.md"
        claude_rule.parent.mkdir(parents=True)
        claude_rule.write_text("Preserve invariant A.\n", encoding="utf-8")
        codex_rule = self.repo / ".codex" / "instructions.md"
        codex_rule.parent.mkdir(parents=True)
        codex_rule.write_text("Preserve invariant A.\n", encoding="utf-8")
        nested = self.repo / "pkg" / "AGENTS.md"
        nested.parent.mkdir()
        nested.write_text("Preserve invariant A.\n", encoding="utf-8")
        old = self.confirm_session()

        claude_rule.write_text("Preserve invariant B.\n", encoding="utf-8")
        self.assertFalse(self.next_turn(), "ignored .claude rules are still CLI context")
        self.assertNotEqual(self.worker.choice.session_id, old)

        mid = self.confirm_session()
        codex_rule.write_text("Preserve invariant B.\n", encoding="utf-8")
        self.assertFalse(self.next_turn(), "ignored .codex instructions are still CLI context")
        self.assertNotEqual(self.worker.choice.session_id, mid)

        later = self.confirm_session()
        nested.write_text("Preserve invariant B.\n", encoding="utf-8")
        self.assertFalse(self.next_turn(), "ignored nested AGENTS.md is still CLI context")
        self.assertNotEqual(self.worker.choice.session_id, later)


class HistoryAndBranchBoundaryTests(ContextFixture):
    def test_rewound_history_is_not_reused(self) -> None:
        self.git("commit", "--allow-empty", "-qm", "progress")
        old = self.confirm_session()
        self.git("reset", "--hard", "HEAD~1")
        self.assertFalse(self.next_turn())
        self.assertNotEqual(self.worker.choice.session_id, old)

    def test_fast_forward_commits_keep_a_compatible_session(self) -> None:
        old = self.confirm_session()
        (self.repo / "source.py").write_text("value = 2\n", encoding="utf-8")
        self.git("commit", "-qam", "implementation progress")
        self.assertTrue(self.next_turn())
        self.assertEqual(self.worker.choice.session_id, old)

    def test_branch_change_starts_fresh(self) -> None:
        old = self.confirm_session()
        self.git("switch", "-c", "ai/claude/issue-381-other")
        self.assertFalse(self.next_turn())
        self.assertNotEqual(self.worker.choice.session_id, old)

    def test_future_timestamp_is_not_treated_as_freshly_used(self) -> None:
        old = self.confirm_session()
        state = self.worker.read_state()
        state["cli_sessions"]["primary"]["updated_at"] = time.time() + 86_400
        self.worker.write_state(state)
        self.assertFalse(self.next_turn())
        self.assertNotEqual(self.worker.choice.session_id, old)

    def test_provider_change_does_not_reuse_the_other_clis_session(self) -> None:
        old = self.confirm_session()
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-terra", "medium", old)
        self.assertFalse(self.next_turn(resume=True))
        self.assertNotEqual(self.worker.choice.session_id, old)


class IndependentPassFailClosedTests(ContextFixture):
    def test_independent_pass_flag_cannot_resume_the_implementer(self) -> None:
        implementer = self.confirm_session()
        seen: list[tuple[bool, str]] = []

        def runner(prompt, env, activity="working"):
            seen.append((self.worker.choice.resume, self.worker.choice.session_id))
            self.worker._last_ai_raw_output = json.dumps({
                "type": "result", "session_id": self.worker.choice.session_id,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            })
            return 0

        self.worker._independent_ai_pass = True
        self.worker.choice.resume = True
        self.worker.choice.session_id = implementer
        try:
            with mock.patch.object(self.worker, "_run_claude", side_effect=runner), \
                 mock.patch.object(self.worker, "provider_environment", return_value={}):
                status = self.worker.run_ai("documentation review prompt")
        finally:
            self.worker._independent_ai_pass = False

        self.assertEqual(status, 0)
        self.assertEqual(len(seen), 1)
        self.assertFalse(
            seen[0][0],
            "an independent AI pass resumed the implementer's native session")
        self.assertNotEqual(seen[0][1], implementer)
        stored = self.worker.read_state().get("cli_sessions") or {}
        self.assertEqual(stored.get("primary", {}).get("id"), implementer)


class CompactionAndResumeClassificationTests(unittest.TestCase):
    def test_successful_compaction_diagnostics_are_not_resume_failures(self) -> None:
        successes = (
            json.dumps({"type": "system", "subtype": "compact_boundary"}),
            json.dumps({
                "type": "system", "subtype": "compact_boundary",
                "message": "context window was full; compacted successfully",
            }),
            "Compacted conversation to 12% of the context window",
            "Successfully compacted the conversation after the context window was full.",
        )
        for text in successes:
            with self.subTest(text=text[:60]):
                self.assertFalse(
                    resume_failure(text),
                    "successful native compaction must not trigger a session reset")

    def test_exhausted_context_and_missing_sessions_still_recover(self) -> None:
        self.assertTrue(resume_failure("prompt is too long"))
        self.assertTrue(resume_failure(json.dumps({
            "type": "error", "error": {"code": "context_length_exceeded"},
        })))


class CliArgvAndConfirmationTests(ContextFixture):
    def test_claude_argv_uses_an_explicit_uuid_and_never_last(self) -> None:
        captured: list[list[str]] = []
        session = self.confirm_session()

        class FakeProcess:
            def __init__(self, command, **kwargs):
                captured.append([str(part) for part in command])
                self.stdin = mock.Mock()
                sid = session
                if "--session-id" in command:
                    sid = command[command.index("--session-id") + 1]
                elif "--resume" in command:
                    sid = command[command.index("--resume") + 1]
                self.stdout = io.StringIO(json.dumps({
                    "type": "result", "session_id": sid,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }) + "\n")
                self.returncode = 0

            def wait(self):
                return 0

        real_run = self.worker._run_claude

        def wrapped(prompt, env, activity="working"):
            with mock.patch("subprocess.Popen", FakeProcess):
                return real_run(prompt, env, activity)

        with mock.patch.object(self.worker, "provider_bin", return_value="/usr/bin/claude"), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch.object(self.worker, "_run_claude", side_effect=wrapped):
            self.assertEqual(self.worker.run_ai("continue the work"), 0)

        self.assertTrue(captured)
        argv = captured[0]
        joined = " ".join(argv)
        self.assertNotIn("--last", argv)
        self.assertNotIn(" --last", joined)
        self.assertIn("--resume", argv)
        identifier = argv[argv.index("--resume") + 1]
        self.assertTrue(valid_session_id(identifier), identifier)
        self.assertEqual(identifier, session)

    def test_stored_last_flag_is_never_passed_to_the_cli(self) -> None:
        self.confirm_session()
        state = self.worker.read_state()
        state["cli_sessions"]["primary"]["id"] = "--last"
        self.worker.write_state(state)
        self.worker.choice.resume = True
        self.worker.choice.session_id = "--last"
        self.worker.prepare_cli_session()
        self.assertFalse(self.worker.choice.resume)
        self.assertTrue(valid_session_id(self.worker.choice.session_id))
        self.assertNotEqual(self.worker.choice.session_id, "--last")

    def test_codex_success_without_thread_started_is_not_reused(self) -> None:
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-terra", "medium", "")
        self.worker.save_new_state(
            self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))

        def runner(prompt, env, activity="working"):
            self.worker._last_ai_raw_output = json.dumps({
                "type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 1},
            })
            return 0

        with mock.patch.object(self.worker, "_run_codex", side_effect=runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}):
            self.assertEqual(self.worker.run_ai("implement the spec"), 0)

        stored = (self.worker.read_state().get("cli_sessions") or {}).get("primary") or {}
        self.assertFalse(stored.get("started"))
        self.assertFalse(self.next_turn())


class FixerContextTests(ContextFixture):
    def _stage_and_loop(self):
        self.worker.update_state(adversarial={
            "active": True, "phase": "fix", "epoch": 1, "round": 1,
            "results": [{"id": "adversarial-issue381-session-lifecycle", "exit_code": 1}],
            "rounds": [{"round": 0}],
        })
        (self.repo / "source.py").write_text("value = 2  # current patch\n", encoding="utf-8")
        stage = next(item for item in ADVERSARIAL_STAGES if item.key == "adversarial")
        return stage, self.worker.read_state()["adversarial"]

    def test_resumed_fixer_omits_stale_spec_but_gets_current_patch_and_failures(self) -> None:
        stage, _loop = self._stage_and_loop()
        self.confirm_session()
        self.assertTrue(self.next_turn())
        prompt = self.worker.adversarial_prompt(stage, self.worker.read_state()["adversarial"])
        self.assertIn("unchanged specification is retained", prompt)
        self.assertNotIn(SPEC_MARKER, prompt)
        self.assertIn("current patch", prompt)
        self.assertIn("adversarial-issue381-session-lifecycle", prompt)

    def test_recovered_fixer_receives_the_full_current_specification(self) -> None:
        stage, _loop = self._stage_and_loop()
        self.git("commit", "--allow-empty", "-qm", "later")
        self.confirm_session()
        self.git("reset", "--hard", "HEAD~1")
        (self.repo / "source.py").write_text("value = 3  # recovered patch\n", encoding="utf-8")
        rebuilt = self.next_turn()
        self.assertFalse(rebuilt)
        prompt = self.worker.adversarial_prompt(stage, self.worker.read_state()["adversarial"])
        self.assertIn(SPEC_MARKER, prompt)
        self.assertNotIn("unchanged specification is retained", prompt)
        self.assertIn("recovered patch", prompt)


class MalformedRoleAndMetadataTests(ContextFixture):
    def test_boolean_epoch_and_round_fail_closed(self) -> None:
        self.confirm_session()
        for payload in (
            {"active": True, "phase": "fix", "epoch": True, "round": 1},
            {"active": True, "phase": "test", "epoch": 1, "round": False},
            {"active": True, "phase": "fix", "epoch": 0, "round": 1},
            {"active": True, "phase": "fix", "epoch": float("inf"), "round": 1},
        ):
            with self.subTest(payload=payload):
                self.worker.update_state(adversarial=payload)
                self.worker.choice.resume = True
                try:
                    rebuilt = self.worker.prepare_cli_session()
                except (TypeError, ValueError, OverflowError) as error:
                    self.fail(f"malformed role state crashed session selection: {error}")
                self.assertTrue(rebuilt)
                self.assertFalse(self.worker.choice.resume)

    def test_session_records_are_identities_not_source_snapshots(self) -> None:
        self.confirm_session()
        entry = self.worker.read_state()["cli_sessions"]["primary"]
        extra = set(entry) - SESSION_IDENTITY_KEYS
        self.assertEqual(extra, set(), f"unexpected session fields: {extra}")
        blob = json.dumps(entry)
        self.assertNotIn("value = 1", blob)
        self.assertNotIn(SPEC_MARKER, blob)
        self.assertNotIn("Required behaviour", blob)
        totals = entry.get("usage_totals") or {}
        self.assertTrue(all(key.endswith("tokens") for key in totals), totals)

    def test_quota_pause_keeps_session_metadata(self) -> None:
        old = self.confirm_session()
        self.worker.mark_quota_paused()
        state = self.worker.read_state()
        self.assertEqual(state["cli_sessions"]["primary"]["id"], old)
        self.assertTrue(state["cli_sessions"]["primary"]["started"])


class NoProductToggleTests(unittest.TestCase):
    def test_no_prompt_cache_toggle_in_settings_surfaces(self) -> None:
        pattern = re.compile(
            r"(?is)(enable|disable|toggle)\W{0,40}(prompt[ -]?cach|session[ -]?reuse|"
            r"native[ -]?cach)|cache[ -]?optimization[ -]?toggle"
        )
        hits = []
        for relative in (
            "ui/index.html", "ui/app.js", "ui/usage-cost.js",
            "src/config.rs", "src/main.rs",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            if pattern.search(text):
                hits.append(relative)
        self.assertEqual(hits, [], f"issue forbids a cache toggle; found one in {hits}")


if __name__ == "__main__":
    unittest.main()
