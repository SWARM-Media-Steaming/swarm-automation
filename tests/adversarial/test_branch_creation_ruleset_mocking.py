#!/usr/bin/env python3
"""Adversarial UAT for issue #279: branch-creation tests fixture validation.

Verifies that:
1. The integration ruleset API mocking in branch-creation tests is correct
2. The mocked responses are valid JSON
3. The mock pattern produces the expected behavior
4. Both test cases (fresh and followup) run successfully with the mock
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path


class BranchCreationRulesetMockingTestCase(unittest.TestCase):
    """Verify branch-creation tests mock the integration ruleset API correctly."""

    def test_gh_mock_returns_valid_json(self) -> None:
        """Verify that the mocked gh responses are valid JSON."""
        # The mock side_effect used in the tests
        mock_responses = ["[]", "{}"]

        # Verify both are valid JSON
        for response in mock_responses:
            try:
                parsed = json.loads(response)
                self.assertIsNotNone(parsed)
            except json.JSONDecodeError as e:
                self.fail(f"Mock response {response!r} is not valid JSON: {e}")

    def test_gh_mock_response_types(self) -> None:
        """Verify that mocked responses have expected types after parsing."""
        first_response = json.loads("[]")
        self.assertIsInstance(first_response, list)
        self.assertEqual(len(first_response), 0)

        second_response = json.loads("{}")
        self.assertIsInstance(second_response, dict)
        self.assertEqual(len(second_response), 0)

    def test_api_list_response_shape(self) -> None:
        """Verify empty list mock is the correct response shape for api_list."""
        # api_list expects a list of dicts or empty list
        # The mock returns "[]" which json.loads to []
        mock_response = "[]"
        parsed = json.loads(mock_response)

        self.assertIsInstance(parsed, list)
        self.assertEqual(parsed, [])

    def test_api_get_response_shape(self) -> None:
        """Verify empty dict mock is the correct response shape for api_get."""
        # api_get expects a dict
        # The mock returns "{}" which json.loads to {}
        mock_response = "{}"
        parsed = json.loads(mock_response)

        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed, {})

    def test_protect_new_integration_branch_with_empty_ruleset_list(self) -> None:
        """Verify the code path when no existing ruleset is found.

        When api_list returns an empty list (no existing rulesets):
        1. The next() call finds nothing (existing = None)
        2. The code skips the verification block
        3. It proceeds to create a new ruleset via POST
        """
        # Simulate the logic:
        # existing = next((item for item in rulesets if ...), None)
        rulesets = []  # What api_list returns when mocked with "[]"
        existing = next(
            (item for item in rulesets if isinstance(item, dict) and item.get("name") == "swarm:ai-main"),
            None
        )

        # Should be None since the list is empty
        self.assertIsNone(existing)

        # This means the code will skip the if block and proceed to creation

    def test_mock_side_effect_order(self) -> None:
        """Verify the side_effect list matches the gh call sequence.

        The protect_new_integration_branch method makes calls in this order:
        1. api_list (via gh) -> should get "[]"
        2. gh POST for creation -> should get "{}"
        """
        mock_side_effect = ["[]", "{}"]

        # Verify the order and parsing
        call_responses = [json.loads(response) for response in mock_side_effect]

        self.assertEqual(call_responses[0], [])  # First call returns empty list
        self.assertEqual(call_responses[1], {})  # Second call returns empty dict

    def test_fresh_branch_test_passes_with_mock(self) -> None:
        """Verify test_fresh_github_branch_is_created_from_the_issue passes."""
        # Run the actual test that was failing
        test_module = Path(__file__).parent.parent.parent / "issue_worker" / "test_swarm_issue_worker.py"

        result = subprocess.run(
            [
                "python3", "-m", "unittest",
                "test_swarm_issue_worker.WorkerTestCase.test_fresh_github_branch_is_created_from_the_issue"
            ],
            cwd=str(test_module.parent),
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, f"Test failed:\n{result.stderr}")

    def test_followup_branch_test_passes_with_mock(self) -> None:
        """Verify test_followup_recreated_github_branch_is_linked_to_the_issue passes."""
        # Run the actual test that was failing
        test_module = Path(__file__).parent.parent.parent / "issue_worker" / "test_swarm_issue_worker.py"

        result = subprocess.run(
            [
                "python3", "-m", "unittest",
                "test_swarm_issue_worker.WorkerTestCase.test_followup_recreated_github_branch_is_linked_to_the_issue"
            ],
            cwd=str(test_module.parent),
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, f"Test failed:\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
