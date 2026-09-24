"""Issue #267: the deletion safeguard must actually stay recognized once it
is reconciled on every GitHub run, not just at first creation.

Before #267, `protect_new_integration_branch` only ran when the worker was
about to create a brand-new integration branch, so its "does this existing
same-named ruleset actually protect deletion?" check almost never mattered in
practice -- a pre-existing same-named ruleset at creation time was a rare
edge case. #267's fix (`synchronize_integration_branch`,
issue_worker/swarm_issue_worker.py) now calls `protect_new_integration_branch`
on *every* GitHub run, including every run after the very first one that
installed the ruleset successfully. That means the "existing ruleset" branch
of `protect_new_integration_branch` is now exercised on essentially every
single work-round forever, for any repository whose integration branch
already exists remotely -- which, per the issue's own reproduction, includes
this very repository.

That branch reads:

    existing = next(
        (item for item in rulesets if isinstance(item, dict) and item.get("name") == name), None
    )
    if existing is not None:
        protected_refs = existing.get("conditions", {}).get("ref_name", {}).get("include", [])
        has_deletion_rule = any(
            isinstance(rule, dict) and rule.get("type") == "deletion"
            for rule in existing.get("rules", [])
        )
        if (... and protected_refs ... and has_deletion_rule):
            return
        raise WorkerError(...)

`rulesets` comes from `self.github.api_list(f"repos/{repo}/rulesets")`, i.e.
GitHub's *List repository rulesets* endpoint. That endpoint's response
objects are the abbreviated ruleset summary shape (id, name, target,
source_type, source, enforcement, node_id, _links) -- `conditions` and
`rules` are only returned by the *Get a repository ruleset* endpoint
(`GET /repos/{owner}/{repo}/rulesets/{ruleset_id}`), which this code never
calls. So for a real GitHub response, `existing.get("conditions", {})` and
`existing.get("rules", [])` are always absent, `protected_refs` is always
`[]`, `has_deletion_rule` is always `False`, and the `if` above always fails
-- even for the exact ruleset this same code installed moments earlier.

The practical consequence, if the implementation stopped at the list
endpoint alone: on the very first run after this fix ships, the worker
installs the safeguard correctly (creation path, `existing is None`). On
every run after that, it would re-list rulesets, find its own ruleset by
name, misread the summary as "does not protect deletion", and raise
`WorkerError`, which per issue-branch-delivery.md aborts delivery before any
push.

Revision (adjudicated dispute): the first draft of this test asserted that a
list-summary match alone -- `id`/`name`/`target`/`enforcement` only, no
`conditions`/`rules` -- must be accepted as proof of protection, using a
fixture that left the worker's `gh` binary as the default `/usr/bin/false`
stub with no mock for a detail call. That is not a fixture bug to paper
over: issue #261 exists specifically because a same-named ruleset was once
trusted without checking whether it actually blocks deletion, and
`test_integration_branch_ruleset_summary_trust_bypass.py` (also #267)
requires that any summary-shaped match be positively verified against the
single-ruleset detail endpoint (`GET .../rulesets/{id}`) before it is
trusted. A test that expects the summary shape to be accepted on its own
would demand reopening that exact bypass. The correct fix for the shape
problem this test identified is what the implementation actually does:
fetch the detail record by `id` and evaluate `conditions`/`rules` from
*that* response. This test now verifies that outcome -- a genuinely
installed ruleset, encountered through the list endpoint's summary shape,
is not misread as broken once its detail record confirms real protection --
without granting the list summary blind trust.
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

# The exact shape GitHub's "List repository rulesets" endpoint returns for
# each item: no "conditions", no "rules" -- those only appear on the
# single-ruleset "Get a repository ruleset" response.
REAL_LIST_RULESETS_SUMMARY = {
    "id": 4242,
    "name": "SWARM safeguard: prevent deletion of ai-main",
    "target": "branch",
    "source_type": "Repository",
    "source": "DotNetRockStar/swarm",
    "enforcement": "active",
    "node_id": "RS_kgD_example",
    "_links": {
        "self": {"href": "https://api.github.com/repos/DotNetRockStar/swarm/rulesets/4242"},
        "html": {"href": "https://github.com/DotNetRockStar/swarm/rules/4242"},
    },
}

# What that same ruleset's own detail endpoint (GET .../rulesets/4242)
# actually reveals: it really does target ai-main for deletion. Only this
# response -- not the list summary -- can prove that.
REAL_DETAIL_CONFIRMING_PROTECTION = {
    **REAL_LIST_RULESETS_SUMMARY,
    "conditions": {"ref_name": {"include": ["refs/heads/ai-main"], "exclude": []}},
    "rules": [{"type": "deletion"}],
}


class IntegrationBranchRulesetListResponseShapeTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv
    remote_heads = staticmethod(fixtures.WorkerTestCase.remote_heads)
    remote_branches = fixtures.WorkerTestCase.remote_branches

    def test_correctly_installed_ruleset_is_not_reported_as_unprotected(self) -> None:
        """A ruleset that genuinely protects the branch from deletion --
        active, right name, right branch, has a deletion rule -- must not be
        flagged as broken merely because the list endpoint's response omits
        the detail fields needed to prove that. The implementation must
        establish the real state by fetching the single-ruleset detail
        endpoint by id (not by trusting the list summary alone), and the
        end result must be that a genuinely safe branch is treated as
        safe."""
        with (
            mock.patch.object(
                self.worker.github, "api_list", return_value=[REAL_LIST_RULESETS_SUMMARY]
            ),
            mock.patch.object(
                self.worker.github,
                "gh",
                return_value=json.dumps(REAL_DETAIL_CONFIRMING_PROTECTION),
            ) as github_gh,
        ):
            # Must not raise: the branch really is protected, once verified.
            self.worker.protect_new_integration_branch("ai-main")

        # The list summary alone must not have been enough -- a real detail
        # fetch is required to reach that conclusion.
        github_gh.assert_called_once()

    def test_repo_with_preexisting_integration_branch_keeps_working_after_first_run(self) -> None:
        """End-to-end version of the same defect: on a repository where
        ai-main already exists (this repository's own situation, and the
        exact scenario issue #267 is about), the safeguard is reconciled on
        every run. A second, later run must still be able to push -- it must
        not be permanently blocked just because the ruleset it is looking at
        now comes back in the summary shape instead of the full-detail shape
        it might have seen when the ruleset was first created -- provided
        the detail endpoint, once consulted, confirms real protection."""
        with (
            mock.patch.object(self.worker, "remote_is_github_host", return_value=True),
            mock.patch.object(
                self.worker.github, "api_list", return_value=[REAL_LIST_RULESETS_SUMMARY]
            ),
            mock.patch.object(
                self.worker.github,
                "gh",
                return_value=json.dumps(REAL_DETAIL_CONFIRMING_PROTECTION),
            ),
        ):
            try:
                self.worker.synchronize_integration_branch()
            except WorkerError as error:
                self.fail(
                    "synchronize_integration_branch raised WorkerError for an "
                    f"already-protected integration branch: {error}"
                )

        self.assertIn("ai-main", self.remote_branches())


if __name__ == "__main__":
    unittest.main()
