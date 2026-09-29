"""Issue #314 acceptance test: historical rounds must show real fixer effort,
not silently blank it out, whenever the underlying value is actually known.

Round zero is the independent assessment of the original implementation, not
a counted fix/re-test round. `AdversarialStageMixin.initialize_stage`
(issue_worker/adversarial_core.py) nonetheless already reuses the
`fixer_provider`/`fixer_model` columns for round zero to name the
*implementer* (the provider/model that wrote the code under review) — see
`"fixer_provider": self.choice.name, "fixer_model": self.choice.model,` there.
The Overview and history views (ui/adversarial-uat.js, ui/adversarial-
security.js `agent()`) faithfully render whatever is persisted, so a real,
just-recorded provider and model show up correctly for round zero today.

`AdversarialStageMixin.round_progress` computes the matching `fixer_effort`
for that same round-zero record as:

    "fixer_effort": str(loop.get("fixer_effort") or "") if round_number else ""

For round_number == 0 this is unconditionally "" — not because the
implementer's effort is unknown (it is the exact same `self.choice.effort`
already used to populate `fixer_model`/`fixer_provider` a few lines above in
`initialize_stage`), but because `initialize_stage` never threads it through
`loop["fixer_effort"]`, and `round_progress` special-cases round 0 to ""
regardless.

The issue's acceptance criteria say: "Historical rounds show fixer and
tester provider/model/effort, with `Not recorded` for legacy missing
effort." The qualifier is "legacy" — rows written before this feature
existed, where the value genuinely was never captured. A round-zero record
created today, by a build that has this feature, is not legacy: its
implementer effort is trivially available (it is `self.choice.effort` at
`initialize_stage` time) and must be persisted and shown like its provider
and model already are, not manufactured as `Not recorded` by construction.
This test drives one real clean-first-pass round zero through the actual
history-recording path (no internals mocked besides providers/GitHub/AI) and
pins that its `fixer_effort` equals the implementer's real configured effort,
matching its `fixer_provider`/`fixer_model` siblings.
"""

from __future__ import annotations

import contextlib
import dataclasses
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

import adversarial_uat as uat  # noqa: E402
import test_swarm_issue_worker as fixtures  # noqa: E402
from ai_execution_history import ExecutionHistoryService  # noqa: E402
from swarm_issue_worker import IssueContext, ProviderChoice, ProviderUsage  # noqa: E402


class RoundZeroFixerEffortTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def prepare(self) -> None:
        self.worker.config = dataclasses.replace(
            self.worker.config,
            adversarial_uat_enabled=True,
            ai_execution_history_enabled=True,
            execution_history_db=self.state / "history.sqlite3",
        )
        self.worker.history = ExecutionHistoryService(True, self.state / "history.sqlite3")
        self.worker.issue = IssueContext(
            314, "Show adversarial agent, model, and effort",
            "Round zero must record the implementer's real effort, not blank it.",
            [], "https://example.invalid/issues/314",
        )
        # The implementer's real, non-empty configured effort. This is the
        # exact object `initialize_stage` reads `.name`/`.model`/`.effort`
        # from — round zero's provider/model come from here, so its effort
        # must be able to, too.
        self.worker.choice = ProviderChoice("Claude", "implementer-model", "high", "implementer-session")
        self.git("switch", "-c", "ai/claude/issue-314")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()
        (self.repo / "tracked.txt").write_text("fixed\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "[claude] Implementation (#314)")
        self.worker.initialize_adversarial(self.git("rev-parse", "HEAD"), "## Summary\nImplementation")

    def add_passing_suite(self) -> None:
        # The tester must leave at least one adversarial test file behind;
        # this one always passes, so round zero stays a clean first pass.
        directory = self.repo / "tests/adversarial"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "test_issue314_fixture.py").write_text(
            "import unittest\nfrom pathlib import Path\n\n"
            "class Requirement(unittest.TestCase):\n"
            "    def test_tracked_file_is_fixed(self):\n"
            "        self.assertEqual(Path('tracked.txt').read_text().strip(), 'fixed')\n",
            encoding="utf-8",
        )
        definition = uat.read_definition(self.repo)
        definition["suites"].append({
            "id": "adversarial-314-fixture", "name": "Issue #314 fixture acceptance",
            "origin": "adversarial", "enabled": True, "disruptive": False,
            "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests/adversarial",
                        "-p", "test_issue314_fixture.py"],
            "timeoutSeconds": 20,
        })
        (self.repo / uat.DEFINITION).write_text(json.dumps(definition), encoding="utf-8")

    def role(self, prompt: str, activity: str = "") -> int:
        # Round zero's independent assessment finds nothing to fix: the one
        # registered suite already passes, so the review is clean on the
        # first pass and the pipeline never enters a counted fix/re-test
        # round.
        self.add_passing_suite()
        self.worker.ai_output_file.write_text(
            uat.RESULT_MARKER + ' {"dispute_resolution": "", "out_of_scope": []}',
            encoding="utf-8",
        )
        return 0

    def gh(self, args, provider=None, body=None):
        if args[:2] in (["pr", "list"], ["issue", "list"]):
            return "[]"
        if args[:2] == ["pr", "create"]:
            return "https://example.invalid/pull/314"
        return ""

    def test_round_zero_persists_the_implementers_real_effort_not_blank(self) -> None:
        self.prepare()
        with (
            mock.patch.object(self.worker, "provider_usage", return_value=ProviderUsage(0, 80)),
            mock.patch.object(self.worker, "ensure_bot_auth"),
            mock.patch.object(self.worker, "comments", return_value=[]),
            mock.patch.object(self.worker, "run_ai", side_effect=self.role),
            mock.patch.object(self.worker.github, "gh", side_effect=self.gh),
            mock.patch.object(self.worker, "minor_bump_requested_by_trusted_user", return_value=False),
            # Delivery mechanics (PR, comments, labels) are not this test's
            # concern; only the persisted round-zero record is.
            mock.patch.object(self.worker, "finalize_issue"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            status = self.worker.run_adversarial_delivery()

        self.assertEqual(status, 10, "expected a clean first pass to complete adversarial delivery")

        execution_id = self.worker.history.execution_id
        self.assertTrue(execution_id, "execution history must have recorded this run")
        rounds = self.worker.history.repository.adversarial_rounds_for([execution_id])[execution_id]
        round_zero = next(r for r in rounds if r["round_number"] == 0)

        # Sanity: round zero's provider/model already correctly name the
        # implementer today — this pins that baseline stays true.
        self.assertEqual(round_zero["fixer_provider"], "Claude")
        self.assertEqual(round_zero["fixer_model"], "implementer-model")

        # The bug: the same choice's effort ("high") is available at the
        # exact point fixer_provider/fixer_model are captured
        # (issue_worker/adversarial_core.py AdversarialStageMixin.
        # initialize_stage), yet round_progress unconditionally blanks
        # fixer_effort for round_number == 0. A brand-new round-zero record
        # is not a legacy row with genuinely missing data; per the issue's
        # acceptance criteria, "Not recorded" is reserved for legacy rows,
        # not manufactured for current ones that have the value on hand.
        self.assertEqual(
            round_zero["fixer_effort"], "high",
            "round zero's fixer_effort must be the implementer's real configured "
            "effort (matching fixer_provider/fixer_model), not blanked to '' "
            "and rendered as 'Not recorded' when the value was available",
        )


if __name__ == "__main__":
    unittest.main()
