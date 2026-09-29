# Model pricing catalog

How SWARM Automation turns recorded token counts into the **Estimated cost**
figures shown in Feedback's *Usage & cost* tab (issue #295), and how to keep
those numbers right.

## What "Estimated cost" means

It is token-equivalent model list pricing: the tokens a provider reported for
one invocation, multiplied by that model's published per-million rates.

It is **not**:

- an invoice, or anything your provider actually billed you;
- subscription or plan-quota consumption (provider quota remaining is a
  separate, unrelated figure and stays on the Overview page);
- a prediction, budget, or threshold — nothing in the app enforces or acts on
  a cost.

Every place a monetary value appears, it is labelled as an estimate and is
accompanied by how many of the invocations behind it could actually be
priced. A total over "3 of 11 priced" means something very different from the
same total over "11 of 11", and the UI never lets you read one as the other.

## Where the catalog lives

`issue_worker/model_pricing.py`. It is a plain Python table, deliberately not
a data file: it ships inside the packaged app through the same
`issue_worker/*.py` bundle rule as everything else the worker needs, with no
extra resource to go missing at runtime.

Each entry is a `ModelPrice`:

| Field | Meaning |
| --- | --- |
| `rate_id` | Stable identifier for this rate, persisted with every estimate it produces. |
| `provider` | `claude`, `codex` or `grok` (display names and vendor names both normalize onto these). |
| `model` | The canonical model slug the app invokes the provider with. |
| `aliases` | Other spellings that mean the same model (for example a dated model id). |
| `currency` | ISO code. Everything is `USD` today. |
| `input_per_million` | Fresh (non-cached) input tokens. |
| `cached_input_per_million` | Cache reads / cache hits. |
| `cache_write_per_million` | Cache creation, where the provider meters it separately. `None` where it does not. |
| `output_per_million` | Output tokens. |
| `reasoning_per_million` | Only when the provider bills reasoning *on top of* output. `None` everywhere today — see below. |
| `effective_from` | When this rate started applying. |
| `effective_to` | When it stopped, or `None` while it is current. |
| `source` | The official pricing page the numbers came from. |

The catalog as a whole carries `PRICING_CATALOG_VERSION`.

## How a rate is chosen

`resolve_price(model, provider=…, at=…)` picks the single entry whose
effective window contains **the invocation's own start timestamp** — not the
time the report is being read, and not the time the usage row was written.
Windows are half-open `[effective_from, effective_to)`, so a rate change at
midnight belongs entirely to the new rate.

Four situations deliberately produce **no price** rather than a guess. The
invocation still keeps all of its token counts and appears in the report
under *Tokens only*:

- the model is not in the catalog;
- a name resolves to two different canonical models;
- two entries for one model overlap at that instant;
- no entry covers that instant (the call predates every rate, or the only
  rate has been retired with no successor).

## Updating a price

**Never edit an existing entry's numbers in place.** Doing so silently
restates history for anything recosted later. Instead:

1. Look the current rate up on the provider's own pricing page (the `source`
   field of the entry you are replacing).
2. Set `effective_to` on the existing entry to the instant the new price
   takes effect.
3. Append a new entry with the same `provider`/`model`, a new `rate_id`, and
   `effective_from` equal to that same instant.
4. Bump `PRICING_CATALOG_VERSION`.
5. Run the validator.

Adding a brand-new model is just step 3 plus step 4. The automatic upgrade to a
newer release only lands on a model that has a price here, so a release stays
out of that path until its provider-page rate is added.

### Corrections

A rate that was simply *wrong* is fixed the same way, not edited in place: end
the wrong entry at the date the fix lands and append the right one. Estimates
already stored keep the rate they were priced with, so anything recorded
before the fix stays overstated or understated by the old error; only new
invocations use the corrected rate. Record what was wrong and why in a comment
next to the entries.

Example: on 2026-09-29 Sonnet 5 ($3/$15), Opus 5 ($15/$75), Fable 5 and
Fable 5.1 ($15/$75) were found to differ from Anthropic's pricing page
(Sonnet 5 $2/$10, Opus 5 $5/$25, Fable $10/$50). The old windows end that day.
Sonnet 5.5 ($2/$10), Opus 5.5 ($4/$20) and the earlier Opus 4.6-4.8 and
Sonnet 4.6 releases were added at the same time.

Aliases such as `sonnet` and `opus` follow the latest release, like the Claude
CLI's own aliases, and must belong to exactly one canonical model or the name
becomes ambiguous and unpriced.

Retiring a model means giving its last entry an `effective_to`. Calls made
before then keep their prices; calls after it become *Tokens only*.

## Validating

```sh
python3 issue_worker/model_pricing.py
# or, from the desktop app's own CLI surface:
python3 issue_worker/ai_execution_history.py --validate-pricing
```

Both print a summary and exit non-zero listing every problem. The same checks
run in `issue_worker/test_model_pricing.py`, so a bad edit fails CI. They
reject:

- two overlapping effective windows for one provider/model;
- a negative, non-finite or non-numeric rate;
- a missing input or output rate, currency, source or `rate_id`;
- a duplicate `rate_id`;
- an alias claimed by two different canonical models;
- an `effective_to` at or before its `effective_from`.

Validation is not run at import. Pricing is observability, and a bad catalog
must never stop the worker from delivering an issue — a model it cannot price
is reported unpriced instead.

## Provenance, and why stored costs never move

`Worker._record_usage_event` prices each invocation as it happens and stores
the result *with the rate that produced it*: `pricing_status`,
`pricing_version`, `pricing_rate_id`, `pricing_source`, and the four
individual per-million rates, all on the `ai_token_usage` row (migration 7).

Nothing recomputes a stored estimate. The Usage & cost report reads
`estimated_cost` verbatim, so correcting or extending the catalog changes
what *future* invocations cost and leaves every historical figure exactly as
it was recorded — and each of those figures can still be explained, because
the row says which rate it used.

## Token semantics

Three rules keep the arithmetic honest, and they live in the normalized usage
rather than in a provider check inside the estimator:

- **Provider totals win.** When the usage object includes `total_tokens`,
  that figure is stored as-is. Cached and reasoning tokens are never added
  on top of it. A computed total (Anthropic: input + cache + output;
  OpenAI-shaped: input + output) is used only when the provider did not
  report one.
- **Cached tokens are billed once.** Anthropic reports cache counters *in
  addition to* `input_tokens`; the OpenAI-shaped APIs (Codex, Grok) report
  cached tokens *inside* `input_tokens`. `NormalizedUsage.
  cached_tokens_included_in_input` records which, and the estimator subtracts
  the cached portion out of the full-price input only in the second case.
  Without that, identifying a cache hit would *raise* the estimate.
- **Reasoning tokens are not billed twice.** Every normalizer already folds
  reasoning inside `output_tokens` where the provider does, so
  `reasoning_per_million` stays `None` for every catalogued provider. It
  exists for a provider that starts metering reasoning as a separate line
  item, and only then does the estimator add it.

Cache reads and cache writes are stored separately (`cache_read_tokens` /
`cache_write_tokens`) because a cache write is a premium metered operation
while a cache read is the discount — a report that cannot tell them apart
cannot price them.

## Relationship to model routing calibration

Dynamic Model Routing scores models against a 1–5 *relative* cost rank
(`dynamic_router.model_cost`), and Model Routing Calibration keeps that rank
and other routing inputs fresh from external sources. That is a routing
concern: "which of these is cheaper".

This catalog is a reporting concern: "what did this cost". They are
deliberately separate, and a change to one is not a change to the other. The
dashboard used to derive cost from the routing rank, which could not
distinguish two differently-billed models that happened to share a rank;
issue #295 replaced that.
