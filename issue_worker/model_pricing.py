"""Versioned, effective-dated per-model token pricing (issue #295).

Before this module the only "pricing" in the app was
``token_usage._RANK_RATES_PER_MILLION``: five generic rate pairs keyed by the
1-5 relative cost *rank* every model carries in ``dynamic_router``'s catalog.
That is fine for "which model is cheaper", which is what routing needs, and
useless for "what did this month cost", which is what the Usage & cost report
needs — two models sharing a rank are billed at completely different real
rates.

What replaces it:

* One **catalog** (``PRICING_CATALOG``) of explicit, dated rate definitions,
  each naming its provider, canonical model slug, aliases, currency, the
  per-million rate for every token class the provider bills separately, the
  window it applies to, and where the numbers came from.
* A **catalog version** (``PRICING_CATALOG_VERSION``) plus a per-entry
  ``rate_id``. Both are persisted with every costed invocation, so a stored
  estimate can always be explained — and never silently changes when the
  catalog is later corrected, because nothing recomputes a stored estimate.
* Resolution **by invocation timestamp**, not by "now": a call made in March
  is priced with March's rates even after an April price change lands.

Two rules this module exists to enforce:

1. **Never guess.** An unknown model, an alias that resolves two ways, two
   overlapping effective windows, or a missing rate all resolve to
   *unpriced*. The report shows that invocation's tokens under "Tokens only"
   rather than inventing a number. A wrong dollar figure is worse than an
   absent one.
2. **Never break AI work.** Every entry point returns a value instead of
   raising. Pricing is observability; it may not participate in whether an
   issue was delivered (issue #295 acceptance criterion 11).

See ``docs/model-pricing.md`` for how to add or retire a rate.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
from typing import Any, Iterable, Sequence


#: Bumped whenever ``PRICING_CATALOG`` changes in a way that can change a new
#: estimate. Persisted with each costed invocation; never back-applied.
PRICING_CATALOG_VERSION = "2026-09-28"

DEFAULT_CURRENCY = "USD"

#: How a cost estimate turned out. Anything other than ``priced`` means the
#: invocation's tokens are reported with no money attached.
PRICING_STATUS_PRICED = "priced"
PRICING_STATUS_UNKNOWN_MODEL = "unknown_model"
PRICING_STATUS_AMBIGUOUS = "ambiguous_alias"
PRICING_STATUS_NO_EFFECTIVE_PRICE = "no_effective_price"
PRICING_STATUS_NO_USAGE = "no_usage"

#: Provider display names, CLI keys and vendor names all collapse onto these.
#: ``UsageRecord.provider`` holds the display name (``"Claude"``), the router
#: holds the key (``"claude"``), and public pricing pages use the vendor
#: (``"anthropic"``) — all three have to find the same row.
_PROVIDER_ALIASES: dict[str, str] = {
    "claude": "claude",
    "anthropic": "claude",
    "codex": "codex",
    "openai": "codex",
    "grok": "grok",
    "xai": "grok",
}


def normalize_provider(value: Any) -> str:
    """The canonical provider key for a display name, CLI key or vendor name.

    Returns ``""`` for anything unrecognized, which makes a lookup fall back
    to matching on the model slug alone rather than silently pricing against
    the wrong vendor.
    """
    text = str(value or "").strip().lower()
    return _PROVIDER_ALIASES.get(text, "")


def normalize_model(value: Any) -> str:
    return str(value or "").strip().lower()


@dataclasses.dataclass(frozen=True)
class ModelPrice:
    """One provider/model's published rates over one effective window.

    Every rate is US-dollar-equivalent list price **per million tokens** in
    ``currency``. ``None`` means "this provider does not bill this token class
    separately", which is different from zero:

    ``cached_input_per_million``
        Cache *read* / cache-hit input. Every supported provider discounts
        these steeply; that discount is the entire point of prompt caching.
    ``cache_write_per_million``
        Cache *creation*, only where the provider meters it as its own
        operation (Anthropic does; the OpenAI-shaped APIs do not).
    ``reasoning_per_million``
        Set **only** if the provider bills reasoning tokens on top of output
        tokens. None of the currently catalogued providers do — their usage
        objects already count reasoning inside ``output_tokens`` — so this is
        ``None`` everywhere today and exists so that a provider which starts
        billing them separately does not need a schema change.
    """

    rate_id: str
    provider: str
    model: str
    input_per_million: float
    output_per_million: float
    effective_from: str
    source: str
    aliases: tuple[str, ...] = ()
    currency: str = DEFAULT_CURRENCY
    cached_input_per_million: float | None = None
    cache_write_per_million: float | None = None
    reasoning_per_million: float | None = None
    effective_to: str | None = None

    @property
    def model_names(self) -> tuple[str, ...]:
        """The canonical slug plus every alias, all normalized."""
        return (normalize_model(self.model), *(normalize_model(a) for a in self.aliases))


# ---------------------------------------------------------------------------
# The catalog.
#
# Rates are the providers' published list prices per million tokens for the
# model slugs this app actually invokes (see ``dynamic_router._MODEL_CATALOG``
# — a model absent from that catalog can never be routed to, so it can never
# appear here either). Each entry names the pricing page it was taken from and
# the window it applies to.
#
# Adding a price change is *always* additive: close the current entry with an
# ``effective_to`` and append a new entry starting at the same instant, so
# every invocation that already happened keeps resolving to the rate that was
# live when it ran. Editing an existing entry's numbers in place is a bug —
# it rewrites history for anything recosted later.
#
# A model with no entry here is deliberately unpriced rather than approximated.
# ---------------------------------------------------------------------------
_ANTHROPIC_PRICING = "https://www.anthropic.com/pricing"
_OPENAI_PRICING = "https://openai.com/api/pricing/"
_XAI_PRICING = "https://x.ai/api"

#: Anthropic prices a cache read at 10% of the fresh-input rate and a 5-minute
#: cache write at 125% of it. Both multipliers are published alongside the
#: per-model rates, so deriving them keeps the table readable and keeps the
#: three numbers from drifting apart when a base rate is updated.
_CACHE_READ_FACTOR = 0.1
_CACHE_WRITE_FACTOR = 1.25

#: Fallback used only when a catalogued model gives no explicit cache-read
#: rate: every supported provider discounts a cache hit, and charging one at
#: the full fresh-input rate would overstate the estimate far more than this
#: conventional 10% understates it.
CACHED_INPUT_RATE_FACTOR = 0.1


def _anthropic(
    model: str, input_rate: float, output_rate: float, *, aliases: tuple[str, ...] = ()
) -> ModelPrice:
    return ModelPrice(
        rate_id=f"claude/{model}@2026-01-01",
        provider="claude",
        model=model,
        aliases=aliases,
        input_per_million=input_rate,
        output_per_million=output_rate,
        cached_input_per_million=round(input_rate * _CACHE_READ_FACTOR, 6),
        cache_write_per_million=round(input_rate * _CACHE_WRITE_FACTOR, 6),
        effective_from="2026-01-01T00:00:00+00:00",
        source=_ANTHROPIC_PRICING,
    )


def _openai(
    model: str, input_rate: float, cached_rate: float, output_rate: float
) -> ModelPrice:
    return ModelPrice(
        rate_id=f"codex/{model}@2026-01-01",
        provider="codex",
        model=model,
        input_per_million=input_rate,
        # OpenAI-shaped billing meters a cache read and never meters a cache
        # write separately, so ``cache_write_per_million`` stays None and the
        # estimator treats every cached token as a read.
        cached_input_per_million=cached_rate,
        output_per_million=output_rate,
        effective_from="2026-01-01T00:00:00+00:00",
        source=_OPENAI_PRICING,
    )


def _xai(model: str, input_rate: float, cached_rate: float, output_rate: float) -> ModelPrice:
    return ModelPrice(
        rate_id=f"grok/{model}@2026-01-01",
        provider="grok",
        model=model,
        input_per_million=input_rate,
        cached_input_per_million=cached_rate,
        output_per_million=output_rate,
        effective_from="2026-01-01T00:00:00+00:00",
        source=_XAI_PRICING,
    )


PRICING_CATALOG: tuple[ModelPrice, ...] = (
    # Anthropic / Claude. `claude-haiku-4-5-20251001` is the dated model id the
    # same model is sometimes configured under; it is an alias, not a row of
    # its own, so both spellings resolve to one rate.
    _anthropic("claude-haiku-4-5", 1.00, 5.00, aliases=("claude-haiku-4-5-20251001", "haiku")),
    _anthropic("claude-sonnet-5", 3.00, 15.00, aliases=("sonnet",)),
    _anthropic("claude-opus-5", 15.00, 75.00, aliases=("opus",)),
    _anthropic("claude-fable-5", 15.00, 75.00),
    _anthropic("claude-fable-5-1", 15.00, 75.00, aliases=("fable",)),
    # OpenAI / Codex.
    _openai("gpt-5.6-luna", 0.50, 0.05, 4.00),
    _openai("gpt-5.6-terra", 1.25, 0.125, 10.00),
    _openai("gpt-5.6-sol", 5.00, 0.50, 40.00),
    _openai("gpt-6-astra", 15.00, 1.50, 120.00),
    # xAI / Grok.
    _xai("grok-4.5", 0.30, 0.03, 1.50),
    _xai("grok-4.6", 0.60, 0.06, 3.00),
    _xai("grok-4.7-build-fast", 1.50, 0.15, 7.50),
    _xai("grok-4.7", 3.00, 0.30, 15.00),
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_RATE_FIELDS = (
    "input_per_million",
    "output_per_million",
    "cached_input_per_million",
    "cache_write_per_million",
    "reasoning_per_million",
)


def parse_timestamp(value: Any) -> dt.datetime | None:
    """An ISO-8601 date or datetime as an aware UTC ``datetime``.

    Naive values are read as UTC; a bare date is read as that date's start.
    Anything unparseable returns ``None``, which callers treat as "no usable
    timestamp" rather than as an error.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = dt.datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _windows_overlap(left: ModelPrice, right: ModelPrice) -> bool:
    left_from = parse_timestamp(left.effective_from)
    right_from = parse_timestamp(right.effective_from)
    if left_from is None or right_from is None:
        # An unparseable bound is already reported as malformed; treat the
        # window as unbounded so the pair is flagged rather than waved past.
        return True
    left_to = parse_timestamp(left.effective_to) if left.effective_to else None
    right_to = parse_timestamp(right.effective_to) if right.effective_to else None
    # Half-open [from, to): a rate ending exactly when the next one starts is
    # a clean handover, not an overlap.
    if left_to is not None and left_to <= right_from:
        return False
    if right_to is not None and right_to <= left_from:
        return False
    return True


def validate_catalog(catalog: Sequence[ModelPrice] = PRICING_CATALOG) -> list[str]:
    """Every problem with ``catalog``, as human-readable strings.

    An empty list means the catalog is safe to price against. Checked here
    rather than at import so a bad edit surfaces as a failing test (and a
    non-zero ``--validate-pricing`` exit) instead of as a worker that will not
    start — see the module docstring's second rule.
    """
    problems: list[str] = []
    seen_rate_ids: set[str] = set()
    by_model: dict[tuple[str, str], list[ModelPrice]] = {}
    alias_owners: dict[tuple[str, str], set[str]] = {}

    for entry in catalog:
        label = entry.rate_id or f"{entry.provider}/{entry.model}"
        if not entry.rate_id:
            problems.append(f"{label}: rate_id is required")
        elif entry.rate_id in seen_rate_ids:
            problems.append(f"{label}: duplicate rate_id")
        seen_rate_ids.add(entry.rate_id)

        provider = normalize_provider(entry.provider)
        if not provider:
            problems.append(f"{label}: unknown provider {entry.provider!r}")
        if not normalize_model(entry.model):
            problems.append(f"{label}: model is required")
        if not str(entry.currency or "").strip():
            problems.append(f"{label}: currency is required")
        if not str(entry.source or "").strip():
            problems.append(f"{label}: source reference is required")

        for field in _RATE_FIELDS:
            rate = getattr(entry, field)
            if rate is None:
                continue
            if isinstance(rate, bool) or not isinstance(rate, (int, float)):
                problems.append(f"{label}: {field} must be a number, got {rate!r}")
            elif not math.isfinite(float(rate)):
                problems.append(f"{label}: {field} must be finite, got {rate!r}")
            elif float(rate) < 0:
                problems.append(f"{label}: {field} must not be negative, got {rate!r}")
        if entry.input_per_million is None or entry.output_per_million is None:
            problems.append(f"{label}: input and output rates are both required")

        starts = parse_timestamp(entry.effective_from)
        if starts is None:
            problems.append(f"{label}: effective_from {entry.effective_from!r} is not a timestamp")
        if entry.effective_to:
            ends = parse_timestamp(entry.effective_to)
            if ends is None:
                problems.append(f"{label}: effective_to {entry.effective_to!r} is not a timestamp")
            elif starts is not None and ends <= starts:
                problems.append(f"{label}: effective_to must be after effective_from")

        by_model.setdefault((provider, normalize_model(entry.model)), []).append(entry)
        for name in entry.model_names:
            alias_owners.setdefault((provider, name), set()).add(normalize_model(entry.model))

    for (provider, model), entries in sorted(by_model.items()):
        for index, left in enumerate(entries):
            for right in entries[index + 1 :]:
                if _windows_overlap(left, right):
                    problems.append(
                        f"{provider}/{model}: overlapping effective windows "
                        f"{left.rate_id} and {right.rate_id}"
                    )

    for (provider, name), owners in sorted(alias_owners.items()):
        if len(owners) > 1:
            problems.append(
                f"{provider}/{name}: alias resolves to multiple models {sorted(owners)}"
            )

    return problems


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PriceResolution:
    """The outcome of looking one invocation's rate up."""

    status: str
    price: ModelPrice | None = None

    @property
    def priced(self) -> bool:
        return self.status == PRICING_STATUS_PRICED and self.price is not None


def _candidates(model: str, provider: str) -> list[ModelPrice]:
    slug = normalize_model(model)
    if not slug:
        return []
    key = normalize_provider(provider)
    matches = [entry for entry in PRICING_CATALOG if slug in entry.model_names]
    if key:
        scoped = [entry for entry in matches if normalize_provider(entry.provider) == key]
        # A provider that names a model nobody else does still resolves; a
        # provider filter that matches nothing falls back to the slug so a
        # renamed display name cannot silently unprice a known model.
        if scoped:
            return scoped
    return matches


def resolve_price(model: str, *, provider: str = "", at: str = "") -> PriceResolution:
    """The rate in force for ``model`` at ``at`` (an ISO timestamp).

    ``at`` is the invocation's own start time, not "now": re-reading an old
    record must reproduce the rate that was live when the call ran. An empty
    or unparseable ``at`` falls back to the present, which is the right answer
    for a call being priced as it happens.
    """
    candidates = _candidates(model, provider)
    if not candidates:
        return PriceResolution(PRICING_STATUS_UNKNOWN_MODEL)
    slugs = {normalize_model(entry.model) for entry in candidates}
    if len(slugs) > 1:
        # The same name is claimed by two different canonical models and the
        # provider did not disambiguate: refuse rather than pick one.
        return PriceResolution(PRICING_STATUS_AMBIGUOUS)

    moment = parse_timestamp(at) or dt.datetime.now(dt.timezone.utc)
    effective: list[ModelPrice] = []
    for entry in candidates:
        starts = parse_timestamp(entry.effective_from)
        if starts is None or starts > moment:
            continue
        if entry.effective_to:
            ends = parse_timestamp(entry.effective_to)
            if ends is None or ends <= moment:
                continue
        effective.append(entry)
    if not effective:
        return PriceResolution(PRICING_STATUS_NO_EFFECTIVE_PRICE)
    if len(effective) > 1:
        # Overlapping windows for one model: the catalog is wrong, and any
        # answer would be arbitrary. Report the invocation as unpriced.
        return PriceResolution(PRICING_STATUS_AMBIGUOUS)
    return PriceResolution(PRICING_STATUS_PRICED, effective[0])


@dataclasses.dataclass(frozen=True)
class CostEstimate:
    """One invocation's estimated cost plus everything needed to explain it.

    ``cost`` is ``None`` for every non-``priced`` status. The rate fields are
    persisted alongside the estimate so the number can be reproduced later
    even if the catalog entry is retired: an estimate is evidence about what
    was charged, not a view over the current catalog.
    """

    cost: float | None
    status: str
    currency: str = DEFAULT_CURRENCY
    catalog_version: str = ""
    rate_id: str = ""
    source: str = ""
    input_rate_per_million: float | None = None
    cached_input_rate_per_million: float | None = None
    cache_write_rate_per_million: float | None = None
    output_rate_per_million: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _unpriced(status: str) -> CostEstimate:
    return CostEstimate(cost=None, status=status, catalog_version=PRICING_CATALOG_VERSION)


def estimate_invocation_cost(
    *,
    model: str,
    provider: str = "",
    at: str = "",
    input_tokens: int | None,
    output_tokens: int | None,
    cached_input_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    cached_tokens_included_in_input: bool = False,
) -> CostEstimate:
    """Cost one invocation from its token counts and the effective rate.

    Two things this must get right, both of which the token classes above
    already encode rather than leaving to a provider check here:

    * **Cached tokens are billed once.** ``cached_tokens_included_in_input``
      says whether the provider's ``input_tokens`` already contains the cached
      ones (OpenAI-shaped: yes; Anthropic: no). When it does, they are removed
      from the full-price portion before being charged at the cache rate —
      otherwise identifying a cache hit would *raise* the estimate, which is
      the opposite of what caching does.
    * **Reasoning tokens are not charged twice.** Every normalizer folds them
      into ``output_tokens`` where the provider does, so they are only billed
      separately when the catalog entry carries an explicit reasoning rate.
    """
    token_fields = (
        input_tokens,
        output_tokens,
        cached_input_tokens,
        cache_read_tokens,
        cache_write_tokens,
        reasoning_tokens,
    )
    # A provider that returned `"usage": {}` (or no usage object) reported
    # nothing — that is unreported, not a free call. Treating every missing
    # count as 0 would attach $0.00 / "priced" to telemetry that never
    # happened. Genuine zeros are still priced: those fields are 0, not None.
    if all(value is None for value in token_fields):
        return _unpriced(PRICING_STATUS_NO_USAGE)

    resolution = resolve_price(model, provider=provider, at=at)
    if not resolution.priced or resolution.price is None:
        return _unpriced(resolution.status)
    price = resolution.price

    def count(value: int | None) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    reads = count(cache_read_tokens)
    writes = count(cache_write_tokens)
    # Older records (and the OpenAI-shaped providers) only report the combined
    # figure. Everything not explicitly a write is charged as a read.
    combined = count(cached_input_tokens)
    if not reads and not writes and combined:
        reads = combined
    elif reads or writes:
        combined = max(combined, reads + writes)

    raw_input = count(input_tokens)
    fresh_input = max(0, raw_input - combined) if cached_tokens_included_in_input else raw_input

    cached_rate = price.cached_input_per_million
    if cached_rate is None:
        cached_rate = price.input_per_million * CACHED_INPUT_RATE_FACTOR
    write_rate = price.cache_write_per_million
    if write_rate is None:
        # The provider does not meter cache creation separately, so those
        # tokens were already billed as ordinary input at the read rate.
        write_rate = cached_rate

    try:
        cost = (
            fresh_input / 1_000_000 * price.input_per_million
            + reads / 1_000_000 * cached_rate
            + writes / 1_000_000 * write_rate
            + count(output_tokens) / 1_000_000 * price.output_per_million
        )
        if price.reasoning_per_million is not None:
            cost += count(reasoning_tokens) / 1_000_000 * price.reasoning_per_million
    except (TypeError, ValueError):
        return _unpriced(PRICING_STATUS_NO_EFFECTIVE_PRICE)

    return CostEstimate(
        cost=round(cost, 6),
        status=PRICING_STATUS_PRICED,
        currency=price.currency,
        catalog_version=PRICING_CATALOG_VERSION,
        rate_id=price.rate_id,
        source=price.source,
        input_rate_per_million=price.input_per_million,
        cached_input_rate_per_million=cached_rate,
        cache_write_rate_per_million=price.cache_write_per_million,
        output_rate_per_million=price.output_per_million,
    )


def catalog_summary(catalog: Iterable[ModelPrice] = PRICING_CATALOG) -> dict[str, Any]:
    """A small, UI-facing description of what the catalog currently covers."""
    entries = list(catalog)
    return {
        "version": PRICING_CATALOG_VERSION,
        "entries": len(entries),
        "models": sorted({f"{normalize_provider(e.provider)}/{normalize_model(e.model)}" for e in entries}),
        "currencies": sorted({str(e.currency or DEFAULT_CURRENCY) for e in entries}),
    }


def main(argv: list[str] | None = None) -> int:
    """``python3 model_pricing.py`` validates the catalog and prints a summary.

    Exits non-zero, listing every problem, when the catalog would price
    anything incorrectly — the same check ``test_model_pricing.py`` runs, kept
    reachable by hand for whoever is editing a rate.
    """
    import json

    del argv
    problems = validate_catalog()
    payload = {"summary": catalog_summary(), "problems": problems}
    print(json.dumps(payload, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
