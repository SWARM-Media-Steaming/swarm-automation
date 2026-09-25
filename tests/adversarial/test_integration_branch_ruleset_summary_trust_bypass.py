"""Issue #267: reconciling the deletion safeguard on every GitHub run must
not weaken the original #261 guarantee that a same-named ruleset actually
protects the integration branch from deletion before it is trusted.

#267's fix to `Worker.protect_new_integration_branch`
(issue_worker/swarm_issue_worker.py) added an `is_list_summary` escape
hatch:

    is_list_summary = "conditions" not in existing and "rules" not in existing
    ...
    if (
        existing.get("target") == "branch"
        and existing.get("enforcement") == "active"
        and (
            is_list_summary
            or (f"refs/heads/{branch}" in protected_refs and has_deletion_rule)
        )
    ):
        log(...)
        return

The stated rationale (see the sibling test
`test_integration_branch_ruleset_list_response_shape.py` and its
`REAL_LIST_RULESETS_SUMMARY` fixture) is that GitHub's real *List
repository rulesets* endpoint never returns `conditions`/`rules` at all --
only the single-ruleset *Get a repository ruleset* endpoint
(`GET /repos/{owner}/{repo}/rulesets/{id}`) does. That much is correct. But
the fix responds to "the list endpoint can't tell us" by skipping the
ref-name/deletion-rule check entirely whenever both keys are absent,
instead of fetching the single-ruleset detail endpoint (which the ruleset's
`id` field, present in every real list-endpoint item, makes trivial) to
actually inspect the real `conditions`/`rules` before trusting it.

`issue_worker/swarm_issue_worker.py` defines no such detail-fetch helper at
all (only `GitHubClient.api_list` and `GitHubClient.gh` exist), and
`protect_new_integration_branch` never calls `self.github.gh` a second time
for the "existing ruleset" branch. So for *every* real GitHub response --
which per the sibling test's own fixture always omits `conditions`/`rules`
-- name + `target: branch` + `enforcement: active` is now the *entire*
check. A ruleset with the exact safeguard name that is active, targets
"branch", but in fact protects a completely different branch and carries no
deletion rule at all (e.g. hand-edited, or left over after the integration
branch was renamed) is indistinguishable, from the list endpoint alone,
from a ruleset that genuinely protects `ai-main` from deletion. This
reopens precisely the silent-failure issue #261 exists to prevent -- a
same-named ruleset that looks installed but does not actually block
deletion -- and does so on essentially every work-round for any repository
whose integration branch already exists (this repository's own situation,
per issue #267's reproduction), because the "genuinely correct" and
"genuinely broken" cases now look identical to this code.

A correct fix must positively verify the real conditions/rules -- e.g. by
fetching the ruleset's own detail endpoint using the `id` every real list
item carries -- rather than treating the absence of that information as
license to skip validation.
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

# The exact shape GitHub's real "List repository rulesets" endpoint returns:
# no "conditions", no "rules" -- those only appear on the single-ruleset
# "Get a repository ruleset" response, fetchable by this "id".
SAME_NAME_ACTIVE_BRANCH_SUMMARY = {
    "id": 9001,
    "name": "SWARM safeguard: prevent deletion of ai-main",
    "target": "branch",
    "source_type": "Repository",
    "source": "acme/widgets",
    "enforcement": "active",
    "node_id": "RS_kgD_example",
    "_links": {
        "self": {"href": "https://api.github.com/repos/acme/widgets/rulesets/9001"},
        "html": {"href": "https://github.com/acme/widgets/rules/9001"},
    },
}

# What that same ruleset's own detail endpoint (GET .../rulesets/9001)
# actually reveals: it protects an unrelated branch and has no deletion
# rule at all -- it does not protect ai-main from deletion in any way.
DETAIL_SHOWING_NO_REAL_PROTECTION = {
    **SAME_NAME_ACTIVE_BRANCH_SUMMARY,
    "conditions": {"ref_name": {"include": ["refs/heads/some-other-branch"], "exclude": []}},
    "rules": [{"type": "pull_request"}],
}


class IntegrationBranchRulesetSummaryTrustBypassTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv
    remote_heads = staticmethod(fixtures.WorkerTestCase.remote_heads)
    remote_branches = fixtures.WorkerTestCase.remote_branches

    def test_summary_shaped_ruleset_must_be_verified_before_trust(self) -> None:
        """A same-named, active, branch-target ruleset in list-summary shape
        must be positively verified (e.g. via its own detail endpoint,
        reachable through the ``id`` every real list item carries) before
        being accepted as already protecting the branch from deletion. It
        must not be accepted purely because the list endpoint's response
        happens to omit the fields that would prove -- or disprove -- real
        protection."""
        with (
            mock.patch.object(
                self.worker.github, "api_list", return_value=[SAME_NAME_ACTIVE_BRANCH_SUMMARY]
            ),
            mock.patch.object(
                self.worker.github, "gh", return_value=json.dumps(DETAIL_SHOWING_NO_REAL_PROTECTION)
            ) as github_gh,
        ):
            with self.assertRaisesRegex(WorkerError, "does not protect"):
                self.worker.protect_new_integration_branch("ai-main")

        # A real verification must have actually looked at the ruleset's
        # detail record -- not merely trusted the list summary at face
        # value because it lacked conditions/rules.
        github_gh.assert_called_once()

    def test_repo_with_unverified_summary_ruleset_does_not_push_unprotected(self) -> None:
        """End-to-end: when the only same-named ruleset GitHub reports is a
        list-summary that cannot be proven to actually block deletion, the
        worker must not go on to push the integration branch to GitHub
        believing it is protected."""
        with (
            mock.patch.object(self.worker, "remote_is_github_host", return_value=True),
            mock.patch.object(
                self.worker.github, "api_list", return_value=[SAME_NAME_ACTIVE_BRANCH_SUMMARY]
            ),
            mock.patch.object(
                self.worker.github, "gh", return_value=json.dumps(DETAIL_SHOWING_NO_REAL_PROTECTION)
            ),
        ):
            with self.assertRaises(WorkerError):
                self.worker.synchronize_integration_branch()


if __name__ == "__main__":
    unittest.main()
