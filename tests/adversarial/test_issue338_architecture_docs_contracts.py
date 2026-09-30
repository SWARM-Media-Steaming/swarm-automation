"""Issue #338: adversarial contracts for the architecture-documentation model.

Expectations derive from the issue: secrets never reach prompts/storage,
authentication changes are architectural impact signals, and a pending patch
must become current once its change reaches the integration branch (the worker
squash-merges issue PRs, so the original commit is never an ancestor).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "issue_worker"))

import architecture_docs as docs  # noqa: E402


class RedactionContracts(unittest.TestCase):
    def test_json_quoted_secret_values_are_redacted(self):
        for text in (
            '{"password": "hunter2"}',
            '{"api_key": "abc123secret"}',
            "'token': 'zzTopSecret'",
        ):
            cleaned = docs.redact(text)
            for leaked in ("hunter2", "abc123secret", "zzTopSecret"):
                self.assertNotIn(leaked, cleaned, text)

    def test_quoted_multiword_secret_is_fully_redacted(self):
        cleaned = docs.redact('password="correct horse battery"')
        self.assertNotIn("horse", cleaned)
        self.assertNotIn("battery", cleaned)

    def test_prompt_diff_json_secret_is_redacted(self):
        prompt = docs.build_prompt(
            repository="o/r", issue_number=1, issue_title="t", signals=["api"],
            name_status=[("M", "src/a.py")], diff_text='+ "client_secret": "s3cr3tvalue"', existing={})
        self.assertNotIn("s3cr3tvalue", prompt)

    def test_stored_entity_never_keeps_json_secret(self):
        review = docs.validate_review({"impact": "update", "reason": "r", "confidence": 0.5, "operations": [{
            "op": "upsert", "entity": {
                "id": "x", "section": "components", "kind": "component", "name": "X",
                "summary": 'config {"secret": "plainvalue99"}', "provenance": "inferred", "confidence": 0.5}}]})
        self.assertNotIn("plainvalue99", json.dumps(review))


class SignalContracts(unittest.TestCase):
    def test_authentication_and_authorization_files_signal(self):
        for path in ("src/authentication/service.py", "src/authorization.py", "src/auth.rs", "lib/oauth2/client.js"):
            self.assertIn("authentication_authorization", docs.impact_signals([("M", path)]), path)


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})


class SquashReconcileContract(unittest.TestCase):
    def test_pending_patch_reconciles_after_squash_merge(self):
        """The worker squash-merges; ancestry of the issue commit cannot be the only test."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "ai-main")
            (repo / "a.txt").write_text("1")
            git(repo, "add", "."); git(repo, "commit", "-qm", "base")
            git(repo, "checkout", "-qb", "issue")
            (repo / "b.txt").write_text("2")
            git(repo, "add", "."); git(repo, "commit", "-qm", "work")
            sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            git(repo, "checkout", "-q", "ai-main")
            git(repo, "merge", "--squash", "issue")
            git(repo, "commit", "-qm", "squashed")
            git(repo, "remote", "add", "origin", str(repo))
            git(repo, "fetch", "-q", "origin")

            class Stub(docs.ArchitectureDocsMixin):
                def __init__(self):
                    self.config = types.SimpleNamespace(
                        remote_name="origin", integration_branch="ai-main", git_bin="git", repo_dir=str(repo))
                def git(self, *a, check=True):
                    return ""
            # Content-equivalence (tree contains the change) is the observable evidence of a squash.
            self.assertTrue(Stub().commit_on_integration_branch(sha),
                            "a squash-merged issue commit must count as on the integration branch")


if __name__ == "__main__":
    unittest.main()
