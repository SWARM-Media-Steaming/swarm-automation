"""Optional dynamic AI routing for one GitHub issue.

The router grades the original issue, scores its complexity, and picks which of
the enabled AI tools runs the work and on which model. The original issue text
is never rewritten. Keep the default tier tables and provider strengths in sync
with ``default_routing_tiers`` / ``ProviderSettings`` in ``src/config.rs``.

The model is the router's own call, not a table lookup. It is shown the full
cross-provider catalog (``_MODEL_CATALOG``) — every model, what it is good at,
and what it costs relative to the others, minus anything needing usage credits
the operator has not allowed — and the operator's ``routing_optimization``
preference decides how to weigh those costs: ``"cost"`` asks for the cheapest
model that can plausibly do the work, escalating on the graded risk score,
while ``"best"`` asks for the best fit for the task and ignores price.

The operator's ``routing_tiers`` table is still graded against and still shown
as reference, and it remains the deterministic safety net: if the router names
a model outside the catalog it gets exactly one corrective follow-up call with
the catalog restated (``build_model_correction_prompt``), and a second invalid
answer resolves through ``tier_for_complexity`` instead — so a persistently
wrong router response degrades to the old behavior rather than stalling the
issue. The same tier path covers a decision where the router's tool pick was
overruled, since its model then belongs to a different tool.

``_MODEL_CATALOG`` exists only here: unlike the tier tables and provider
strengths, it is never sent to the app or persisted in config.json, so there is
no matching copy to keep in sync in ``src/config.rs``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from issue_images import (
    assistant_result_text,
    claude_stream_message,
    codex_image_flags,
    grok_prompt_json,
    inlined_images,
)


PROMPT_GRADES = (
    "A+",
    "A",
    "A-",
    "B+",
    "B",
    "B-",
    "C+",
    "C",
    "C-",
    "D+",
    "D",
    "D-",
    "F",
)
RISK_LEVELS = ("low", "medium", "high")
CONTEXT_REQUIREMENTS = ("small", "medium", "large")
EFFORT_LABELS = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "XHigh",
    "max": "Max",
}

# Complexity bands from the feature request. Models stay in this table so the
# rest of the app does not hardcode which slug serves which score.
_DEFAULT_TIER_ROWS: dict[str, tuple[tuple[int, int, str, str], ...]] = {
    "claude": (
        (1, 3, "claude-haiku-4-5", "low"),
        (4, 6, "claude-sonnet-5", "medium"),
        (7, 8, "claude-opus-5", "high"),
        (9, 10, "claude-opus-5", "max"),
    ),
    "codex": (
        (1, 3, "gpt-5.6-luna", "low"),
        (4, 6, "gpt-5.6-terra", "medium"),
        (7, 8, "gpt-5.6-sol", "high"),
        (9, 10, "gpt-6-astra", "xhigh"),
    ),
    "grok": (
        (1, 3, "grok-4.6", "low"),
        (4, 6, "grok-4.6", "medium"),
        (7, 8, "grok-4.6", "high"),
        (9, 10, "grok-4.6", "xhigh"),
    ),
}
_DEFAULT_ROUTER = {
    "claude": ("claude-haiku-4-5", "low"),
    "codex": ("gpt-5.6-luna", "low"),
    "grok": ("grok-4.6", "low"),
}

# What each AI tool tends to be good at. The router weighs these when it picks
# which enabled tool receives an issue, so "Codex is better at A, Grok at B" is
# a setting an operator edits rather than a judgement baked into this file.
# Keep in sync with ``provider_strengths_preset`` in ``src/config.rs``.
_DEFAULT_PROVIDER_STRENGTHS: dict[str, str] = {
    "claude": (
        "Multi-file refactors, following an existing codebase's conventions, careful "
        "review of someone else's work, and writing documentation or tests in the "
        "surrounding style."
    ),
    "codex": (
        "Precise bug fixes, test-driven changes, and long autonomous edit-run-verify "
        "loops where the work is checked by running it."
    ),
    "grok": (
        "Fast turnarounds on well-scoped changes, scripting and configuration work, "
        "and quick orientation in unfamiliar code."
    ),
}

# How the router weighs cost against capability, from the operator's
# "Optimize routing for cost" toggle (``routing_optimization`` in config.json).
# "cost" asks for the cheapest model that can plausibly do the work, escalating
# on risk; "best" asks for the best fit for the work and ignores price.
ROUTING_OPTIMIZATIONS = ("cost", "best")
DEFAULT_ROUTING_OPTIMIZATION = "best"

# Model families billed against a separate usage-credit balance rather than the
# plan allowance. Keep in sync with ``USAGE_CREDIT_MODELS`` in ``src/tools.rs``:
# the app already hides these everywhere while ``allow_usage_credit_models`` is
# off, so the router must not be offered one either. Matched by family, so both
# the alias (``fable``) and the full name (``claude-fable-5-1``) are caught.
USAGE_CREDIT_FAMILIES = ("fable",)

# Relative price of a model, 1 (cheapest) through 5 (most expensive), comparable
# across providers — the router needs to rank a Claude model against a Codex one
# to optimize for cost, which a per-provider tier table cannot express.
_COST_LABELS = {
    1: "lowest cost",
    2: "low cost",
    3: "moderate cost",
    4: "high cost",
    5: "highest cost",
}


@dataclasses.dataclass(frozen=True)
class CatalogModel:
    """One model the router may name, and what it costs relative to the rest."""

    provider: str
    model: str
    cost: int
    description: str

    @property
    def requires_usage_credits(self) -> bool:
        return requires_usage_credits(self.model)

    @property
    def cost_label(self) -> str:
        return _COST_LABELS.get(self.cost, "")


# The canonical, complete cross-provider model catalog: every model the router
# may choose, what it is good at, and what it costs relative to the others.
# A provider's tier table already places its own models on an increasing
# capability ladder; this spells that out in words and adds the price ordering,
# so the router picks a model knowing both what it is for and what it costs.
# The whole (credit-filtered) list is shown in the router prompt and is what a
# named model is validated against — a model missing from here cannot be routed
# to. Never edited by an operator and never leaves this process: unlike the tier
# tables and provider strengths, there is no counterpart in ``src/config.rs`` to
# keep in sync.
_MODEL_CATALOG: tuple[CatalogModel, ...] = (
    CatalogModel(
        "claude",
        "claude-haiku-4-5",
        1,
        "Claude's fastest, least expensive model. Best for small, well-scoped, "
        "mechanical changes where turnaround matters more than deep reasoning.",
    ),
    CatalogModel(
        "claude",
        "claude-sonnet-5",
        3,
        "Claude's balanced, general-purpose model. The default choice for typical "
        "multi-file feature work and bug fixes.",
    ),
    CatalogModel(
        "claude",
        "claude-opus-5",
        4,
        "Claude's most capable model. Reserved for the largest, most ambiguous, or "
        "highest-risk work, where the deepest reasoning is worth the extra cost and time.",
    ),
    CatalogModel(
        "claude",
        "claude-fable-5",
        5,
        "An earlier release of Claude's usage-credit model, kept for comparison "
        "against the current one.",
    ),
    CatalogModel(
        "claude",
        "claude-fable-5-1",
        5,
        "Claude's deepest-reasoning model, billed against a separate usage-credit "
        "balance rather than the plan allowance. For the hardest work, where that "
        "extra cost is accepted deliberately.",
    ),
    CatalogModel(
        "codex",
        "gpt-5.6-luna",
        1,
        "Codex's lightest, fastest model. Efficient for small, mechanical, "
        "well-defined changes.",
    ),
    CatalogModel(
        "codex",
        "gpt-5.6-terra",
        3,
        "Codex's mid-tier model. A solid default for typical feature work and bug fixes.",
    ),
    CatalogModel(
        "codex",
        "gpt-5.6-sol",
        4,
        "Codex's high-capability model. For larger or subtler changes that need "
        "careful, verified reasoning.",
    ),
    CatalogModel(
        "codex",
        "gpt-6-astra",
        5,
        "Codex's most capable model. Reserved for sweeping, high-risk, or deeply "
        "ambiguous work.",
    ),
    CatalogModel(
        "grok",
        "grok-4.5",
        1,
        "An earlier, smaller Grok model, kept for compatibility where configured.",
    ),
    CatalogModel(
        "grok",
        "grok-4.6",
        2,
        "Grok's general-purpose coding model, balancing speed and capability across a "
        "wide range of complexity.",
    ),
    CatalogModel(
        "grok",
        "grok-4.7-build-fast",
        3,
        "The fast variant of Grok's most capable model. Trades some depth for "
        "noticeably quicker turnaround on well-scoped work.",
    ),
    CatalogModel(
        "grok",
        "grok-4.7",
        4,
        "Grok's most capable model, offering the deepest reasoning in the Grok line.",
    ),
)

# Kept as the by-slug view of the catalog above: the description shown next to a
# tier, folded into ``tier_explanation``, and read back by anything holding only
# a model name.
_MODEL_DESCRIPTIONS: dict[str, str] = {entry.model: entry.description for entry in _MODEL_CATALOG}
_MODEL_COSTS: dict[str, int] = {entry.model: entry.cost for entry in _MODEL_CATALOG}


def requires_usage_credits(model: str) -> bool:
    """Whether a model bills against the separate usage-credit balance."""
    parts = re.split(r"[-_]", str(model or "").strip().lower())
    return any(part in USAGE_CREDIT_FAMILIES for part in parts)


def normalize_routing_optimization(value: Any) -> str:
    key = str(value or "").strip().lower()
    return key if key in ROUTING_OPTIMIZATIONS else DEFAULT_ROUTING_OPTIMIZATION


def model_catalog(
    providers: Sequence[str] = (),
    *,
    allow_usage_credit_models: bool = False,
) -> tuple[CatalogModel, ...]:
    """Every model the router may name, cheapest first within each provider.

    ``providers`` restricts and orders the result; empty means the whole
    catalog in its declared order. Models needing usage credits are dropped
    unless the operator has allowed them, exactly as the desktop app filters
    every other model list.
    """
    keys = [str(key).strip().lower() for key in providers if str(key).strip()]
    if not keys:
        seen: list[str] = []
        for entry in _MODEL_CATALOG:
            if entry.provider not in seen:
                seen.append(entry.provider)
        keys = seen
    catalog: list[CatalogModel] = []
    for key in keys:
        rows = [entry for entry in _MODEL_CATALOG if entry.provider == key]
        if not allow_usage_credit_models:
            rows = [entry for entry in rows if not entry.requires_usage_credits]
        catalog.extend(sorted(rows, key=lambda entry: (entry.cost, entry.model)))
    return tuple(catalog)


def catalog_model_names(
    providers: Sequence[str] = (),
    *,
    allow_usage_credit_models: bool = False,
) -> tuple[str, ...]:
    return tuple(
        entry.model
        for entry in model_catalog(providers, allow_usage_credit_models=allow_usage_credit_models)
    )


def model_description(model: str) -> str:
    return _MODEL_DESCRIPTIONS.get(str(model or "").strip(), "")


def model_cost(model: str) -> int | None:
    return _MODEL_COSTS.get(str(model or "").strip())


# A rework is deliberately sent to a different AI tool than the one that
# produced the previous pass, so the follow-up is an independent second
# opinion. The router may keep the previous tool only when it says so with at
# least this much confidence — "clearly a better choice", not a coin flip.
REWORK_SAME_PROVIDER_MIN_CONFIDENCE = 0.8

# Longest explanation kept for the grade and the complexity score. They are
# shown to whoever receives the grade, so they get more room than a one-liner.
EXPLANATION_LIMIT = 900

ROUTER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string"},
        "selected_provider": {"type": "string"},
        "provider_reason": {"type": "string"},
        "complexity": {"type": "integer", "minimum": 1, "maximum": 10},
        "risk": {"type": "string", "enum": list(RISK_LEVELS)},
        "context_requirement": {"type": "string", "enum": list(CONTEXT_REQUIREMENTS)},
        "selected_model": {"type": "string"},
        "reasoning_effort": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "prompt_grade": {"type": "string", "enum": list(PROMPT_GRADES)},
        "grade_reason": {"type": "string"},
        "complexity_reason": {"type": "string"},
    },
    "required": [
        "task_type",
        "selected_provider",
        "provider_reason",
        "complexity",
        "risk",
        "context_requirement",
        "selected_model",
        "reasoning_effort",
        "confidence",
        "prompt_grade",
        "grade_reason",
        "complexity_reason",
    ],
}


class RouterError(ValueError):
    """The router response cannot be applied. The caller falls back."""


class InvalidRouterModel(RouterError):
    """The router named a model that is not in the catalog.

    Carries the response it came from so the caller can retry once with the
    catalog restated (``build_model_correction_prompt``) and, if that second
    answer is no better, still resolve this one through the complexity tier
    table rather than stalling the issue.
    """

    def __init__(self, model: str, payload: dict[str, Any]) -> None:
        named = str(model or "").strip()
        super().__init__(
            f"router named {named or '(no model)'}, which is not a supported model"
        )
        self.model = named
        self.payload = payload


@dataclasses.dataclass(frozen=True)
class RoutingTier:
    min_complexity: int
    max_complexity: int
    model: str
    effort: str

    def matches(self, complexity: int) -> bool:
        return self.min_complexity <= complexity <= self.max_complexity

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_complexity": self.min_complexity,
            "max_complexity": self.max_complexity,
            "model": self.model,
            "effort": self.effort,
        }


@dataclasses.dataclass(frozen=True)
class RouterCandidate:
    """One enabled AI tool the router may hand this issue to.

    ``tiers`` is that tool's complexity table, ``strengths`` the operator's
    description of what it is good at, and ``usage_remaining`` its headroom at
    selection time (None when it could not be read). ``excluded_models`` names
    models this tool has just rejected in this run (e.g. one that turned out to
    need usage credits): they are kept out of the catalog the router is shown
    and out of what it is allowed to name, so an immediate re-route cannot
    land on the same rejected model again.
    """

    key: str
    name: str
    tiers: tuple[RoutingTier, ...]
    strengths: str = ""
    usage_remaining: float | None = None
    excluded_models: tuple[str, ...] = ()


def default_provider_strengths(provider: str) -> str:
    return _DEFAULT_PROVIDER_STRENGTHS.get(provider, "")


def default_router_model(provider: str) -> str:
    return _DEFAULT_ROUTER.get(provider, ("", "low"))[0]


def default_router_effort(provider: str) -> str:
    return _DEFAULT_ROUTER.get(provider, ("", "low"))[1]


def default_routing_tiers() -> dict[str, tuple[RoutingTier, ...]]:
    return {
        provider: tuple(
            RoutingTier(min_complexity, max_complexity, model, effort)
            for min_complexity, max_complexity, model, effort in rows
        )
        for provider, rows in _DEFAULT_TIER_ROWS.items()
    }


def load_routing_tiers(raw: str) -> dict[str, tuple[RoutingTier, ...]]:
    """Built-in tiers, with any JSON object replacing the listed providers.

    Missing providers keep the built-in table. An empty string means the
    built-in table alone.
    """
    tiers = default_routing_tiers()
    text = str(raw or "").strip()
    if not text:
        return tiers
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"routing tiers are not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("routing tiers must be a JSON object keyed by provider")
    for key, rows in parsed.items():
        provider = str(key).strip().lower()
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"{provider} routing tiers must be a non-empty list")
        tiers[provider] = tuple(_parse_tier(provider, item) for item in rows)
    return tiers


def _parse_tier(provider: str, item: Any) -> RoutingTier:
    if not isinstance(item, dict):
        raise ValueError(f"{provider} routing tier must be an object")
    try:
        tier = RoutingTier(
            min_complexity=int(item["min_complexity"]),
            max_complexity=int(item["max_complexity"]),
            model=str(item["model"]).strip(),
            effort=str(item["effort"]).strip(),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{provider} routing tier is incomplete") from error
    if tier.min_complexity > tier.max_complexity or not tier.model or not tier.effort:
        raise ValueError(f"{provider} routing tier has an invalid range or model")
    if tier.min_complexity < 1 or tier.max_complexity > 10:
        raise ValueError(f"{provider} routing tier must stay within complexity 1–10")
    return tier


def tier_for_complexity(tiers: tuple[RoutingTier, ...] | list[RoutingTier], complexity: int) -> RoutingTier:
    matches = [tier for tier in tiers if tier.matches(complexity)]
    if not matches:
        raise RouterError(f"no routing tier covers complexity {complexity}")
    chosen = matches[0]
    if not chosen.model or not chosen.effort:
        raise RouterError(f"routing tier for complexity {complexity} has no model")
    return chosen


def candidate_catalog(
    candidate: RouterCandidate,
    *,
    allow_usage_credit_models: bool = False,
) -> tuple[CatalogModel, ...]:
    """Every model one AI tool may be asked to run, cheapest first."""
    excluded = {str(model).strip() for model in candidate.excluded_models}
    return tuple(
        entry
        for entry in model_catalog(
            (candidate.key,), allow_usage_credit_models=allow_usage_credit_models
        )
        if entry.model not in excluded
    )


def catalog_prompt_lines(
    candidates: Sequence[RouterCandidate],
    *,
    allow_usage_credit_models: bool = False,
) -> list[str]:
    """The full model catalog, as prompt lines the router selects from."""
    lines = [
        "Model catalog — selected_model must be one of these exact names, and must belong to the",
        "tool you name in selected_provider. Costs are relative and comparable across tools:",
    ]
    for candidate in candidates:
        for entry in candidate_catalog(
            candidate, allow_usage_credit_models=allow_usage_credit_models
        ):
            lines.append(
                f"- {entry.provider} / {entry.model} — {entry.cost_label}. {entry.description}"
            )
    return lines


def optimization_prompt_lines(routing_optimization: str) -> list[str]:
    """How to weigh cost against capability, from the operator's preference."""
    if normalize_routing_optimization(routing_optimization) == "cost":
        return [
            "Routing preference: optimize for cost.",
            "Choose the least expensive model in the catalog that can plausibly do this work well.",
            "Treat the risk score you assigned as the signal to spend more: escalate to a stronger,",
            "more expensive model when risk is high, even at a lower complexity score, and stay on a",
            "cheaper model for low-risk work, even at a higher complexity score. Say in",
            "provider_reason what made the cheaper model sufficient, or what risk forced the escalation.",
        ]
    return [
        "Routing preference: optimize for the best fit, and ignore cost entirely.",
        "Choose whichever model in the catalog genuinely suits what this work demands. This is not",
        "'always pick the strongest model': a small, mechanical, low-risk task is better served by a",
        "lighter, faster model, and picking an oversized one is the wrong answer even though it is",
        "allowed. Say in provider_reason what about the task fits the model you chose.",
    ]


def build_model_correction_prompt(
    prompt: str,
    *,
    named_model: str,
    candidates: Sequence[RouterCandidate],
    allow_usage_credit_models: bool = False,
) -> str:
    """The one corrective follow-up after the router names a model that does not exist.

    The original grading prompt is restated so the second answer is made with
    the same issue in view, followed by the rejected name and the complete
    supported catalog. There is only ever one of these per routing pass; a
    second invalid answer degrades to the complexity tier table instead.
    """
    catalog_lines = catalog_prompt_lines(
        candidates, allow_usage_credit_models=allow_usage_credit_models
    )
    named = str(named_model or "").strip()
    return "\n".join(
        [
            prompt.rstrip("\n"),
            "",
            "Correction — your previous answer to this exact request was rejected.",
            (
                f"You named selected_model {named!r}, which is not a model that exists."
                if named
                else "You did not name a model that exists in selected_model."
            ),
            "Answer the whole request again, unchanged except for selected_model: it must be one of",
            "the exact names below, and must belong to the tool you name in selected_provider.",
            "Return one JSON object and nothing else.",
            "",
            *catalog_lines,
        ]
    ) + "\n"


def build_router_prompt(
    *,
    title: str,
    body: str,
    labels: list[str],
    candidates: Sequence[RouterCandidate],
    previous_provider: str = "",
    rework: bool = False,
    image_count: int = 0,
    comment_image_count: int = 0,
    routing_optimization: str = DEFAULT_ROUTING_OPTIMIZATION,
    allow_usage_credit_models: bool = False,
) -> str:
    """Ask for a grade of the original issue. The issue text is quoted only.

    Every enabled AI tool with capacity is offered, with its strengths and its
    complexity tiers, so the router picks the tool as well as the model. The
    full model catalog for those tools is listed too — grounding the choice in
    the complete valid set up front is what keeps the router from naming a
    model that does not exist — and ``routing_optimization`` decides whether it
    is told to economize or to ignore cost entirely.
    """
    if not candidates:
        raise RouterError("no AI tools are available to route to")
    tool_lines: list[str] = []
    for candidate in candidates:
        headline = f"- {candidate.key} ({candidate.name})"
        if candidate.usage_remaining is not None:
            headline += f" — usage remaining: {candidate.usage_remaining:g}%"
        tool_lines.append(headline)
        if candidate.strengths.strip():
            tool_lines.append(f"  Best at: {candidate.strengths.strip()}")
        tool_lines.append("  Operator reference tiers (guidance, not a rule you must follow):")
        for tier in candidate.tiers:
            line = (
                f"    complexity {tier.min_complexity}-{tier.max_complexity} → "
                f"{tier.model} / {tier.effort}"
            )
            description = model_description(tier.model)
            if description:
                line += f" — {description}"
            tool_lines.append(line)
    ids = ", ".join(candidate.key for candidate in candidates)
    catalog_lines = catalog_prompt_lines(
        candidates, allow_usage_credit_models=allow_usage_credit_models
    )
    preference_lines = optimization_prompt_lines(routing_optimization)
    previous = str(previous_provider or "").strip().lower()
    rework_lines: list[str] = []
    if rework and previous:
        rework_lines = [
            "",
            f"This issue is being reworked. {previous} completed the previous pass.",
            f"Favor a different AI tool so the rework is an independent second opinion. Choose {previous}",
            "again only when it is clearly the better tool for this specific work — say why in",
            f"provider_reason, and report confidence of at least {REWORK_SAME_PROVIDER_MIN_CONFIDENCE:g}",
            f"when you do. Lower confidence in {previous} is read as 'no clear reason' and the work goes elsewhere.",
        ]
    quoted_body = body if body.strip() else "(empty)"
    image_lines: list[str] = []
    if image_count > 0:
        image_lines = [
            "",
            "Attached issue images:",
            f"{image_count} image(s) uploaded on this issue are attached to this message, in the order they appear.",
            "Grade clarity and complexity using what those images show, not only the text.",
        ]
        if comment_image_count > 0:
            image_lines.append(
                f"{comment_image_count} of them come from later GitHub comments rather than the original description."
            )
    return "\n".join(
        [
            "You are the SWARM dynamic AI router.",
            "Grade the original GitHub issue below for an AI coding agent and choose which AI tool runs it.",
            "Do not rewrite, expand, or replace the issue. Return one JSON object and nothing else.",
            "",
            "Score these and only these:",
            "1. task_type — a short label such as debugging, feature, refactor, test, docs, or review.",
            "2. complexity — integer 1 through 10.",
            "3. context_requirement — small, medium, or large.",
            "4. risk — low, medium, or high.",
            f"5. selected_provider — the id of the AI tool best suited to this work, one of: {ids}.",
            "6. provider_reason — one or two sentences naming what about this issue makes that tool the right one.",
            "7. selected_model — the model that should do this work, spelled exactly as it appears in the",
            "    model catalog below, and belonging to the tool you chose for selected_provider.",
            "8. reasoning_effort — one of: " + ", ".join(EFFORT_LABELS) + ".",
            "9. confidence — a number from 0 to 1 for how sure you are of this routing decision.",
            "10. prompt_grade — exactly one of: " + ", ".join(PROMPT_GRADES) + ".",
            "11. grade_reason — a short paragraph, written to the person who filed the issue, on exactly why it earned",
            "    this grade: what it does well, what is missing or ambiguous, and what would raise the grade.",
            "12. complexity_reason — a short paragraph on how the complexity score was determined: the specific factors",
            "    in this issue (scope, number of areas touched, unknowns, risk, testing needed) that put it at that",
            "    score rather than one lower or higher.",
            "",
            "Grade clarity, specificity, requirements, acceptance criteria, useful context, ambiguity,",
            "and whether an AI coding agent could execute the work without guessing.",
            "Score complexity from the size and difficulty of the work itself, not from how well it is written.",
            "Complexity bands: 1-3 small and well-defined, 4-6 moderate, 7-8 large or subtle, 9-10 sweeping or high-risk.",
            "",
            "Available AI tools — pick selected_provider from these ids and match the work to what each is best at:",
            *tool_lines,
            "",
            *catalog_lines,
            "",
            *preference_lines,
            *rework_lines,
            "",
            "Original issue title:",
            title,
            "",
            "Original issue description:",
            quoted_body,
            "",
            "Original issue tags:",
            ", ".join(labels) if labels else "none",
            *image_lines,
        ]
    ) + "\n"


def parse_router_payload(raw: str) -> dict[str, Any]:
    """Pull the JSON object out of a model response, including fenced output."""
    text = str(raw or "").strip()
    if not text:
        raise RouterError("router returned an empty response")
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fence.group(1) if fence else text
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise RouterError("router response did not contain a JSON object")
        try:
            payload = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as error:
            raise RouterError("router response was not valid JSON") from error
    if not isinstance(payload, dict):
        raise RouterError("router response JSON was not an object")
    return payload


def _confidence(value: Any) -> float:
    if isinstance(value, str):
        stripped = value.strip().rstrip("%")
        try:
            number = float(stripped)
        except ValueError as error:
            raise RouterError("router confidence was not a number") from error
        if value.strip().endswith("%"):
            number /= 100
    else:
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise RouterError("router confidence was not a number") from error
    if number > 1 and number <= 100:
        number /= 100
    if number < 0 or number > 1:
        raise RouterError("router confidence was outside 0–1")
    return number


def resolve_routing_decision(
    payload: dict[str, Any] | str,
    candidates: Sequence[RouterCandidate],
    *,
    default_provider: str,
    router_provider: str,
    router_model: str,
    router_effort: str,
    previous_provider: str = "",
    rework: bool = False,
    minimum_same_provider_confidence: float = REWORK_SAME_PROVIDER_MIN_CONFIDENCE,
    routing_optimization: str = DEFAULT_ROUTING_OPTIMIZATION,
    allow_usage_credit_models: bool = False,
    allow_tier_fallback: bool = True,
) -> dict[str, Any]:
    """Validate the router object, pick the AI tool, and apply its model choice.

    The router names the worker model itself, weighing cost against capability
    under ``routing_optimization``; the name is honoured once it is in the
    catalog for the tool that ends up running the work. A tool the router names
    that is not an available candidate falls back to ``default_provider``. On a
    rework the previous tool is only kept when the router is at least
    ``minimum_same_provider_confidence`` sure; otherwise the next candidate
    takes the round.

    Whenever the router's own tool pick is overruled, its model belongs to a
    different tool, so the replacement's ``tier_for_complexity`` table decides
    instead. A model that is simply not in the catalog raises
    ``InvalidRouterModel`` while ``allow_tier_fallback`` is false, so the caller
    can spend its one corrective follow-up call; with it true (the default, and
    what that retry uses) the tier table decides rather than raising.
    """
    if isinstance(payload, str):
        parsed = parse_router_payload(payload)
    elif isinstance(payload, dict):
        parsed = payload
    else:
        raise RouterError("router payload was not a JSON object")
    if not candidates:
        raise RouterError("no AI tools are available to route to")
    try:
        complexity = int(parsed["complexity"])
    except (KeyError, TypeError, ValueError) as error:
        raise RouterError("router complexity was missing") from error
    if complexity < 1 or complexity > 10:
        raise RouterError("router complexity was outside 1–10")
    grade = str(parsed.get("prompt_grade") or "").strip()
    if grade not in PROMPT_GRADES:
        raise RouterError(f"router prompt grade {grade or '(empty)'} is not an allowed grade")
    risk = str(parsed.get("risk") or "").strip().lower()
    if risk not in RISK_LEVELS:
        raise RouterError("router risk was not low, medium, or high")
    context = str(parsed.get("context_requirement") or "").strip().lower()
    if context not in CONTEXT_REQUIREMENTS:
        raise RouterError("router context requirement was not small, medium, or large")
    task_type = str(parsed.get("task_type") or "").strip().lower()
    if not task_type or len(task_type) > 40:
        raise RouterError("router task type was missing")
    reason = str(parsed.get("grade_reason") or "").strip()
    if not reason:
        raise RouterError("router grade explanation was missing")
    complexity_reason = str(parsed.get("complexity_reason") or "").strip()
    confidence = _confidence(parsed.get("confidence"))
    chosen, override = _select_candidate(
        parsed.get("selected_provider"),
        candidates,
        default_provider=default_provider,
        previous_provider=previous_provider,
        rework=rework,
        confidence=confidence,
        minimum_same_provider_confidence=minimum_same_provider_confidence,
    )
    tier = tier_for_complexity(chosen.tiers, complexity)
    optimization = normalize_routing_optimization(routing_optimization)
    suggested_model = str(parsed.get("selected_model") or "").strip()
    suggested_effort = str(parsed.get("reasoning_effort") or "").strip()
    allowed = {
        entry.model
        for entry in candidate_catalog(
            chosen, allow_usage_credit_models=allow_usage_credit_models
        )
    }
    if override:
        model, effort, model_source = tier.model, tier.effort, "tier"
        explanation = describe_tier(chosen, tier, complexity)
    elif suggested_model in allowed:
        model = suggested_model
        effort = _normalize_effort(suggested_effort) or tier.effort
        model_source = "router"
        explanation = describe_model_choice(
            chosen, model, effort, complexity, optimization=optimization
        )
    elif not allow_tier_fallback:
        raise InvalidRouterModel(suggested_model, parsed)
    else:
        model, effort, model_source = tier.model, tier.effort, "tier"
        named = suggested_model or "no model"
        explanation = (
            f"The router named {named}, which is not a model {chosen.name} can run, so its "
            f"configured complexity tier decided instead. " + describe_tier(chosen, tier, complexity)
        )
    return {
        "provider": chosen.key,
        "provider_name": chosen.name,
        "provider_reason": str(parsed.get("provider_reason") or "").strip()[:500],
        "provider_override_reason": override,
        "provider_candidates": [candidate.key for candidate in candidates],
        "router_provider": router_provider,
        "task_type": task_type,
        "complexity": complexity,
        "risk": risk,
        "context_requirement": context,
        "selected_model": model,
        "reasoning_effort": effort,
        "model_source": model_source,
        "routing_optimization": optimization,
        "router_suggested_provider": str(parsed.get("selected_provider") or "").strip().lower(),
        "router_suggested_model": str(parsed.get("selected_model") or "").strip(),
        "router_suggested_effort": str(parsed.get("reasoning_effort") or "").strip(),
        "confidence": confidence,
        "prompt_grade": grade,
        "grade_reason": reason[:EXPLANATION_LIMIT],
        "complexity_reason": complexity_reason[:EXPLANATION_LIMIT],
        "tier_explanation": explanation,
        "router_model": router_model,
        "router_effort": router_effort,
        "fallback": False,
    }


def _normalize_effort(value: Any) -> str:
    """The router's effort, or "" when it is not one this app can invoke."""
    key = str(value or "").strip().lower()
    return key if key in EFFORT_LABELS else ""


def describe_model_choice(
    candidate: RouterCandidate,
    model: str,
    effort: str,
    complexity: int,
    *,
    optimization: str,
) -> str:
    """How the router's own model choice was reached, in plain words."""
    model_name = display_model_name(model)
    goal = (
        "the least expensive model that can do the work"
        if normalize_routing_optimization(optimization) == "cost"
        else "the best fit for the work, regardless of cost"
    )
    text = (
        f"Complexity {complexity}/10. The router chose {candidate.name} {model_name} at "
        f"{display_effort(effort)} reasoning, optimizing for {goal}."
    )
    description = model_description(model)
    if description:
        text += f" {model_name}: {description}"
    return text


def describe_tier(candidate: RouterCandidate, tier: RoutingTier, complexity: int) -> str:
    """How a complexity score became a worker model and effort, in plain words."""
    band = (
        f"{tier.min_complexity}"
        if tier.min_complexity == tier.max_complexity
        else f"{tier.min_complexity}–{tier.max_complexity}"
    )
    model_name = display_model_name(tier.model)
    text = (
        f"Complexity {complexity}/10 falls in {candidate.name}'s {band} band, which maps to "
        f"{model_name} at {display_effort(tier.effort)} reasoning."
    )
    description = model_description(tier.model)
    if description:
        text += f" {model_name}: {description}"
    return text


def _select_candidate(
    requested: Any,
    candidates: Sequence[RouterCandidate],
    *,
    default_provider: str,
    previous_provider: str,
    rework: bool,
    confidence: float,
    minimum_same_provider_confidence: float,
) -> tuple[RouterCandidate, str]:
    """The AI tool that runs this issue, plus any note about overruling the router."""
    by_key = {candidate.key: candidate for candidate in candidates}
    by_name = {candidate.name.lower(): candidate for candidate in candidates}
    default = by_key.get(str(default_provider).strip().lower(), candidates[0])
    asked = str(requested or "").strip().lower()
    chosen = by_key.get(asked) or by_name.get(asked)
    override = ""
    if chosen is None:
        named = f"named {asked}, which is not an available AI tool" if asked else "named no AI tool"
        override = f"The router {named}; {default.name} kept this issue."
        chosen = default
    previous = str(previous_provider or "").strip().lower()
    if rework and previous and chosen.key == previous:
        alternatives = [candidate for candidate in candidates if candidate.key != previous]
        if alternatives and confidence < minimum_same_provider_confidence:
            replacement = alternatives[0]
            override = (
                f"Rework: {chosen.name} completed the previous pass and was re-selected with only "
                f"{int(round(confidence * 100))}% confidence, so {replacement.name} takes this round."
            )
            chosen = replacement
    return chosen, override


def fallback_routing_decision(
    *,
    provider: str,
    provider_name: str = "",
    model: str,
    effort: str,
    reason: str,
    router_model: str,
    router_effort: str,
    router_provider: str = "",
    candidates: Sequence[str] = (),
    routing_optimization: str = DEFAULT_ROUTING_OPTIMIZATION,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "provider_name": provider_name or provider.capitalize(),
        "provider_reason": "",
        "provider_override_reason": "",
        "provider_candidates": list(candidates),
        "router_provider": router_provider or provider,
        "task_type": "",
        "complexity": None,
        "risk": "",
        "context_requirement": "",
        "selected_model": model,
        "reasoning_effort": effort,
        "model_source": "configured",
        "routing_optimization": normalize_routing_optimization(routing_optimization),
        "confidence": 0,
        "prompt_grade": "",
        "grade_reason": reason.strip()[:EXPLANATION_LIMIT],
        "complexity_reason": "",
        "tier_explanation": "",
        "router_model": router_model,
        "router_effort": router_effort,
        "fallback": True,
    }


def display_model_name(value: str) -> str:
    """Human label for a model slug. ``gpt-5.6-sol`` becomes ``GPT-5.6 Sol``."""
    parts = [part for part in re.split(r"[-_]", str(value or "").strip()) if part]
    if not parts:
        return str(value or "")
    rendered: list[str] = []
    for part in parts:
        lower = part.lower()
        if lower == "gpt":
            rendered.append("GPT")
        elif lower in {"claude", "grok"}:
            rendered.append(lower.capitalize())
        elif re.fullmatch(r"[0-9.]+", part):
            rendered.append(part)
        else:
            rendered.append(part[:1].upper() + part[1:])
    collapsed: list[str] = []
    for part in rendered:
        if collapsed and re.fullmatch(r"[0-9]+", collapsed[-1]) and re.fullmatch(r"[0-9]+", part):
            collapsed[-1] = f"{collapsed[-1]}.{part}"
        else:
            collapsed.append(part)
    rendered = collapsed
    if rendered[0] == "GPT" and len(rendered) > 1 and re.fullmatch(r"[0-9.]+", rendered[1]):
        tail = " ".join(rendered[2:])
        return f"{rendered[0]}-{rendered[1]}" + (f" {tail}" if tail else "")
    return " ".join(rendered)


def display_effort(value: str) -> str:
    key = str(value or "").strip().lower()
    if key in EFFORT_LABELS:
        return EFFORT_LABELS[key]
    return key[:1].upper() + key[1:] if key else str(value or "")


def provider_display_name(decision: dict[str, Any]) -> str:
    name = str(decision.get("provider_name") or "").strip()
    if name:
        return name
    key = str(decision.get("provider") or "").strip()
    return key.capitalize() if key else ""


def router_description(decision: dict[str, Any]) -> str:
    """Who graded and routed the issue: ``Claude (Haiku 4.5, low reasoning)``.

    The pre-flight grader is a different AI, model, and effort from the worker
    the decision selects, so every report that shows the worker also needs this
    to be readable: without it a grade cannot be attributed to the AI that gave
    it, and router effectiveness/bias cannot be compared across providers.
    Returns ``""`` when the decision predates these fields.
    """
    provider = str(decision.get("router_provider") or "").strip()
    model = str(decision.get("router_model") or "").strip()
    effort = str(decision.get("router_effort") or "").strip()
    name = provider.capitalize() if provider else ""
    detail = ", ".join(
        part
        for part in (
            display_model_name(model) if model else "",
            f"{display_effort(effort)} reasoning" if effort else "",
        )
        if part
    )
    if name and detail:
        return f"{name} ({detail})"
    return name or detail


def routing_optimization_label(value: Any) -> str:
    """The operator's cost preference, for a report a person reads.

    Empty for a decision recorded before the preference existed, so an older
    routing notice is not retroactively labelled with a preference that was
    never applied to it.
    """
    key = str(value or "").strip().lower()
    if key == "cost":
        return "Cheapest model that fits the work"
    if key == "best":
        return "Best model for the work, regardless of cost"
    return ""


def format_routing_notice(decision: dict[str, Any]) -> str:
    """Issue-comment block shown when SWARM takes ownership."""
    model = display_model_name(str(decision.get("selected_model") or ""))
    effort = display_effort(str(decision.get("reasoning_effort") or ""))
    provider = provider_display_name(decision)
    grader = router_description(decision)
    if decision.get("fallback"):
        lines = [
            "SWARM AI Routing",
            "Routing fell back to the configured worker model and reasoning effort.",
        ]
        if provider:
            lines.append(f"Selected AI: {provider}")
        lines.extend([f"Selected Model: {model}", f"Reasoning: {effort}"])
        if grader:
            lines.append(f"Routed by: {grader}")
        reason = str(decision.get("grade_reason") or "").strip()
        if reason:
            lines.extend(["", reason])
        return "\n".join(lines)
    confidence = float(decision.get("confidence") or 0)
    percent = int(round(confidence * 100))
    considered = [
        str(key).capitalize()
        for key in decision.get("provider_candidates") or []
        if str(key).strip()
    ]
    lines = [
        "SWARM AI Routing",
        f"Prompt Grade: {decision.get('prompt_grade')}",
        f"Complexity: {decision.get('complexity')}/10",
        f"Selected AI: {provider}",
        f"Selected Model: {model}",
        f"Reasoning: {effort}",
        f"Routing Confidence: {percent}%",
    ]
    if grader:
        lines.append(f"Graded and routed by: {grader}")
    preference = routing_optimization_label(decision.get("routing_optimization"))
    if preference:
        lines.append(f"Routing Preference: {preference}")
    if considered:
        lines.append(f"AI Tools Considered: {', '.join(considered)}")
    provider_reason = str(decision.get("provider_reason") or "").strip()
    if provider_reason:
        lines.append(f"Why {provider}: {provider_reason}")
    override = str(decision.get("provider_override_reason") or "").strip()
    if override:
        lines.append(override)
    grade_reason = str(decision.get("grade_reason") or "").strip()
    if grade_reason:
        lines.extend(["", f"Why this grade ({decision.get('prompt_grade')}): {grade_reason}"])
    complexity_reason = str(decision.get("complexity_reason") or "").strip()
    tier_explanation = str(decision.get("tier_explanation") or "").strip()
    if complexity_reason or tier_explanation:
        lines.append("")
        if complexity_reason:
            lines.append(f"How complexity was determined ({decision.get('complexity')}/10): {complexity_reason}")
        if tier_explanation:
            lines.append(tier_explanation)
    return "\n".join(lines)


def _command_available(path: str) -> bool:
    if not path:
        return False
    candidate = Path(path)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return True
    return shutil.which(path) is not None


def extract_router_text(provider: str, stdout: str, last_message: str = "") -> str:
    """Normalize Claude, Codex, and Grok one-shot output to the model's text."""
    if provider == "codex" and last_message.strip():
        return last_message.strip()
    raw = stdout.strip()
    # Claude image input uses stream-json, whose `result` event holds the grade.
    # A single `--output-format json` object has the same `type`/`result` shape.
    wrapped = assistant_result_text(raw)
    if wrapped.strip():
        return wrapped.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(payload, dict):
        if "prompt_grade" in payload or "task_type" in payload:
            return json.dumps(payload)
        for key in ("result", "text", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                return json.dumps(value)
    return raw


def run_provider_router(
    *,
    provider: str,
    bin_path: str,
    model: str,
    effort: str,
    prompt: str,
    cwd: Path,
    timeout: float = 180,
    images: Sequence[Path] = (),
) -> str:
    """One-shot router call that does not start or resume the worker session.

    Claude runs with no tools and no session persistence. Codex is ephemeral
    and sandboxed read-only. Grok is limited to one turn and cannot ask to
    edit the repository. A failure raises ``RouterError`` so the worker can
    keep the manually configured model.
    """
    if not _command_available(bin_path):
        raise RouterError(f"{provider} executable is unavailable")
    if not model.strip():
        raise RouterError(f"{provider} router model is empty")
    schema = json.dumps(ROUTER_RESPONSE_SCHEMA, separators=(",", ":"))
    with tempfile.TemporaryDirectory(prefix="swarm-router-") as temporary:
        temp = Path(temporary)
        schema_path = temp / "router-schema.json"
        schema_path.write_text(schema, encoding="utf-8")
        last_message = temp / "router-last.txt"
        prompt_path = temp / "router-prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        image_paths = [Path(path) for path in images if Path(path).is_file()]
        if provider == "claude":
            inlined = inlined_images(prompt, image_paths, kind="claude") if image_paths else []
            if image_paths and not inlined:
                print(
                    "WARNING: Issue images could not be inlined into the router prompt; "
                    "grading will use the issue text only.",
                    file=sys.stderr,
                )
            command = [
                bin_path,
                "--model",
                model,
                "--effort",
                effort or "low",
                "-p",
            ]
            if inlined:
                command.extend(
                    ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
                )
                stdin: str | None = claude_stream_message(prompt, inlined)
            else:
                command.extend(["--output-format", "json"])
                stdin = prompt
            command.extend(
                [
                    "--json-schema",
                    schema,
                    "--tools",
                    "",
                    "--no-session-persistence",
                ]
            )
            completed = _run(command, cwd=cwd, timeout=timeout, stdin=stdin)
            return extract_router_text(provider, completed)
        if provider == "codex":
            command = [
                bin_path,
                "exec",
                *codex_image_flags(image_paths),
                "-m",
                model,
                "-c",
                f'model_reasoning_effort="{effort or "low"}"',
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--json",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(last_message),
                "-C",
                str(cwd),
                "-",
            ]
            completed = _run(command, cwd=cwd, timeout=timeout, stdin=prompt)
            message = last_message.read_text(encoding="utf-8") if last_message.exists() else ""
            return extract_router_text(provider, completed, message)
        if provider == "grok":
            inlined = inlined_images(prompt, image_paths, kind="grok") if image_paths else []
            if image_paths and not inlined:
                print(
                    "WARNING: Issue images could not be inlined into the router prompt; "
                    "grading will use the issue text only.",
                    file=sys.stderr,
                )
            command = [bin_path]
            if inlined:
                command.extend(["--prompt-json", grok_prompt_json(prompt, inlined)])
            else:
                command.extend(["--prompt-file", str(prompt_path)])
            command.extend(
                [
                    "--model",
                    model,
                    "--reasoning-effort",
                    effort or "low",
                    "--output-format",
                    "json",
                    "--json-schema",
                    schema,
                    "--max-turns",
                    "1",
                    "--permission-mode",
                    "dontAsk",
                    "--disable-web-search",
                    "--no-subagents",
                    "--cwd",
                    str(cwd),
                ]
            )
            completed = _run(command, cwd=cwd, timeout=timeout, stdin=None)
            return extract_router_text(provider, completed)
    raise RouterError(f"no router runner for provider {provider}")


def _run(command: list[str], *, cwd: Path, timeout: float, stdin: str | None) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RouterError("router timed out") from error
    except OSError as error:
        raise RouterError(f"router could not be started: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        suffix = f": {detail[-1][:240]}" if detail else ""
        raise RouterError(f"router exited with status {completed.returncode}{suffix}")
    return completed.stdout or ""
