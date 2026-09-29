"""Issue #299: Jev's typed response contract must fail safe, not fail silent.

Oracle is issue #299 section 2, derived before reading the diff: "Validate
all Jev responses before using them. Invalid or low-confidence output must
never directly control an irreversible workflow action." Section 8 adds:
"Never allow a low-confidence Jev result to suppress a security finding."

decision_engine.build_jev_request offers Jev human-readable criteria labels
for "score" questions (e.g. security_risk: ["none","low","moderate","high",
"critical"]) and "noul" questions (e.g. cyber_recommended: true="Cyber
recommended", false="Cyber not needed..."). A response that answers with one
of those exact offered labels, rather than a synthesized bare 0..1 float or
a bare true/false/yes/no, must not be silently reinterpreted as the safest-
looking (lowest-risk / not-recommended) answer at full confidence — that
defeats validation entirely rather than triggering it. This is not a
hypothetical shape: it is the exact label text this app's own request
builder offers Jev as the criteria to choose from.

Contrast with the "choice" question path (task_type, rag_scope, action),
which already degrades safely: an unrecognized label falls back to a
conservative default (UNKNOWN / HUMAN_REVIEW / INCOMPLETE) via
decision_engine._enum_value. The score/noul paths have no such safety net —
they silently produce a concrete, wrong-polarity value that is
indistinguishable from a correctly parsed one.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ISSUE_WORKER = Path(__file__).resolve().parents[3] / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

from decision_engine import DecisionType, interpret_jev_response  # noqa: E402
from jev_cli import JevResponse  # noqa: E402


class ScoreAnswerContractTests(unittest.TestCase):
    def test_a_critical_security_risk_label_is_not_silently_read_as_zero_risk(self) -> None:
        response = JevResponse(
            answers={
                "task_type": {"value": "SECURITY", "confidence": 0.95},
                "complexity": {"value": 0.5, "confidence": 0.9},
                # Jev answers with the exact criteria label this app offered
                # ("criteria": ["none","low","moderate","high","critical"])
                # instead of a synthesized 0..1 float.
                "security_risk": {"value": "critical", "confidence": 0.97},
                "cross_repo": {"value": 0.1},
            }
        )
        result = interpret_jev_response(DecisionType.ISSUE_TRIAGE.value, {}, response)
        security_risk = result.scores.get("securityRisk")
        self.assertIsNotNone(security_risk, "a 'critical' answer must not vanish from scores entirely")
        self.assertGreater(
            security_risk,
            0.5,
            "a confident 'critical' security_risk answer must not be silently read as "
            f"near-zero risk; got securityRisk={security_risk!r}. The label was accepted "
            "as though it were a valid low score instead of being rejected as malformed "
            "(triggering fallback) or mapped from its position in the offered criteria.",
        )

    def test_an_extreme_complexity_label_is_not_silently_read_as_trivial(self) -> None:
        response = JevResponse(
            answers={
                "task_type": {"value": "ARCHITECTURE_REFACTOR", "confidence": 0.9},
                # criteria offered: trivial/simple/standard/complex/very_complex/extreme
                "complexity": {"value": "extreme", "confidence": 0.93},
                "security_risk": {"value": 0.2, "confidence": 0.9},
                "cross_repo": {"value": 0.9},
            }
        )
        result = interpret_jev_response(DecisionType.TASK_CLASSIFICATION.value, {}, response)
        complexity = result.scores.get("complexity")
        self.assertIsNotNone(complexity)
        self.assertGreater(
            complexity,
            0.5,
            "an 'extreme' complexity answer must not be silently read as near-zero complexity "
            f"(got {complexity!r}); a mislabelled trivial-complexity reading can route this "
            "task to an underpowered cheap model under cost-first routing.",
        )


class NoulAnswerContractTests(unittest.TestCase):
    def test_echoed_cyber_recommended_label_is_not_silently_read_as_not_recommended(self) -> None:
        # build_jev_request offers exactly this label for the true branch:
        # "true": "Cyber recommended".
        response = JevResponse(
            answers={
                "task_type": {"value": "SECURITY", "confidence": 0.95},
                "complexity": {"value": 0.7, "confidence": 0.9},
                "security_risk": {"value": 0.9, "confidence": 0.95},
                "cross_repo": {"value": 0.1},
                "cyber_recommended": {"value": "Cyber recommended", "confidence": 0.93},
            }
        )
        result = interpret_jev_response(DecisionType.TASK_CLASSIFICATION.value, {}, response)
        self.assertTrue(
            result.metadata.get("cyberRecommended"),
            "Jev explicitly answered with its own offered 'Cyber recommended' label on a "
            "SECURITY-classified issue, but the typed result silently flipped it to "
            "not-recommended instead of failing safe. A downstream consumer of this signal "
            "would read 'Cyber not needed' from an issue Jev itself flagged as needing it.",
        )

    def test_echoed_uat_recommended_label_is_not_silently_read_as_not_recommended(self) -> None:
        response = JevResponse(
            answers={
                "task_type": {"value": "BUG", "confidence": 0.9},
                "complexity": {"value": 0.4, "confidence": 0.9},
                "security_risk": {"value": 0.1, "confidence": 0.9},
                "cross_repo": {"value": 0.1},
                # build_jev_request offers exactly this label for true.
                "uat_recommended": {"value": "UAT recommended", "confidence": 0.9},
            }
        )
        result = interpret_jev_response(DecisionType.ISSUE_TRIAGE.value, {}, response)
        self.assertTrue(
            result.metadata.get("uatRecommended"),
            "an explicit 'UAT recommended' answer, using this app's own offered label text, "
            "must not silently become False",
        )

    def test_echoed_cross_repo_true_label_is_not_silently_read_as_single_repo(self) -> None:
        # build_jev_request offers exactly this noul true label:
        # "true": "Cross-repository work".
        # Unlike uat/cyber, the pre-flight path stores this as a probability
        # score (crossRepoProbability). An offered-label answer must not be
        # float-parsed into 0.0 — that inverts "multiple repositories are
        # likely involved" into "they are not" at full confidence.
        response = JevResponse(
            answers={
                "task_type": {"value": "ARCHITECTURE_REFACTOR", "confidence": 0.94},
                "complexity": {"value": 0.81, "confidence": 0.9},
                "security_risk": {"value": 0.34, "confidence": 0.9},
                "cross_repo": {"value": "Cross-repository work", "confidence": 0.93},
            }
        )
        result = interpret_jev_response(DecisionType.TASK_CLASSIFICATION.value, {}, response)
        cross_repo = result.scores.get("crossRepoProbability")
        self.assertIsNotNone(
            cross_repo,
            "a 'Cross-repository work' answer must not vanish from scores",
        )
        self.assertGreater(
            cross_repo,
            0.5,
            "Jev answered with its own offered 'Cross-repository work' label, but "
            f"crossRepoProbability={cross_repo!r} reads as single-repository. "
            "Section 3 requires this signal before model selection; a silent 0.0 "
            "hides a multi-repo task from the router and RAG-scope path.",
        )

    def test_echoed_tests_needed_label_is_not_silently_read_as_tests_unlikely(self) -> None:
        response = JevResponse(
            answers={
                "task_type": {"value": "FEATURE", "confidence": 0.9},
                "complexity": {"value": 0.5, "confidence": 0.9},
                "security_risk": {"value": 0.2, "confidence": 0.9},
                "testing_likely": {"value": "Tests needed", "confidence": 0.91},
            }
        )
        result = interpret_jev_response(DecisionType.ISSUE_TRIAGE.value, {}, response)
        self.assertTrue(
            result.metadata.get("testingLikely"),
            "an explicit 'Tests needed' answer, using this app's own offered noul "
            "label, must not silently become False",
        )


class InvalidScoreContractTests(unittest.TestCase):
    def test_unrecognized_security_risk_label_does_not_keep_high_confidence_zero_risk(self) -> None:
        """Issue #299 §2: validate Jev responses before using them.

        A string that is not a number and not one of the offered criteria
        (none/low/moderate/high/critical) is invalid. Treating it as a
        concrete 0.0 score, or omitting it while keeping automation-grade
        confidence, is indistinguishable from Jev reporting no security
        sensitivity — the same silent inversion the 'critical' label test
        exists to prevent, for a synonym Jev may emit instead of the
        offered token.
        """
        response = JevResponse(
            answers={
                "task_type": {"value": "SECURITY", "confidence": 0.96},
                "complexity": {"value": 0.6, "confidence": 0.9},
                "security_risk": {"value": "SEVERE", "confidence": 0.97},
                "cross_repo": {"value": 0.1},
            }
        )
        result = interpret_jev_response(DecisionType.ISSUE_TRIAGE.value, {}, response)
        security_risk = result.scores.get("securityRisk")
        mapped_high = security_risk is not None and security_risk > 0.5
        rejected = result.confidence < 0.70
        self.assertTrue(
            mapped_high or rejected,
            "unrecognized security_risk label 'SEVERE' was accepted as a "
            f"high-confidence result (confidence={result.confidence!r}) with "
            f"securityRisk={security_risk!r}. Invalid score fields must fail "
            "closed (fallback / low confidence) or map conservatively to a "
            "high-risk reading; they must not drop the security signal.",
        )


class CostFirstDefaultContractTests(unittest.TestCase):
    def test_route_request_defaults_to_cost_first_per_its_own_module_contract(self) -> None:
        """model_router.py's own module docstring (added for issue #299,
        acceptance criteria #32-35) states: "RouteRequest.cost_consideration_
        enabled defaults to on. Passing False is only for isolated
        scoring-formula tests of the non-cost weight set; it is not a
        user-facing routing mode." The dataclass default must match that
        documented contract: automatic routing is supposed to be cost-first
        everywhere, not opt-in per call site. Every production call site
        happens to pass the flag explicitly today, but a future or
        overlooked caller that forgets the explicit override would silently
        regress to quality-only, cost-blind routing with no test catching it,
        directly violating "No automatic route selects a faster or preferred
        model over a cheaper adequately capable model" (acceptance #35).
        """
        from model_router import RouteRequest

        request = RouteRequest(task_type="general_reasoning", complexity="STANDARD")
        self.assertTrue(
            request.cost_consideration_enabled,
            "RouteRequest.cost_consideration_enabled defaults to False, contradicting "
            "the module's own documented contract ('defaults to on') and issue #299's "
            "cost-first-is-the-only-automatic-mode requirement.",
        )


if __name__ == "__main__":
    unittest.main()
