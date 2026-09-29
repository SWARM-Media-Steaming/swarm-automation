import unittest

from adversarial_security import SECURITY_STAGE
from adversarial_uat import UAT_STAGE
from swarm_issue_worker import ProviderChoice


class AttributionLogTests(unittest.TestCase):
    def test_both_stages_log_role_provider_model_and_effort(self):
        choice = ProviderChoice("Grok", "grok-4.6", "medium")
        self.assertEqual(
            UAT_STAGE.attribution_log(7, {"phase": "fix"}, choice),
            "Adversarial UAT for issue #7: fixer Grok model grok-4.6 with effort medium.")
        self.assertEqual(
            SECURITY_STAGE.attribution_log(7, {"phase": "test"}, choice),
            "Adversarial Cybersecurity for issue #7: tester Grok model grok-4.6 with effort medium.")

    def test_missing_values_are_default_not_blank(self):
        line = UAT_STAGE.attribution_log(7, {"phase": "test"}, ProviderChoice("Claude", "", ""))
        self.assertTrue(line.endswith("tester Claude model <unconfigured> with effort <unconfigured>."))


if __name__ == "__main__":
    unittest.main()
