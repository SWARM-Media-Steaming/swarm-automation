"""Issue #280 (Per-Prompt AI Token Usage Tracking) — ``estimate_cost``
double-charges cached input tokens for OpenAI-shaped usage (Codex and Grok),
sometimes making a fully-cached call cost *more* than an equivalent call with
no caching at all.

Derived expectation, from the issue spec and domain knowledge, before looking
at the implementation:

- Every major provider bills a cache *hit* at a steep discount off the fresh
  input rate (that is the entire commercial point of prompt caching). A
  correct cost estimator must therefore never report a higher cost for a
  request than the same request would cost with zero cache credit.
- The two provider families this codebase supports use two different wire
  semantics for what "cached" means relative to "input":
    * Anthropic (Claude): ``usage.input_tokens`` is *fresh* input only.
      ``cache_creation_input_tokens``/``cache_read_input_tokens`` are
      additional tokens processed on top of it, billed themselves (a cache
      write is a real, metered operation; a cache read is heavily
      discounted). Summing input + cache + output for a *token count* total
      is correct, and charging input at full rate plus cache at a separate
      rate on top is also correct.
    * OpenAI-compatible (Codex, and this codebase's Grok CLI, which the
      module's own comment says is "OpenAI-compatible"): ``input_tokens``/
      ``prompt_tokens`` already *include* whatever was served from cache;
      ``input_tokens_details.cached_tokens`` is a breakdown of that same
      total, not an addition. Billing must charge the *non-cached* remainder
      at full price and the cached remainder at the discount — never charge
      the discounted rate a second time on top of an input figure that
      already contains those tokens.

``issue_worker/token_usage.py`` itself documents this distinction correctly,
in two different places:
  - ``normalize_claude_usage``: "Anthropic bills both cache counters *in
    addition to* input_tokens ... so both are folded into cached_input_tokens
    and added into the computed total".
  - ``_openai_style_usage``: "input_tokens/prompt_tokens here already
    *include* any cached tokens — the *_details.cached_tokens figure is a
    breakdown, not an addition".

But ``estimate_cost`` ignores that distinction entirely: it applies the same
formula — ``input_tokens * input_rate + cached_input_tokens * input_rate *
0.1 + output_tokens * output_rate`` — to every ``NormalizedUsage`` regardless
of which provider produced it. For Claude usage that formula matches the
additive semantics and is correct. For Codex/Grok usage, where
``cached_input_tokens`` is already inside ``input_tokens``, it charges those
tokens twice: once at full price (as part of ``input_tokens``) and again at
the "discount" rate on top — which, since the discount factor (0.1) is
positive, can only ever *raise* the total versus not having identified any
cache hit at all. A fully cached Codex/Grok call therefore costs *more* per
this formula than an entirely uncached call with the same reported
``input_tokens``, which is the opposite of what prompt caching is for and
directly contradicts issue #280 item 6's "Account for different pricing...
for ... Cached input" requirement (there is no such accounting here — cached
tokens make the estimate worse, not better).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

from token_usage import (  # noqa: E402
    NormalizedUsage,
    estimate_cost,
    normalize_claude_usage,
    normalize_codex_usage,
    normalize_grok_usage,
)

# claude-sonnet-5 is rank 3 in dynamic_router's catalog: $1.00/M input,
# $5.00/M output. The exact rank/rate is irrelevant to the bug (it reproduces
# at every rank); a real, mapped model name is used so the test exercises the
# actual `dynamic_router.model_cost` lookup `estimate_cost` depends on,
# rather than asserting against an invented rate table.
MODEL = "claude-sonnet-5"


def _uncached_baseline_cost(usage: NormalizedUsage) -> float:
    """What ``estimate_cost`` would charge for the same input/output tokens
    if no portion of the input were ever identified as cached — the floor a
    correct cache-aware estimate must never exceed."""
    zero_cache = NormalizedUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=0,
        total_tokens=usage.total_tokens,
    )
    cost = estimate_cost(MODEL, zero_cache)
    assert cost is not None
    return cost


class CachedCostNeverExceedsUncachedBaselineTests(unittest.TestCase):
    """A cache hit is a discount. Whatever the provider's own wire semantics
    for what "cached" tokens count against, identifying part of a call's
    input as cached must never make the estimated cost of that exact call
    higher than treating it as if nothing were cached."""

    def test_fully_cached_codex_call_must_not_cost_more_than_fully_uncached(self) -> None:
        # A Codex turn that resent a large, previously-cached context: all
        # 1,000,000 reported input tokens were served from cache, per OpenAI's
        # own reporting convention where `input_tokens` already contains the
        # cached count. `output_tokens=100_000` is real, uncached generation.
        events = [
            {"type": "thread.started", "thread_id": "t-1"},
            {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1_000_000,
                        "output_tokens": 100_000,
                        "input_tokens_details": {"cached_tokens": 1_000_000},
                    }
                },
            },
        ]
        raw = "\n".join(json.dumps(event) for event in events)
        usage = normalize_codex_usage(raw)
        self.assertIsNotNone(usage)
        # Confirm the fixture actually landed on the subset semantics the
        # bug analysis above depends on: cached tokens counted inside input.
        self.assertEqual(usage.input_tokens, 1_000_000)
        self.assertEqual(usage.cached_input_tokens, 1_000_000)

        cost = estimate_cost(MODEL, usage)
        baseline = _uncached_baseline_cost(usage)
        self.assertIsNotNone(cost)
        self.assertLessEqual(
            cost,
            baseline,
            f"A 100% cache-hit Codex call was estimated at ${cost:.6f}, more than the "
            f"${baseline:.6f} the same input/output tokens would cost with no cache "
            "credit at all — estimate_cost is double-charging cached_input_tokens that "
            "are already counted inside input_tokens for OpenAI-shaped usage.",
        )

    def test_fully_cached_grok_call_must_not_cost_more_than_fully_uncached(self) -> None:
        # Grok's CLI is OpenAI-compatible per token_usage.py's own docstring,
        # so it shares the same subset-not-addition cached-token semantics.
        payload = {
            "text": "done",
            "sessionId": "s-1",
            "usage": {
                "prompt_tokens": 500_000,
                "completion_tokens": 20_000,
                "prompt_tokens_details": {"cached_tokens": 500_000},
            },
        }
        usage = normalize_grok_usage(json.dumps(payload))
        self.assertIsNotNone(usage)
        self.assertEqual(usage.input_tokens, 500_000)
        self.assertEqual(usage.cached_input_tokens, 500_000)

        cost = estimate_cost(MODEL, usage)
        baseline = _uncached_baseline_cost(usage)
        self.assertIsNotNone(cost)
        self.assertLessEqual(
            cost,
            baseline,
            f"A 100% cache-hit Grok call was estimated at ${cost:.6f}, more than the "
            f"${baseline:.6f} baseline with no cache credit — same double-charge as Codex.",
        )

    def test_partially_cached_codex_call_costs_less_than_the_same_call_with_no_cache_hits(self) -> None:
        # A more realistic mixed case: half the input was a cache hit. Any
        # correct accounting must charge *less* than treating the whole
        # request as fresh input, never the same amount or more.
        events = [
            {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 200_000,
                        "output_tokens": 10_000,
                        "input_tokens_details": {"cached_tokens": 100_000},
                    }
                },
            }
        ]
        usage = normalize_codex_usage("\n".join(json.dumps(e) for e in events))
        self.assertIsNotNone(usage)
        cost = estimate_cost(MODEL, usage)
        baseline = _uncached_baseline_cost(usage)
        self.assertIsNotNone(cost)
        self.assertLess(
            cost,
            baseline,
            f"A half-cached Codex call (${cost:.6f}) did not cost less than the same "
            f"call with zero cache credit (${baseline:.6f}); cached tokens counted "
            "inside input_tokens are being charged again on top of it.",
        )


class ClaudeAdditiveCacheSemanticsRemainCorrectTests(unittest.TestCase):
    """Control group: Claude's cache tokens are genuinely additional to
    input_tokens, so charging them as an extra line item on top is correct
    there. This pins down that any fix for the Codex/Grok double-charge above
    must stay provider-aware rather than, e.g., naively subtracting
    cached_input_tokens from input_tokens for every provider — doing that
    generically would instead *undercharge* Claude, which never included
    those tokens in input_tokens to begin with."""

    def test_claude_cache_read_adds_to_cost_on_top_of_base_input(self) -> None:
        raw = json.dumps(
            {
                "type": "result",
                "result": "done",
                "usage": {
                    "input_tokens": 100_000,
                    "output_tokens": 5_000,
                    "cache_read_input_tokens": 400_000,
                },
            }
        )
        usage = normalize_claude_usage(raw)
        self.assertIsNotNone(usage)
        self.assertEqual(usage.input_tokens, 100_000)
        self.assertEqual(usage.cached_input_tokens, 400_000)

        cost = estimate_cost(MODEL, usage)
        no_cache_usage = NormalizedUsage(
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, cached_input_tokens=0,
        )
        no_cache_cost = estimate_cost(MODEL, no_cache_usage)
        self.assertIsNotNone(cost)
        self.assertIsNotNone(no_cache_cost)
        # Real, additional tokens were processed for the cache read, so this
        # call must cost strictly more than the same call without them —
        # unlike the OpenAI-shaped cases above, this is correct behavior.
        self.assertGreater(cost, no_cache_cost)
        # But still far cheaper than if those 400k tokens had been fresh
        # input at the full rate, since a cache read is a steep discount.
        full_price_equivalent = estimate_cost(
            MODEL,
            NormalizedUsage(
                input_tokens=usage.input_tokens + usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                cached_input_tokens=0,
            ),
        )
        self.assertIsNotNone(full_price_equivalent)
        self.assertLess(cost, full_price_equivalent)


if __name__ == "__main__":
    unittest.main()
