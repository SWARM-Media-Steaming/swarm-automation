"""Issue #261: prevent the ai-main integration branch from being deleted by
accident, by installing a deliberate, named GitHub deletion ruleset before
the worker's first push of a newly created integration branch.

`Worker.protect_new_integration_branch` (issue_worker/swarm_issue_worker.py)
makes two separate GitHub CLI calls: a GET to list existing rulesets, then
(when none matches) a POST to create the safeguard. The existing unit tests
(`issue_worker/test_swarm_issue_worker.py::test_new_integration_branch_is_not_pushed_when_safeguard_creation_fails`)
and the sibling adversarial suite
(`tests/adversarial/test_integration_branch_deletion_ruleset_validation.py`)
only exercise: (a) the GET call itself raising, and (b) a same-named but
insufficient existing ruleset. Neither exercises the single most realistic
real-world failure the feature's own documentation (README.md,
`.claude/skills/swarm-automation-dev/SKILL.md`) calls out by name: a `gh`
identity that can *read* rulesets (GET succeeds, returns an empty list) but
lacks the repository-administrator rights to *create* one (POST fails with
a permission error). A `gh` token scoped for ordinary repository read access
routinely has exactly this shape — list/read endpoints are far more commonly
grantable than ruleset-admin write endpoints — so this is not a hypothetical
edge case.

Also untested: `rulesets = json.loads(self.github.gh(arguments))` succeeding
(valid JSON, no exception) but returning something other than a bare list —
e.g. an envelope object like `{"total_count": 0, "rulesets": []}`, which is
the actual response shape GitHub uses for some other paginated list
endpoints. The existing tests only cover `gh` raising outright (malformed
non-JSON output / HTTP failure), never "valid JSON, wrong shape". If the
`isinstance(rulesets, list)` guard were ever weakened (e.g. someone changes
it to `if rulesets:` while refactoring), a dict-shaped response would be
truthy, `next(...)` over a dict would iterate its keys as `item.get(...)`
and raise `AttributeError` on a plain string key -- or, more dangerously, if
someone instead "fixed" the AttributeError by wrapping it in a broad
try/except, the branch could be pushed to GitHub believing itself protected
when the ruleset lookup never actually happened. Both scenarios must fail
closed with a clear `WorkerError`, and the branch must never reach GitHub.
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


class IntegrationBranchDeletionSafeguardFailureModeTests(unittest.TestCase):
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

    def test_readable_but_not_administerable_gh_identity_blocks_first_push(self) -> None:
        """The GET (listing rulesets) succeeds and finds nothing; the POST
        (creating the safeguard) fails because the configured ``gh``
        identity can read the repository but is not a repository
        administrator. This is the exact scenario README.md documents as
        the reason the worker "stops before pushing the new integration
        branch": if this regressed to only checking the GET call's outcome,
        a POST-side permission failure would slip through un-tested and the
        branch could end up pushed unprotected."""
        self._recreate_ai_main_locally_and_remotely_removed()
        with (
            mock.patch.object(self.worker, "remote_is_github_host", return_value=True),
            mock.patch.object(
                self.worker.github,
                "gh",
                side_effect=[
                    "[]",
                    WorkerError(
                        "HTTP 403: Resource not accessible by personal access token "
                        "(POST /repos/acme/widgets/rulesets)"
                    ),
                ],
            ) as github,
        ):
            with self.assertRaisesRegex(WorkerError, "administrator"):
                self.worker.synchronize_integration_branch()

        # Both the listing GET and the creation POST must have been
        # attempted, in that order -- and nothing after the POST failure.
        self.assertEqual(github.call_count, 2)
        first_call_args = github.call_args_list[0].args[0]
        second_call_args = github.call_args_list[1].args[0]
        self.assertIn("GET", first_call_args)
        self.assertIn("POST", second_call_args)
        # The integration branch must never reach GitHub without its
        # deletion safeguard actually having been installed.
        self.assertEqual(self.remote_heads(self.remote), ["main"])

    def test_envelope_shaped_ruleset_response_is_not_mistaken_for_a_list(self) -> None:
        """GitHub's rulesets-list endpoint returns a bare JSON array today.
        If the worker ever talks to a host/proxy that answers with an
        envelope object instead (a shape GitHub uses elsewhere, e.g.
        ``{"total_count": N, "rulesets": [...]}``), that response is valid
        JSON and truthy, but is not the list of rulesets the code assumes.
        The worker must fail closed with a clear error rather than silently
        mis-parsing it as either the ruleset name and description
        the payload happens to contain, or as zero rulesets to be created."""
        self._recreate_ai_main_locally_and_remotely_removed()
        envelope_response = json.dumps(
            {
                "total_count": 1,
                "rulesets": [
                    {
                        "name": "SWARM safeguard: prevent deletion of ai-main",
                        "target": "branch",
                        "enforcement": "active",
                        "conditions": {"ref_name": {"include": ["refs/heads/ai-main"], "exclude": []}},
                        "rules": [{"type": "deletion"}],
                    }
                ],
            }
        )
        with (
            mock.patch.object(self.worker, "remote_is_github_host", return_value=True),
            mock.patch.object(
                self.worker.github, "gh", return_value=envelope_response
            ) as github,
        ):
            with self.assertRaisesRegex(WorkerError, "unexpected ruleset list"):
                self.worker.synchronize_integration_branch()

        github.assert_called_once()
        self.assertEqual(self.remote_heads(self.remote), ["main"])


if __name__ == "__main__":
    unittest.main()
