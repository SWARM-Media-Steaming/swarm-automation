"""Security regression for issue #328's critical RAG retention boundary."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[3]
ISSUE_WORKER = ROOT / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

from decision_engine import DecisionType  # noqa: E402
from engineering_knowledge import (  # noqa: E402
    DEFAULT_CONTEXT_TOKEN_LIMIT,
    KnowledgeContextPack,
)
from jev_cli import JevSettings  # noqa: E402
from swarm_issue_worker import Worker  # noqa: E402


class _KnowledgeScorer:
    score_knowledge_pack = Worker.score_knowledge_pack
    _is_security_context = staticmethod(Worker._is_security_context)

    def __init__(self) -> None:
        self.config = SimpleNamespace(jev=JevSettings(enabled=True, use_rag=True))

    def evaluate_decision(self, kind: str, context: dict) -> dict:
        if kind == DecisionType.RAG_SCOPE.value:
            return {"decision": "REPOSITORY"}
        title = str(context.get("candidate") or "")
        return {
            "decision": "KEEP",
            "scores": {"relevance": 0.01 if title == "Prior production finding" else 0.90},
        }


class CriticalSecurityRetentionTests(unittest.TestCase):
    def test_token_bounding_cannot_discard_security_finding_before_jev_scores_it(self) -> None:
        critical = {
            "title": "Prior production finding",
            "summary": "Known login bypass permits administrator impersonation.",
            "object_type": "finding",
            "repository": "acme/app",
            "source_kind": "security_finding",
            "metadata": {"stage": "security"},
        }
        ordinary = [
            {
                "title": f"Attacker-controlled matching issue {index}",
                "summary": "A" * 400,
                "object_type": "issue",
                "repository": "acme/app",
                "source_kind": "github_issue",
            }
            for index in range(22)
        ]
        pack = KnowledgeContextPack(
            [*ordinary, critical],
            token_limit=DEFAULT_CONTEXT_TOKEN_LIMIT,
        )

        # KnowledgeService.build_context_pack() currently performs this render
        # before Worker.score_knowledge_pack(), and render mutates pack.items.
        pack.render()
        _KnowledgeScorer().score_knowledge_pack(pack)
        final_prompt_context = pack.render()

        self.assertIn(
            critical["title"],
            final_prompt_context,
            "the pre-Jev token pass removed the known security finding before "
            "the provenance retention policy could inspect it",
        )


if __name__ == "__main__":
    unittest.main()
