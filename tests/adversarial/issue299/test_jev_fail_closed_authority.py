"""Issue #299 fail-closed edges the first adversarial pass did not pin.

Oracle is the issue text, not the current worker:

- Invalid or low-confidence Jev output never controls an irreversible action
  (section 2 / 12). COMPLETE is irreversible.
- A --version success is connectivity, not authentication (section 1).
- Authentication failures must not be retried, including API-key spellings
  the CLI actually emits (section 1 / 13).
- Section 9: a single low RAG score must not permanently drop potentially
  critical context just because three other chunks already cleared the
  relevance threshold.
- The request file sent to the Jev CLI must not embed process credentials.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from harness import SECRET_TOKEN, JevWorkerFixture, pack_with
from decision_engine import CompletionVerdict, DecisionType, swarm_policy_action
from jev_cli import JevCli, JevError, JevSettings


class LowConfidenceCompleteTests(JevWorkerFixture, unittest.TestCase):
    def test_policy_band_complete_cannot_override_a_conservative_swarm_default(self) -> None:
        """Section 12: >= 0.90 is the floor for normal automated continuation.

        0.80 is the policy band. COMPLETE is irreversible. When Swarm's own
        default is not COMPLETE, Jev must not promote the gate to COMPLETE
        by echoing its recommendation through default_action.
        """
        self.bind_issue()
        self.install_jev_engine(
            lambda *_a, **_k: self.decision(
                decision_type=DecisionType.COMPLETION.value,
                decision=CompletionVerdict.COMPLETE.value,
                confidence=0.80,
            )
        )
        action = self.worker.evaluate_completion_gate(
            failed_tests=False,
            blocking_security=False,
            default=CompletionVerdict.NEEDS_HUMAN_REVIEW.value,
        )
        payload = self.worker.evaluate_decision(
            "COMPLETION",
            {"failed_tests": False, "blocking_security": False},
        )
        self.assertNotEqual(
            action,
            CompletionVerdict.COMPLETE.value,
            "evaluate_completion_gate returned COMPLETE for a 0.80-confidence Jev "
            "COMPLETE while Swarm's default was NEEDS_HUMAN_REVIEW. Section 12 "
            "reserves automated continuation for >= 0.90.",
        )
        self.assertNotEqual(
            payload.get("swarmAction"),
            CompletionVerdict.COMPLETE.value,
            "evaluate_decision with no default_action set swarmAction=COMPLETE at "
            "confidence 0.80. Filling default from Jev's own irreversible "
            "recommendation lets low-confidence output control the action.",
        )

    def test_swarm_policy_does_not_echo_unactionable_complete(self) -> None:
        settings = JevSettings(enabled=True)
        result = self.decision(
            decision_type=DecisionType.COMPLETION.value,
            decision=CompletionVerdict.COMPLETE.value,
            confidence=0.80,
        )
        action = swarm_policy_action(result, settings=settings, default="")
        self.assertNotEqual(
            action,
            CompletionVerdict.COMPLETE.value,
            "swarm_policy_action echoed COMPLETE when default was empty "
            f"(confidence={result.confidence}). A missing conservative fallback "
            "turns 'not actionable' into the irreversible action itself.",
        )


class HealthAndCredentialTests(unittest.TestCase):
    def test_version_success_without_credentials_is_sign_in_required(self) -> None:
        """Section 1: connectivity/health check must not treat --version as auth.

        The AI Configuration connection pill distinguishes 'Sign-in required'
        from 'Connected'. If health() sets authenticated=True whenever
        `jev --version` exits 0, the UI reports Connected with no key present.
        """

        def runner(command, timeout, stdin):
            self.assertIn(command[1:], (["--version"], ["eval", "--help"]))
            return SimpleNamespace(stdout="jev 0.1.0\n", stderr="", returncode=0)

        cli = JevCli(JevSettings(enabled=True, bin="/usr/bin/jev"), runner=runner)
        cli.bin_path = "/usr/bin/jev"
        with mock.patch("jev_cli.jev_auth_present", return_value=False):
            health = cli.health()
        self.assertFalse(
            health["authenticated"],
            "health() reported authenticated=True from a successful --version "
            "despite jev_auth_present()=False. Version reachability is not a "
            f"credential. health={health!r}",
        )
        self.assertEqual(
            health["status"],
            "sign_in_required",
            f"expected sign_in_required when the binary runs but no credential "
            f"is present; got status={health.get('status')!r}",
        )

    def test_api_hyphen_key_failure_is_authentication_and_is_not_retried(self) -> None:
        """CLIs commonly say 'invalid API-key' / 'API_KEY rejected' without
        the exact substring 'api key'. Those are still auth failures: retrying
        them burns the retry budget and can look like a flaky timeout.
        """
        calls = {"n": 0}

        def runner(command, timeout, stdin):
            calls["n"] += 1
            return SimpleNamespace(
                stdout="",
                stderr=f"invalid API-KEY for this account ({SECRET_TOKEN})",
                returncode=1,
            )

        cli = JevCli(
            JevSettings(enabled=True, bin="/usr/bin/jev", max_retries=2),
            runner=runner,
            sleeper=lambda _delay: None,
        )
        cli.bin_path = "/usr/bin/jev"
        with self.assertRaises(JevError) as raised:
            cli.ask(state={"title": "x"}, questions={"q": {}})
        self.assertEqual(calls["n"], 1, "authentication-shaped failures must not be retried")
        self.assertEqual(
            raised.exception.error_type,
            "authentication",
            f"stderr 'invalid API-KEY' was classified as {raised.exception.error_type!r}; "
            "authentication failures must fail closed, not as a generic retryable exit.",
        )
        self.assertNotIn(SECRET_TOKEN, str(raised.exception))

    def test_cli_request_file_does_not_embed_process_credentials(self) -> None:
        captured = {}

        def runner(command, timeout, stdin):
            self.assertEqual(command[1], "eval")
            request_path = Path(command[command.index("--file") + 1])
            captured["blob"] = request_path.read_text(encoding="utf-8")
            return SimpleNamespace(
                stdout=json.dumps({"answers": {"task_type": {"value": "BUG", "confidence": 0.9}}}),
                stderr="",
                returncode=0,
            )

        env = {
            **os.environ,
            "JEV_API_KEY": SECRET_TOKEN,
            "TYPESAFE_API_KEY": SECRET_TOKEN,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cli = JevCli(
                JevSettings(enabled=True, bin="/usr/bin/jev", max_retries=0),
                runner=runner,
            )
            cli.bin_path = "/usr/bin/jev"
            cli.ask(
                state={"title": "Fix login", "repository": "acme/app"},
                questions={"task_type": {"type": "choice"}},
            )
        self.assertIn("blob", captured)
        self.assertNotIn(
            SECRET_TOKEN,
            captured["blob"],
            "the Jev CLI request JSON embedded a process credential. Section 1 / 31: "
            "never persist or transmit secrets in the structured request.",
        )


class RagFallbackBeyondThreeChunksTests(JevWorkerFixture, unittest.TestCase):
    def test_low_scored_security_incident_is_kept_when_three_other_chunks_rank_higher(self) -> None:
        """Section 9 forbids discarding potentially critical context solely on
        a single low score. Keeping 'top 3 above threshold' still drops a
        historical security incident once three other chunks already qualify.

        The issue's own example names that incident as context worth scoring.
        """
        self.bind_issue()

        def evaluate(kind, context):
            if kind == DecisionType.RAG_SCOPE.value:
                return self.decision(
                    decision_type=kind, decision="ORGANIZATION", confidence=0.94
                )
            title = str((context or {}).get("candidate") or "")
            if title == "Architecture document":
                relevance = 0.94
            elif title == "Previous GitHub issue":
                relevance = 0.88
            elif "security incident" in title.lower():
                relevance = 0.08
            else:
                relevance = 0.40
            return self.decision(
                decision_type=DecisionType.CONTEXT_RELEVANCE.value,
                decision="KEEP" if relevance >= 0.35 else "LOW",
                confidence=0.9,
                scores={"relevance": relevance},
            )

        self.install_jev_engine(evaluate)
        pack = pack_with(
            {"title": "Architecture document", "summary": "pipeline"},
            {"title": "Previous GitHub issue", "summary": "related work"},
            {"title": "Unrelated repository README", "summary": "readme"},
            {"title": "Historical security incident", "summary": "incident"},
        )
        kept = self.worker.score_knowledge_pack(pack)
        titles = [str(item.get("title") or "") for item in kept.items]
        self.assertIn(
            "Historical security incident",
            titles,
            "Jev scored the historical security incident 0.08 while three other "
            "chunks ranked higher. Section 9: a single low score must not "
            f"permanently discard potentially critical context. kept={titles!r}",
        )


if __name__ == "__main__":
    unittest.main()
