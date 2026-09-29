"""Tests for the versioned pricing catalog (issue #295)."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import model_pricing  # noqa: E402
from model_pricing import (  # noqa: E402
    PRICING_CATALOG,
    PRICING_STATUS_AMBIGUOUS,
    PRICING_STATUS_NO_EFFECTIVE_PRICE,
    PRICING_STATUS_NO_USAGE,
    PRICING_STATUS_PRICED,
    PRICING_STATUS_UNKNOWN_MODEL,
    ModelPrice,
    estimate_invocation_cost,
    normalize_provider,
    parse_timestamp,
    resolve_price,
    validate_catalog,
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


class ShippedCatalogTests(unittest.TestCase):
    def test_shipped_catalog_validates(self) -> None:
        self.assertEqual(validate_catalog(), [])

    def test_every_routable_model_is_either_priced_or_deliberately_absent(self) -> None:
        # A catalog entry for a model the router cannot choose is dead weight;
        # the reverse (a routable model with no price) is allowed, and shows
        # up in the report as "Tokens only" rather than as a guessed number.
        import dynamic_router

        routable = {entry.model for entry in dynamic_router._MODEL_CATALOG}
        priced = {model_pricing.normalize_model(entry.model) for entry in PRICING_CATALOG}
        self.assertEqual(priced - routable, set())

    def test_no_cached_rate_exceeds_its_own_fresh_input_rate(self) -> None:
        # A cache hit is a discount at every provider. If this ever inverted,
        # identifying cached tokens would raise an estimate.
        for entry in PRICING_CATALOG:
            if entry.cached_input_per_million is None:
                continue
            self.assertLessEqual(
                entry.cached_input_per_million,
                entry.input_per_million,
                f"{entry.rate_id} charges more for a cache read than for fresh input",
            )


class ValidationTests(unittest.TestCase):
    def test_overlapping_effective_windows_are_rejected(self) -> None:
        catalog = [
            _price(rate_id="a", effective_from="2026-01-01", effective_to="2026-06-01"),
            _price(rate_id="b", effective_from="2026-03-01"),
        ]
        problems = validate_catalog(catalog)
        self.assertTrue(any("overlapping effective windows" in problem for problem in problems))

    def test_windows_that_hand_over_exactly_are_not_overlapping(self) -> None:
        catalog = [
            _price(rate_id="a", effective_from="2026-01-01", effective_to="2026-06-01"),
            _price(rate_id="b", effective_from="2026-06-01"),
        ]
        self.assertEqual(validate_catalog(catalog), [])

    def test_negative_and_malformed_rates_are_rejected(self) -> None:
        problems = validate_catalog([_price(input_per_million=-1.0)])
        self.assertTrue(any("must not be negative" in problem for problem in problems))
        problems = validate_catalog([_price(output_per_million=float("inf"))])
        self.assertTrue(any("must be finite" in problem for problem in problems))
        problems = validate_catalog([_price(cached_input_per_million="cheap")])
        self.assertTrue(any("must be a number" in problem for problem in problems))

    def test_duplicate_rate_ids_are_rejected(self) -> None:
        problems = validate_catalog([_price(), _price(model="other-model")])
        self.assertTrue(any("duplicate rate_id" in problem for problem in problems))

    def test_an_alias_claimed_by_two_models_is_rejected(self) -> None:
        catalog = [
            _price(rate_id="a", model="model-one", aliases=("shared",)),
            _price(rate_id="b", model="model-two", aliases=("shared",)),
        ]
        problems = validate_catalog(catalog)
        self.assertTrue(any("resolves to multiple models" in problem for problem in problems))

    def test_missing_source_currency_and_provider_are_rejected(self) -> None:
        problems = validate_catalog([_price(source="", currency="", provider="nobody")])
        self.assertTrue(any("source reference is required" in problem for problem in problems))
        self.assertTrue(any("currency is required" in problem for problem in problems))
        self.assertTrue(any("unknown provider" in problem for problem in problems))

    def test_effective_to_before_effective_from_is_rejected(self) -> None:
        problems = validate_catalog(
            [_price(effective_from="2026-06-01", effective_to="2026-01-01")]
        )
        self.assertTrue(any("must be after effective_from" in problem for problem in problems))


class ProviderNormalizationTests(unittest.TestCase):
    def test_display_names_cli_keys_and_vendors_all_resolve(self) -> None:
        for value in ("Claude", "claude", "ANTHROPIC"):
            self.assertEqual(normalize_provider(value), "claude")
        for value in ("Codex", "openai"):
            self.assertEqual(normalize_provider(value), "codex")
        for value in ("Grok", "xai"):
            self.assertEqual(normalize_provider(value), "grok")
        self.assertEqual(normalize_provider("nobody"), "")
        self.assertEqual(normalize_provider(None), "")


class ResolutionTests(unittest.TestCase):
    def test_unknown_model_is_unpriced_rather_than_guessed(self) -> None:
        resolution = resolve_price("not-a-real-model")
        self.assertEqual(resolution.status, PRICING_STATUS_UNKNOWN_MODEL)
        self.assertIsNone(resolution.price)
        self.assertFalse(resolution.priced)

    def test_an_alias_resolves_to_its_canonical_model(self) -> None:
        dated = resolve_price("claude-haiku-4-5-20251001")
        canonical = resolve_price("claude-haiku-4-5")
        self.assertTrue(dated.priced)
        self.assertEqual(dated.price.rate_id, canonical.price.rate_id)

    def test_a_provider_hint_does_not_unprice_a_known_model(self) -> None:
        # The stored provider is a display name ("Claude"), the router's is a
        # key ("claude"); neither may turn a catalogued model unpriced.
        for provider in ("Claude", "claude", "anthropic", "", "nonsense"):
            self.assertTrue(resolve_price("claude-sonnet-5", provider=provider).priced)

    def test_price_selection_follows_the_invocation_timestamp(self) -> None:
        catalog = (
            _price(rate_id="old", effective_from="2026-01-01", effective_to="2026-06-01",
                   input_per_million=3.0),
            _price(rate_id="new", effective_from="2026-06-01", input_per_million=2.0),
        )
        with _catalog(catalog):
            before = resolve_price("test-model", at="2026-05-31T23:59:59+00:00")
            on_boundary = resolve_price("test-model", at="2026-06-01T00:00:00+00:00")
            after = resolve_price("test-model", at="2026-09-01T00:00:00+00:00")
        self.assertEqual(before.price.rate_id, "old")
        # The window is half-open, so the instant a new rate starts belongs
        # to the new rate, never to both.
        self.assertEqual(on_boundary.price.rate_id, "new")
        self.assertEqual(after.price.rate_id, "new")

    def test_an_invocation_before_every_window_is_unpriced(self) -> None:
        with _catalog((_price(effective_from="2026-06-01"),)):
            resolution = resolve_price("test-model", at="2026-01-01T00:00:00+00:00")
        self.assertEqual(resolution.status, PRICING_STATUS_NO_EFFECTIVE_PRICE)

    def test_a_retired_rate_with_no_successor_is_unpriced(self) -> None:
        with _catalog((_price(effective_from="2026-01-01", effective_to="2026-02-01"),)):
            resolution = resolve_price("test-model", at="2026-09-01T00:00:00+00:00")
        self.assertEqual(resolution.status, PRICING_STATUS_NO_EFFECTIVE_PRICE)

    def test_overlapping_windows_refuse_to_pick_one(self) -> None:
        catalog = (
            _price(rate_id="a", effective_from="2026-01-01"),
            _price(rate_id="b", effective_from="2026-02-01"),
        )
        with _catalog(catalog):
            resolution = resolve_price("test-model", at="2026-03-01T00:00:00+00:00")
        self.assertEqual(resolution.status, PRICING_STATUS_AMBIGUOUS)

    def test_an_alias_shared_by_two_models_refuses_to_pick_one(self) -> None:
        catalog = (
            _price(rate_id="a", model="model-one", aliases=("shared",)),
            _price(rate_id="b", model="model-two", aliases=("shared",)),
        )
        with _catalog(catalog):
            self.assertEqual(resolve_price("shared").status, PRICING_STATUS_AMBIGUOUS)


class CostEstimateTests(unittest.TestCase):
    def test_fresh_input_and_output_are_charged_at_their_own_rates(self) -> None:
        with _catalog((_price(),)):
            estimate = estimate_invocation_cost(
                model="test-model", input_tokens=1_000_000, output_tokens=1_000_000
            )
        self.assertEqual(estimate.status, PRICING_STATUS_PRICED)
        self.assertAlmostEqual(estimate.cost, 18.0)

    def test_cache_reads_and_writes_are_billed_at_their_own_rates(self) -> None:
        with _catalog((_price(),)):
            estimate = estimate_invocation_cost(
                model="test-model",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=1_000_000,
                cache_write_tokens=1_000_000,
            )
        # 0.30 for the read + 3.75 for the write; a write is a premium
        # operation and must not be folded into the read discount.
        self.assertAlmostEqual(estimate.cost, 4.05)

    def test_cached_tokens_inside_input_are_not_charged_twice(self) -> None:
        with _catalog((_price(),)):
            included = estimate_invocation_cost(
                model="test-model",
                input_tokens=1_000_000,
                output_tokens=0,
                cached_input_tokens=1_000_000,
                cached_tokens_included_in_input=True,
            )
            uncached = estimate_invocation_cost(
                model="test-model", input_tokens=1_000_000, output_tokens=0
            )
        # Every reported input token was a cache hit, so only the discounted
        # rate applies — identifying the cache must lower the estimate.
        self.assertAlmostEqual(included.cost, 0.3)
        self.assertLess(included.cost, uncached.cost)

    def test_reasoning_tokens_are_not_charged_unless_billed_separately(self) -> None:
        with _catalog((_price(),)):
            without = estimate_invocation_cost(
                model="test-model", input_tokens=0, output_tokens=1_000_000
            )
            with_reasoning = estimate_invocation_cost(
                model="test-model",
                input_tokens=0,
                output_tokens=1_000_000,
                reasoning_tokens=500_000,
            )
        # The normalizers already count reasoning inside output for every
        # supported provider, so no catalogued rate may charge it again.
        self.assertEqual(without.cost, with_reasoning.cost)
        with _catalog((_price(reasoning_per_million=10.0),)):
            billed = estimate_invocation_cost(
                model="test-model",
                input_tokens=0,
                output_tokens=1_000_000,
                reasoning_tokens=500_000,
            )
        self.assertAlmostEqual(billed.cost, 15.0 + 5.0)

    def test_missing_token_fields_are_unpriced_not_a_free_call(self) -> None:
        with _catalog((_price(),)):
            estimate = estimate_invocation_cost(
                model="test-model", input_tokens=None, output_tokens=None
            )
            zeros = estimate_invocation_cost(
                model="test-model", input_tokens=0, output_tokens=0
            )
        self.assertIsNone(estimate.cost)
        self.assertEqual(estimate.status, PRICING_STATUS_NO_USAGE)
        self.assertEqual(zeros.status, PRICING_STATUS_PRICED)
        self.assertEqual(zeros.cost, 0.0)

    def test_an_unknown_model_yields_no_cost_and_a_reason(self) -> None:
        estimate = estimate_invocation_cost(
            model="not-a-real-model", input_tokens=1000, output_tokens=1000
        )
        self.assertIsNone(estimate.cost)
        self.assertEqual(estimate.status, PRICING_STATUS_UNKNOWN_MODEL)
        self.assertEqual(estimate.rate_id, "")

    def test_provenance_is_returned_with_every_priced_estimate(self) -> None:
        estimate = estimate_invocation_cost(
            model="claude-sonnet-5", provider="Claude", input_tokens=1000, output_tokens=100
        )
        self.assertEqual(estimate.status, PRICING_STATUS_PRICED)
        self.assertEqual(estimate.catalog_version, model_pricing.PRICING_CATALOG_VERSION)
        self.assertTrue(estimate.rate_id)
        self.assertTrue(estimate.source.startswith("https://"))
        self.assertIsNotNone(estimate.input_rate_per_million)
        self.assertIsNotNone(estimate.output_rate_per_million)

    def test_a_stored_estimate_is_unaffected_by_a_later_catalog_change(self) -> None:
        # The report reads a persisted estimate; nothing recosts it. This
        # pins the contract that makes that safe: re-pricing the *same*
        # timestamp against a catalog that has since added a later rate still
        # returns the rate that was in force then.
        catalog_then = (_price(rate_id="old", effective_from="2026-01-01", input_per_million=3.0),)
        with _catalog(catalog_then):
            first = estimate_invocation_cost(
                model="test-model", at="2026-03-01T00:00:00+00:00",
                input_tokens=1_000_000, output_tokens=0,
            )
        catalog_now = (
            _price(rate_id="old", effective_from="2026-01-01", effective_to="2026-07-01",
                   input_per_million=3.0),
            _price(rate_id="new", effective_from="2026-07-01", input_per_million=9.0),
        )
        with _catalog(catalog_now):
            again = estimate_invocation_cost(
                model="test-model", at="2026-03-01T00:00:00+00:00",
                input_tokens=1_000_000, output_tokens=0,
            )
            today = estimate_invocation_cost(
                model="test-model", at="2026-09-01T00:00:00+00:00",
                input_tokens=1_000_000, output_tokens=0,
            )
        self.assertEqual(first.cost, again.cost)
        self.assertEqual(first.rate_id, again.rate_id)
        self.assertNotEqual(first.cost, today.cost)

    def test_a_model_without_a_cache_rate_leaves_cache_hits_unpriced(self) -> None:
        with _catalog((_price(cached_input_per_million=None, cache_write_per_million=None),)):
            estimate = estimate_invocation_cost(
                model="test-model", input_tokens=0, output_tokens=0,
                cached_input_tokens=1_000_000,
            )
        self.assertIsNone(estimate.cost)
        self.assertNotEqual(estimate.status, model_pricing.PRICING_STATUS_PRICED)


class TimestampTests(unittest.TestCase):
    def test_dates_naive_datetimes_and_z_suffixes_all_parse_as_utc(self) -> None:
        self.assertEqual(parse_timestamp("2026-03-01").isoformat(), "2026-03-01T00:00:00+00:00")
        self.assertEqual(
            parse_timestamp("2026-03-01T12:00:00").isoformat(), "2026-03-01T12:00:00+00:00"
        )
        self.assertEqual(
            parse_timestamp("2026-03-01T12:00:00Z").isoformat(), "2026-03-01T12:00:00+00:00"
        )
        self.assertEqual(
            parse_timestamp("2026-03-01T13:00:00+01:00").isoformat(), "2026-03-01T12:00:00+00:00"
        )

    def test_unparseable_values_return_none_rather_than_raising(self) -> None:
        self.assertIsNone(parse_timestamp(""))
        self.assertIsNone(parse_timestamp(None))
        self.assertIsNone(parse_timestamp("not a date"))


class _catalog:
    """Swap the module catalog for one test, restoring it afterwards."""

    def __init__(self, catalog) -> None:
        self.catalog = tuple(catalog)
        self.previous: tuple = ()

    def __enter__(self):
        self.previous = model_pricing.PRICING_CATALOG
        model_pricing.PRICING_CATALOG = self.catalog
        return self.catalog

    def __exit__(self, *exc) -> None:
        model_pricing.PRICING_CATALOG = self.previous


class CatalogSummaryTests(unittest.TestCase):
    def test_summary_describes_what_is_covered(self) -> None:
        summary = model_pricing.catalog_summary()
        self.assertEqual(summary["version"], model_pricing.PRICING_CATALOG_VERSION)
        self.assertEqual(summary["entries"], len(PRICING_CATALOG))
        self.assertIn("claude/claude-sonnet-5", summary["models"])
        self.assertEqual(summary["currencies"], ["USD"])

    def test_price_definitions_are_immutable(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            PRICING_CATALOG[0].input_per_million = 99.0  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
