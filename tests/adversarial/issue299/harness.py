"""Issue #299 adversarial fixtures. No live Jev binary, network, or GitHub."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
ISSUE_WORKER = ROOT / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

import test_swarm_issue_worker as fixtures  # noqa: E402
from ai_execution_history import ExecutionHistoryService  # noqa: E402
from decision_engine import (  # noqa: E402
    CompositeDecisionEngine,
    DecisionResult,
    DecisionType,
    Source,
)
from jev_cli import JevSettings  # noqa: E402
from swarm_issue_worker import IssueContext, ProviderChoice  # noqa: E402

SECRET_TOKEN = "sk-jev-adversarial-secret-token-value"


class JevWorkerFixture:
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def enable_history(self) -> Path:
        db = self.state / "history.sqlite3"
        self.worker.config = dataclasses.replace(
            self.worker.config,
            ai_execution_history_enabled=True,
            execution_history_db=db,
        )
        self.worker.history = ExecutionHistoryService(True, db)
        return db

    def bind_issue(self, *, number: int = 299, title: str = "Wire Jev as a decision engine") -> None:
        self.worker.issue = IssueContext(
            number,
            title,
            "Acceptance: Jev recommends; Swarm owns execution authority.",
            ["enhancement"],
            f"https://example.invalid/issues/{number}",
        )
        self.worker.choice = ProviderChoice("Claude", "claude-haiku-4-5", "low", "fixture-session")

    def enable_jev(self, **overrides) -> JevSettings:
        settings = dataclasses.replace(
            JevSettings(enabled=True, bin="/tmp/jev-fixture"),
            **overrides,
        )
        self.worker.config = dataclasses.replace(self.worker.config, jev=settings)
        self.worker._decision_engine = None
        return settings

    def install_jev_engine(self, side_effect) -> mock.Mock:
        settings = self.enable_jev()
        jev = mock.Mock()
        jev.evaluate.side_effect = side_effect
        self.worker._decision_engine = CompositeDecisionEngine(settings, jev=jev)
        return jev

    def decision(
        self,
        *,
        decision_type: str = DecisionType.WORKFLOW.value,
        decision: str = "CONTINUE",
        confidence: float = 0.96,
        source: str = Source.JEV.value,
        **kwargs,
    ) -> DecisionResult:
        return DecisionResult(
            decision_type=decision_type,
            decision=decision,
            confidence=confidence,
            source=source,
            **kwargs,
        )


def pack_with(*items: dict) -> SimpleNamespace:
    return SimpleNamespace(items=list(items), metadata={})
