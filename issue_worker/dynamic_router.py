"""Optional dynamic AI routing for one GitHub issue.

The router grades the original issue, scores its complexity, and picks which of
the enabled AI tools runs the work and on which model. The original issue text
is never rewritten. Keep the provider strengths in sync with
``ProviderSettings`` in ``src/config.rs``.

The model is the router's own call, not a table lookup. It is shown the full
cross-provider catalog (``_MODEL_CATALOG``) — every model, what it is good at,
and what it costs relative to the others, minus anything needing usage credits
the operator has not allowed. Automatic routing is always cost-first after
capability, expected-success, safety, and context-fit gates: among candidates
that clear those floors, the lowest estimated total cost wins. A frontier
model is a last resort at complexity 9 or 10. Saved ``"best"`` preferences
migrate to ``"cost"``.

No tier table is stored anywhere. The per-band reference tiers shown to the
router are computed on demand (``derived_routing_tiers``) from the same live
catalog the scoring router reads, and they are the deterministic safety net: if
the router names a model outside the catalog it gets exactly one corrective
follow-up call with the catalog restated (``build_model_correction_prompt``),
and a second invalid answer resolves through the band's derived tier instead —
so a persistently wrong router response degrades rather than stalling the
issue. The same path covers a decision where the router's tool pick was
overruled, since its model then belongs to a different tool.

``_MODEL_CATALOG`` exists only here: unlike the provider strengths, it is never
sent to the app or persisted in config.json, so there is no matching copy to
keep in sync in ``src/config.rs``.

Whenever the router's own free-choice model (above) is not usable — its pick
was outside the catalog, or its tool pick was overruled — the deterministic
safety net is no longer a flat complexity-band lookup. ``_scored_tier_decision``
asks the reusable Dynamic Model Router (issue #195, ``model_router.py`` plus
``skills/model-router/{models,routing-rules}.yaml``) to score every eligible
model/effort for the chosen tool against the graded complexity, task type, and
risk, and only falls back to the old ``tier_for_complexity`` table if that
scoring config cannot be loaded or nothing is eligible.
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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from issue_images import (
    assistant_result_text,
    claude_stream_message,
    codex_image_flags,
    grok_prompt_json,
    inlined_images,
)
import available_models as _available_models
import model_pricing as _model_pricing
import model_router as _model_router


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

# Automatic routing is always cost-first. ``"best"`` remains accepted on
# input so older configs and flags migrate instead of failing, then
# normalize to ``"cost"``.
ROUTING_OPTIMIZATIONS = ("cost", "best")
DEFAULT_ROUTING_OPTIMIZATION = "cost"

# Under cost optimization, a frontier model is allowed only at this complexity
# or at the top of the 1–10 scale. Interpolated into the router prompt; the
# answer is not clamped if the router still names a frontier model below it.
FRONTIER_COMPLEXITY_FLOOR = 9
COMPLEXITY_SCALE_TOP = 10

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
    frontier: bool = False

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
        "claude-sonnet-5-5",
        3,
        "Claude's balanced, general-purpose model. The default choice for typical "
        "multi-file feature work and bug fixes.",
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
        "claude-opus-5-5",
        4,
        "Claude's most capable model. Reserved for the largest, most ambiguous, or "
        "highest-risk work, where the deepest reasoning is worth the extra cost and time.",
        frontier=True,
    ),
    CatalogModel(
        "claude",
        "claude-opus-5",
        4,
        "Claude's most capable model. Reserved for the largest, most ambiguous, or "
        "highest-risk work, where the deepest reasoning is worth the extra cost and time.",
        frontier=True,
    ),
    CatalogModel(
        "claude",
        "claude-fable-5",
        5,
        "An earlier release of Claude's usage-credit model, kept for comparison "
        "against the current one.",
        frontier=True,
    ),
    CatalogModel(
        "claude",
        "claude-fable-5-1",
        5,
        "Claude's deepest-reasoning model, billed against a separate usage-credit "
        "balance rather than the plan allowance. For the hardest work, where that "
        "extra cost is accepted deliberately.",
        frontier=True,
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
        frontier=True,
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
        frontier=True,
    ),
)

# Kept as the by-slug view of the catalog above: the description shown next to a
# tier, folded into ``tier_explanation``, and read back by anything holding only
# a model name.


def _catalog_with_discovered() -> tuple[CatalogModel, ...]:
    """The checked-in catalog plus every model the provider CLIs report.

    A discovered model borrows its cost rank and frontier flag from the closest
    catalogued release of the same family and says so in its description.
    Releases the catalog has already moved past are left out of the routing
    list (they remain visible to the decision engine through
    ``available_models``), so an older model can never win a cost tie against
    the current one.
    """
    rows = list(_MODEL_CATALOG)
    for agent in _available_models.agents():
        known = [row for row in rows if row.provider == agent]
        names = {_available_models.canonical(row.model) for row in known}
        for found in _available_models.discovered(agent):
            if _available_models.canonical(found.value) in names:
                continue
            names.add(_available_models.canonical(found.value))
            relative = _available_models.closest_relative(
                found.value, ((row.model, row) for row in known)
            )
            peer, older = relative if relative else (None, False)
            if older:
                continue
            fallback = min(known, key=lambda row: row.cost, default=None) if peer is None else None
            label = _available_models.display_label(found)
            basis = (
                f"cost and capability inferred from {peer.model}" if peer
                else "no catalogued relative, so the provider's lightest model's cost is assumed"
            )
            row = CatalogModel(
                agent,
                found.value,
                peer.cost if peer else (fallback.cost if fallback else 3),
                f"{label}, discovered from the {agent} CLI ({basis}); not yet benchmarked.",
                frontier=bool(peer and peer.frontier),
            )
            # Same tie rule as model_router: a newer release precedes its peer.
            rows.insert(rows.index(peer) if peer in rows else len(rows), row)
            known.append(row)
    return tuple(rows)


def requires_usage_credits(model: str) -> bool:
    """Whether a model bills against the separate usage-credit balance."""
    parts = re.split(r"[-_]", str(model or "").strip().lower())
    return any(part in USAGE_CREDIT_FAMILIES for part in parts)


def normalize_routing_optimization(value: Any) -> str:
    """Always ``cost``. Legacy ``best`` and unknown values migrate here."""
    del value
    return DEFAULT_ROUTING_OPTIMIZATION


def cost_consideration_enabled(value: Any = None) -> bool:
    """Automatic routing is always cost-first after capability gates."""
    del value
    return True


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
        for entry in _catalog_with_discovered():
            if entry.provider not in seen:
                seen.append(entry.provider)
        keys = seen
    catalog: list[CatalogModel] = []
    for key in keys:
        rows = [entry for entry in _catalog_with_discovered() if entry.provider == key]
        # Blacklisted rows stay in the catalog so a newer release can infer
        # from them; they are never offered or named by the router.
        rows = [entry for entry in rows if not _available_models.is_blacklisted(entry.model)]
        # A model with no price would run with its spend unrecorded, so the
        # router is not offered it until it is priced.
        rows = [entry for entry in rows if _model_pricing.resolve_price(entry.model, provider=entry.provider).priced]
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
    wanted = str(model or "").strip()
    return next((row.description for row in _catalog_with_discovered() if row.model == wanted), "")


def model_route_profile(agent: str, model: str, effort: str) -> tuple[int, float | None] | None:
    """``(relative capability, estimated dollar cost)`` of one route, or None.

    Read from the same catalog live routing scores (the active calibration
    when one is applied, else the bundled catalog plus discovered models), so
    comparing a saved route with a fresh one uses the router's own numbers.
    """
    calibrated = _active_calibration_catalog()
    try:
        catalog = calibrated if calibrated is not None else _model_router.load_model_catalog()
    except _model_router.ModelRouterConfigError:
        return None
    key = str(agent).strip().lower()
    spec = next(
        (m for m in catalog if m.agent == key and model in (m.model, m.model_id)),
        None,
    )
    if spec is None:
        return None
    return spec.relative_capability, _model_router.estimated_dollar_cost(spec, effort)


@dataclasses.dataclass(frozen=True)
class ReleaseUpgrade:
    """A newer release of the same model family, and why it is safe to use."""

    model: str
    previous: str
    reason: str


# A newer release may cost at most this much more per token than the one it
# replaces. Zero would forbid a rounding difference; a real price increase is
# a different decision than "use the latest".
UPGRADE_PRICE_TOLERANCE = 1.05
# A measured score this far below the older release's blocks the upgrade: the
# default is "newer is better", and only evidence overrides it.
UPGRADE_SCORE_MARGIN = 1.0


def latest_release(
    agent: str,
    model: str,
    effort: str,
    *,
    allow_usage_credit_models: bool = False,
    excluded: Sequence[tuple[str, str]] = (),
) -> ReleaseUpgrade | None:
    """The newest release of ``model``'s family, when using it is a safe upgrade.

    Routing may name an older release — a fallback tier table, a model the
    router remembered — even though a newer one of the same family now exists.
    Newer is better on the measured index for Sonnet and Opus, so this moves the
    pick forward, with limits:

    * same provider and same family only (Sonnet stays Sonnet, Opus stays Opus);
    * the release must be active, offered by the provider CLI, allowed by the
      usage-credit setting, support the chosen effort, and not be excluded;
    * it must have a price in the pricing catalog, so its spend is recorded;
    * it must not cost meaningfully more per token than the model it replaces;
    * a measured Intelligence Index that is clearly *lower* at the same effort
      vetoes it.
    """
    key = str(agent).strip().lower()
    family, version = _available_models.family_and_version(model)
    if not model or not version:
        return None
    calibrated = _active_calibration_catalog()
    try:
        catalog = calibrated if calibrated is not None else _model_router.load_model_catalog()
    except _model_router.ModelRouterConfigError:
        return None
    current = next((m for m in catalog if m.agent == key and model in (m.model, m.model_id)), None)
    barred = {(str(a).lower(), str(m)) for a, m in excluded}
    newer = [
        spec for spec in catalog
        if spec.agent == key and spec.active and not spec.deprecated
        and _available_models.family_and_version(spec.model)[0] == family
        and _available_models.family_and_version(spec.model)[1] > version
        and effort in spec.supported_efforts
        and (allow_usage_credit_models or not requires_usage_credits(spec.model))
        and (key, spec.model) not in barred
    ]
    # An upgrade must land on a model whose spend the app can record: a model
    # with no price in the pricing catalog would run with its cost unknown.
    newer = [spec for spec in newer if _model_pricing.resolve_price(spec.model, provider=key).priced]
    if not newer:
        return None
    best = max(newer, key=lambda spec: _available_models.family_and_version(spec.model)[1])

    # Price: compare per-token prices when both are known, else the relative rank.
    if current is not None:
        if None not in (current.input_cost, current.output_cost, best.input_cost, best.output_cost):
            if (best.input_cost > current.input_cost * UPGRADE_PRICE_TOLERANCE
                    or best.output_cost > current.output_cost * UPGRADE_PRICE_TOLERANCE):
                return None
        elif best.relative_cost > current.relative_cost:
            return None
        old_scores, new_scores = dict(current.intelligence_by_effort), dict(best.intelligence_by_effort)
        if effort in old_scores and effort in new_scores:
            if new_scores[effort] < old_scores[effort] - UPGRADE_SCORE_MARGIN:
                return None
    elif not _available_models.is_blacklisted(model) and None in (best.input_cost, best.output_cost):
        # Nothing to compare against and no known price: do not guess. A
        # blacklisted model has been ruled out by the operator, so its
        # successor needs no comparison, only the pricing check above.
        return None
    reason = f"the latest {' '.join(family) or agent} release, same or lower price"
    if current is not None:
        old_scores, new_scores = dict(current.intelligence_by_effort), dict(best.intelligence_by_effort)
        if effort in old_scores and effort in new_scores:
            reason = (
                f"the latest {' '.join(family)} release, measured "
                f"{new_scores[effort]:.1f} against {old_scores[effort]:.1f} at {effort}"
            )
    return ReleaseUpgrade(best.model, model, reason)


def model_cost(model: str) -> int | None:
    wanted = str(model or "").strip()
    return next((row.cost for row in _catalog_with_discovered() if row.model == wanted), None)


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


def suggested_defaults(
    agent: str,
    *,
    routing_optimization: str = DEFAULT_ROUTING_OPTIMIZATION,
    allow_usage_credit_models: bool = False,
) -> dict[str, str]:
    """Starting worker and router settings for ``agent``, computed from the catalog.

    The worker default is what the scoring router picks for a simple task
    (complexity 3) and the router default what it picks for a trivial one
    (complexity 1), so a fresh install starts on the cheapest capable models and
    follows the live measurements, prices and blacklist. When the router cannot
    decide, the cheapest model the catalog offers for the provider is used; an
    unknown provider yields empty models.
    """
    key = str(agent).strip().lower()
    catalog = _routing_catalog()
    options = dict(
        routing_optimization=routing_optimization,
        allow_usage_credit_models=allow_usage_credit_models,
        catalog=catalog,
    )
    worker = scored_floor(key, 3, **options)
    router = scored_floor(key, 1, **options)
    if worker is None or router is None:
        rows = model_catalog((key,), allow_usage_credit_models=allow_usage_credit_models)
        last_resort = (rows[0].model, "low") if rows else ("", "low")
        worker, router = worker or last_resort, router or last_resort
    return {"model": worker[0], "effort": worker[1], "router_model": router[0], "router_effort": router[1]}


def default_router_model(provider: str) -> str:
    return suggested_defaults(provider)["router_model"]


def default_router_effort(provider: str) -> str:
    return suggested_defaults(provider)["router_effort"]


def derived_routing_tiers(
    agent: str,
    *,
    routing_optimization: str = DEFAULT_ROUTING_OPTIMIZATION,
    allow_usage_credit_models: bool = False,
    fallback: tuple[str, str] | None = None,
    excluded: Sequence[str] = (),
) -> tuple[RoutingTier, ...]:
    """The reference tiers for ``agent``, computed now from the catalog.

    Nothing is stored and no band is named here: the scoring router is asked
    which model and effort it would run at each complexity from 1 to 10 (live
    measurements, prices, the blacklist and what the provider CLI offers), and
    neighbouring scores with the same answer are merged into one range. A score
    the router cannot decide for takes ``fallback`` (the provider's configured
    model and effort); with neither, that score is left uncovered.
    """
    catalog = _routing_catalog()
    skipped = set(excluded)
    picks: list[tuple[int, tuple[str, str] | None]] = []
    for complexity in range(1, COMPLEXITY_SCALE_TOP + 1):
        picked = scored_floor(
            agent, complexity,
            routing_optimization=routing_optimization,
            allow_usage_credit_models=allow_usage_credit_models,
            excluded_models=excluded,
            catalog=catalog,
        ) or (fallback if fallback and fallback[0] not in skipped else None)
        picks.append((complexity, picked))
    rows: list[RoutingTier] = []
    for complexity, picked in picks:
        if picked is None:
            continue
        last = rows[-1] if rows else None
        if last and last.max_complexity == complexity - 1 and (last.model, last.effort) == picked:
            rows[-1] = RoutingTier(last.min_complexity, complexity, picked[0], picked[1])
        else:
            rows.append(RoutingTier(complexity, complexity, picked[0], picked[1]))
    return tuple(rows)


def tier_for_complexity(tiers: tuple[RoutingTier, ...] | list[RoutingTier], complexity: int) -> RoutingTier:
    matches = [tier for tier in tiers if tier.matches(complexity)]
    if not matches:
        raise RouterError(f"no routing tier covers complexity {complexity}")
    chosen = matches[0]
    if not chosen.model or not chosen.effort:
        raise RouterError(f"routing tier for complexity {complexity} has no model")
    return chosen


# Freeform pre-flight task_type text -> the reusable model_router's fixed
# vocabulary (issue #195). Matched against exact aliases first, then by
# substring, so "deep debugging" and "debugging deep dive" both land on
# "deep_debugging"; anything unrecognized falls back to "general_reasoning"
# rather than failing the routing pass.
_TASK_TYPE_ALIASES: dict[str, str] = {
    "doc": "documentation",
    "docs": "documentation",
    "documentation": "documentation",
    "test": "test_generation",
    "tests": "test_generation",
    "testing": "test_generation",
    "bug": "simple_bug_fix",
    "bugfix": "simple_bug_fix",
    "fix": "simple_bug_fix",
    "feature": "feature",
    "refactor": "refactor",
    "refactoring": "refactor",
    "debug": "debugging",
    "debugging": "debugging",
    "review": "code_review",
    "code_review": "code_review",
    "architecture": "architecture",
    "design": "architecture",
    "security": "security_analysis",
    "performance": "performance_analysis",
    "perf": "performance_analysis",
    "infrastructure": "infrastructure",
    "infra": "infrastructure",
    "devops": "devops",
    "deploy": "devops",
    "deployment": "devops",
    "research": "research",
    "planning": "planning",
    "plan": "planning",
    "repository_analysis": "repository_analysis",
    "analysis": "repository_analysis",
    "mechanical": "mechanical_edit",
    "formatting": "mechanical_edit",
    "rename": "mechanical_edit",
}


def _normalize_task_type(raw: str) -> str:
    key = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in _model_router.TASK_TYPES:
        return key
    if key in _TASK_TYPE_ALIASES:
        return _TASK_TYPE_ALIASES[key]
    for alias, canonical in _TASK_TYPE_ALIASES.items():
        if alias in key:
            return canonical
    return "general_reasoning"


def describe_scored_tier(
    candidate: RouterCandidate,
    decision: "_model_router.RoutingDecision",
    complexity: int,
) -> str:
    """How the reusable model router (issue #195) reached a tier decision, in plain words."""
    model_name = display_model_name(decision.model)
    text = (
        f"Complexity {complexity}/10 falls in {candidate.name}'s {decision.complexity} band; the "
        f"model router scored {model_name} at {display_effort(decision.effort)} reasoning as the best fit. "
        f"{decision.reason}"
    )
    description = model_description(decision.model)
    if description:
        text += f" {model_name}: {description}"
    return text


def active_calibration_catalog_path() -> Path | None:
    """Optional override catalog from an activated Model Routing Calibration.

    ``SWARM_MODEL_CALIBRATION_CATALOG`` is set by the desktop app (whenever a
    calibration has been activated) to the ``active_catalog.json`` a calibration was promoted to (see
    ``model_calibration.py``'s ``ModelCalibrationService.activate``). Unset,
    missing, or unreadable, this returns ``None`` and every caller falls back
    to the bundled ``models.yaml`` exactly as before this existed — activating
    a calibration is the only thing that can ever change what gets loaded.
    """
    raw = os.environ.get("SWARM_MODEL_CALIBRATION_CATALOG", "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def _active_calibration_catalog() -> tuple[_model_router.ModelSpec, ...] | None:
    path = active_calibration_catalog_path()
    if path is None:
        return None
    try:
        return _model_router.load_model_catalog(path)
    except _model_router.ModelRouterConfigError:
        return None


def _scored_tier_decision(
    candidate: RouterCandidate,
    complexity: int,
    task_type: str,
    risk: str,
    *,
    routing_optimization: str,
    allow_usage_credit_models: bool,
) -> tuple[str, str, str]:
    """(model, effort, explanation) from the reusable scoring router.

    Falls back to the reference tier for the band (itself derived, see
    ``derived_routing_tiers``) when the scoring router's own config cannot be
    loaded or nothing is eligible — the same "degrade, never block" rule every
    other part of this module follows.
    """
    model, effort, explanation, _ = scored_tier(
        candidate,
        complexity,
        task_type,
        risk,
        routing_optimization=routing_optimization,
        allow_usage_credit_models=allow_usage_credit_models,
    )
    return model, effort, explanation


def scored_tier(
    candidate: RouterCandidate,
    complexity: int,
    task_type: str,
    risk: str,
    *,
    routing_optimization: str,
    allow_usage_credit_models: bool,
) -> tuple[str, str, str, "_model_router.RoutingDecision | None"]:
    """``_scored_tier_decision`` plus the scoring router's full decision.

    The decision (with every scored candidate) is ``None`` when the band's
    reference tier decided instead. The routing calculator uses this so it shows exactly
    what the worker would do, from the one implementation.
    """
    calibrated = _active_calibration_catalog()
    eligible = None if calibrated is None else {
        model.model for model in calibrated
        if model.agent == candidate.key and model.active and not model.deprecated
    }
    try:
        catalog = calibrated if calibrated is not None else _model_router.load_model_catalog()
        disabled = {
            model.model
            for model in catalog
            if not allow_usage_credit_models and requires_usage_credits(model.model)
        }
        disabled |= {str(model).strip() for model in candidate.excluded_models}
        # A model with no price would run with its spend unrecorded and cannot
        # be compared fairly on cost, so it stays out of routing until priced.
        disabled |= {model.model for model in catalog if not _model_router.is_priced(model)}
        cost_on = cost_consideration_enabled(routing_optimization)
        decision = _model_router.route(
            _model_router.RouteRequest(
                task_type=_normalize_task_type(task_type),
                complexity=complexity,
                cost_consideration_enabled=cost_on,
                cost_sensitive=cost_on,
                quality_requirement="high" if risk == "high" else "normal",
            ),
            catalog=catalog,
            availability=_model_router.RoutingAvailability(
                enabled_agents=frozenset({candidate.key}),
                disabled_models=frozenset(disabled),
            ),
        )
        return decision.model, decision.effort, describe_scored_tier(candidate, decision, complexity), decision
    except (_model_router.ModelRouterError, _model_router.ModelRouterConfigError):
        tier = tier_for_complexity(candidate.tiers, complexity)
        if eligible is not None and tier.model not in eligible:
            raise RouterError(f"No eligible calibrated model for {candidate.name}.")
        return tier.model, tier.effort, describe_tier(candidate, tier, complexity), None


def _routing_catalog() -> "tuple[_model_router.ModelSpec, ...]":
    """The catalog live routing scores: the active calibration, else the bundled one."""
    calibrated = _active_calibration_catalog()
    return calibrated if calibrated is not None else _model_router.load_model_catalog()


def scored_floor(
    agent: str,
    complexity: int,
    *,
    routing_optimization: str = DEFAULT_ROUTING_OPTIMIZATION,
    allow_usage_credit_models: bool = False,
    excluded_models: Sequence[str] = (),
    catalog: Sequence["_model_router.ModelSpec"] | None = None,
) -> tuple[str, str] | None:
    """``(model, effort)`` the scoring router picks for ``agent`` at ``complexity``.

    Read from the live catalog only, so nothing stored can drag it up or down.
    ``None`` when the router cannot decide.
    """
    try:
        catalog = catalog if catalog is not None else _routing_catalog()
        disabled = {
            model.model for model in catalog
            if not allow_usage_credit_models and requires_usage_credits(model.model)
        } | {str(model).strip() for model in excluded_models}
        disabled |= {model.model for model in catalog if not _model_router.is_priced(model)}
        cost_on = cost_consideration_enabled(routing_optimization)
        decision = _model_router.route(
            _model_router.RouteRequest(
                task_type="general_reasoning",
                complexity=complexity,
                cost_consideration_enabled=cost_on,
                cost_sensitive=cost_on,
            ),
            catalog=catalog,
            availability=_model_router.RoutingAvailability(
                enabled_agents=frozenset({str(agent).strip().lower()}),
                disabled_models=frozenset(disabled),
            ),
        )
    except (_model_router.ModelRouterError, _model_router.ModelRouterConfigError):
        return None
    return decision.model, decision.effort


def cheapest_capable_model(
    agent: str,
    min_capability: int,
    *,
    allow_usage_credit_models: bool = False,
) -> tuple[str, tuple[str, ...]] | None:
    """The cheapest priced, current model of ``agent`` with capability >= ``min_capability``.

    Falls back to the provider's most capable model when none reaches the
    requirement. Returns ``(model, supported efforts)``, or ``None`` when the
    provider has no routable model.
    """
    key = str(agent).strip().lower()
    try:
        catalog = _routing_catalog()
    except _model_router.ModelRouterConfigError:
        return None
    rows = [
        spec for spec in catalog
        if spec.agent == key and spec.active and not spec.deprecated
        and _model_router.is_priced(spec)
        and (allow_usage_credit_models or not requires_usage_credits(spec.model))
    ]
    if not rows:
        return None
    capable = [spec for spec in rows if spec.relative_capability >= min_capability]
    if capable:
        best = min(capable, key=lambda spec: (spec.relative_cost, spec.relative_capability, spec.model))
    else:
        top = max(spec.relative_capability for spec in rows)
        best = min((spec for spec in rows if spec.relative_capability == top),
                   key=lambda spec: (spec.relative_cost, spec.model))
    return best.model, tuple(best.supported_efforts)


def supported_efforts(agent: str, model: str) -> tuple[str, ...]:
    """Reasoning efforts the catalog lists for ``model``; empty when unknown."""
    try:
        catalog = _routing_catalog()
    except _model_router.ModelRouterConfigError:
        return ()
    spec = next((s for s in catalog if s.agent == str(agent).strip().lower()
                 and model in (s.model, s.model_id)), None)
    return tuple(spec.supported_efforts) if spec else ()


def candidate_catalog(
    candidate: RouterCandidate,
    *,
    allow_usage_credit_models: bool = False,
) -> tuple[CatalogModel, ...]:
    """Every model one AI tool may be asked to run, cheapest first."""
    excluded = {str(model).strip() for model in candidate.excluded_models}
    calibrated = _active_calibration_catalog()
    eligible = None if calibrated is None else {
        model.model for model in calibrated
        if model.agent == candidate.key and model.active and not model.deprecated
    }
    return tuple(
        entry
        for entry in model_catalog(
            (candidate.key,), allow_usage_credit_models=allow_usage_credit_models
        )
        if entry.model not in excluded and (eligible is None or entry.model in eligible)
    )


def offered_catalog(
    candidates: Sequence[RouterCandidate],
    *,
    allow_usage_credit_models: bool = False,
) -> tuple[CatalogModel, ...]:
    """Every model the offered tools may run, in catalog order, credit-filtered."""
    entries: list[CatalogModel] = []
    for candidate in candidates:
        entries.extend(
            candidate_catalog(
                candidate, allow_usage_credit_models=allow_usage_credit_models
            )
        )
    return tuple(entries)


def frontier_model_names(catalog: Sequence[CatalogModel]) -> tuple[str, ...]:
    """The most-capable model of each line present in ``catalog``."""
    return tuple(entry.model for entry in catalog if entry.frontier)


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
    for entry in offered_catalog(
        candidates, allow_usage_credit_models=allow_usage_credit_models
    ):
        lines.append(
            f"- {entry.provider} / {entry.model} — {entry.cost_label}. {entry.description}"
        )
    return lines


def optimization_prompt_lines(
    routing_optimization: str,
    catalog: Sequence[CatalogModel] = (),
) -> list[str]:
    """How to weigh cost against capability. Automatic routing is always cost-first."""
    del routing_optimization
    floor = FRONTIER_COMPLEXITY_FLOOR
    top = COMPLEXITY_SCALE_TOP
    named = frontier_model_names(catalog)
    if named:
        frontier_line = (
            "The frontier models in this catalog are: " + ", ".join(named) + "."
        )
    else:
        frontier_line = "This catalog currently names no frontier models."
    return [
        "Routing preference: optimize for cost.",
        "Start from the least expensive model in the catalog that is actually capable of this",
        "task, and prefer it whenever one exists.",
        "A frontier model is a last resort.",
        frontier_line,
        f"Frontier complexity floor: {floor}",
        f"Complexity scale top: {top}",
        f"Use a frontier model only when the complexity score is {floor} or {top}.",
        "High risk may justify leaving the cheapest tier for a capable mid-tier model only.",
        "High risk is not a license to pick a frontier model below the floor.",
        "The reference tiers are the best-fit ladder under cost optimization.",
        "Do not follow a tier that names a frontier model below the floor.",
        "In provider_reason, name the cheaper capable model you considered and why it cannot",
        "do this task.",
        "Score complexity from the work itself.",
        f"Scoring {floor} or {top} in order to unlock a frontier model is not allowed.",
        "Speed and provider preference must not beat a cheaper adequately capable model.",
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
    historical_signals: str = "",
) -> str:
    """Ask for a grade of the original issue. The issue text is quoted only.

    Every enabled AI tool with capacity is offered, with its strengths and its
    complexity tiers, so the router picks the tool as well as the model. The
    full model catalog for those tools is listed too — grounding the choice in
    the complete valid set up front is what keeps the router from naming a
    model that does not exist — and automatic routing is always cost-first.
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
        tool_lines.append("  Reference tiers (what scoring would pick per band; guidance, not a rule you must follow):")
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
    catalog = offered_catalog(
        candidates, allow_usage_credit_models=allow_usage_credit_models
    )
    catalog_lines = catalog_prompt_lines(
        candidates, allow_usage_credit_models=allow_usage_credit_models
    )
    preference_lines = optimization_prompt_lines(routing_optimization, catalog)
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
    history_lines: list[str] = []
    signals = str(historical_signals or "").strip()
    if signals:
        history_lines = [
            "",
            "Historical SWARM engineering knowledge (sample-backed; not a rule you must follow):",
            signals,
        ]
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
            *history_lines,
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
    optimization = normalize_routing_optimization(routing_optimization)
    tier_model, tier_effort, tier_explanation = _scored_tier_decision(
        chosen,
        complexity,
        task_type,
        risk,
        routing_optimization=optimization,
        allow_usage_credit_models=allow_usage_credit_models,
    )
    suggested_model = str(parsed.get("selected_model") or "").strip()
    suggested_effort = str(parsed.get("reasoning_effort") or "").strip()
    allowed = {
        entry.model
        for entry in candidate_catalog(
            chosen, allow_usage_credit_models=allow_usage_credit_models
        )
    }
    if override:
        model, effort, model_source = tier_model, tier_effort, "tier"
        explanation = tier_explanation
    elif suggested_model in allowed:
        model = suggested_model
        effort = _normalize_effort(suggested_effort) or tier_effort
        model_source = "router"
        explanation = describe_model_choice(
            chosen, model, effort, complexity, optimization=optimization
        )
    elif not allow_tier_fallback:
        raise InvalidRouterModel(suggested_model, parsed)
    else:
        model, effort, model_source = tier_model, tier_effort, "tier"
        named = suggested_model or "no model"
        explanation = (
            f"The router named {named}, which is not a model {chosen.name} can run, so its "
            f"configured routing decided instead. " + tier_explanation
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
        "cost_consideration_enabled": cost_consideration_enabled(optimization),
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
    del optimization
    model_name = display_model_name(model)
    goal = "the least expensive model that can do the work"
    named = selected_model_label(candidate, model)
    text = (
        f"Complexity {complexity}/10. The router chose {named} at "
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
    dynamic_model_routing: bool = True,
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
        "cost_consideration_enabled": cost_consideration_enabled(routing_optimization),
        "router_suggested_provider": "",
        "router_suggested_model": "",
        "router_suggested_effort": "",
        "confidence": 0,
        "prompt_grade": "",
        "grade_reason": reason.strip()[:EXPLANATION_LIMIT],
        "complexity_reason": "",
        "tier_explanation": "",
        "router_model": router_model,
        "router_effort": router_effort,
        "fallback": True,
        "dynamic_model_routing": bool(dynamic_model_routing),
    }


def pin_configured_routing_decision(
    decision: dict[str, Any],
    *,
    provider: str,
    provider_name: str,
    model: str,
    effort: str,
) -> dict[str, Any]:
    """Keep the operator's pair; remember what the router would have run."""
    pinned = dict(decision)
    if not str(pinned.get("router_suggested_model") or "").strip():
        pinned["router_suggested_model"] = str(decision.get("selected_model") or "")
    if not str(pinned.get("router_suggested_effort") or "").strip():
        pinned["router_suggested_effort"] = str(decision.get("reasoning_effort") or "")
    if not str(pinned.get("router_suggested_provider") or "").strip():
        pinned["router_suggested_provider"] = str(decision.get("provider") or "")
    pinned["provider"] = provider
    pinned["provider_name"] = provider_name
    pinned["selected_model"] = model
    pinned["reasoning_effort"] = effort
    pinned["model_source"] = "configured"
    pinned["provider_override_reason"] = ""
    pinned["dynamic_model_routing"] = False
    pinned["fallback"] = False
    return pinned


def routing_score_snapshot(
    candidate: RouterCandidate,
    complexity: int,
    task_type: str,
    risk: str,
    *,
    allow_usage_credit_models: bool = False,
) -> dict[str, Any] | None:
    """One pass of the reusable scorer for baseline/modified snapshots."""
    try:
        calibrated = _active_calibration_catalog()
        catalog = calibrated if calibrated is not None else _model_router.load_model_catalog()
        disabled = {
            model.model
            for model in catalog
            if not allow_usage_credit_models and requires_usage_credits(model.model)
        }
        disabled |= {str(model).strip() for model in candidate.excluded_models}
        decision = _model_router.route(
            _model_router.RouteRequest(
                task_type=_normalize_task_type(task_type),
                complexity=complexity,
                cost_consideration_enabled=True,
                cost_sensitive=True,
                quality_requirement="high" if str(risk).lower() == "high" else "normal",
            ),
            catalog=catalog,
            availability=_model_router.RoutingAvailability(
                enabled_agents=frozenset({candidate.key}),
                disabled_models=frozenset(disabled),
            ),
        )
        return decision.as_dict()
    except (_model_router.ModelRouterError, _model_router.ModelRouterConfigError):
        return None


def blend_complexity(baseline: int, jev_complexity: float | None, confidence: float) -> int:
    """Bounded mix of Swarm's 1–10 grade and Jev's 0–1 complexity score."""
    try:
        base = int(baseline)
    except (TypeError, ValueError):
        base = 5
    base = min(10, max(1, base))
    if jev_complexity is None:
        return base
    from decision_engine import complexity_out_of_ten

    jev = complexity_out_of_ten(jev_complexity)
    weight = min(1.0, max(0.0, float(confidence))) * 0.5
    blended = int(round(base + (jev - base) * weight))
    return min(base + 2, max(base - 2, min(10, max(1, blended))))


def apply_jev_signals_to_decision(
    decision: dict[str, Any],
    jev_payload: Mapping[str, Any] | None,
    *,
    candidates: Sequence[RouterCandidate],
    jev_status: str,
    allow_usage_credit_models: bool = False,
    apply_model_change: bool = True,
) -> dict[str, Any]:
    """Attach baseline/Jev/modified scores. Swarm still owns the applied pick.

    ``jev_payload`` is a DecisionResult.as_dict() or None when Jev did not run.
    Baseline values are copied from the existing Swarm decision and never
    overwritten. Modified scores re-run the existing scorer with bounded Jev
    inputs. When Jev is disabled or unusable, modified equals baseline.
    """
    updated = dict(decision)
    provider = str(updated.get("provider") or "")
    chosen = next((item for item in candidates if item.key == provider), candidates[0] if candidates else None)
    complexity = updated.get("complexity")
    try:
        complexity_i = int(complexity) if complexity is not None else 5
    except (TypeError, ValueError):
        complexity_i = 5
    task_type = str(updated.get("task_type") or "general_reasoning")
    risk = str(updated.get("risk") or "medium")
    baseline_snapshot = None
    if chosen is not None:
        baseline_snapshot = routing_score_snapshot(
            chosen,
            complexity_i,
            task_type,
            risk,
            allow_usage_credit_models=allow_usage_credit_models,
        )
    baseline_native = None
    baseline_normalized = None
    baseline_candidates: list[dict[str, Any]] = []
    if baseline_snapshot:
        baseline_native = baseline_snapshot.get("native_score")
        baseline_normalized = baseline_snapshot.get("normalized_score")
        baseline_candidates = list(baseline_snapshot.get("candidates") or [])
    if baseline_normalized is None:
        try:
            baseline_normalized = float(updated.get("confidence") or 0)
        except (TypeError, ValueError):
            baseline_normalized = 0.0
    baseline_route = {
        "provider": provider,
        "model": str(updated.get("selected_model") or ""),
        "effort": str(updated.get("reasoning_effort") or ""),
        "prompt_grade": str(updated.get("prompt_grade") or ""),
        "complexity": complexity_i if updated.get("complexity") is not None else None,
        "task_type": task_type,
        "risk": risk,
        "context_requirement": str(updated.get("context_requirement") or ""),
        "confidence": updated.get("confidence"),
        "native_score": baseline_native,
        "normalized_score": baseline_normalized,
        "candidates": baseline_candidates,
        "reason_codes": [],
    }
    jev_block: dict[str, Any] | None = None
    modified_route = dict(baseline_route)
    status = str(jev_status or "disabled")
    if isinstance(jev_payload, Mapping) and status == "enabled":
        scores = jev_payload.get("scores") if isinstance(jev_payload.get("scores"), dict) else {}
        jev_block = {
            "decision": str(jev_payload.get("decision") or ""),
            "confidence": jev_payload.get("confidence"),
            "scores": scores,
            "reason_codes": list(jev_payload.get("reasonCodes") or jev_payload.get("reason_codes") or []),
            "model": str(jev_payload.get("model") or ""),
            "version": str(jev_payload.get("version") or ""),
            "source": str(jev_payload.get("source") or "jev"),
            "native_score": jev_payload.get("confidence"),
            "normalized_score": jev_payload.get("confidence"),
            "metadata": dict(jev_payload.get("metadata") or {}),
        }
        try:
            jev_confidence = float(jev_payload.get("confidence") or 0)
        except (TypeError, ValueError):
            jev_confidence = 0.0
        if jev_confidence >= 0.70 and chosen is not None:
            jev_task = str((jev_payload.get("metadata") or {}).get("routerTaskType") or "")
            blended_complexity = blend_complexity(
                complexity_i, (scores or {}).get("complexity"), jev_confidence
            )
            blended_task = jev_task or task_type
            security_risk = float((scores or {}).get("securityRisk") or 0)
            blended_risk = "high" if security_risk >= 0.7 or risk == "high" else risk
            modified_snapshot = routing_score_snapshot(
                chosen,
                blended_complexity,
                blended_task,
                blended_risk,
                allow_usage_credit_models=allow_usage_credit_models,
            )
            if modified_snapshot:
                modified_route = {
                    "provider": str(modified_snapshot.get("agent") or provider),
                    "model": str(modified_snapshot.get("model") or baseline_route["model"]),
                    "effort": str(modified_snapshot.get("effort") or baseline_route["effort"]),
                    "prompt_grade": baseline_route["prompt_grade"],
                    "complexity": blended_complexity,
                    "task_type": blended_task,
                    "risk": blended_risk,
                    "context_requirement": baseline_route["context_requirement"],
                    "confidence": modified_snapshot.get("confidence"),
                    "native_score": modified_snapshot.get("native_score"),
                    "normalized_score": modified_snapshot.get("normalized_score"),
                    "candidates": list(modified_snapshot.get("candidates") or []),
                    "reason_codes": jev_block["reason_codes"],
                    "estimated_cost": modified_snapshot.get("estimated_cost"),
                }
                if apply_model_change:
                    updated["selected_model"] = modified_route["model"]
                    updated["reasoning_effort"] = modified_route["effort"]
                    updated["complexity"] = blended_complexity
                    updated["risk"] = blended_risk
                    if jev_task:
                        updated["task_type"] = blended_task
                    extra = str(updated.get("tier_explanation") or "")
                    note = str(modified_snapshot.get("reason") or "")
                    if note:
                        updated["tier_explanation"] = (extra + " " + note).strip()
                    updated["model_source"] = updated.get("model_source") or "tier"
    elif status != "enabled":
        jev_block = None

    def _num(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    base_norm = _num(baseline_route.get("normalized_score")) or 0.0
    mod_norm = _num(modified_route.get("normalized_score"))
    if mod_norm is None:
        mod_norm = base_norm
        modified_route["normalized_score"] = base_norm
        modified_route["native_score"] = baseline_route.get("native_score")
    delta_abs = round(mod_norm - base_norm, 4)
    delta_pct = round((delta_abs / base_norm) * 100, 4) if base_norm else (0.0 if delta_abs == 0 else None)
    routing_changed = (
        str(modified_route.get("provider") or "") != str(baseline_route.get("provider") or "")
        or str(modified_route.get("model") or "") != str(baseline_route.get("model") or "")
        or str(modified_route.get("effort") or "") != str(baseline_route.get("effort") or "")
    )
    updated["jev"] = {
        "status": status,
        "baseline": baseline_route,
        "jev": jev_block,
        "modified": modified_route,
        "delta": {
            "absolute": delta_abs,
            "percent": delta_pct,
            "routing_changed": routing_changed,
        },
        "routing_before": {
            "provider": baseline_route.get("provider"),
            "model": baseline_route.get("model"),
            "effort": baseline_route.get("effort"),
        },
        "routing_after": {
            "provider": modified_route.get("provider"),
            "model": modified_route.get("model"),
            "effort": modified_route.get("effort"),
        },
    }
    updated["routing_optimization"] = normalize_routing_optimization(
        updated.get("routing_optimization")
    )
    updated["cost_consideration_enabled"] = True
    return updated


def routing_choice_was_applied(decision: dict[str, Any]) -> bool:
    source = str(decision.get("model_source") or "")
    if source in {"router", "tier"}:
        return True
    if source == "configured" or decision.get("fallback"):
        return False
    # Decisions recorded before model_source existed were applied picks.
    return decision.get("dynamic_model_routing") is not False


def selected_model_label(candidate: RouterCandidate, model: str) -> str:
    """Provider plus model, without repeating a name the model already carries."""
    model_name = display_model_name(model)
    if model_name.lower().startswith(candidate.name.lower()):
        return model_name
    return f"{candidate.name} {model_name}"


def how_model_was_chosen(decision: dict[str, Any]) -> str:
    """One line saying whether the setting or dynamic routing selected the worker."""
    if routing_choice_was_applied(decision):
        preference = routing_optimization_label(decision.get("routing_optimization"))
        if preference:
            return (
                "How this was chosen: Dynamic Model Routing applied this model and effort "
                f"({preference.lower()})."
            )
        return "How this was chosen: Dynamic Model Routing applied this model and effort."
    if decision.get("fallback"):
        if decision.get("dynamic_model_routing") is False:
            return (
                "How this was chosen: Set in SWARM Automation. Pre-flight grading was unavailable."
            )
        return ""
    if str(decision.get("model_source") or "") == "configured" or (
        decision.get("dynamic_model_routing") is False
    ):
        return (
            "How this was chosen: Set in SWARM Automation. Dynamic Model Routing is off, so the "
            "pre-flight grade was recorded and was not applied."
        )
    return ""


def router_recommendation_line(decision: dict[str, Any]) -> str:
    """The pair the router would have run, labelled as a recommendation."""
    suggested_model = str(decision.get("router_suggested_model") or "").strip()
    suggested_effort = str(decision.get("router_suggested_effort") or "").strip()
    if not suggested_model:
        return ""
    model = display_model_name(suggested_model)
    effort = display_effort(suggested_effort)
    line = f"Router recommendation: {model} at {effort} reasoning"
    selected_model = str(decision.get("selected_model") or "").strip()
    selected_effort = str(decision.get("reasoning_effort") or "").strip().lower()
    if suggested_model == selected_model and suggested_effort.lower() == selected_effort:
        line += " (matches the configured setting)"
    return line


def routing_history_message(
    decision: dict[str, Any],
    provider_name: str,
    model: str,
    effort: str,
) -> tuple[str, str]:
    """Execution-history kind and text for the pair that actually ran."""
    if decision.get("fallback"):
        reason = str(decision.get("grade_reason") or "router unavailable")
        if decision.get("dynamic_model_routing") is False:
            return (
                "warning",
                "Pre-flight grading was unavailable; the configured setting was used: "
                f"{provider_name} {model} with effort {effort}. {reason}",
            )
        return (
            "warning",
            f"Dynamic routing fell back to the configured worker model: {reason}",
        )
    source = str(decision.get("model_source") or "")
    grade = decision.get("prompt_grade")
    if source == "configured":
        message = (
            f"The configured setting was used: {provider_name} {model} with effort {effort}; "
            f"prompt grade {grade}."
        )
        suggested_model = str(decision.get("router_suggested_model") or "").strip()
        suggested_effort = str(decision.get("router_suggested_effort") or "").strip()
        if suggested_model:
            message += f" Router recommended {suggested_model} with effort {suggested_effort}."
    else:
        message = (
            f"Dynamic routing selected {provider_name} {model} with effort {effort}; "
            f"prompt grade {grade}."
        )
        provider_reason = str(decision.get("provider_reason") or "").strip()
        if provider_reason:
            message += f" Why {provider_name}: {provider_reason}"
        override = str(decision.get("provider_override_reason") or "").strip()
        if override:
            message += f" {override}"
    complexity_reason = str(decision.get("complexity_reason") or "").strip()
    if complexity_reason:
        message += f" Complexity {decision.get('complexity')}/10: {complexity_reason}"
    return ("note", message)


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
    if not key:
        return ""
    return "Cheapest model that fits the work"


def format_routing_notice(decision: dict[str, Any]) -> str:
    """Issue-comment block shown when SWARM takes ownership."""
    model = display_model_name(str(decision.get("selected_model") or ""))
    effort = display_effort(str(decision.get("reasoning_effort") or ""))
    provider = provider_display_name(decision)
    grader = router_description(decision)
    applied = routing_choice_was_applied(decision)
    if decision.get("fallback"):
        if decision.get("dynamic_model_routing") is False:
            heading = (
                "Pre-flight grading was unavailable. The configured worker model and "
                "reasoning effort were used."
            )
        else:
            heading = "Routing fell back to the configured worker model and reasoning effort."
        lines = ["SWARM AI Routing", heading]
        if provider:
            lines.append(f"Selected AI: {provider}")
        lines.extend([f"Selected Model: {model}", f"Reasoning: {effort}"])
        chosen = how_model_was_chosen(decision)
        if chosen:
            lines.append(chosen)
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
    ]
    chosen = how_model_was_chosen(decision)
    if chosen:
        lines.append(chosen)
    recommendation = router_recommendation_line(decision) if not applied else ""
    if recommendation:
        lines.append(recommendation)
    lines.append(f"Routing Confidence: {percent}%")
    if grader:
        lines.append(f"Graded and routed by: {grader}")
    preference = routing_optimization_label(decision.get("routing_optimization"))
    if preference and applied:
        lines.append(f"Routing Preference: {preference}")
    if considered:
        lines.append(f"AI Tools Considered: {', '.join(considered)}")
    provider_reason = str(decision.get("provider_reason") or "").strip()
    if provider_reason and applied:
        lines.append(f"Why {provider}: {provider_reason}")
    override = str(decision.get("provider_override_reason") or "").strip()
    if override and applied:
        lines.append(override)
    grade_reason = str(decision.get("grade_reason") or "").strip()
    if grade_reason:
        lines.extend(["", f"Why this grade ({decision.get('prompt_grade')}): {grade_reason}"])
    complexity_reason = str(decision.get("complexity_reason") or "").strip()
    tier_explanation = str(decision.get("tier_explanation") or "").strip() if applied else ""
    if complexity_reason or tier_explanation:
        lines.append("")
        if complexity_reason:
            lines.append(
                f"How complexity was determined ({decision.get('complexity')}/10): {complexity_reason}"
            )
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
    usage_sink: list[Any] | None = None,
) -> str:
    """One-shot router call that does not start or resume the worker session.

    Claude runs with no tools and no session persistence. Codex is ephemeral
    and sandboxed read-only. Grok is limited to one turn and cannot ask to
    edit the repository. A failure raises ``RouterError`` so the worker can
    keep the manually configured model.

    ``usage_sink``, when given a list, gets exactly one item appended: the
    normalized token usage for this call (``None`` if the provider reported
    none). An out-param rather than a richer return type keeps every existing
    caller of this function — which only wants the extracted text — unchanged
    (see issue #280).
    """
    return run_provider_oneshot(
        provider=provider, bin_path=bin_path, model=model, effort=effort, prompt=prompt,
        cwd=cwd, schema=ROUTER_RESPONSE_SCHEMA, timeout=timeout, images=images,
        usage_sink=usage_sink,
    )


def run_provider_oneshot(
    *,
    provider: str,
    bin_path: str,
    model: str,
    effort: str,
    prompt: str,
    cwd: Path,
    schema: dict[str, Any],
    timeout: float = 180,
    images: Sequence[Path] = (),
    usage_sink: list[Any] | None = None,
) -> str:
    """Same one-shot, no-tools, no-session-persistence call ``run_provider_router``
    uses, generalized to an arbitrary JSON response schema. Any caller other
    than the model router itself (e.g. the diagnostic explainer) should use
    this directly rather than ``run_provider_router``, which is pinned to
    ``ROUTER_RESPONSE_SCHEMA``.

    See ``run_provider_router`` for what ``usage_sink`` does.
    """
    from token_usage import normalize_usage

    def _record_usage(raw_text: str) -> None:
        if usage_sink is not None:
            usage_sink.append(normalize_usage(provider, raw_text))
    if not _command_available(bin_path):
        raise RouterError(f"{provider} executable is unavailable")
    if not model.strip():
        raise RouterError(f"{provider} router model is empty")
    schema_text = json.dumps(schema, separators=(",", ":"))
    with tempfile.TemporaryDirectory(prefix="swarm-router-") as temporary:
        temp = Path(temporary)
        schema_path = temp / "router-schema.json"
        schema_path.write_text(schema_text, encoding="utf-8")
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
                    schema_text,
                    "--tools",
                    "",
                    "--no-session-persistence",
                ]
            )
            completed = _run(command, cwd=cwd, timeout=timeout, stdin=stdin)
            _record_usage(completed)
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
            _record_usage(completed)
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
                    schema_text,
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
            _record_usage(completed)
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
