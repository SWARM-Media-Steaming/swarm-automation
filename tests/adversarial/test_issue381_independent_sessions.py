"""Issue #381 acceptance: a non-implementation AI pass never borrows the implementer's session.

Oracle (derived from the issue and the repository rules before reading the diff):

* "Track sessions by issue, provider, model, agent role and execution context"
  and "never reuse sessions" where reuse "would negatively affect execution
  quality": session continuity belongs to the worker that is *continuing its own
  work*. Anything else that happens to call ``Worker.run_ai`` is a different
  agent role and must not resume, extend or overwrite the implementer's session.
* ``.claude/rules/architecture-docs.md`` and ``ArchitectureDocsMixin`` define the
  post-delivery documentation review as "one fresh, tool-free AI session". Its
  prompt asks for a structured review, not a continuation of the implementation
  conversation, and any edit it makes is discarded.
* The usage telemetry of that pass must not claim the implementer's session
  identifier or report a reuse that never happened as the implementer's.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ISSUE_WORKER_DIR = Path(__file__).resolve().parents[2] / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

from swarm_issue_worker import Config, IssueContext, ProviderChoice, Worker, build_parser  # noqa: E402

REVIEW_REPLY = json.dumps({"impact": "none", "reason": "no change", "confidence": 0.9, "operations": []})
REVIEW_PROMPT = "Documentation impact review: list affected architecture entities."


class DocumentationReviewSessionTests(unittest.TestCase):
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
        self.worker.issue = IssueContext(381, "Caching", "Required behaviour", [], "https://example.invalid/381")
        self.worker.choice = ProviderChoice(self.provider, self.model, "high", str(uuid.uuid4()))
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))
        self.seen: list[dict] = []

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def implementer_finishes_a_turn(self) -> str:
        """The normal implementation turn: its session is confirmed and remembered."""
        self.worker.prepare_cli_session()
        self.worker.update_state(session_started=True)
        self.worker.remember_cli_session(True)
        return self.worker.choice.session_id

    def run_documentation_pass(self) -> None:
        def runner(prompt, env, activity="working"):
            self.seen.append({"resume": self.worker.choice.resume,
                              "session": self.worker.choice.session_id, "prompt": prompt})
            self.worker.ai_output_file.write_text(REVIEW_REPLY, encoding="utf-8")
            self.worker.update_state(session_started=True)
            self.worker._last_ai_raw_output = json.dumps({"type": "result", "usage": {
                "input_tokens": 5, "output_tokens": 5, "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0}})
            return 0

        with mock.patch.object(self.worker, "_run_claude", side_effect=runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}):
            self.worker._run_documentation_pass(REVIEW_PROMPT)

    def test_review_starts_a_fresh_session_even_when_the_implementers_is_compatible(self) -> None:
        implementer = self.implementer_finishes_a_turn()
        self.run_documentation_pass()
        self.assertEqual(len(self.seen), 1)
        self.assertFalse(
            self.seen[0]["resume"],
            "the documentation review is one FRESH session; it resumed the implementer's conversation")
        self.assertNotEqual(self.seen[0]["session"], implementer)
        self.assertEqual(self.seen[0]["prompt"], REVIEW_PROMPT,
                         "a fresh review gets exactly its own prompt, not a continuation")

    def test_review_does_not_touch_the_implementers_session_record(self) -> None:
        implementer = self.implementer_finishes_a_turn()
        before = json.loads(json.dumps(self.worker.read_state()["cli_sessions"]))
        self.run_documentation_pass()
        after = self.worker.read_state()["cli_sessions"]
        self.assertEqual(after.get("primary", {}).get("id"), implementer,
                         "the implementer's remembered session must still be the implementer's")
        self.assertEqual(set(after), set(before), "the review must not add a competing session role")
        identity = ("id", "provider", "model", "effort", "context", "started", "head")
        self.assertEqual({k: after["primary"].get(k) for k in identity},
                         {k: before["primary"].get(k) for k in identity})

    def test_review_never_becomes_the_session_a_later_implementer_turn_resumes(self) -> None:
        # No implementer session was remembered (for instance a follow-up round
        # starting from reset state). The review must not manufacture one.
        self.run_documentation_pass()
        review_session = self.seen[0]["session"]
        fresh = ProviderChoice(self.provider, self.model, "high", str(uuid.uuid4()))
        fresh.resume = True
        self.worker.choice = fresh
        self.worker.prepare_cli_session()
        self.assertNotEqual(self.worker.choice.session_id, review_session,
                            "the next implementation turn resumed the documentation review conversation")
        self.assertFalse(self.worker.choice.resume)

    def test_review_telemetry_does_not_claim_the_implementers_session(self) -> None:
        implementer = self.implementer_finishes_a_turn()
        self.run_documentation_pass()
        events = self.worker.read_state().get("token_usage_events", [])
        self.assertTrue(events, "the review's usage must still be recorded")
        review = events[-1]
        self.assertNotEqual(review.get("agent_run_id"), implementer,
                            "the review's usage row names the implementer's session")
        self.assertFalse(review.get("session_reused"),
                         "a fresh review session is reported as reused")

    def test_implementer_continuity_survives_the_review(self) -> None:
        implementer = self.implementer_finishes_a_turn()
        self.run_documentation_pass()
        self.worker.choice = ProviderChoice(self.provider, self.model, "high", implementer)
        self.worker.choice.resume = True
        self.worker.prepare_cli_session()
        self.assertTrue(self.worker.choice.resume, "a compatible implementer session is still reusable")
        self.assertEqual(self.worker.choice.session_id, implementer)


if __name__ == "__main__":
    unittest.main()
