"""Per-prompt AI token usage: provider normalization, cost estimation, and the
GitHub-facing report.

Centralizing this here (rather than inside each provider runner) is what lets
``swarm_issue_worker.run_ai`` and ``run_router`` record every model
invocation the same way regardless of which of the three provider CLIs made
it — see issue #280. A new agent that calls through those two entry points
gets usage tracking automatically; nothing here should ever be duplicated
per-agent.

Token counts are only ever what the provider itself reported. When a
provider's CLI output does not contain a recognizable usage object, the
normalizer returns ``None`` rather than guessing — callers must not
estimate tokens.
"""
from __future__ import annotations

import dataclasses
import enum
import json
import math
from collections.abc import Mapping
from typing import Any, Iterable

import model_pricing


class AgentType(str, enum.Enum):
    """Which kind of agent made the call. Prefer this over a bare string so a
    typo can't silently create a new, uncounted bucket in GitHub/DB reports."""

    PRIMARY = "primary"
    ROUTER = "router"
    PLANNER = "planner"
    ADVERSARIAL_UAT = "adversarial_uat"
    ADVERSARIAL_CYBERSECURITY = "adversarial_cybersecurity"
    REVIEW = "review"
    REMEDIATION = "remediation"
    SUMMARIZER = "summarizer"
    OTHER = "other"


class PromptType(str, enum.Enum):
    """What this particular call within an agent's lifecycle was doing."""

    INITIAL = "initial"
    CONTINUATION = "continuation"
    TOOL_FOLLOWUP = "tool_followup"
    REVIEW = "review"
    ADVERSARIAL_SCAN = "adversarial_scan"
    REMEDIATION = "remediation"
    RETRY = "retry"
    ISSUE_CREATION = "issue_creation"
    SUMMARY = "summary"


AGENT_LABELS: dict[str, str] = {
    AgentType.PRIMARY.value: "Primary",
    AgentType.ROUTER.value: "Router",
    AgentType.PLANNER.value: "Planner",
    AgentType.ADVERSARIAL_UAT.value: "UAT Adversarial",
    AgentType.ADVERSARIAL_CYBERSECURITY.value: "Cyber Adversarial",
    AgentType.REVIEW.value: "Review",
    AgentType.REMEDIATION.value: "Remediation",
    AgentType.SUMMARIZER.value: "Summarizer",
    AgentType.OTHER.value: "Other",
}

PROMPT_LABELS: dict[str, str] = {
    PromptType.INITIAL.value: "Implementation",
    PromptType.CONTINUATION.value: "Continuation",
    PromptType.TOOL_FOLLOWUP.value: "Tool follow-up",
    PromptType.REVIEW.value: "Review",
    PromptType.ADVERSARIAL_SCAN.value: "Adversarial Scan",
    PromptType.REMEDIATION.value: "Remediation",
    PromptType.RETRY.value: "Retry",
    PromptType.ISSUE_CREATION.value: "Issue Creation",
    PromptType.SUMMARY.value: "Summary",
}


@dataclasses.dataclass
class NormalizedUsage:
    """Provider usage translated into one common shape.

    ``None`` on any field means the provider did not report that figure —
    never a fabricated ``0``. ``total_tokens`` is the provider's own reported
    total when it gave one — that figure is authoritative and is never
    replaced by summing the other fields. Otherwise it is computed by the
    normalizer that built this object, following that provider's own
    input/cache semantics (see each ``normalize_*_usage`` function).

    ``cached_tokens_included_in_input`` records which of the two cache wire
    semantics this usage follows, so ``estimate_cost`` can bill it correctly
    without knowing which provider produced it: Anthropic's cache counters are
    additional to ``input_tokens`` (``False``, the default); OpenAI-shaped
    usage (Codex, Grok) already counts cached tokens inside ``input_tokens``
    (``True``). See ``_openai_style_usage`` and ``normalize_claude_usage``.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_input_tokens: int | None = None
    #: The two halves of ``cached_input_tokens``, kept apart because they are
    #: billed at different rates where the provider distinguishes them (a
    #: cache *write* is a metered operation; a cache *read* is the discount).
    #: ``None`` means the provider did not report that class at all, which is
    #: not the same as reporting zero of it.
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens_included_in_input: bool = False
    reported_cost: float | None = None
    usage_scope: str = "invocation"
    raw: dict[str, Any] = dataclasses.field(default_factory=dict)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
        return number if 0 <= number <= 2**63 - 1 and number == float(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _as_cost(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _sum_optional(*values: int | None) -> int | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present)


def _iter_json_events(raw: str) -> Iterable[dict[str, Any]]:
    """Every JSON object line in ``raw`` (a `stream-json`/JSONL transcript).

    Falls back to parsing the whole text as one JSON object so a plain
    ``--output-format json`` response (no newlines) is handled the same way.
    Non-JSON lines (CLI tracing noise mixed into a merged stdout/stderr
    capture) are skipped rather than raising.
    """
    saw_any = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        saw_any = True
        if isinstance(event, dict):
            yield event
    if saw_any:
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return
    if isinstance(payload, dict):
        yield payload


def normalize_claude_usage(raw: str) -> NormalizedUsage | None:
    """Usage from a Claude Code CLI `json`/`stream-json` transcript.

    The final ``type: "result"`` event carries a ``usage`` object with
    ``input_tokens``/``output_tokens`` plus ``cache_creation_input_tokens``
    and ``cache_read_input_tokens``. Anthropic bills both cache counters
    *in addition to* ``input_tokens`` (they are not a subset of it), so both
    are folded into ``cached_input_tokens``. When the provider also supplies
    ``total_tokens``, that figure is kept as-is; cached and reasoning tokens
    are never added on top of it. The additive input + cache + output total
    is only computed when the provider did not report one.
    """
    usage_payload: dict[str, Any] | None = None
    reported_cost = None
    for event in _iter_json_events(raw):
        if event.get("type") == "result":
            reported_cost = _as_cost(event.get("total_cost_usd"))
            if isinstance(event.get("usage"), dict):
                usage_payload = event["usage"]
    if usage_payload is None:
        return NormalizedUsage(reported_cost=reported_cost) if reported_cost is not None else None
    input_tokens = _as_int(usage_payload.get("input_tokens"))
    output_tokens = _as_int(usage_payload.get("output_tokens"))
    cache_read = _as_int(usage_payload.get("cache_read_input_tokens"))
    cache_creation = _as_int(usage_payload.get("cache_creation_input_tokens"))
    cached_input_tokens = _sum_optional(cache_read, cache_creation)
    # A supplied total is the provider's own figure. Recomputing
    # input + cache + output would invent a different number when the
    # provider already counted those fields (or reported only a total).
    total_tokens = _as_int(usage_payload.get("total_tokens"))
    if total_tokens is None:
        total_tokens = _sum_optional(input_tokens, cached_input_tokens, output_tokens)
    return NormalizedUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=_as_int(usage_payload.get("reasoning_tokens")),
        cached_input_tokens=cached_input_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_creation,
        total_tokens=total_tokens,
        reported_cost=reported_cost,
        raw={"usage": usage_payload},
    )


def _openai_style_usage(payload: dict[str, Any]) -> NormalizedUsage:
    """Shared normalization for the OpenAI-shaped usage object Codex and Grok
    both report (Grok's CLI is OpenAI-compatible).

    Unlike Claude, ``input_tokens``/``prompt_tokens`` here already *include*
    any cached tokens — the ``*_details.cached_tokens`` figure is a
    breakdown, not an addition — and ``output_tokens``/``completion_tokens``
    already include reasoning tokens. So the total is just input + output;
    adding cached or reasoning again would double-count.
    """
    input_tokens = _as_int(payload.get("input_tokens"))
    if input_tokens is None:
        input_tokens = _as_int(payload.get("prompt_tokens"))
    output_tokens = _as_int(payload.get("output_tokens"))
    if output_tokens is None:
        output_tokens = _as_int(payload.get("completion_tokens"))
    total_tokens = _as_int(payload.get("total_tokens"))

    cached = payload.get("cached_input_tokens")
    if cached is None:
        details = payload.get("input_tokens_details")
        if isinstance(details, dict):
            cached = details.get("cached_tokens")
    if cached is None:
        details = payload.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = details.get("cached_tokens")

    reasoning = payload.get("reasoning_output_tokens")
    if reasoning is None:
        reasoning = payload.get("reasoning_tokens")
    if reasoning is None:
        details = payload.get("output_tokens_details")
        if isinstance(details, dict):
            reasoning = details.get("reasoning_tokens")
    if reasoning is None:
        details = payload.get("completion_tokens_details")
        if isinstance(details, dict):
            reasoning = details.get("reasoning_tokens")

    if total_tokens is None:
        total_tokens = _sum_optional(input_tokens, output_tokens)

    return NormalizedUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=_as_int(reasoning),
        cached_input_tokens=_as_int(cached),
        # OpenAI-shaped usage reports one cached figure and never meters a
        # cache write of its own, so every cached token here is a read.
        cache_read_tokens=_as_int(cached),
        cache_write_tokens=None,
        total_tokens=total_tokens,
        cached_tokens_included_in_input=True,
        raw={"usage": payload},
    )


def normalize_codex_usage(raw: str) -> NormalizedUsage | None:
    """Usage from a Codex CLI ``--json`` JSONL event transcript.

    Prefer ``turn.completed`` usage for the current invocation, then a bare
    top-level ``usage`` object for older CLI versions. ``token_count``'s
    ``info.total_token_usage`` is a session total and needs a resume baseline.
    """
    usage_payload = None
    turn_usage = None
    cumulative = None
    # turn.completed is authoritative for this invocation; token_count totals
    # may cover the *whole resumed session*, not just this new CLI process.
    for event in _iter_json_events(raw):
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            turn_usage = event["usage"]
        elif event.get("type") == "token_count":
            info = event.get("info")
            candidate = info.get("total_token_usage") if isinstance(info, dict) else None
            if isinstance(candidate, dict):
                cumulative = candidate
            elif isinstance(event.get("usage"), dict):
                usage_payload = event["usage"]
        elif isinstance(event.get("usage"), dict):
            usage_payload = event["usage"]
    if turn_usage is not None:
        return _openai_style_usage(turn_usage)
    if usage_payload is not None:
        return _openai_style_usage(usage_payload)
    if cumulative is not None:
        usage = _openai_style_usage(cumulative)
        usage.usage_scope = "session"
        return usage
    return None


def normalize_grok_usage(raw: str) -> NormalizedUsage | None:
    """Usage from Grok CLI ``--output-format json``'s single JSON object.

    The CLI's own payload is ``{"text": ..., "sessionId": ...}``; a ``usage``
    key alongside those, when present, follows the same OpenAI-compatible
    shape Codex uses.
    """
    usage_payload: dict[str, Any] | None = None
    for event in _iter_json_events(raw):
        if isinstance(event.get("usage"), dict):
            usage_payload = event["usage"]
    if usage_payload is None:
        return None
    return _openai_style_usage(usage_payload)


_NORMALIZERS = {
    "claude": normalize_claude_usage,
    "codex": normalize_codex_usage,
    "grok": normalize_grok_usage,
}


def normalize_usage(provider: str, raw: str) -> NormalizedUsage | None:
    """Dispatch to the right provider normalizer. Never raises: a malformed
    or unrecognized transcript just means no usage could be recorded."""
    normalizer = _NORMALIZERS.get(str(provider or "").strip().lower())
    if normalizer is None or not raw:
        return None
    try:
        return normalizer(raw)
    except (TypeError, ValueError, AttributeError):
        return None


# Per-model dollar pricing lives in ``model_pricing.py`` — a versioned,
# effective-dated catalog (issue #295). It replaced the five generic
# rank-keyed rate pairs this module used to carry, which could not tell two
# differently-billed models apart once they shared a routing cost rank.
# ``CACHED_INPUT_RATE_FACTOR`` stays exported here for the callers that
# already imported it; the catalog is now what defines it.
CACHED_INPUT_RATE_FACTOR = model_pricing.CACHED_INPUT_RATE_FACTOR
DEFAULT_CURRENCY = model_pricing.DEFAULT_CURRENCY


def estimate_cost_detailed(
    model: str,
    usage: NormalizedUsage | None,
    *,
    provider: str = "",
    at: str = "",
) -> model_pricing.CostEstimate:
    """Cost one invocation and return the provenance with it.

    ``at`` is the invocation's own start timestamp, so a call is priced with
    the rate that was effective when it ran rather than whatever the catalog
    says today. The returned estimate carries the catalog version, rate id,
    source and the individual rates used, which is what lets a stored
    estimate still be explained after the catalog moves on.

    Never raises and never guesses: an unknown model, an ambiguous alias or a
    gap in the effective windows comes back with ``cost=None`` and a status
    saying which, and the caller reports the tokens with no money attached.
    """
    if usage is None:
        return model_pricing.CostEstimate(
            cost=None,
            status=model_pricing.PRICING_STATUS_NO_USAGE,
            catalog_version=model_pricing.PRICING_CATALOG_VERSION,
        )
    try:
        return model_pricing.estimate_invocation_cost(
            model=model,
            provider=provider,
            at=at,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            cached_tokens_included_in_input=usage.cached_tokens_included_in_input,
        )
    except Exception:  # noqa: BLE001 - pricing must never break AI work
        return model_pricing.CostEstimate(
            cost=None,
            status=model_pricing.PRICING_STATUS_NO_EFFECTIVE_PRICE,
            catalog_version=model_pricing.PRICING_CATALOG_VERSION,
        )


def estimate_cost(
    model: str,
    usage: NormalizedUsage | None,
    *,
    provider: str = "",
    at: str = "",
) -> float | None:
    """Estimated cost of one invocation, or ``None`` when it cannot be priced.

    The thin form of ``estimate_cost_detailed`` for callers that only want the
    number. Reasoning tokens are not charged separately unless the catalog
    entry says the provider bills them that way: every normalizer above
    already folds them inside ``output_tokens`` where the provider does, so
    charging them again would double-count.
    """
    return estimate_cost_detailed(model, usage, provider=provider, at=at).cost


def cache_metrics(usage: NormalizedUsage | None, estimate: model_pricing.CostEstimate) -> dict[str, Any]:
    """Provider-aware denominator and net API-equivalent cache discount.

    Cache write premiums count against savings. Missing rates/counters stay
    unavailable; these figures never claim realized subscription savings.
    """
    result = {"cache_input_tokens": None, "cache_savings_estimate": None}
    if usage is None or usage.input_tokens is None or usage.cache_read_tokens is None:
        return result
    read, write = usage.cache_read_tokens, usage.cache_write_tokens
    if usage.cached_tokens_included_in_input:
        total = usage.input_tokens
        write = 0  # OpenAI does not expose a separately metered cache write.
    elif write is not None:
        total = usage.input_tokens + read + write
    else:
        return result
    if read > total:
        return result
    result["cache_input_tokens"] = total
    rate, cached_rate, write_rate = (estimate.input_rate_per_million,
                                    estimate.cached_input_rate_per_million,
                                    estimate.cache_write_rate_per_million)
    if rate is not None and cached_rate is not None and (not write or write_rate is not None):
        result["cache_savings_estimate"] = (
            read * (rate - cached_rate) + (write or 0) * (rate - (write_rate if write_rate is not None else rate))
        ) / 1_000_000
    return result


_USAGE_COUNTERS = ("input_tokens", "output_tokens", "reasoning_tokens", "cached_input_tokens",
                   "cache_read_tokens", "cache_write_tokens", "total_tokens")


def invocation_usage(usage: NormalizedUsage | None, resumed: bool,
                     baseline: object | None) -> NormalizedUsage | None:
    if usage is None or usage.usage_scope != "session" or not resumed:
        return usage
    prior = baseline if isinstance(baseline, Mapping) else {}
    values = {}
    for name in _USAGE_COUNTERS:
        current, previous = getattr(usage, name), prior.get(name)
        values[name] = (current - previous if type(previous) is int and current is not None
                        and 0 <= previous <= current else None)
    return dataclasses.replace(usage, **values)


@dataclasses.dataclass
class UsageRecord:
    """One AI model invocation's telemetry — the row shape used for the
    in-memory/persisted event list, the DB table, and the GitHub report."""

    id: str
    sequence: int
    agent_type: str
    prompt_type: str
    provider: str
    model: str
    reasoning_effort: str
    attempt_number: int
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    cached_input_tokens: int | None
    total_tokens: int | None
    estimated_cost: float | None
    currency: str
    started_at: str
    completed_at: str
    duration_ms: int | None
    success: bool
    error_type: str
    workflow_run_id: str = ""
    agent_run_id: str = ""
    prompt_id: str = ""
    #: The cache-read/cache-write split behind ``cached_input_tokens``. Kept
    #: separately because the two are billed differently where a provider
    #: distinguishes them, and because a report must be able to say which it
    #: was rather than only how many cached tokens there were.
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    #: Pricing provenance (issue #295). Persisted with the estimate so it can
    #: be reproduced and explained later, and so a catalog update never
    #: silently restates a historical cost: nothing recomputes these.
    #: ``pricing_status`` is ``priced`` or the reason there is no money on
    #: this row — an unpriced invocation still keeps all of its tokens.
    session_reused: bool | None = None
    session_role: str = ""
    cache_input_tokens: int | None = None
    cache_savings_estimate: float | None = None
    reported_cost: float | None = None
    pricing_status: str = ""
    pricing_version: str = ""
    pricing_rate_id: str = ""
    pricing_source: str = ""
    input_rate_per_million: float | None = None
    cached_input_rate_per_million: float | None = None
    cache_write_rate_per_million: float | None = None
    output_rate_per_million: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UsageRecord":
        fields = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in fields})


def _format_int(value: int | None) -> str:
    return f"{value:,}" if isinstance(value, int) else "—"


def _format_cost(value: float | None) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    sign = "-" if value < 0 else ""
    if value != 0 and abs(value) < 0.01:
        return f"{sign}${abs(value):.4f}"
    return f"{sign}${abs(value):,.2f}"


def render_ai_usage_markdown(events: Iterable[dict[str, Any]]) -> str:
    """The ``### AI Usage`` section for the GitHub completion comment: one
    row per recorded invocation plus a totals block. Empty when there is
    nothing recorded, so a repository/run with no captured usage does not
    grow the comment with an empty table."""
    records = [UsageRecord.from_dict(event) for event in events]
    if not records:
        return ""
    lines = [
        "### AI Usage",
        "",
        "| # | Agent | Provider / Model | Prompt | Input | Cached | Reasoning | Output | Total "
        "| Estimated cost |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    total_input = total_cached = total_reasoning = total_output = total_tokens = None
    total_cost = 0.0
    any_cost = False
    for index, record in enumerate(records, start=1):
        agent_label = AGENT_LABELS.get(record.agent_type, record.agent_type or "Other")
        prompt_label = PROMPT_LABELS.get(record.prompt_type, record.prompt_type or "—")
        provider_model = f"{record.provider} / {record.model}" if record.provider else record.model
        lines.append(
            f"| {index} | {agent_label} | {provider_model} | {prompt_label} | "
            f"{_format_int(record.input_tokens)} | {_format_int(record.cached_input_tokens)} | "
            f"{_format_int(record.reasoning_tokens)} | {_format_int(record.output_tokens)} | "
            f"{_format_int(record.total_tokens)} | {_format_cost(record.estimated_cost)} |"
        )
        if record.input_tokens is not None:
            total_input = (0 if total_input is None else total_input) + record.input_tokens
        if record.cached_input_tokens is not None:
            total_cached = (0 if total_cached is None else total_cached) + record.cached_input_tokens
        if record.reasoning_tokens is not None:
            total_reasoning = (0 if total_reasoning is None else total_reasoning) + record.reasoning_tokens
        if record.output_tokens is not None:
            total_output = (0 if total_output is None else total_output) + record.output_tokens
        if record.total_tokens is not None:
            total_tokens = (0 if total_tokens is None else total_tokens) + record.total_tokens
        if record.estimated_cost is not None:
            total_cost += record.estimated_cost
            any_cost = True
    lines.append("")
    lines.append("**AI Usage Totals**")
    lines.append("")
    lines.append(f"**Input:** {_format_int(total_input)}  ")
    lines.append(f"**Cached Input:** {_format_int(total_cached)}  ")
    lines.append(f"**Reasoning:** {_format_int(total_reasoning)}  ")
    lines.append(f"**Output:** {_format_int(total_output)}  ")
    lines.append(f"**Total Tokens:** {_format_int(total_tokens)}  ")
    lines.append(f"**Estimated Cost:** {_format_cost(total_cost) if any_cost else '—'}  ")
    lines.append(f"**AI Invocations:** {len(records)}")
    read = _sum_optional(*(r.cache_read_tokens for r in records))
    write = _sum_optional(*(r.cache_write_tokens for r in records))
    measured = [r for r in records if r.cache_input_tokens is not None and r.cache_read_tokens is not None]
    denominator = sum(r.cache_input_tokens for r in measured)
    efficiency = f"{sum(r.cache_read_tokens for r in measured) / denominator:.1%}" if denominator else "—"
    savings = _sum_optional(*(r.cache_savings_estimate for r in records))
    reported = _sum_optional(*(r.reported_cost for r in records))
    known = [r for r in records if r.session_reused is not None]
    reuse = f"{sum(bool(r.session_reused) for r in known)} / {len(known)}" if known else "—"
    lines.extend(["", "**Cache efficiency**", "",
                  f"**Cache read / write tokens:** {_format_int(read)} / {_format_int(write)}  ",
                  f"**Cache hit efficiency:** {efficiency} ({len(measured)} measured invocations)  ",
                  f"**Session reuse:** {reuse}  ",
                  f"**Provider-reported cost:** {_format_cost(reported)}  ",
                  f"**Estimated API-equivalent cache savings:** {_format_cost(savings)}  ",
                  "Reported costs are CLI usage figures, not verified subscription charges. "
                  "Estimated savings are not realized billing savings. — means unavailable."])
    return "\n".join(lines) + "\n"


def format_usage_log_line(
    *,
    issue_number: int | None,
    agent_type: str,
    provider: str,
    model: str,
    usage: NormalizedUsage | None,
    cost: float | None,
) -> str:
    """One structured ``AI_USAGE_RECORDED`` diagnostic line (issue #280 item
    14). Never includes prompt text or credentials — token counts and
    identifiers only."""
    input_tokens = usage.input_tokens if usage else None
    output_tokens = usage.output_tokens if usage else None
    total_tokens = usage.total_tokens if usage else None
    return (
        "AI_USAGE_RECORDED "
        f"issue={issue_number if issue_number is not None else '-'} "
        f"agent={agent_type} provider={provider} model={model} "
        f"input_tokens={input_tokens if input_tokens is not None else 'NULL'} "
        f"output_tokens={output_tokens if output_tokens is not None else 'NULL'} "
        f"total_tokens={total_tokens if total_tokens is not None else 'NULL'} "
        f"cost={f'{cost:.6f}' if cost is not None else 'NULL'}"
    )
