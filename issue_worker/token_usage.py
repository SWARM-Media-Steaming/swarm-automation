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
from typing import Any, Iterable


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
    total when it gave one; otherwise it is computed by the normalizer that
    built this object, following that provider's own input/cache semantics
    (see the module docstring of each ``normalize_*_usage`` function).
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_input_tokens: int | None = None
    total_tokens: int | None = None
    raw: dict[str, Any] = dataclasses.field(default_factory=dict)


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
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
    are folded into ``cached_input_tokens`` and added into the computed
    total rather than treated as already counted.
    """
    usage_payload: dict[str, Any] | None = None
    for event in _iter_json_events(raw):
        if event.get("type") == "result" and isinstance(event.get("usage"), dict):
            usage_payload = event["usage"]
    if usage_payload is None:
        return None
    input_tokens = _as_int(usage_payload.get("input_tokens"))
    output_tokens = _as_int(usage_payload.get("output_tokens"))
    cache_read = _as_int(usage_payload.get("cache_read_input_tokens"))
    cache_creation = _as_int(usage_payload.get("cache_creation_input_tokens"))
    cached_input_tokens = _sum_optional(cache_read, cache_creation)
    total_tokens = _sum_optional(input_tokens, cached_input_tokens, output_tokens)
    return NormalizedUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=None,
        cached_input_tokens=cached_input_tokens,
        total_tokens=total_tokens,
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
        total_tokens=total_tokens,
        raw={"usage": payload},
    )


def normalize_codex_usage(raw: str) -> NormalizedUsage | None:
    """Usage from a Codex CLI ``--json`` JSONL event transcript.

    Looks for the last ``type: "token_count"`` event's
    ``info.total_token_usage`` object (the cumulative usage for the whole
    turn); falls back to a bare top-level ``usage`` key for robustness
    against CLI version differences.
    """
    usage_payload: dict[str, Any] | None = None
    for event in _iter_json_events(raw):
        if event.get("type") == "token_count":
            info = event.get("info")
            candidate = info.get("total_token_usage") if isinstance(info, dict) else None
            if isinstance(candidate, dict):
                usage_payload = candidate
                continue
            if isinstance(event.get("usage"), dict):
                usage_payload = event["usage"]
        elif isinstance(event.get("usage"), dict):
            usage_payload = event["usage"]
    if usage_payload is None:
        return None
    return _openai_style_usage(usage_payload)


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


# Approximate USD list price per million tokens, keyed by the 1 (cheapest)
# through 5 (most expensive) relative rank already assigned to every model in
# dynamic_router's catalog (``model_cost``). There is no per-model dollar
# pricing table anywhere in this app; reusing that existing routing
# calibration data — rather than inventing a second, parallel pricing concept
# — means a cost estimate changes if that ranking changes, with nothing here
# to edit. Update these rates directly if real pricing drifts; nothing that
# calls ``estimate_cost`` needs to change.
_RANK_RATES_PER_MILLION: dict[int, dict[str, float]] = {
    1: {"input": 0.25, "output": 1.25},
    2: {"input": 0.50, "output": 2.50},
    3: {"input": 1.00, "output": 5.00},
    4: {"input": 3.00, "output": 15.00},
    5: {"input": 5.00, "output": 25.00},
}
# Cached/reused input tokens are conventionally billed at a steep discount off
# the base input rate (prompt caching across providers is commonly ~10% of
# the fresh-input price).
CACHED_INPUT_RATE_FACTOR = 0.1
DEFAULT_CURRENCY = "USD"


def estimate_cost(model: str, usage: NormalizedUsage | None) -> float | None:
    """Estimated USD cost of one invocation, or ``None`` when it cannot be
    estimated (no usage, or the model has no cost rank). Reasoning tokens are
    not charged separately: every normalizer above already folds them inside
    ``output_tokens`` when the provider does, per that provider's own
    semantics, so charging them again here would double-count."""
    if usage is None:
        return None
    from dynamic_router import model_cost  # local import: keeps this module import-light

    rank = model_cost(model)
    if rank is None:
        return None
    rates = _RANK_RATES_PER_MILLION.get(rank)
    if rates is None:
        return None
    try:
        input_cost = (usage.input_tokens or 0) / 1_000_000 * rates["input"]
        cached_cost = (
            (usage.cached_input_tokens or 0) / 1_000_000 * rates["input"] * CACHED_INPUT_RATE_FACTOR
        )
        output_cost = (usage.output_tokens or 0) / 1_000_000 * rates["output"]
        return round(input_cost + cached_cost + output_cost, 6)
    except (TypeError, ValueError):
        return None


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

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UsageRecord":
        fields = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in fields})


def _format_int(value: int | None) -> str:
    return f"{value:,}" if isinstance(value, int) else "—"


def _format_cost(value: float | None) -> str:
    return f"${value:,.2f}" if isinstance(value, (int, float)) else "—"


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
        "| # | Agent | Provider / Model | Prompt | Input | Cached | Reasoning | Output | Total | Cost |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    total_input = total_cached = total_reasoning = total_output = total_tokens = 0
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
        total_input += record.input_tokens or 0
        total_cached += record.cached_input_tokens or 0
        total_reasoning += record.reasoning_tokens or 0
        total_output += record.output_tokens or 0
        total_tokens += record.total_tokens or 0
        if record.estimated_cost is not None:
            total_cost += record.estimated_cost
            any_cost = True
    lines.append("")
    lines.append("**AI Usage Totals**")
    lines.append("")
    lines.append(f"Input: {total_input:,}  ")
    lines.append(f"Cached Input: {total_cached:,}  ")
    lines.append(f"Reasoning: {total_reasoning:,}  ")
    lines.append(f"Output: {total_output:,}  ")
    lines.append(f"Total Tokens: {total_tokens:,}  ")
    lines.append(f"Estimated Cost: {_format_cost(total_cost) if any_cost else '—'}  ")
    lines.append(f"AI Invocations: {len(records)}")
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
