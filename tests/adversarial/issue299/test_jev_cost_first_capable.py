"""Issue #299: among adequately capable models, lowest cost wins.

AC 19 / 33 / 35: after capability, expected-success, safety, and context-fit
gates, automatic routing prefers the lowest estimated total cost. Latency,
provider preference, and extra capability must not beat a cheaper model that
already clears those gates.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ISSUE_WORKER = Path(__file__).resolve().parents[3] / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

from model_router import RouteRequest, RoutingAvailability, route  # noqa: E402
from test_model_router import _fixture_model  # noqa: E402


class CostFirstAmongCapableTests(unittest.TestCase):
    def test_cheaper_adequate_model_beats_more_expensive_overqualified_peer(self) -> None:
        """Both models clear STANDARD's min_capability (3). The expensive one
        is two capability steps stronger and faster. Cost-first still has to
        pick the cheaper adequate model; extra capability is not a license to
        spend more (AC 19, 35).
        """
        cheap_adequate = _fixture_model(
            "cheap-adequate",
            capability=3,
            cost=1,
            token_efficiency=3,
            latency=1,
            efforts=("medium",),
        )
        expensive_strong = _fixture_model(
            "expensive-strong",
            capability=5,
            cost=5,
            token_efficiency=3,
            latency=5,
            efforts=("medium",),
        )
        decision = route(
            RouteRequest("general_reasoning", "STANDARD", cost_consideration_enabled=True),
            catalog=[cheap_adequate, expensive_strong],
        )
        self.assertEqual(
            decision.model,
            "cheap-adequate",
            "automatic routing picked the more expensive overqualified model "
            f"{decision.model!r} over a cheaper model that already meets the "
            "STANDARD capability / expected-success gate. Latency and extra "
            "capability must not override cost-first among adequately capable "
            "candidates.",
        )

    def test_preferred_provider_cannot_override_a_cheaper_capable_model(self) -> None:
        cheap = _fixture_model(
            "cheap-claude",
            capability=4,
            cost=1,
            token_efficiency=3,
            latency=3,
            efforts=("medium",),
            provider="anthropic",
            agent="claude",
        )
        expensive = _fixture_model(
            "expensive-codex",
            capability=4,
            cost=5,
            token_efficiency=3,
            latency=5,
            efforts=("medium",),
            provider="openai",
            agent="codex",
        )
        decision = route(
            RouteRequest("general_reasoning", "STANDARD", cost_consideration_enabled=True),
            catalog=[cheap, expensive],
            availability=RoutingAvailability(preferred_provider="openai"),
        )
        self.assertEqual(
            decision.model,
            "cheap-claude",
            "preferred_provider selected the more expensive equally capable "
            f"model {decision.model!r}. AC 35: provider preference is a "
            "tie-breaker only and must not override a cheaper adequately "
            "capable candidate.",
        )

    def test_latency_sensitive_flag_cannot_pick_a_pricier_capable_model(self) -> None:
        """Even if a caller sets latency_sensitive, automatic cost-first still
        owns the ranking among models that already pass the gates. Latency
        may only break a cost tie (AC 19 / 35).
        """
        cheap_slow = _fixture_model(
            "cheap-slow",
            capability=4,
            cost=1,
            token_efficiency=3,
            latency=1,
            efforts=("medium",),
        )
        expensive_fast = _fixture_model(
            "expensive-fast",
            capability=4,
            cost=5,
            token_efficiency=3,
            latency=5,
            efforts=("medium",),
        )
        decision = route(
            RouteRequest(
                "general_reasoning",
                "STANDARD",
                cost_consideration_enabled=True,
                latency_sensitive=True,
            ),
            catalog=[cheap_slow, expensive_fast],
        )
        self.assertEqual(
            decision.model,
            "cheap-slow",
            "latency_sensitive=True let the faster expensive model "
            f"{decision.model!r} beat a cheaper equally capable peer. "
            "Speed must not override cost-first after the capability gate.",
        )


if __name__ == "__main__":
    unittest.main()
