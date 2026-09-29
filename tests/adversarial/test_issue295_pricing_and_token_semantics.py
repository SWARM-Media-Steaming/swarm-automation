"""Issue #295 — pricing catalog and token-cost semantics, independently.

Derived from the issue before looking at the shipped catalog:

* Dashboard cost uses an accurate, effective-dated per-model catalog. The
  1–5 routing rank is not a price.
* Unknown models, ambiguous aliases, overlapping windows, and unavailable
  prices yield Tokens only / Unpriced — never a guessed dollar figure.
* Historical stored estimates must not silently change when the catalog is
  later updated; each estimate keeps the rate that produced it.
* Cached and reasoning tokens are never double-counted.
* Missing usage is distinct from zero usage. An invocation whose provider
  returned no token fields is unreported, not a priced $0.00 call.
* Pricing failures must never raise into AI work.
* Overlapping effective windows and malformed/negative rates are rejected
  by automated validation, which does not run at import.
"""

from __future__ import annotations

import dataclasses
import json
import math
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import dynamic_router  # noqa: E402
import model_pricing  # noqa: E402
import token_usage  # noqa: E402
from model_pricing import (  # noqa: E402
    PRICING_CATALOG,
    PRICING_STATUS_AMBIGUOUS,
    PRICING_STATUS_NO_EFFECTIVE_PRICE,
    PRICING_STATUS_NO_USAGE,
    PRICING_STATUS_PRICED,
    PRICING_STATUS_UNKNOWN_MODEL,
    ModelPrice,
    estimate_invocation_cost,
    resolve_price,
    validate_catalog,
)
from token_usage import (  # noqa: E402
    NormalizedUsage,
    estimate_cost,
    estimate_cost_detailed,
    normalize_claude_usage,
    normalize_codex_usage,
    normalize_grok_usage,
)


def _price(**overrides) -> ModelPrice:
    base = dict(
        rate_id="test/model@2026-01-01",
        provider="claude",
        model="test-model",
        input_per_million=3.0,
        output_per_million=15.0,
        cached_input_per_million=0.3,
        cache_write_per_million=3.75,
        effective_from="2026-01-01T00:00:00+00:00",
        source="https://example.test/pricing",
    )
    base.update(overrides)
    return ModelPrice(**base)


class _catalog:
    def __init__(self, catalog) -> None:
        self.catalog = tuple(catalog)
        self.previous = ()

    def __enter__(self):
        self.previous = model_pricing.PRICING_CATALOG
        model_pricing.PRICING_CATALOG = self.catalog
        return self.catalog

    def __exit__(self, *exc) -> None:
        model_pricing.PRICING_CATALOG = self.previous


class RankIsNotAPriceTests(unittest.TestCase):
    def test_rank_rate_table_is_gone_from_token_usage(self) -> None:
        self.assertFalse(hasattr(token_usage, "_RANK_RATES_PER_MILLION"))
        source = Path(token_usage.__file__).read_text(encoding="utf-8")
        self.assertNotIn("dynamic_router.model_cost", source)
        self.assertNotIn("model_cost(", source)

    def test_models_that_share_a_routing_rank_are_billed_at_their_own_rates(self) -> None:
        # Rank 3 is shared by Claude Sonnet, Codex Terra, and Grok Build Fast.
        # The whole point of replacing the five-rank approximation is that
        # those three are not the same price.
        usage = NormalizedUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        models = ("claude-sonnet-5", "gpt-5.6-terra", "grok-4.7-build-fast")
        ranks = {dynamic_router.model_cost(model) for model in models}
        self.assertEqual(ranks, {3})
        costs = {estimate_cost(model, usage) for model in models}
        self.assertEqual(len(costs), 3, costs)
        for cost in costs:
            self.assertIsNotNone(cost)
            self.assertGreater(cost, 0)

    def test_estimate_cost_does_not_consult_model_cost(self) -> None:
        usage = NormalizedUsage(input_tokens=1000, output_tokens=100)
        with mock.patch.object(dynamic_router, "model_cost", side_effect=AssertionError("rank lookup")):
            cost = estimate_cost("claude-sonnet-5", usage, provider="claude")
        self.assertIsNotNone(cost)


class NeverGuessAPriceTests(unittest.TestCase):
    def test_unknown_model_is_unpriced(self) -> None:
        estimate = estimate_invocation_cost(
            model="not-in-the-catalog", input_tokens=10_000, output_tokens=500
        )
        self.assertIsNone(estimate.cost)
        self.assertEqual(estimate.status, PRICING_STATUS_UNKNOWN_MODEL)
        self.assertEqual(estimate.rate_id, "")

    def test_an_invocation_with_no_token_fields_is_unpriced_not_free(self) -> None:
        # Spec: missing usage is distinct from zero usage, and an unavailable
        # price must not become a guessed figure. An empty usage object (the
        # provider returned `"usage": {}`) has no authoritative totals, so
        # attaching $0.00 / "priced" would make it look like a free, fully
        # reported call.
        empty = NormalizedUsage()
        estimate = estimate_cost_detailed("claude-sonnet-5", empty, provider="claude")
        self.assertIsNone(
            estimate.cost,
            f"empty usage was priced as {estimate.cost!r} ({estimate.status}); "
            "unreported tokens must stay unpriced, not $0.00",
        )
        self.assertNotEqual(estimate.status, PRICING_STATUS_PRICED)
        self.assertIn(
            estimate.status,
            {PRICING_STATUS_NO_USAGE, PRICING_STATUS_NO_EFFECTIVE_PRICE, PRICING_STATUS_UNKNOWN_MODEL},
        )

        none = estimate_cost_detailed("claude-sonnet-5", None, provider="claude")
        self.assertIsNone(none.cost)
        self.assertEqual(none.status, PRICING_STATUS_NO_USAGE)

        zeros = NormalizedUsage(input_tokens=0, output_tokens=0, total_tokens=0)
        zero_estimate = estimate_cost_detailed("claude-sonnet-5", zeros, provider="claude")
        self.assertEqual(zero_estimate.status, PRICING_STATUS_PRICED)
        self.assertEqual(zero_estimate.cost, 0.0)

    def test_ambiguous_alias_is_unpriced(self) -> None:
        catalog = (
            _price(rate_id="a", model="model-one", aliases=("shared",)),
            _price(rate_id="b", model="model-two", aliases=("shared",)),
        )
        with _catalog(catalog):
            resolution = resolve_price("shared")
            estimate = estimate_invocation_cost(
                model="shared", input_tokens=1000, output_tokens=100
            )
        self.assertEqual(resolution.status, PRICING_STATUS_AMBIGUOUS)
        self.assertIsNone(estimate.cost)

    def test_no_effective_window_is_unpriced(self) -> None:
        with _catalog((_price(effective_from="2026-06-01", effective_to="2026-07-01"),)):
            before = resolve_price("test-model", at="2025-12-01T00:00:00+00:00")
            after = resolve_price("test-model", at="2026-08-01T00:00:00+00:00")
        self.assertEqual(before.status, PRICING_STATUS_NO_EFFECTIVE_PRICE)
        self.assertEqual(after.status, PRICING_STATUS_NO_EFFECTIVE_PRICE)

    def test_overlapping_windows_refuse_to_pick(self) -> None:
        catalog = (
            _price(rate_id="a", effective_from="2026-01-01"),
            _price(rate_id="b", effective_from="2026-02-01"),
        )
        with _catalog(catalog):
            resolution = resolve_price("test-model", at="2026-03-01T00:00:00+00:00")
        self.assertEqual(resolution.status, PRICING_STATUS_AMBIGUOUS)


class EffectiveDateAndProvenanceTests(unittest.TestCase):
    def test_price_at_the_invocation_timestamp_not_now(self) -> None:
        catalog = (
            _price(rate_id="old", effective_from="2026-01-01", effective_to="2026-06-01",
                   input_per_million=10.0),
            _price(rate_id="new", effective_from="2026-06-01", input_per_million=1.0),
        )
        usage = dict(model="test-model", input_tokens=1_000_000, output_tokens=0)
        with _catalog(catalog):
            before = estimate_invocation_cost(at="2026-05-31T23:59:59+00:00", **usage)
            boundary = estimate_invocation_cost(at="2026-06-01T00:00:00+00:00", **usage)
        self.assertEqual(before.rate_id, "old")
        self.assertAlmostEqual(before.cost, 10.0)
        self.assertEqual(boundary.rate_id, "new")
        self.assertAlmostEqual(boundary.cost, 1.0)

    def test_a_later_catalog_edit_does_not_restate_an_earlier_timestamp(self) -> None:
        usage = dict(model="test-model", input_tokens=1_000_000, output_tokens=0)
        with _catalog((_price(rate_id="old", effective_from="2026-01-01", input_per_million=3.0),)):
            original = estimate_invocation_cost(at="2026-03-01T00:00:00+00:00", **usage)
        with _catalog((
            _price(rate_id="old", effective_from="2026-01-01", effective_to="2026-07-01",
                   input_per_million=3.0),
            _price(rate_id="new", effective_from="2026-07-01", input_per_million=99.0),
        )):
            again = estimate_invocation_cost(at="2026-03-01T00:00:00+00:00", **usage)
            later = estimate_invocation_cost(at="2026-09-01T00:00:00+00:00", **usage)
        self.assertEqual(original.cost, again.cost)
        self.assertEqual(original.rate_id, again.rate_id)
        self.assertNotEqual(original.cost, later.cost)

    def test_priced_estimate_carries_reproducible_provenance(self) -> None:
        estimate = estimate_invocation_cost(
            model="claude-sonnet-5",
            provider="Claude",
            at="2026-03-01T00:00:00+00:00",
            input_tokens=1000,
            output_tokens=100,
        )
        self.assertEqual(estimate.status, PRICING_STATUS_PRICED)
        self.assertEqual(estimate.catalog_version, model_pricing.PRICING_CATALOG_VERSION)
        self.assertTrue(estimate.rate_id)
        self.assertTrue(estimate.source)
        self.assertIsNotNone(estimate.input_rate_per_million)
        self.assertIsNotNone(estimate.output_rate_per_million)

    def test_alias_and_display_provider_resolve_to_the_same_rate(self) -> None:
        dated = resolve_price("claude-haiku-4-5-20251001", provider="Anthropic")
        canonical = resolve_price("claude-haiku-4-5", provider="Claude")
        self.assertTrue(dated.priced and canonical.priced)
        self.assertEqual(dated.price.rate_id, canonical.price.rate_id)


class CacheAndReasoningSemanticsTests(unittest.TestCase):
    def test_openai_shaped_cached_tokens_are_not_charged_twice(self) -> None:
        events = [
            {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1_000_000,
                        "output_tokens": 0,
                        "input_tokens_details": {"cached_tokens": 1_000_000},
                    }
                },
            }
        ]
        usage = normalize_codex_usage("\n".join(json.dumps(event) for event in events))
        self.assertTrue(usage.cached_tokens_included_in_input)
        cached = estimate_cost("gpt-5.6-luna", usage, provider="codex")
        uncached = estimate_cost(
            "gpt-5.6-luna",
            NormalizedUsage(input_tokens=1_000_000, output_tokens=0),
            provider="codex",
        )
        self.assertIsNotNone(cached)
        self.assertIsNotNone(uncached)
        self.assertLess(cached, uncached)

    def test_grok_cache_hits_are_a_discount(self) -> None:
        payload = {
            "text": "done",
            "usage": {
                "prompt_tokens": 500_000,
                "completion_tokens": 0,
                "prompt_tokens_details": {"cached_tokens": 500_000},
            },
        }
        usage = normalize_grok_usage(json.dumps(payload))
        cached = estimate_cost("grok-4.6", usage, provider="grok")
        uncached = estimate_cost(
            "grok-4.6",
            NormalizedUsage(input_tokens=500_000, output_tokens=0),
            provider="grok",
        )
        self.assertLess(cached, uncached)

    def test_anthropic_cache_read_and_write_are_billed_at_different_rates(self) -> None:
        raw = json.dumps(
            {
                "type": "result",
                "usage": {
                    "input_tokens": 1_000_000,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 1_000_000,
                    "cache_creation_input_tokens": 1_000_000,
                },
            }
        )
        usage = normalize_claude_usage(raw)
        self.assertFalse(usage.cached_tokens_included_in_input)
        self.assertEqual(usage.cache_read_tokens, 1_000_000)
        self.assertEqual(usage.cache_write_tokens, 1_000_000)
        estimate = estimate_cost_detailed("claude-sonnet-5", usage, provider="claude")
        # Sonnet 5: $2 fresh, $0.20 read, $2.50 write (Anthropic's pricing page).
        # Folding the write into the read discount would understate the
        # premium cache-creation line.
        self.assertAlmostEqual(estimate.cost, 2.0 + 0.2 + 2.5)
        reads_only = estimate_invocation_cost(
            model="claude-sonnet-5",
            provider="claude",
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=1_000_000,
            cache_write_tokens=0,
            cached_tokens_included_in_input=False,
        )
        self.assertGreater(estimate.cost, reads_only.cost)

    def test_reasoning_tokens_are_not_added_when_the_catalog_does_not_bill_them(self) -> None:
        with_reasoning = estimate_invocation_cost(
            model="claude-sonnet-5",
            provider="claude",
            input_tokens=0,
            output_tokens=1_000_000,
            reasoning_tokens=500_000,
        )
        without = estimate_invocation_cost(
            model="claude-sonnet-5",
            provider="claude",
            input_tokens=0,
            output_tokens=1_000_000,
        )
        self.assertEqual(with_reasoning.cost, without.cost)
        for entry in PRICING_CATALOG:
            self.assertIsNone(
                entry.reasoning_per_million,
                f"{entry.rate_id} bills reasoning separately; every current "
                "normalizer already folds reasoning into output_tokens",
            )


class CatalogValidationTests(unittest.TestCase):
    def test_shipped_catalog_has_no_problems(self) -> None:
        self.assertEqual(validate_catalog(), [])

    def test_validation_is_not_run_at_import(self) -> None:
        source = Path(model_pricing.__file__).read_text(encoding="utf-8")
        header = source.split("def ", 1)[0]
        self.assertNotIn("validate_catalog()", header)
        self.assertNotIn("raise SystemExit(validate_catalog", source.split("def main")[0])
        # Import already succeeded; a bad catalog must not stop the worker.
        self.assertTrue(callable(validate_catalog))

    def test_overlapping_windows_and_negative_rates_are_rejected(self) -> None:
        overlap = [
            _price(rate_id="a", effective_from="2026-01-01", effective_to="2026-06-01"),
            _price(rate_id="b", effective_from="2026-03-01"),
        ]
        self.assertTrue(any("overlapping" in problem for problem in validate_catalog(overlap)))
        self.assertTrue(any("negative" in problem for problem in validate_catalog([_price(input_per_million=-1)])))
        self.assertTrue(any("finite" in problem for problem in validate_catalog([_price(output_per_million=math.inf)])))

    def test_exact_handover_is_not_an_overlap(self) -> None:
        catalog = [
            _price(rate_id="a", effective_from="2026-01-01", effective_to="2026-06-01"),
            _price(rate_id="b", effective_from="2026-06-01"),
        ]
        self.assertEqual(validate_catalog(catalog), [])

    def test_pricing_never_raises_on_garbage_input(self) -> None:
        cases = [
            dict(model="", input_tokens=None, output_tokens=None),
            dict(model="claude-sonnet-5", input_tokens="nope", output_tokens=object()),
            dict(model="claude-sonnet-5", input_tokens=-5, output_tokens=None, at="not-a-date"),
        ]
        for kwargs in cases:
            try:
                estimate = estimate_invocation_cost(**kwargs)
            except Exception as error:  # noqa: BLE001
                self.fail(f"pricing raised {error!r} for {kwargs}")
            self.assertTrue(hasattr(estimate, "cost"))

    def test_estimate_cost_detailed_swallows_estimator_exceptions(self) -> None:
        usage = NormalizedUsage(input_tokens=10, output_tokens=10)
        with mock.patch.object(
            model_pricing, "estimate_invocation_cost", side_effect=RuntimeError("catalog exploded")
        ):
            estimate = estimate_cost_detailed("claude-sonnet-5", usage)
        self.assertIsNone(estimate.cost)
        self.assertNotEqual(estimate.status, PRICING_STATUS_PRICED)

    def test_price_definitions_are_immutable(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            PRICING_CATALOG[0].input_per_million = 0.0  # type: ignore[misc]

    def test_maintenance_doc_exists_and_names_official_sources(self) -> None:
        doc = REPO_ROOT / "docs" / "model-pricing.md"
        self.assertTrue(doc.is_file(), "issue #295 requires a catalog maintenance document")
        text = doc.read_text(encoding="utf-8")
        self.assertIn("Estimated cost", text)
        self.assertIn("effective_to", text)
        self.assertIn("PRICING_CATALOG_VERSION", text)
        self.assertRegex(
            text,
            r"official pricing page",
            "maintenance doc must tell maintainers to use official provider pricing",
        )


if __name__ == "__main__":
    unittest.main()
