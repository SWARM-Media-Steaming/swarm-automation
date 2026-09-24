"""Issue #266 acceptance: Integration-branch ruleset lookup paginates GitHub's rulesets list.

protect_new_integration_branch() lists existing repository rulesets to check if
one already exists before attempting creation. GitHub's rulesets list endpoint
defaults to 30 items per page. The fix uses api_list() with --paginate --slurp
to ensure pagination is handled automatically, so repositories with 30+ rulesets
do not fail with spurious "ruleset name already exists" errors when an existing
safeguard is on a later page.
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


class RulesetPaginationTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def test_existing_ruleset_on_second_page_is_found(self) -> None:
        """Pagination works: ruleset on page 2 is correctly identified."""
        from swarm_issue_worker import Worker  # noqa: F811

        ruleset_name = "SWARM safeguard: prevent deletion of ai-main"
        target_branch = "ai-main"

        # Simulate many rulesets (first 30 from page 1, then the target on what would be page 2+)
        # api_list returns a flat list after pagination is handled
        all_rulesets = [
            {
                "name": f"other-ruleset-{i}",
                "target": "branch",
                "enforcement": "active",
                "conditions": {"ref_name": {"include": [f"refs/heads/other-{i}"]}},
                "rules": [{"type": "deletion"}],
            }
            for i in range(30)
        ]

        target_ruleset = {
            "name": ruleset_name,
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": [f"refs/heads/{target_branch}"]}},
            "rules": [{"type": "deletion"}],
        }

        all_rulesets.append(target_ruleset)
        all_rulesets.append({"name": "yet-another-ruleset", "target": "branch"})

        with mock.patch.object(
            self.worker.github, "api_list", return_value=all_rulesets
        ) as api_mock:
            # Should not raise; should recognize existing safeguard
            self.worker.protect_new_integration_branch(target_branch)
            api_mock.assert_called_once()

            # Verify it tried to list rulesets from the correct endpoint
            call_args = api_mock.call_args
            self.assertIn("rulesets", call_args[0][0])

    def test_existing_ruleset_on_first_page_is_found(self) -> None:
        """Pagination works: ruleset on page 1 is still correctly identified."""
        from swarm_issue_worker import Worker  # noqa: F811

        ruleset_name = "SWARM safeguard: prevent deletion of ai-main"
        target_branch = "ai-main"

        target_ruleset = {
            "name": ruleset_name,
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": [f"refs/heads/{target_branch}"]}},
            "rules": [{"type": "deletion"}],
        }

        # Single-page response (flat list, not wrapped in another list)
        rulesets = [target_ruleset]

        with mock.patch.object(
            self.worker.github, "api_list", return_value=rulesets
        ) as api_mock:
            # Should not raise; should recognize existing safeguard
            self.worker.protect_new_integration_branch(target_branch)
            api_mock.assert_called_once()

    def test_many_pages_of_rulesets_paginated_correctly(self) -> None:
        """Pagination works: hundreds of rulesets across many pages."""
        from swarm_issue_worker import Worker  # noqa: F811

        ruleset_name = "SWARM safeguard: prevent deletion of ai-main"
        target_branch = "ai-main"

        # Build flat list simulating rulesets from 5 pages (150 total)
        all_rulesets = []
        for page_num in range(5):
            for i in range(30):
                all_rulesets.append({
                    "name": f"ruleset-page{page_num}-item{i}",
                    "target": "branch",
                    "enforcement": "active",
                    "conditions": {"ref_name": {"include": [f"refs/heads/test-{page_num}-{i}"]}},
                    "rules": [{"type": "deletion"}],
                })

        # Put our target after 120 rulesets (would be on page 4+)
        target_ruleset = {
            "name": ruleset_name,
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": [f"refs/heads/{target_branch}"]}},
            "rules": [{"type": "deletion"}],
        }
        all_rulesets.insert(120, target_ruleset)

        with mock.patch.object(
            self.worker.github, "api_list", return_value=all_rulesets
        ) as api_mock:
            # Should find it even though it's past the 100th item and not raise
            self.worker.protect_new_integration_branch(target_branch)
            api_mock.assert_called_once()

    def test_non_list_response_raises_worker_error(self) -> None:
        """Non-list rulesets response (e.g., error dict) raises WorkerError."""
        from swarm_issue_worker import Worker  # noqa: F811

        # Simulate GitHub returning an error wrapped in an object instead of a list
        error_response = {"message": "Not Found"}

        with mock.patch.object(
            self.worker.github, "api_list", return_value=error_response
        ):
            with self.assertRaisesRegex(WorkerError, "unexpected ruleset list"):
                self.worker.protect_new_integration_branch("ai-main")

    def test_api_list_error_propagates_correctly(self) -> None:
        """WorkerError from api_list (e.g., non-list response) is wrapped correctly."""
        from swarm_issue_worker import Worker  # noqa: F811

        with mock.patch.object(
            self.worker.github,
            "api_list",
            side_effect=WorkerError("GitHub returned a non-list response"),
        ):
            with self.assertRaisesRegex(WorkerError, "Could not verify the deletion safeguard"):
                self.worker.protect_new_integration_branch("ai-main")

    def test_json_decode_error_raises_worker_error(self) -> None:
        """JSON decode errors during api_list call are wrapped as WorkerError."""
        from swarm_issue_worker import Worker  # noqa: F811

        import json

        with mock.patch.object(
            self.worker.github,
            "api_list",
            side_effect=json.JSONDecodeError("Expecting value", "", 0),
        ):
            with self.assertRaisesRegex(WorkerError, "Could not verify the deletion safeguard"):
                self.worker.protect_new_integration_branch("ai-main")

    def test_ruleset_validation_after_pagination(self) -> None:
        """After pagination, a same-named but incomplete ruleset is rejected."""
        from swarm_issue_worker import Worker  # noqa: F811

        ruleset_name = "SWARM safeguard: prevent deletion of ai-main"
        target_branch = "ai-main"

        # Ruleset exists by name but lacks the deletion rule
        incomplete_ruleset = {
            "name": ruleset_name,
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": [f"refs/heads/{target_branch}"]}},
            "rules": [],  # Missing deletion rule
        }

        # Flat list with the incomplete ruleset at the start
        all_rulesets = [incomplete_ruleset]

        with mock.patch.object(self.worker.github, "api_list", return_value=all_rulesets):
            with self.assertRaisesRegex(
                WorkerError, "does not protect.*from deletion"
            ):
                self.worker.protect_new_integration_branch(target_branch)


if __name__ == "__main__":
    unittest.main()
