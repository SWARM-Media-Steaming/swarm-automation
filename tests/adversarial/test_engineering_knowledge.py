"""Issue #291 UAT: Engineering Knowledge reuses existing records and cites sources."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

from ai_execution_history import ExecutionHistoryRepository, ExecutionStart  # noqa: E402
from engineering_knowledge import (  # noqa: E402
    KnowledgeService,
    KnowledgeSettings,
    RepositorySpec,
    SwarmExecutionProvider,
)


class EngineeringKnowledgeUat(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="swarm-knowledge-uat.")
        self.root = Path(self.temporary.name)
        self.db = self.root / "swarm-automation.sqlite3"
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        (self.workspace / "README.md").write_text(
            "Checkout uses Kafka instead of synchronous APIs.\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_acceptance_scenarios_use_existing_execution_history(self) -> None:
        history = ExecutionHistoryRepository(self.db)
        execution_id = history.create(
            ExecutionStart(
                repository="acme/checkout",
                issue_number=417,
                issue_url="https://github.com/acme/checkout/issues/417",
                issue_title="Use Kafka for checkout events",
                issue_body="We chose Kafka instead of synchronous APIs.",
                provider="Claude",
                model="claude-sonnet-5",
                effort="high",
                branch_name="ai/claude/issue-417",
                application_version="1.0.0",
            ),
            "2026-01-15T10:00:00+00:00",
        )
        history.update(
            execution_id,
            "2026-01-15T11:00:00+00:00",
            files_changed=["src/payments/kafka.rs"],
            adversarial_filed_findings=[
                {"title": "Race in checkout total", "url": "https://github.com/acme/checkout/issues/512"}
            ],
            final_status="completed",
        )
        with history.connect() as database:
            database.execute(
                """INSERT INTO ai_token_usage (
                    id, execution_id, repository, issue_number, model, total_tokens,
                    estimated_cost, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("tok-1", execution_id, "acme/checkout", 417, "claude-sonnet-5", 8000, 0.9, "2026-01-15T10:05:00+00:00"),
            )
        service = KnowledgeService(self.db)
        spec = RepositorySpec(name="acme/checkout", workspace=str(self.workspace), project_id="acme")
        service.refresh([spec], KnowledgeSettings(), providers=[SwarmExecutionProvider()])
        repo_answer = service.ask(
            "Why does this repository use Kafka?",
            scope_kind="repository",
            scope_id="acme/checkout",
            repositories=[spec],
        )
        self.assertIn("Kafka", repo_answer["answer"])
        self.assertTrue(repo_answer["citations"])
        org_answer = service.ask(
            "Which of my repositories interact with Kafka?",
            scope_kind="all",
            repositories=[spec],
        )
        self.assertTrue(org_answer["citations"])
        cost = service.ask(
            "How much have similar Kafka issues historically cost to implement?",
            repositories=[spec],
        )
        self.assertGreaterEqual(cost["sampleSize"], 1)
        pack = service.build_context_pack(
            repository="acme/checkout",
            issue_title="Change checkout payments",
            issue_body="Touch Kafka producer",
            files=["src/payments/kafka.rs"],
        )
        self.assertTrue(pack.object_ids)


if __name__ == "__main__":
    unittest.main()
