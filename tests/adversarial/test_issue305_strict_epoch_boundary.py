"""Issue #305 acceptance: the real default strict-epoch-per-run boundary.

Independent oracle, derived from the issue and `.claude/rules/adversarial-uat-
testing.md` before reading the diff:

  * With the "Allow best-effort adversarial merge after 3 rounds" toggle left
    off (the required default, never inferred), a persistently failing stage
    must run three full three-round epochs, escalating reasoning effort and
    provider/model choice at each boundary, and then checkpoint (never merge,
    never ask a human) rather than silently starting a fourth epoch in the
    same process.
  * "Escalate ... to the strongest appropriate model/provider after repeated
    no-progress results" must actually happen after two consecutive stalled
    epochs, not just be representable in a hand-built fixture.
  * A later resumption of that same checkpoint must be able to continue
    counting epochs and still deliver a verified-clean result once real
    progress happens — strict mode is not a one-way trip to `cap_hit`.

`issue_worker/test_adversarial_uat.py::test_strict_epoch_exhaustion_starts_
another_epoch_instead_of_merging` (the implementer's own regression test)
patches `adversarial_core.STRICT_EPOCHS_PER_RUN` down to 1 to keep the test
fast. That hides two things no test anywhere else in the tree checks: whether
the real default of 3 stops at exactly three full epochs (not two, not four),
and the `STALLED_EPOCHS_BEFORE_STRONGEST = 2` "strongest model" escalation
path, which needs at least two real stalled-epoch closures to ever fire. This
suite intentionally leaves `STRICT_EPOCHS_PER_RUN` untouched and drives the
real durable pipeline (real git, real suite subprocesses; only the coding
provider and GitHub's API are faked) far enough to exercise both.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import adversarial_core  # noqa: E402
import adversarial_uat as uat  # noqa: E402
import test_swarm_issue_worker as fixtures  # noqa: E402
from ai_execution_history import ExecutionHistoryService  # noqa: E402
from swarm_issue_worker import (  # noqa: E402
    ADVERSARIAL_EPOCH_YIELD_EXIT_CODE,
    ISSUE_COMPLETED_EXIT_CODE,
    IssueContext,
    ProviderChoice,
    ProviderUsage,
)


class StrictEpochBoundaryTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def prepare(self) -> None:
        self.worker.config = dataclasses.replace(
            self.worker.config,
            adversarial_uat_enabled=True,
            auto_approve=True,
            auto_promote=True,
            ai_execution_history_enabled=True,
            execution_history_db=self.state / "history.sqlite3",
        )
        self.worker.history = ExecutionHistoryService(True, self.state / "history.sqlite3")
        self.worker.issue = IssueContext(
            305, "Replace adversarial cap with escalation epochs",
            "A persistently failing acceptance test must run three full strict "
            "epochs, escalating each time, before checkpointing.",
            [], "https://example.invalid/issues/305",
        )
        self.worker.choice = ProviderChoice("Claude", "fixer-model", "medium", "implementer-session")
        self.git("switch", "-c", "ai/claude/issue-305")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_execution_history()
        (self.repo / "tracked.txt").write_text("broken\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "[claude] Implementation (#305)")
        self.worker.initialize_adversarial(self.git("rev-parse", "HEAD"), "## Summary\nImplementation")
        self.calls: list[tuple] = []
        self.comments: list[str] = []
        self.fixed = False

    def add_failing_suite(self) -> None:
        tests = self.repo / "tests/adversarial"
        tests.mkdir(parents=True, exist_ok=True)
        test_file = tests / "test_issue305_fixture.py"
        test_file.write_text(
            "import unittest\nfrom pathlib import Path\n\n"
            "class Requirement(unittest.TestCase):\n"
            "    def test_implementation_is_repaired(self):\n"
            "        self.assertEqual(Path('tracked.txt').read_text().strip(), 'fixed')\n",
            encoding="utf-8",
        )
        definition = uat.read_definition(self.repo)
        definition["suites"] = [s for s in definition["suites"] if s.get("id") != "adversarial-305-cap"]
        definition["suites"].append({
            "id": "adversarial-305-cap", "name": "Issue #305 persistent failure fixture",
            "origin": "adversarial", "enabled": True, "disruptive": False,
            "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests/adversarial",
                       "-p", test_file.name],
            "timeoutSeconds": 20,
        })
        (self.repo / uat.DEFINITION).write_text(json.dumps(definition), encoding="utf-8")

    def role(self, prompt: str, activity: str = "") -> int:
        loop = self.worker.read_state()["adversarial"]
        self.calls.append((loop["phase"], loop["round"], self.worker.choice.name, self.worker.choice.effort))
        if loop["phase"] == "test":
            if not (self.repo / "tests/adversarial/test_issue305_fixture.py").exists():
                self.add_failing_suite()
            output = uat.RESULT_MARKER + ' {"dispute_resolution": "", "out_of_scope": []}'
        else:
            if self.fixed:
                (self.repo / "tracked.txt").write_text("fixed\n", encoding="utf-8")
                output = "Repaired the acceptance failure."
            else:
                output = "Attempted repair did not satisfy the acceptance test."
        self.worker.ai_output_file.write_text(output, encoding="utf-8")
        return 0

    def gh(self, args, provider=None, body=None):
        if args[:2] in (["pr", "list"], ["issue", "list"]):
            return "[]"
        if args[:2] == ["pr", "create"]:
            return "https://example.invalid/pull/305"
        if args[:2] == ["issue", "create"]:
            return "https://example.invalid/issues/306"
        if args[:2] == ["issue", "comment"]:
            self.comments.append(body)
        return ""

    def patches(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(self.worker, "provider_usage", return_value=ProviderUsage(0, 80)))
        stack.enter_context(mock.patch.object(self.worker, "ensure_bot_auth"))
        stack.enter_context(mock.patch.object(self.worker, "comments", return_value=[]))
        stack.enter_context(mock.patch.object(self.worker, "run_ai", side_effect=self.role))
        stack.enter_context(mock.patch.object(self.worker.github, "gh", side_effect=self.gh))
        stack.enter_context(mock.patch.object(self.worker, "minor_bump_requested_by_trusted_user", return_value=False))
        return stack

    def test_default_strict_budget_runs_exactly_three_full_epochs_then_yields(self):
        self.prepare()
        self.assertFalse(self.worker.config.adversarial_best_effort_merge)
        self.assertEqual(adversarial_core.STRICT_EPOCHS_PER_RUN, 3)
        with self.patches(), mock.patch.object(self.worker, "approve_pull_request") as approve, \
                mock.patch.object(self.worker, "merge_pull_request") as merge, \
                mock.patch.object(self.worker, "auto_promote_integration_branch") as promote:
            status = self.worker.run_adversarial_delivery()
        self.assertEqual(status, ADVERSARIAL_EPOCH_YIELD_EXIT_CODE)
        approve.assert_not_called()
        merge.assert_not_called()
        promote.assert_not_called()
        self.assertEqual(self.comments, [], "a renewing strict epoch must never post a completion comment")

        test_rounds = [round_number for phase, round_number, *_ in self.calls if phase == "test"]
        fix_rounds = [round_number for phase, round_number, *_ in self.calls if phase == "fix"]
        self.assertEqual(test_rounds, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
                         "exactly one assessment plus nine re-tests: three full epochs, not two or four")
        self.assertEqual(fix_rounds, list(range(1, 10)))

        loop = self.worker.read_state()["adversarial"]
        self.assertEqual((loop["round"], loop["epoch"], loop["phase"], loop["outcome"]), (10, 4, "fix", ""))
        self.assertEqual(loop["merge_policy"], "strict")
        self.assertEqual([e["epoch_number"] for e in loop["epochs"]], [1, 2, 3])
        self.assertTrue(all(e["outcome"] == "exhausted" for e in loop["epochs"]))
        self.assertTrue(all(e["merge_policy"] == "strict" for e in loop["epochs"]))
        self.assertEqual([e["stalled_epochs"] for e in loop["epochs"]], [1, 2, 3])

        # STALLED_EPOCHS_BEFORE_STRONGEST = 2: only after epoch 2 closes with
        # no progress (its own closure being the second consecutive stall)
        # must the *next* epoch's escalation move to the strongest model. No
        # other suite in the tree drives two real stalled-epoch closures, so
        # this flag has never actually been produced end-to-end before.
        self.assertFalse(loop["epochs"][0]["next_escalation"]["strongest"])
        self.assertTrue(loop["epochs"][1]["next_escalation"]["strongest"])
        self.assertTrue(loop["escalation"]["strongest"])

        row = self.worker.history.repository.for_repository(self.worker.config.github_repository)[0]
        self.assertEqual(row["final_status"], "adversarial_epoch_continuing")
        self.assertNotIn(row["adversarial_outcome"], ("cap_hit", "clean_first_pass", "resolved_after_n"))
        epochs = self.worker.history.repository.adversarial_epochs_for([row["execution_id"]])[row["execution_id"]]
        self.assertEqual(sorted(e["epoch_number"] for e in epochs), [1, 2, 3])

        efforts = [effort for phase, _round, _name, effort in self.calls if phase == "fix"]
        self.assertGreater(
            adversarial_core.effort_rank(efforts[-1]), adversarial_core.effort_rank(efforts[0]),
            "the round-9 fixer must be escalated above round-1's starting effort after repeated stalls",
        )
        self.assertEqual(efforts[-1], adversarial_core.EFFORT_LADDER[-2],
                         "two stalled epochs plus a strongest-model escalation should reach the second-"
                         "highest rung or above by round 9")

    def test_resuming_the_checkpoint_continues_into_a_fourth_epoch_and_can_still_deliver_clean(self):
        self.prepare()
        with self.patches(), mock.patch.object(self.worker, "approve_pull_request"), \
                mock.patch.object(self.worker, "merge_pull_request") as merge, \
                mock.patch.object(self.worker, "auto_promote_integration_branch"):
            merge.return_value = self.git("rev-parse", "HEAD")
            first = self.worker.run_adversarial_delivery()
            self.assertEqual(first, ADVERSARIAL_EPOCH_YIELD_EXIT_CODE)
            # A scheduler resume is just another call against the same
            # on-disk checkpoint (`run_adversarial_pipeline` always re-reads
            # state); it is not a distinguished code path of its own.
            self.fixed = True
            second = self.worker.run_adversarial_delivery()
        self.assertEqual(second, ISSUE_COMPLETED_EXIT_CODE)
        self.assertEqual(len(self.comments), 1)
        self.assertIn("resolved after 10 rounds across 4 epochs", self.comments[0])
        self.assertNotIn("Best-effort", self.comments[0])
        row = self.worker.history.repository.for_repository(self.worker.config.github_repository)[0]
        self.assertEqual(row["adversarial_outcome"], "resolved_after_n")
        self.assertEqual(row["adversarial_delivery"], "verified_clean")
        self.assertEqual(row["adversarial_epoch_count"], 4)
        epochs = self.worker.history.repository.adversarial_epochs_for([row["execution_id"]])[row["execution_id"]]
        self.assertEqual(sorted(e["epoch_number"] for e in epochs), [1, 2, 3],
                         "epoch 4 succeeded without a fix/re-test deadlock, so it never closes as its own row")


if __name__ == "__main__":
    unittest.main()
