#!/usr/bin/env python3
"""Tests for the Engineering Knowledge Platform (issue #291)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _adopt_adversarial_twin_during_uat_discovery() -> bool:
    """Yield to tests/adversarial/test_engineering_knowledge.py when that tree is being collected.

    Unittest discover of `tests/adversarial` with pattern `test_*.py` (the
    rustfmt suite) imports earlier files that insert `issue_worker/` at the
    front of sys.path. The later import of this module name then loads this
    file instead of the adversarial twin and aborts collection.
    """
    here = Path(__file__).resolve()
    twin = here.parents[1] / "tests" / "adversarial" / "test_engineering_knowledge.py"
    if not twin.is_file() or twin == here:
        return False
    twin_dir = twin.parent
    discovering = False
    for entry in sys.path:
        if not entry:
            continue
        try:
            if Path(entry).resolve() == twin_dir:
                discovering = True
                break
        except OSError:
            continue
    if not discovering:
        return False
    spec = importlib.util.spec_from_file_location(__name__, twin)
    if spec is None or spec.loader is None:
        return False
    module = importlib.util.module_from_spec(spec)
    sys.modules[__name__] = module
    spec.loader.exec_module(module)
    return True


_adopt_adversarial_twin_during_uat_discovery()

from ai_execution_history import ExecutionHistoryRepository, ExecutionStart
from engineering_knowledge import (
    DEFAULT_OWNER_SCOPE_ID,
    OBJECT_AGENT_EXECUTION,
    OBJECT_COMPONENT,
    OBJECT_DECISION,
    OBJECT_FINDING,
    OBJECT_ISSUE,
    OBJECT_REPOSITORY,
    PROVENANCE_GENERATED_SUMMARY,
    PROVENANCE_INFERRED,
    PROVENANCE_SOURCE_FACT,
    REL_BELONGS_TO,
    REL_CORRECTED_BY,
    REL_DISCOVERED_BY,
    REL_MODIFIES,
    REL_PRODUCED,
    REL_RESPONDED_TO,
    REL_VERIFIED_BY,
    REL_WORKED_ON,
    GitHistoryProvider,
    IndexContext,
    KnowledgeProvider,
    KnowledgeService,
    KnowledgeSettings,
    KnowledgeStore,
    ObjectDraft,
    RelationshipDraft,
    RepositoryFilesystemProvider,
    RepositorySpec,
    SwarmExecutionProvider,
    classify_question,
    estimate_tokens,
    handle_action,
    main as knowledge_main,
    register_provider,
    registered_providers,
)


def start_execution(repo: ExecutionHistoryRepository, **overrides: object) -> str:
    payload = dict(
        repository="acme/checkout",
        issue_number=417,
        issue_url="https://github.com/acme/checkout/issues/417",
        issue_title="Use Kafka for checkout events",
        issue_body="We chose Kafka instead of synchronous APIs so payment and inventory stay decoupled.",
        provider="Claude",
        model="claude-sonnet-5",
        effort="high",
        branch_name="ai/claude/issue-417",
        application_version="1.0.0",
        routing_decision={"provider": "claude", "selected_model": "claude-sonnet-5", "reasoning_effort": "high"},
    )
    payload.update(overrides)
    return repo.create(ExecutionStart(**payload), "2026-01-15T10:00:00+00:00")  # type: ignore[arg-type]


class KnowledgeFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="swarm-knowledge.")
        self.root = Path(self.temporary.name)
        self.db = self.root / "swarm-automation.sqlite3"
        self.store = KnowledgeStore(self.db)
        self.history = ExecutionHistoryRepository(self.db)
        self.repo_dir = self.root / "checkout"
        self.repo_dir.mkdir()
        (self.repo_dir / "README.md").write_text(
            "# Checkout\nThis service uses Kafka and Redis for authentication tokens.\n",
            encoding="utf-8",
        )
        (self.repo_dir / "docs").mkdir()
        (self.repo_dir / "docs" / "adr-001-kafka.md").write_text(
            "# ADR 001\nKafka was selected instead of synchronous APIs.\n",
            encoding="utf-8",
        )
        (self.repo_dir / "Cargo.toml").write_text("[package]\nname='checkout'\n[dependencies]\nserde = '1'\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def spec(self, name: str = "acme/checkout") -> RepositorySpec:
        return RepositorySpec(name=name, workspace=str(self.repo_dir), project_id="acme")

    def service(self) -> KnowledgeService:
        return KnowledgeService(self.db, owner_scope_id=DEFAULT_OWNER_SCOPE_ID)

    def test_migrate_is_idempotent_and_does_not_duplicate_execution_tables(self) -> None:
        KnowledgeStore(self.db)
        KnowledgeStore(self.db)
        with self.store.connect() as database:
            versions = [row[0] for row in database.execute("SELECT version FROM knowledge_schema_migrations")]
            tables = {
                row[0]
                for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        self.assertEqual(versions, [1])
        self.assertIn("ai_executions", tables)
        self.assertIn("knowledge_objects", tables)
        self.assertIn("knowledge_relationships", tables)

    def test_existing_executions_are_linked_not_copied(self) -> None:
        execution_id = start_execution(self.history)
        self.history.update(
            execution_id,
            "2026-01-15T11:00:00+00:00",
            files_changed=["src/payments/kafka.rs", "src/auth/session.rs"],
            changes_summary="Wired checkout events through Kafka.",
            final_status="completed",
            pull_request_number=88,
            pull_request_url="https://github.com/acme/checkout/pull/88",
        )
        result = self.service().refresh(
            [self.spec()],
            KnowledgeSettings(automatic_generation=False),
            providers=[SwarmExecutionProvider()],
        )
        self.assertEqual(result["status"], "success")
        linked = self.store.lookup_id(OBJECT_AGENT_EXECUTION, "acme/checkout", "ai_executions", execution_id)
        self.assertTrue(linked)
        obj = self.store.get_object(linked)
        assert obj is not None
        self.assertEqual(obj["source_ref"], execution_id)
        self.assertEqual(obj["provenance_kind"], PROVENANCE_SOURCE_FACT)
        with self.history.connect() as database:
            copies = database.execute("SELECT COUNT(*) FROM ai_executions").fetchone()[0]
        self.assertEqual(copies, 1)
        # Re-index reuses the same knowledge object.
        again = self.service().refresh(
            [self.spec()],
            KnowledgeSettings(automatic_generation=False),
            providers=[SwarmExecutionProvider()],
        )
        self.assertEqual(again["objectsCreated"], 0)
        self.assertEqual(
            self.store.lookup_id(OBJECT_AGENT_EXECUTION, "acme/checkout", "ai_executions", execution_id),
            linked,
        )

    def test_relationships_are_queryable_and_traversable(self) -> None:
        execution_id = start_execution(self.history)
        self.history.update(
            execution_id,
            "2026-01-15T11:00:00+00:00",
            files_changed=["src/payments/kafka.rs"],
            final_status="completed",
        )
        self.service().refresh([self.spec()], KnowledgeSettings(), providers=[SwarmExecutionProvider()])
        execution_obj = self.store.lookup_id(OBJECT_AGENT_EXECUTION, "acme/checkout", "ai_executions", execution_id)
        issue_obj = self.store.lookup_id(OBJECT_ISSUE, "acme/checkout", "github_issue", "417")
        edges = self.store.traverse(execution_obj, [REL_WORKED_ON, REL_MODIFIES], depth=2)
        types = {edge["relationship_type"] for edge in edges}
        self.assertIn(REL_WORKED_ON, types)
        self.assertIn(REL_MODIFIES, types)
        neighbors = {item["object_id"] for item in self.store.neighbors(execution_obj, REL_WORKED_ON)}
        self.assertIn(issue_obj, neighbors)

    def test_agent_finding_correction_verification_graph(self) -> None:
        execution_id = start_execution(self.history)
        self.history.record_adversarial_round(
            execution_id,
            {
                "stage": "uat",
                "round_number": 0,
                "fixer_provider": "Claude",
                "fixer_model": "claude-sonnet-5",
                "tester_provider": "Codex",
                "tester_model": "gpt-5.6-luna",
                "findings_found": 1,
                "findings_fixed": 0,
                "started_at": "2026-01-15T10:10:00+00:00",
            },
        )
        self.history.record_adversarial_round(
            execution_id,
            {
                "stage": "uat",
                "round_number": 1,
                "fixer_provider": "Claude",
                "fixer_model": "claude-sonnet-5",
                "tester_provider": "Codex",
                "tester_model": "gpt-5.6-luna",
                "findings_found": 0,
                "findings_fixed": 1,
                "started_at": "2026-01-15T10:20:00+00:00",
            },
        )
        self.history.update(
            execution_id,
            "2026-01-15T11:00:00+00:00",
            adversarial_filed_findings=[
                {"title": "Race in checkout total", "url": "https://github.com/acme/checkout/issues/512"}
            ],
            files_changed=["src/payments/total.rs"],
            final_status="completed",
        )
        self.service().refresh([self.spec()], KnowledgeSettings(), providers=[SwarmExecutionProvider()])
        finding_id = self.store.lookup_id(
            OBJECT_FINDING, "acme/checkout", "uat_finding", "https://github.com/acme/checkout/issues/512"
        )
        execution_obj = self.store.lookup_id(OBJECT_AGENT_EXECUTION, "acme/checkout", "ai_executions", execution_id)
        produced = self.store.traverse(execution_obj, [REL_PRODUCED], depth=1)
        discovered = self.store.traverse(finding_id, [REL_DISCOVERED_BY], depth=1)
        self.assertTrue(produced)
        self.assertTrue(discovered)
        round0 = self.store.lookup_id(
            "agent_round", "acme/checkout", "adversarial_rounds", f"{execution_id}:uat:0"
        )
        # round_id is a uuid; look up by listing
        rounds = self.store.objects_for_repository("acme/checkout", "agent_round")
        self.assertGreaterEqual(len(rounds), 2)
        types = set()
        for item in rounds:
            for edge in self.store.traverse(item["object_id"], [REL_RESPONDED_TO, REL_CORRECTED_BY, REL_VERIFIED_BY], depth=1):
                types.add(edge["relationship_type"])
        self.assertIn(REL_RESPONDED_TO, types)
        self.assertTrue({REL_CORRECTED_BY, REL_VERIFIED_BY} & types)
        _ = round0

    def test_historical_revision_is_preserved_on_update(self) -> None:
        draft = ObjectDraft(
            object_type=OBJECT_COMPONENT,
            repository="acme/checkout",
            title="payments",
            source_kind="component",
            source_ref="payments",
            body="v1 uses Redis",
            summary="v1 uses Redis",
            effective_at="2026-01-01T00:00:00+00:00",
        )
        object_id, created, _ = self.store.upsert_object(draft, "2026-01-01T00:00:00+00:00")
        self.assertTrue(created)
        draft.body = "v2 uses Kafka"
        draft.summary = "v2 uses Kafka"
        self.store.upsert_object(draft, "2026-06-01T00:00:00+00:00")
        current = self.store.get_object(object_id)
        assert current is not None
        self.assertIn("Kafka", current["body"])
        historical = self.store.get_object(object_id, as_of="2026-02-01T00:00:00+00:00")
        assert historical is not None
        self.assertIn("Redis", historical["body"])
        self.assertTrue(historical["historical"])

    def test_repository_scope_isolation(self) -> None:
        start_execution(self.history, repository="acme/checkout", issue_title="Kafka checkout")
        start_execution(
            self.history,
            repository="acme/inventory",
            issue_number=9,
            issue_title="Inventory Redis cache",
            issue_body="Redis only",
        )
        other = RepositorySpec(name="acme/inventory", project_id="acme")
        self.service().refresh(
            [self.spec(), other],
            KnowledgeSettings(),
            providers=[SwarmExecutionProvider()],
        )
        checkout_hits = self.store.search("Kafka", repositories=["acme/checkout"])
        inventory_hits = self.store.search("Kafka", repositories=["acme/inventory"])
        self.assertTrue(any(item["repository"] == "acme/checkout" for item in checkout_hits))
        self.assertFalse(any("Kafka" in (item.get("title") or "") and item["repository"] == "acme/inventory" for item in inventory_hits))

    def test_project_and_org_scope_retrieval(self) -> None:
        start_execution(self.history, issue_title="Kafka producer")
        start_execution(
            self.history,
            repository="acme/inventory",
            issue_number=12,
            issue_title="Kafka consumer for inventory",
            issue_body="This inventory service consumes Kafka topics.",
        )
        inventory_dir = self.root / "inventory"
        inventory_dir.mkdir()
        (inventory_dir / "README.md").write_text("Inventory consumes Kafka.\n", encoding="utf-8")
        self.service().refresh(
            [
                self.spec(),
                RepositorySpec(name="acme/inventory", workspace=str(inventory_dir), project_id="acme"),
            ],
            KnowledgeSettings(),
        )
        hits = self.store.search("Kafka", project_ids=["acme"])
        repos = {item["repository"] for item in hits}
        self.assertIn("acme/checkout", repos)
        self.assertIn("acme/inventory", repos)

    def test_filesystem_and_decision_provenance(self) -> None:
        self.service().refresh(
            [self.spec()],
            KnowledgeSettings(),
            providers=[RepositoryFilesystemProvider()],
        )
        adr = self.store.lookup_id(OBJECT_DECISION, "acme/checkout", "adr", "docs/adr-001-kafka.md")
        self.assertTrue(adr)
        obj = self.store.get_object(adr)
        assert obj is not None
        self.assertEqual(obj["provenance_kind"], PROVENANCE_SOURCE_FACT)
        kafka = self.store.lookup_id(OBJECT_COMPONENT, "acme/checkout", "technology", "kafka")
        self.assertTrue(kafka)
        inferred = self.store.get_object(kafka)
        assert inferred is not None
        self.assertEqual(inferred["provenance_kind"], PROVENANCE_INFERRED)

    def test_ask_swarm_repository_and_org_questions(self) -> None:
        start_execution(self.history)
        self.service().refresh([self.spec()], KnowledgeSettings())
        repo_answer = self.service().ask(
            "Why does this repository use Kafka?",
            scope_kind="repository",
            scope_id="acme/checkout",
            repositories=[self.spec()],
        )
        self.assertIn("Kafka", repo_answer["answer"])
        self.assertTrue(repo_answer["citations"])
        self.assertTrue(all(item["provenanceKind"] for item in repo_answer["citations"]))
        org_answer = self.service().ask(
            "Which of my repositories interact with Kafka?",
            scope_kind="all",
            repositories=[self.spec()],
        )
        self.assertGreaterEqual(len(org_answer["citations"]), 1)

    def test_cost_question_requires_sample_size(self) -> None:
        empty = self.service().ask("How much have similar issues historically cost to implement?")
        self.assertTrue(empty["insufficientData"])
        self.assertIn("sample size 0", empty["answer"].lower())
        execution_id = start_execution(self.history, issue_title="Add authentication cookies")
        with self.history.connect() as database:
            database.execute(
                """INSERT INTO ai_token_usage (
                    id, execution_id, repository, issue_number, provider, model, agent_type,
                    prompt_type, total_tokens, estimated_cost, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "tok-1",
                    execution_id,
                    "acme/checkout",
                    417,
                    "claude",
                    "claude-sonnet-5",
                    "primary",
                    "initial",
                    12000,
                    1.25,
                    "2026-01-15T10:05:00+00:00",
                ),
            )
        self.service().refresh([self.spec()], KnowledgeSettings(), providers=[SwarmExecutionProvider()])
        answer = self.service().ask(
            "How much have similar authentication issues historically cost to implement?",
            repositories=[self.spec()],
        )
        self.assertGreaterEqual(answer["sampleSize"], 1)
        self.assertIn("1.25", answer["answer"])
        self.assertFalse(answer["insufficientData"])

    def test_historical_component_question(self) -> None:
        execution_id = start_execution(self.history, issue_title="Change payment timeout")
        self.history.update(
            execution_id,
            "2026-03-01T00:00:00+00:00",
            files_changed=["src/payments/timeout.rs"],
            changes_summary="Increased payment timeout after checkout retries.",
            final_status="completed",
        )
        self.service().refresh([self.spec()], KnowledgeSettings(), providers=[SwarmExecutionProvider()])
        answer = self.service().ask(
            "What happened the last time we changed the payments component?",
            scope_kind="repository",
            scope_id="acme/checkout",
            repositories=[self.spec()],
        )
        self.assertIn("payment", answer["answer"].lower())

    def test_context_pack_is_bounded_and_recorded(self) -> None:
        start_execution(self.history)
        self.history.update(
            start_execution(self.history, issue_number=512, issue_title="Race in checkout total"),
            "2026-01-16T00:00:00+00:00",
            adversarial_filed_findings=[
                {"title": "Race in checkout total", "url": "https://github.com/acme/checkout/issues/512"}
            ],
            files_changed=["src/payments/total.rs"],
            final_status="completed",
        )
        self.service().refresh([self.spec()], KnowledgeSettings(), providers=[SwarmExecutionProvider()])
        pack = self.service().build_context_pack(
            repository="acme/checkout",
            issue_title="Adjust checkout totals",
            issue_body="Fix rounding in payments.",
            issue_number=900,
            files=["src/payments/total.rs"],
            token_limit=400,
        )
        rendered = pack.render()
        self.assertIn("Engineering Knowledge Context", rendered)
        self.assertLessEqual(estimate_tokens(rendered), 400 + 50)
        self.assertTrue(pack.object_ids)
        with self.store.connect() as database:
            rows = list(database.execute("SELECT * FROM knowledge_context_injections"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["issue_number"]), 900)

    def test_generated_knowledge_respects_enable_disable(self) -> None:
        start_execution(self.history)
        off = self.service().refresh(
            [self.spec()],
            KnowledgeSettings(automatic_generation=False, generate_repository_summaries=True),
        )
        self.assertTrue(off["generation"]["disabled"])
        on = self.service().refresh(
            [self.spec()],
            KnowledgeSettings(
                automatic_generation=True,
                generate_repository_summaries=True,
                generate_architecture_summaries=False,
                generate_engineering_decisions=False,
                generate_component_documentation=False,
                generate_risk_summaries=False,
                generate_issue_clustering=False,
            ),
        )
        self.assertGreaterEqual(on["generation"]["generated"], 1)
        generated = [
            item
            for item in self.store.objects_for_repository("acme/checkout")
            if item["provenance_kind"] == PROVENANCE_GENERATED_SUMMARY
        ]
        self.assertTrue(generated)
        self.assertTrue(all(item.get("metadata", {}).get("generation_date") for item in generated))

    def test_refresh_and_rebuild_and_failure_handling(self) -> None:
        start_execution(self.history)
        first = self.service().refresh([self.spec()], KnowledgeSettings(automatic_generation=True))
        self.assertEqual(first["status"], "success")
        rebuilt = self.service().refresh(
            [self.spec()], KnowledgeSettings(automatic_generation=True), mode="rebuild"
        )
        self.assertEqual(rebuilt["mode"], "rebuild")
        status = self.service().status(KnowledgeSettings())
        self.assertGreaterEqual(status["repositoriesIndexed"], 1)
        self.assertTrue(status["lastRefresh"])

        class BoomProvider(KnowledgeProvider):
            provider_id = "boom"
            display_name = "Boom"

            def collect_objects(self, context, store):
                raise RuntimeError("provider exploded")

        mixed = self.service().refresh(
            [self.spec()],
            KnowledgeSettings(),
            providers=[BoomProvider(), SwarmExecutionProvider()],
        )
        self.assertTrue(any("provider exploded" in err for err in mixed["errors"]))
        self.assertGreaterEqual(mixed["objectsCreated"] + mixed["objectsChanged"], 0)

    def test_provider_abstraction_accepts_external_source(self) -> None:
        class JiraStub(KnowledgeProvider):
            provider_id = "jira"
            display_name = "Jira"

            def collect_objects(self, context, store):
                return [
                    ObjectDraft(
                        object_type=OBJECT_ISSUE,
                        repository="acme/checkout",
                        title="JIRA-9 Timeout",
                        source_kind="jira_issue",
                        source_ref="JIRA-9",
                        source_provider="jira",
                        body="Timeout increased after incident.",
                        search_text="timeout jira",
                    )
                ]

            def collect_relationships(self, context, store):
                return [
                    RelationshipDraft(
                        from_ref=("issue", "acme/checkout", "jira_issue", "JIRA-9"),
                        to_ref=("repository", "acme/checkout", "repository", "acme/checkout"),
                        relationship_type=REL_BELONGS_TO,
                    )
                ]

        register_provider(JiraStub())
        from engineering_knowledge import _PROVIDERS
        self.addCleanup(lambda: _PROVIDERS.pop("jira", None))
        self.assertIn("jira", registered_providers())
        self.service().refresh(
            [self.spec()],
            KnowledgeSettings(),
            providers=[SwarmExecutionProvider(), JiraStub()],
        )
        self.assertTrue(self.store.lookup_id(OBJECT_ISSUE, "acme/checkout", "jira_issue", "JIRA-9"))

    def test_routing_signals_need_sample(self) -> None:
        signals = self.service().routing_signals(
            repository="acme/checkout", issue_title="Auth", issue_body="cookies"
        )
        self.assertEqual(signals, "")
        start_execution(self.history, issue_number=1, issue_title="Auth cookies")
        start_execution(self.history, issue_number=2, issue_title="Auth session")
        with self.history.connect() as database:
            for index, execution in enumerate(
                database.execute("SELECT execution_id FROM ai_executions"), start=1
            ):
                database.execute(
                    """INSERT INTO ai_token_usage (
                        id, execution_id, repository, issue_number, model, total_tokens,
                        estimated_cost, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (f"t{index}", execution[0], "acme/checkout", index, "claude-sonnet-5", 1000, 0.2, "2026-01-15T00:00:00+00:00"),
                )
        self.service().refresh([self.spec()], KnowledgeSettings(), providers=[SwarmExecutionProvider()])
        filled = self.service().routing_signals(
            repository="acme/checkout", issue_title="Auth", issue_body="session cookies"
        )
        self.assertIn("sample size", filled)

    def test_cli_status_and_ask(self) -> None:
        start_execution(self.history)
        payload = {
            "action": "refresh",
            "repositories": [{"name": "acme/checkout", "workspace": str(self.repo_dir), "projectId": "acme"}],
            "settings": {"enabled": True, "automaticGeneration": False},
        }
        refreshed = handle_action(payload, self.db)
        self.assertEqual(refreshed["status"], "success")
        asked = handle_action(
            {
                "action": "ask",
                "question": "Why does this repository use Kafka?",
                "scopeKind": "repository",
                "scopeId": "acme/checkout",
                "repositories": [{"name": "acme/checkout", "workspace": str(self.repo_dir)}],
                "settings": {"enabled": True},
            },
            self.db,
        )
        self.assertIn("Kafka", asked["answer"])
        disabled = handle_action({"action": "ask", "question": "Why Kafka?", "settings": {"enabled": False}}, self.db)
        self.assertIn("turned off", disabled["answer"])
        code = knowledge_main(["--db", str(self.db), "--action", "status", "--payload", json.dumps({"settings": {"enabled": True}})])
        self.assertEqual(code, 0)

    def test_classify_question(self) -> None:
        self.assertEqual(classify_question("How much did similar issues cost?"), "cost")
        self.assertEqual(classify_question("Which of my repositories use Kafka?"), "organization")
        self.assertEqual(classify_question("What happened the last time we changed payments?"), "historical")
        self.assertEqual(classify_question("Why was Kafka chosen instead of REST?"), "decision")
        self.assertEqual(classify_question("What security findings affected checkout?"), "adversarial")

    def test_git_provider_survives_missing_repo(self) -> None:
        context = IndexContext(repositories=[RepositorySpec(name="acme/missing", workspace=str(self.root / "nope"))])
        drafts = GitHistoryProvider().collect_objects(context, self.store)
        self.assertEqual(drafts, [])

    def test_corrupt_metadata_does_not_crash_read(self) -> None:
        object_id, _, _ = self.store.upsert_object(
            ObjectDraft(
                object_type=OBJECT_REPOSITORY,
                repository="acme/checkout",
                title="acme/checkout",
                source_kind="repository",
                source_ref="acme/checkout",
            )
        )
        with self.store.connect() as database:
            database.execute(
                "UPDATE knowledge_objects SET metadata_json = 'not-json' WHERE object_id = ?",
                (object_id,),
            )
        obj = self.store.get_object(object_id)
        assert obj is not None
        self.assertEqual(obj["metadata"], {})

    def test_ask_uses_existing_router_when_answer_fn_supplied(self) -> None:
        start_execution(self.history)
        self.service().refresh([self.spec()], KnowledgeSettings(), providers=[SwarmExecutionProvider()])
        seen: dict[str, object] = {}

        def fake_answer(*, prompt: str, routing: dict, sources: list) -> str:
            seen["prompt"] = prompt
            seen["routing"] = routing
            seen["sources"] = sources
            return "Kafka was selected for decoupling. Sources: retrieved items."

        result = self.service().ask(
            "Why does this repository use Kafka?",
            repositories=[self.spec()],
            routing={"dynamicModelRouting": True, "providers": [{"id": "grok", "enabled": True, "model": "grok-4.6"}]},
            answer_fn=fake_answer,
        )
        self.assertIn("Kafka was selected", result["answer"])
        self.assertIn("Ask SWARM", str(seen.get("prompt")))


class KnowledgeModuleDiscoveryTests(unittest.TestCase):
    def test_adversarial_discover_collects_the_uat_twin_when_issue_worker_is_on_path(self) -> None:
        """Broad tests/adversarial discovery must not abort on this module name."""
        repo = Path(__file__).resolve().parents[1]
        script = r"""
import sys
import unittest
from pathlib import Path
repo = Path(%r).resolve()
sys.path.insert(0, str(repo / "tests" / "adversarial"))
sys.path.insert(0, str(repo / "issue_worker"))
loader = unittest.TestLoader()
suite = loader.discover(str(repo / "tests" / "adversarial"), pattern="test_engineering_knowledge.py")
if loader.errors:
    raise SystemExit("".join(loader.errors))
print("COUNT", suite.countTestCases())
""" % (str(repo),)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertRegex(output, r"COUNT [1-9]\d*")
        self.assertNotIn("incorrectly imported", output)


if __name__ == "__main__":
    unittest.main()
