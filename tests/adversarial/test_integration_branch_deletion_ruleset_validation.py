"""Issue #261: prevent the ai-main integration branch from being deleted by
accident, by installing a deliberate, named GitHub deletion ruleset before
the worker's first push of a newly created integration branch.

`Worker.protect_new_integration_branch`
(issue_worker/swarm_issue_worker.py) is only correct if it fails closed the
moment a same-named ruleset already exists on GitHub but does not actually
protect the branch from deletion. That situation is realistic, not
hypothetical: a ruleset named "SWARM safeguard: prevent deletion of
ai-main" could already exist with `enforcement: "evaluate"` (GitHub's
dry-run mode, which reports violations but never blocks them) left over
from a manual test, or with no `deletion` rule at all because someone
edited it by hand. The existing test suite
(`issue_worker/test_swarm_issue_worker.py`) only covers the case where no
ruleset exists yet, and the case where an existing ruleset is already
fully correct (`test_existing_deletion_safeguard_is_not_recreated`) — it
never exercises the "found by name, but does not actually protect
deletion" branch of `protect_new_integration_branch`, even though that
function contains a dedicated code path for exactly this:

    if (
        existing.get("target") == "branch"
        and existing.get("enforcement") == "active"
        and f"refs/heads/{branch}" in protected_refs
        and has_deletion_rule
    ):
        ...
        return
    raise WorkerError(...)

If that `raise` were ever weakened to a silent `return` (e.g. "a ruleset
with this name exists, good enough"), the worker would push a brand new
integration branch to GitHub while believing it is protected, when in
fact nothing stops `git push origin :ai-main`. That is precisely the
silent-failure shape issue #261 exists to prevent — a deletion safeguard
that looks installed but is not — so it must be pinned down with an
executable test, not left to be caught only by code review.
"""

from __future__ import annotations

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
from swarm_issue_worker import WorkerError  # noqa: E402


class IntegrationBranchDeletionRulesetValidationTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv
    remote_heads = staticmethod(fixtures.WorkerTestCase.remote_heads)

    def _recreate_ai_main_locally_and_remotely_removed(self) -> None:
        """Put the checkout back in the state ``synchronize_integration_branch``
        sees right before it would create a brand-new integration branch:
        no local ``ai-main``, no remote ``ai-main``."""
        self.git("switch", "-q", "main")
        self.git("branch", "-D", "ai-main")
        self.git("push", "-q", "origin", ":ai-main")
        self.git("fetch", "-q", "--prune", "origin")

    def test_ruleset_with_matching_name_but_evaluate_enforcement_blocks_first_push(self) -> None:
        """A ruleset can exist under the exact safeguard name and cover the
        exact ref and rule type, yet still not protect anything, because
        GitHub's ``evaluate`` enforcement mode only reports what a rule
        *would* have blocked — it never actually blocks it. Treating that
        as "already protected" would let the branch go live with no real
        safeguard in place."""
        self._recreate_ai_main_locally_and_remotely_removed()
        non_blocking_ruleset = {
            "name": "SWARM safeguard: prevent deletion of ai-main",
            "target": "branch",
            "enforcement": "evaluate",
            "conditions": {"ref_name": {"include": ["refs/heads/ai-main"], "exclude": []}},
            "rules": [{"type": "deletion"}],
        }
        with (
            mock.patch.object(self.worker, "remote_is_github_host", return_value=True),
            mock.patch.object(
                self.worker.github, "gh", return_value=json.dumps([non_blocking_ruleset])
            ) as github,
        ):
            with self.assertRaisesRegex(WorkerError, "does not protect"):
                self.worker.synchronize_integration_branch()

        # Only the read of the existing rulesets happened; the worker must
        # not have gone on to push the unprotected branch to GitHub.
        github.assert_called_once()
        self.assertEqual(self.remote_heads(self.remote), ["main"])

    def test_ruleset_with_matching_name_but_no_deletion_rule_blocks_first_push(self) -> None:
        """Same trap, different shape: a ruleset that is active, targets the
        right branch, but was edited (by hand, or by a future refactor of
        this function) to drop its ``deletion`` rule -- e.g. only a
        ``pull_request`` rule remains. Name-matching alone must not be
        mistaken for "this branch is safe from deletion"."""
        self._recreate_ai_main_locally_and_remotely_removed()
        ruleset_missing_deletion_rule = {
            "name": "SWARM safeguard: prevent deletion of ai-main",
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/heads/ai-main"], "exclude": []}},
            "rules": [{"type": "pull_request"}],
        }
        with (
            mock.patch.object(self.worker, "remote_is_github_host", return_value=True),
            mock.patch.object(
                self.worker.github, "gh",
                return_value=json.dumps([ruleset_missing_deletion_rule]),
            ) as github,
        ):
            with self.assertRaisesRegex(WorkerError, "does not protect"):
                self.worker.synchronize_integration_branch()

        github.assert_called_once()
        self.assertEqual(self.remote_heads(self.remote), ["main"])

    def test_ruleset_with_matching_name_but_wrong_ref_blocks_first_push(self) -> None:
        """A same-named ruleset that is active and has a deletion rule, but
        whose ``ref_name.include`` protects a different branch (e.g. left
        over from a previous integration-branch name, or a typo), must not
        be treated as covering the branch actually being created."""
        self._recreate_ai_main_locally_and_remotely_removed()
        ruleset_for_wrong_branch = {
            "name": "SWARM safeguard: prevent deletion of ai-main",
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
            "rules": [{"type": "deletion"}],
        }
        with (
            mock.patch.object(self.worker, "remote_is_github_host", return_value=True),
            mock.patch.object(
                self.worker.github, "gh",
                return_value=json.dumps([ruleset_for_wrong_branch]),
            ) as github,
        ):
            with self.assertRaisesRegex(WorkerError, "does not protect"):
                self.worker.synchronize_integration_branch()

        github.assert_called_once()
        self.assertEqual(self.remote_heads(self.remote), ["main"])


if __name__ == "__main__":
    unittest.main()
