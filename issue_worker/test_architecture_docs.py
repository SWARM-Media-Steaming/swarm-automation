import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import architecture_docs as docs

SHA_A = "a" * 40
SHA_B = "b" * 40


def upsert(identifier="api", **overrides):
    entity = {
        "id": identifier, "section": "components", "kind": "component", "name": "API",
        "summary": "Serves requests.", "provenance": "observed", "confidence": 0.9,
        "evidence": [{"type": "path", "ref": "src/api.rs", "line": 3}],
    }
    entity.update(overrides)
    return {"op": "upsert", "entity": entity}


def update_review(*operations):
    return docs.validate_review(
        {"impact": "update", "reason": "New API", "confidence": 0.8, "operations": list(operations)})


NONE_REVIEW = {"impact": "none", "reason": "No change", "confidence": 0.9, "operations": []}


class RedactionTests(unittest.TestCase):
    def test_secrets_urls_and_addresses_are_removed(self):
        text = (
            "token=abc123 password: hunter2 ghp_" + "a" * 30 + " AKIAABCDEFGHIJKLMNOP "
            "https://internal.example.corp/x http://10.1.2.3:8080 postgres://u:pw@db.internal/x"
        )
        cleaned = docs.redact(text)
        for leaked in ("abc123", "hunter2", "AKIA", "internal.example", "10.1.2.3", "u:pw", "ghp_"):
            self.assertNotIn(leaked, cleaned)

    def test_private_key_blocks_and_long_blobs_are_removed(self):
        cleaned = docs.redact("-----BEGIN PRIVATE KEY-----\nMIIabc\n-----END PRIVATE KEY----- " + "A" * 60)
        self.assertNotIn("MII", cleaned)
        self.assertNotIn("A" * 40, cleaned)

    def test_sensitive_paths_are_never_evidence(self):
        for path in (".env", "config/.env.production", "certs/server.pem", "../etc/passwd", "/abs/x", "C:/x", "a/../b"):
            self.assertEqual(docs.safe_repo_path(path), "", path)
        self.assertEqual(docs.safe_repo_path("src/api.rs"), "src/api.rs")


class SignalTests(unittest.TestCase):
    def test_signals_cover_architectural_changes(self):
        cases = {
            "Cargo.toml": "dependencies",
            "migrations/001.sql": "schema_persistence",
            ".github/workflows/ci.yml": "ci",
            "Dockerfile": "deployment",
            "src/auth/session.rs": "authentication_authorization",
        }
        for path, expected in cases.items():
            self.assertIn(expected, docs.impact_signals([("M", path)]), path)

    def test_docs_and_test_only_edits_have_no_signals(self):
        self.assertEqual(docs.impact_signals([("M", "README.md"), ("M", "tests/test_x.py"), ("M", "src/x.py")]), [])

    def test_added_module_and_bulk_tests_are_signals(self):
        self.assertIn("module_structure", docs.impact_signals([("A", "src/new_service.py")]))
        added = [("A", f"tests/test_{i}.py") for i in range(3)]
        self.assertIn("major_tests", docs.impact_signals(added))

    def test_parse_name_status(self):
        self.assertEqual(docs.parse_name_status("M\ta.py\nR100\told.py\tnew.py\n"), [("M", "a.py"), ("R", "new.py")])


class ValidationTests(unittest.TestCase):
    def test_valid_patch_is_normalized_and_redacted(self):
        review = update_review(upsert(summary="see https://secret.internal/x token=abc"))
        entity = review["operations"][0]["entity"]
        self.assertNotIn("secret.internal", entity["summary"])
        self.assertNotIn("abc", entity["summary"])

    def test_rejects_unsafe_or_malformed_patches(self):
        bad = [
            upsert(id="../x"), upsert(section="nope"), upsert(kind="nope"), upsert(provenance="human"),
            upsert(provenance="magic"), upsert(confidence=2), upsert(evidence=[]),
            upsert(extra="field"), upsert(depends_on=["Bad Id"]), upsert(personas={"ceo": "x"}),
        ]
        for op in bad:
            with self.assertRaises(docs.PatchError, msg=str(op)):
                update_review(op)
        with self.assertRaises(docs.PatchError):
            update_review(*[upsert(f"e{i}") for i in range(docs.MAX_OPERATIONS + 1)])
        with self.assertRaises(docs.PatchError):
            docs.validate_review({"impact": "update", "operations": []})

    def test_unsafe_evidence_paths_are_dropped_not_stored(self):
        review = update_review(upsert(evidence=[
            {"type": "path", "ref": ".env"}, {"type": "path", "ref": "src/ok.rs"}]))
        self.assertEqual([e["ref"] for e in review["operations"][0]["entity"]["evidence"]], ["src/ok.rs"])

    def test_parse_review_accepts_fenced_json_and_rejects_prose(self):
        payload = json.dumps({"impact": "none", "reason": "x", "confidence": "high"})
        self.assertEqual(docs.parse_review(f"Here:\n```json\n{payload}\n```")["impact"], "none")
        with self.assertRaises(docs.PatchError):
            docs.parse_review("I changed the html directly")


class StoreLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = docs.ArchitectureStore(Path(self.tmp.name) / "docs", "octo/repo")

    def record(self, review, commit=SHA_A, merged=True, issue=1):
        return self.store.record_review(
            issue_number=issue, issue_title="T", commit=commit, review=review, signals=["api"],
            merged=merged, branch="ai/claude/issue-1", pull_request="5")

    def test_new_repository_is_empty_and_disabled_view_is_safe(self):
        view = self.store.view(enabled=False)
        self.assertEqual(view["freshness"], "empty")
        self.assertFalse(view["enabled"])
        self.assertEqual(view["entities"], [])

    def test_merged_update_becomes_current_with_provenance(self):
        self.record(update_review(upsert()))
        view = self.store.view()
        self.assertEqual(view["freshness"], "current")
        self.assertEqual(view["documentedThrough"], SHA_A)
        entity = view["entities"][0]
        self.assertEqual(entity["updatedBy"]["issue"], 1)
        self.assertEqual(entity["provenance"], "observed")

    def test_unmerged_update_stays_pending_and_never_current(self):
        self.record(update_review(upsert()), merged=False)
        view = self.store.view()
        self.assertEqual(view["freshness"], "empty")  # nothing applied yet
        self.assertEqual(view["documentedThrough"], "")
        self.assertEqual(len(view["pending"]), 1)
        self.assertEqual(view["pending"][0]["touches"], ["API"])
        self.assertEqual(view["reviews"][0]["state"], "pending")

    def test_pending_shows_as_pending_over_existing_docs_then_reconciles(self):
        self.record(update_review(upsert()), commit=SHA_A)
        self.record(update_review(upsert("db", name="DB")), commit=SHA_B, merged=False, issue=2)
        view = self.store.view()
        self.assertEqual(view["freshness"], "pending")
        self.assertEqual(view["documentedThrough"], SHA_A)
        self.assertEqual([e["id"] for e in view["entities"]], ["api"])
        self.assertEqual(self.store.reconcile(lambda commit: False), 0)
        self.assertEqual(self.store.reconcile(lambda commit: commit == SHA_B), 1)
        view = self.store.view()
        self.assertEqual(view["freshness"], "current")
        self.assertEqual(view["documentedThrough"], SHA_B)
        self.assertEqual({e["id"] for e in view["entities"]}, {"api", "db"})
        self.assertEqual(view["pending"], [])

    def test_reconcile_treats_errors_as_still_pending(self):
        self.record(update_review(upsert()), merged=False)
        def boom(_commit):
            raise RuntimeError("git failed")
        self.assertEqual(self.store.reconcile(boom), 0)
        self.assertEqual(len(self.store.view()["pending"]), 1)

    def test_no_change_does_not_touch_entities_and_only_advances_when_merged(self):
        self.record(update_review(upsert()), commit=SHA_A)
        before = self.store.load()["entities"]
        self.record(NONE_REVIEW, commit=SHA_B, merged=False, issue=2)
        snapshot = self.store.load()
        self.assertEqual(snapshot["entities"], before)
        self.assertEqual(snapshot["documentedThrough"], SHA_A)
        self.record(NONE_REVIEW, commit=SHA_B, merged=True, issue=2)
        snapshot = self.store.load()
        self.assertEqual(snapshot["entities"], before)
        self.assertEqual(snapshot["documentedThrough"], SHA_B)
        self.assertEqual(snapshot["reviews"][-1]["status"], "no_change")

    def test_failed_review_changes_nothing_but_the_audit_trail(self):
        self.record(update_review(upsert()), commit=SHA_A)
        self.store.record_failure(issue_number=2, commit=SHA_B, error="quota exhausted token=abc")
        snapshot = self.store.load()
        self.assertEqual(snapshot["documentedThrough"], SHA_A)
        self.assertEqual(snapshot["reviews"][-1]["status"], "failed")
        self.assertNotIn("abc", snapshot["reviews"][-1]["reason"])

    def test_human_authored_entities_cannot_be_overwritten_or_removed_by_ai(self):
        snapshot = self.store.load()
        snapshot["entities"]["decision"] = docs.validate_entity(
            dict(upsert("decision", provenance="human", kind="decision", section="decisions")["entity"]),
            allow_human=True)
        self.store.save(snapshot)
        self.record(update_review(upsert("decision", name="Overwritten"), {"op": "remove", "id": "decision"}))
        entity = self.store.load()["entities"]["decision"]
        self.assertEqual(entity["provenance"], "human")
        self.assertEqual(entity["name"], "API")

    def test_stored_file_never_contains_secrets(self):
        self.record(update_review(upsert(summary="key=sk-" + "a" * 30 + " at http://10.0.0.5/x")))
        raw = self.store.path.read_text()
        self.assertNotIn("sk-aaaa", raw)
        self.assertNotIn("10.0.0.5", raw)

    def test_corrupt_or_old_schema_loads_empty(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("{not json")
        self.assertEqual(self.store.view()["freshness"], "empty")
        self.store.path.write_text(json.dumps({"schema": 0}))
        self.assertEqual(self.store.view()["entities"], [])

    def test_repository_names_cannot_escape_the_state_directory(self):
        store = docs.ArchitectureStore(Path(self.tmp.name) / "docs", "../../evil")
        self.assertEqual(store.path.parent, Path(self.tmp.name) / "docs")


class SeedAndCliTests(unittest.TestCase):
    def test_seed_uses_names_only_and_skips_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            (root / "src").mkdir(parents=True)
            (root / "node_modules").mkdir()
            (root / ".git").mkdir()
            (root / "Cargo.toml").write_text("[package]\n")
            (root / ".env").write_text("SECRET=hunter2")
            (root / "README.md").write_text("# Title\n\nA tool for things. token=abc123\n")
            store = docs.ArchitectureStore(Path(tmp) / "state", "o/r")
            self.assertTrue(store.seed_from_workspace(root))
            self.assertFalse(store.seed_from_workspace(root))
            raw = store.path.read_text()
            for leaked in ("hunter2", "abc123"):
                self.assertNotIn(leaked, raw)
            ids = set(store.load()["entities"])
            self.assertIn("seed.dir-src", ids)
            self.assertNotIn("seed.dir-node_modules", ids)
            self.assertEqual(store.view()["freshness"], "baseline")

    def test_handle_action_only_seeds_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            (root / "src").mkdir(parents=True)
            state = Path(tmp) / "state"
            off = docs.handle_action({"repository": "o/r", "enabled": False, "workspace": str(root)}, state)
            self.assertEqual(off["entities"], [])
            on = docs.handle_action({"repository": "o/r", "enabled": True, "workspace": str(root)}, state)
            self.assertTrue(on["entities"])
            self.assertIn("error", docs.handle_action({"repository": ""}, state))


class PromptTests(unittest.TestCase):
    def test_prompt_excludes_sensitive_files_and_redacts_diff(self):
        prompt = docs.build_prompt(
            repository="o/r", issue_number=3, issue_title="Add auth token=abc", signals=["api"],
            name_status=[("M", ".env"), ("M", "src/api.rs"), ("A", "keys/id_rsa")],
            diff_text="+api_key=SUPERSECRET\n+https://internal.example/x", existing={})
        self.assertNotIn("M .env", prompt)
        self.assertNotIn("id_rsa", prompt)
        self.assertIn("src/api.rs", prompt)
        for leaked in ("SUPERSECRET", "internal.example", "abc\n"):
            self.assertNotIn(leaked, prompt)


class WorkerReviewTests(unittest.TestCase):
    """The mixin's lifecycle against a stubbed worker (no real git or AI)."""

    def make_worker(self, tmp, *, enabled=True, changes="M\tsrc/api/routes.rs\n", ai_output=None,
                    ai_status=0, dirty=False):
        import swarm_issue_worker  # noqa: F401 - the mixin imports WorkerError/log from it
        outer = self

        class Stub(docs.ArchitectureDocsMixin):
            def __init__(self):
                self.config = types.SimpleNamespace(
                    architecture_docs_enabled=enabled, execution_history_db=Path(tmp) / "db.sqlite3",
                    github_repository="o/r", repo_dir=str(tmp), remote_name="origin",
                    integration_branch="ai-main", git_bin="git")
                self.issue = types.SimpleNamespace(number=9, title="Add route")
                self.choice = types.SimpleNamespace(name="Claude", session_id="s", resume=True)
                self.ai_output_file = Path(tmp) / "out.log"
                self.prompts = []
                self.dirty = dirty

            def worktree_status(self):
                return " M x" if self.dirty else ""

            def git(self, *args, check=True):
                return ""

            def run_ai(self, prompt, activity="working"):
                self.prompts.append(prompt)
                self.ai_output_file.write_text(ai_output or "")
                return ai_status

        stub = Stub()
        patcher = mock.patch.object(docs, "collect_change", return_value=(docs.parse_name_status(changes), "diff"))
        patcher.start()
        self.addCleanup(patcher.stop)
        # dataclasses.replace needs a dataclass; provide a real one for the choice.
        import swarm_issue_worker as worker_module
        stub.choice = worker_module.ProviderChoice("Claude", "m", "high", "s", True)
        return stub

    def run_review(self, stub, merged=True):
        return stub.run_architecture_docs_review(
            base_sha=SHA_A, commit_sha=SHA_B, merged=merged,
            pull_request_url="https://github.com/o/r/pull/12", branch="ai/claude/issue-9")

    def test_disabled_feature_does_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = self.make_worker(tmp, enabled=False)
            self.assertEqual(self.run_review(stub), "disabled")
            self.assertEqual(stub.prompts, [])
            self.assertFalse((Path(tmp) / "architecture_docs").exists())

    def test_no_signals_records_a_lightweight_no_change_without_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = self.make_worker(tmp, changes="M\tREADME.md\n")
            self.assertEqual(self.run_review(stub), "no_signals")
            self.assertEqual(stub.prompts, [])
            snapshot = stub.architecture_store().load()
            self.assertEqual(snapshot["entities"], {})
            self.assertEqual(snapshot["reviews"][-1]["status"], "no_change")

    def test_signal_runs_a_fresh_session_and_applies_merged_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = json.dumps({"impact": "update", "reason": "route", "confidence": 0.8,
                                 "operations": [upsert()]})
            stub = self.make_worker(tmp, ai_output=output)
            original = stub.choice
            self.assertEqual(self.run_review(stub), "updated")
            self.assertIs(stub.choice, original)  # session identity restored
            view = stub.architecture_store().view()
            self.assertEqual(view["freshness"], "current")
            self.assertEqual(view["documentedThrough"], SHA_B)
            self.assertEqual(view["reviews"][0]["pullRequest"], "12")

    def test_unmerged_branch_is_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = json.dumps({"impact": "update", "reason": "r", "confidence": 0.8, "operations": [upsert()]})
            stub = self.make_worker(tmp, ai_output=output)
            self.assertEqual(self.run_review(stub, merged=False), "pending")
            view = stub.architecture_store().view()
            self.assertEqual(view["documentedThrough"], "")
            self.assertEqual(len(view["pending"]), 1)

    def test_failed_or_unparseable_ai_never_marks_docs_current(self):
        for kwargs in ({"ai_status": 1, "ai_output": "quota"}, {"ai_output": "not json at all"}):
            with tempfile.TemporaryDirectory() as tmp:
                stub = self.make_worker(tmp, **kwargs)
                self.assertEqual(self.run_review(stub), "failed")
                snapshot = stub.architecture_store().load()
                self.assertEqual(snapshot["documentedThrough"], "")
                self.assertEqual(snapshot["reviews"][-1]["status"], "failed")

    def test_ai_that_edits_the_repository_is_discarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = json.dumps({"impact": "update", "reason": "r", "confidence": 0.8, "operations": [upsert()]})
            stub = self.make_worker(tmp, ai_output=output)
            calls = []

            def run_ai(prompt, activity="working"):
                stub.ai_output_file.write_text(output)
                stub.dirty = True
                return 0
            stub.run_ai = run_ai
            stub.git = lambda *args, check=True: calls.append(args) or ""
            self.assertEqual(self.run_review(stub), "failed")
            self.assertTrue(any(call[:1] == ("clean",) for call in calls))
            self.assertEqual(stub.architecture_store().load()["entities"], {})

    def test_dirty_checkout_skips_the_ai_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = self.make_worker(tmp, dirty=True)
            self.assertEqual(self.run_review(stub), "skipped")
            self.assertEqual(stub.prompts, [])

    def test_review_never_raises_into_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = self.make_worker(tmp)
            with mock.patch.object(docs, "impact_signals", side_effect=RuntimeError("boom")):
                self.assertEqual(self.run_review(stub), "failed")

    def test_reachability_uses_git_ancestry_on_the_integration_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = self.make_worker(tmp)
            with mock.patch.object(docs.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                self.assertTrue(stub.commit_on_integration_branch(SHA_A))
                self.assertIn("origin/ai-main", run.call_args.args[0])
            self.assertFalse(stub.commit_on_integration_branch("not-a-sha"))


class WorkerConfigTests(unittest.TestCase):
    def test_flag_defaults_off_and_is_parsed(self):
        import swarm_issue_worker as worker_module
        parser_source = Path(worker_module.__file__).read_text()
        self.assertIn('"--architecture-docs-enabled"', parser_source)
        self.assertIn("SWARM_ARCHITECTURE_DOCS_ENABLED\", False", parser_source)
        self.assertFalse(worker_module.Config.__dataclass_fields__["architecture_docs_enabled"].default)


if __name__ == "__main__":
    unittest.main()
