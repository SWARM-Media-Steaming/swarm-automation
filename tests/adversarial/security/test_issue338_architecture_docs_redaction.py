import os
import sys
import tempfile
import unittest
from pathlib import Path

WORKER = Path(__file__).resolve().parents[3] / "issue_worker"
sys.path.insert(0, str(WORKER))

import architecture_docs as docs  # noqa: E402


class PrefixedSecretRedactionTests(unittest.TestCase):
    """Env-style and camelCase credential names must be redacted, not only bare keywords."""

    CASES = {
        "DB_PASSWORD=hunter2": "hunter2",
        "GITHUB_TOKEN=abcd1234": "abcd1234",
        "STRIPE_SECRET_KEY=live_abc": "live_abc",
        "AWS_SECRET_ACCESS_KEY=abcd1234": "abcd1234",
        'dbPassword = "hunter2"': "hunter2",
        "MY_API_KEY: abc123": "abc123",
    }

    def test_prefixed_credential_assignments_are_redacted(self):
        for text, secret in self.CASES.items():
            with self.subTest(text=text):
                self.assertNotIn(secret, docs.redact(text))

    def test_prompt_diff_does_not_leak_prefixed_secrets(self):
        prompt = docs.build_prompt(
            repository="o/r", issue_number=1, issue_title="t", signals=["api"],
            name_status=[("M", "src/config.py")],
            diff_text="+DB_PASSWORD=hunter2\n+STRIPE_SECRET_KEY=live_abc\n", existing={})
        self.assertNotIn("hunter2", prompt)
        self.assertNotIn("live_abc", prompt)


class SeedSymlinkTests(unittest.TestCase):
    def test_readme_symlink_outside_the_repository_is_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.txt"
            outside.write_text("Private local note that must not be ingested\n")
            root = Path(tmp) / "repo"
            root.mkdir()
            os.symlink(outside, root / "README.md")
            entities = docs.seed_entities(root)
            self.assertNotIn("Private local note", repr(entities))


if __name__ == "__main__":
    unittest.main()
