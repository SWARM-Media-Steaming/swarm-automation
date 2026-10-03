"""Reusable Dynamic Model Router (issue #195).

The router's job is choosing a provider/model/reasoning-effort combination
reasonably expected to complete a given task successfully. Automatic routing
is always cost-first after capability, expected-success, safety, and
context-fit gates: among candidates that clear those floors, the lowest
estimated total cost wins. Latency and provider preference only break a
cost/capability tie. A cheaper model is never selected when its expected
ability to complete the task is below the configured safety threshold.

``RouteRequest.cost_consideration_enabled`` defaults to on. Passing False is
only for isolated scoring-formula tests of the non-cost weight set; it is
not a user-facing routing mode.

Model selection and effort selection are scored together but are independently
justified: see ``_score_candidate``.

This module is a pure, deterministic scoring engine. It does not call any AI
provider and does not decide *what* a task's complexity or type are — that
classification is the existing pre-flight grading step in ``dynamic_router.py``
(an AI call over the issue text). This module answers the second question:
given a task's classification (complexity band, task type, and a few
sensitivity flags) and which providers/models/efforts are actually available,
which one should run it. That split is what makes this module testable with
no live API calls, per issue #195's test requirements.

Configuration lives in ``skills/model-router/models.yaml`` (the cross-provider
model catalog: capability, cost, and — where actually measured — benchmark
data) and ``skills/model-router/routing-rules.yaml`` (complexity bands, task
type groupings, scoring weights, and penalty terms). Both are loaded relative
to this file's own location (``_config_dir``), which resolves correctly both
in a source checkout and inside the packaged app, where Tauri bundles
``skills/model-router/`` as a sibling of the bundled ``issue_worker/`` (see
``tauri.conf.json``'s ``bundle.resources`` and ``skills/model-router/SKILL.md``).

Callers should catch ``ModelRouterConfigError`` and fall back to whatever
static behavior they had before this module existed — config-load failure
must degrade, never block, matching every other resilience rule in
``dynamic_router.py``.
"""

from __future__ import annotations

import dataclasses
import math
import json
from pathlib import Path
from typing import Any, Sequence

import available_models as _available_models
import model_pricing as _model_pricing
from model_router_yaml import YamlError
from model_router_yaml import load as load_yaml


class ModelRouterConfigError(RuntimeError):
    """``models.yaml`` / ``routing-rules.yaml`` could not be loaded or parsed.

    Callers should fall back to a static default rather than let this
    propagate into a work-round failure.
    """


class ModelRouterError(ValueError):
    """A routing request could not be satisfied (e.g. no eligible model)."""


COMPLEXITY_LEVELS: tuple[str, ...] = (
    "TRIVIAL",
    "SIMPLE",
    "STANDARD",
    "COMPLEX",
    "VERY_COMPLEX",
    "EXTREME",
)

TASK_TYPES: tuple[str, ...] = (
    "mechanical_edit",
    "documentation",
    "test_generation",
    "simple_bug_fix",
    "feature",
    "complex_feature",
    "refactor",
    "large_refactor",
    "debugging",
    "deep_debugging",
    "architecture",
    "planning",
    "code_review",
    "security_analysis",
    "performance_analysis",
    "infrastructure",
    "devops",
    "repository_analysis",
    "multi_repository",
    "research",
    "general_reasoning",
)

# Keyword vocabulary used to score how well a model's declared strengths (and
# weaknesses) fit a task type. Tokens must match the ones actually used in
# models.yaml's strengths/weaknesses lists — this is intentionally coupled to
# that file's vocabulary rather than free text, so the match stays exact and
# deterministic instead of a fuzzy heuristic.
_TASK_TYPE_KEYWORDS: dict[str, frozenset[str]] = {
    "mechanical_edit": frozenset({"trivial_tasks", "simple_transformations", "lightweight_fixes", "lightweight_changes", "trivial_work"}),
    "documentation": frozenset({"documentation", "lightweight_fixes"}),
    "test_generation": frozenset({"tests", "test_generation", "small_scoped_implementation"}),
    "simple_bug_fix": frozenset({"simple_fixes", "lightweight_fixes", "small_scoped_implementation", "lightweight_changes"}),
    "feature": frozenset({"normal_feature_work", "features", "normal_software_engineering", "repository_implementation"}),
    "complex_feature": frozenset({"substantial_code_changes", "difficult_coding", "difficult_agentic_development"}),
    "refactor": frozenset({"refactoring", "normal_software_engineering"}),
    "large_refactor": frozenset({"large_refactors", "difficult_coding", "high_context_work"}),
    "debugging": frozenset({"debugging", "general_repository_development", "simple_reasoning"}),
    "deep_debugging": frozenset({"deep_debugging", "difficult_coding", "cross_component_reasoning"}),
    "architecture": frozenset({"architecture", "difficult_agentic_development", "complex_repository_work", "sustained_reasoning"}),
    "planning": frozenset({"architecture", "sustained_reasoning"}),
    "code_review": frozenset({"general_repository_development", "complex_repository_work", "ambiguous_implementations"}),
    "security_analysis": frozenset({"ambiguous_implementations", "cross_component_reasoning", "difficult_coding", "very_high_value_tasks"}),
    "performance_analysis": frozenset({"sustained_reasoning", "difficult_coding", "substantial_code_changes"}),
    "infrastructure": frozenset({"scripting", "configuration_work", "high_volume_low_risk_tasks"}),
    "devops": frozenset({"scripting", "configuration_work", "quick_orientation_unfamiliar_code", "fast_turnarounds"}),
    "repository_analysis": frozenset({"complex_repository_work", "quick_orientation_unfamiliar_code", "high_context_work"}),
    "multi_repository": frozenset({"cross_component_reasoning", "high_context_work", "complex_repository_work", "very_high_value_tasks"}),
    "research": frozenset({"quick_orientation_unfamiliar_code", "high_context_work", "classification"}),
    "general_reasoning": frozenset(),
}

_BENCHMARK_FIELDS = ("coding_agent_index", "deep_swe", "terminal_bench", "swe_atlas_qna")


@dataclasses.dataclass(frozen=True)
class BenchmarkEntry:
    coding_agent_index: float | None
    deep_swe: float | None
    terminal_bench: float | None
    swe_atlas_qna: float | None
    benchmark_cost_per_task: float | None
    benchmark_tokens_per_task: float | None
    benchmark_runtime_minutes: float | None
    data_quality: str


@dataclasses.dataclass(frozen=True)
class ModelSpec:
    provider: str
    agent: str
    model: str
    model_id: str | None
    active: bool
    recommended: bool
    deprecated: bool
    superseded_by: str | None
    supported_efforts: tuple[str, ...]
    strengths: frozenset[str]
    weaknesses: frozenset[str]
    relative_capability: int
    relative_cost: int
    relative_token_efficiency: int
    relative_latency: int
    benchmarks: dict[str, BenchmarkEntry]
    benchmark_source: str | None
    benchmark_date: str | None
    notes: str
    input_cost: float | None = None
    output_cost: float | None = None
    reasoning_cost: float | None = None
    # Measured Artificial Analysis Intelligence Index by reasoning effort, as
    # sorted ``(effort, score)`` pairs; ``"max"`` is the provider's top-effort
    # (base) entry. Empty when nothing was measured. ``measured_capability``
    # turns it into the 1-5 rank the router gates and scores on.
    intelligence_by_effort: tuple[tuple[str, float], ...] = ()


# Capability rank from the measured Intelligence Index. The bands are fitted so
# the models the bundled catalog already ranked keep their rank (Sonnet 5 -> 3,
# Opus 5 -> 4, Fable 5.1 and GPT-6 Astra -> 5) while a newer release such as
# Sonnet 5.5 or Opus 5.5 earns the rank its score deserves instead of inheriting
# its predecessor's. Compared at "xhigh", the strongest effort the router uses
# routinely; ``max`` takes minutes to first answer and is only a fallback.
CAPABILITY_BANDS = ((52.0, 5), (44.0, 4), (32.0, 3), (20.0, 2))
# "max" is a base entry known to be the top-effort run (its effort variants
# were also measured); "base" is a base entry with no variants to compare with.
MEASURED_EFFORT_PREFERENCE = ("xhigh", "max", "base", "high", "medium", "low")


def _clean_intelligence(value: Any) -> tuple[tuple[str, float], ...]:
    """Normalize ``{effort: score}`` (or pairs) into sorted, finite pairs."""
    items = value.items() if isinstance(value, dict) else (value or ())
    cleaned: dict[str, float] = {}
    for item in items:
        try:
            effort, score = item
            number = float(score)
        except (TypeError, ValueError):
            continue
        if number == number and abs(number) != float("inf") and str(effort).strip():
            cleaned[str(effort).strip().lower()] = number
    return tuple(sorted(cleaned.items()))


def measured_intelligence(value: Any) -> tuple[float, str] | None:
    """``(score, effort)`` used to rank a model, or None when unmeasured."""
    scores = dict(_clean_intelligence(value))
    for effort in MEASURED_EFFORT_PREFERENCE:
        if effort in scores:
            return scores[effort], effort
    return None


def capability_rank(score: float) -> int:
    for threshold, rank in CAPABILITY_BANDS:
        if score >= threshold:
            return rank
    return 1


def measured_capability(value: Any) -> int | None:
    """The 1-5 capability rank implied by measured scores, if any."""
    measured = measured_intelligence(value)
    return None if measured is None else capability_rank(measured[0])


@dataclasses.dataclass(frozen=True)
class ComplexityBand:
    level: str
    ai_grade_range: tuple[int, int]
    min_capability: int


@dataclasses.dataclass(frozen=True)
class TaskTypeGroup:
    name: str
    task_types: tuple[str, ...]
    benchmark_emphasis: dict[str, float]
    extra_weights: dict[str, float]


@dataclasses.dataclass(frozen=True)
class RoutingRules:
    complexity_bands: tuple[ComplexityBand, ...]
    effort_ladder: tuple[str, ...]
    min_effort_by_complexity: dict[str, str]
    task_type_groups: tuple[TaskTypeGroup, ...]
    weights: dict[str, float]
    cost_consideration_weights: dict[str, float]
    sensitivity_boost: float
    overqualification_penalty_per_level: float
    unnecessary_reasoning_penalty_per_level: float
    cost_consideration_unnecessary_reasoning_multiplier: float
    minimum_expected_success: float
    cost_optimization_quality_tolerance: float
    tie_break_margin: float
    confidence_floor: float
    confidence_score_gap_scale: float

    def active_weights(self, cost_consideration_enabled: bool) -> dict[str, float]:
        source = self.cost_consideration_weights if cost_consideration_enabled else self.weights
        return dict(source)


@dataclasses.dataclass(frozen=True)
class RouteRequest:
    """What the router needs to know about one task. No AI call, no repo I/O.

    ``complexity`` accepts either one of ``COMPLEXITY_LEVELS`` or an int 1-10
    (the existing pre-flight grader's scale — see ``complexity_bands`` in
    routing-rules.yaml for the mapping). ``quality_requirement`` of ``"high"``
    raises the bar by one capability/effort notch for unusually high-stakes
    work without needing a whole extra complexity band.
    """

    task_type: str
    complexity: str | int
    cost_consideration_enabled: bool = True
    cost_sensitive: bool = False
    token_sensitive: bool = False
    latency_sensitive: bool = False
    quality_requirement: str = "normal"
    # Optional for backwards-compatible calculator/stage callers. Issue routing
    # always supplies the complete repository-aware vector and requirements.
    complexity_vector: dict[str, Any] = dataclasses.field(default_factory=dict)
    capability_requirements: dict[str, Any] = dataclasses.field(default_factory=dict)
    historical_performance: tuple[dict[str, Any], ...] = ()


@dataclasses.dataclass(frozen=True)
class RoutingAvailability:
    """Which providers/models this routing pass may actually choose.

    ``None`` means "no restriction"; an empty frozenset means "nothing is
    available" (the caller will get ``ModelRouterError``). ``preferred_provider``
    only breaks ties among near-equal scores — see ``tie_break_margin`` — it
    never overrides a clearly better-scoring candidate.
    """

    enabled_agents: frozenset[str] | None = None
    enabled_models: frozenset[str] | None = None
    disabled_models: frozenset[str] = frozenset()
    preferred_provider: str | None = None


@dataclasses.dataclass(frozen=True)
class RoutingCandidate:
    model: ModelSpec
    effort: str
    score: float
    expected_success: float = 0.0
    estimated_cost: float | None = None
    relative_cost: int = 0
    relative_latency: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.model.provider,
            "agent": self.model.agent,
            "model": self.model.model,
            "effort": self.effort,
            "score": round(self.score, 4),
            "expected_success": round(self.expected_success, 4),
            "estimated_cost": self.estimated_cost,
            "relative_cost": self.relative_cost,
            "relative_latency": self.relative_latency,
        }


@dataclasses.dataclass(frozen=True)
class RoutingDecision:
    provider: str
    agent: str
    model: str
    effort: str
    complexity: str
    task_type: str
    confidence: float
    reason: str
    alternatives: tuple[dict[str, Any], ...]
    cost_consideration_enabled: bool = True
    native_score: float = 0.0
    normalized_score: float = 0.0
    candidates: tuple[dict[str, Any], ...] = ()
    estimated_cost: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "agent": self.agent,
            "model": self.model,
            "effort": self.effort,
            "complexity": self.complexity,
            "task_type": self.task_type,
            "confidence": self.confidence,
            "reason": self.reason,
            "alternatives": list(self.alternatives),
            "cost_consideration_enabled": self.cost_consideration_enabled,
            "native_score": round(self.native_score, 4),
            "normalized_score": round(self.normalized_score, 4),
            "candidates": list(self.candidates),
            "estimated_cost": self.estimated_cost,
        }


def _config_dir() -> Path:
    """``skills/model-router/``, a sibling of this file's own package root.

    In a source checkout that is the repository root; in the packaged app it
    is ``resource_dir()``, since Tauri bundles both ``issue_worker/`` and
    ``skills/model-router/`` under the same resource root (see
    ``tauri.conf.json``).
    """
    return Path(__file__).resolve().parent.parent / "skills" / "model-router"


def _read_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ModelRouterConfigError(f"could not read {path}: {error}") from error
    # A ``.json`` path is a Model Routing Calibration override (issue #205,
    # ``model_calibration.py``'s ``active_catalog.json``) rather than the
    # bundled YAML — accepted here, not given its own loader function, so
    # every existing caller (which always passes a ``.yaml`` path or None)
    # is completely unaffected.
    if path.suffix == ".json":
        try:
            # Calibration publishes the already-filtered router models at the
            # top level, alongside its full review document. Never substitute
            # that full document, which also includes ineligible models.
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise ModelRouterConfigError(f"could not parse {path}: {error}") from error
    try:
        return load_yaml(text)
    except YamlError as error:
        raise ModelRouterConfigError(f"could not parse {path}: {error}") from error


def load_model_catalog(path: Path | None = None) -> tuple[ModelSpec, ...]:
    data = _read_yaml(path or (_config_dir() / "models.yaml"))
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ModelRouterConfigError("models.yaml must contain a top-level 'models' list")
    catalog: list[ModelSpec] = []
    for entry in data["models"]:
        spec = _blacklisted(_parse_model(entry))
        if "available_models" in _available_models._calibration_policy() and not _available_models.cli_offers(spec.agent, spec.model):
            spec = dataclasses.replace(spec, active=False)
        catalog.append(spec)
    # Keep excluded calibration rows as inactive metadata. CLI discovery must
    # not resurrect a row explicitly disabled by the active publication.
    full = data.get("calibration")
    if isinstance(full, dict):
        included = {(spec.provider, spec.model) for spec in catalog}
        for entry in full.get("models") or []:
            if isinstance(entry, dict) and (entry.get("provider"), entry.get("model")) not in included:
                catalog.append(dataclasses.replace(_parse_model(entry), active=False, recommended=False))
    return with_discovered_models(tuple(catalog), _measured_evidence(data))


def _cli_offers(slug: str) -> bool:
    """Whether live or calibration-recorded CLI evidence includes ``slug``."""
    wanted = _available_models.canonical(slug)
    return any(
        _available_models.canonical(model.value) == wanted
        for models in _available_models._discovered_rows().values()
        for model in models
    )


def _blacklisted(spec: ModelSpec) -> ModelSpec:
    """Apply the retirement that is in force, and lift one that is not.

    A blacklisted model stays in the catalog but can never be chosen. Kept,
    not dropped, because a newer discovered release infers its numbers from
    the closest catalogued relative, and that is usually the model it
    replaced. Inactive and deprecated, it is skipped by every routing path.

    A listed retirement whose successor is not offered and priced does not
    stick. The checked-in row may already say deprecated; when the CLI still
    offers the predecessor, that row is routable again.
    """
    in_force = _available_models.blacklist()
    names = {_available_models.canonical(spec.model)}
    if spec.model_id:
        names.add(_available_models.canonical(spec.model_id))
    retired = next((in_force[name] for name in names if name in in_force), None)
    if retired is not None:
        return dataclasses.replace(
            spec, active=False, recommended=False, deprecated=True,
            superseded_by=retired or None,
        )
    listed = _available_models.canonical(spec.model) in _available_models.listed_retirements()
    if listed and (not spec.active or spec.deprecated) and _cli_offers(spec.model):
        return dataclasses.replace(spec, active=True, deprecated=False, superseded_by=None)
    return spec


def _measured_evidence(data: dict[str, Any]) -> dict[str, tuple[tuple[str, float], ...]]:
    """Measured scores a calibration holds for models outside its routable set.

    A calibrated catalog publishes only routable models, but the calibration
    itself also records measurements for models it has not approved — including
    a release the provider CLI just started offering. Those measurements let a
    discovered model be ranked on evidence instead of on its predecessor.
    """
    calibration = data.get("calibration")
    rows: list[Any] = []
    if isinstance(calibration, dict):
        rows += list(calibration.get("discovered_models") or [])
        rows += list(calibration.get("models") or [])
    evidence: dict[str, tuple[tuple[str, float], ...]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        scores = _clean_intelligence(row.get("intelligence_by_effort"))
        if scores:
            for name in (row.get("model"), row.get("model_id")):
                if name:
                    evidence.setdefault(_available_models.canonical(str(name)), scores)
    return evidence


def with_discovered_models(
    catalog: tuple[ModelSpec, ...],
    evidence: dict[str, tuple[tuple[str, float], ...]] | None = None,
) -> tuple[ModelSpec, ...]:
    """Add every model the provider CLIs report that the catalog lacks.

    Applied to every catalog this module loads — the checked-in one and a
    calibrated one alike — so a newly released model is routable without a
    code change. Its numbers are inferred from the closest catalogued release
    of the same family (see ``available_models``); benchmarks stay null and
    ``HEURISTIC`` because nothing was measured. A model with no relative
    borrows the provider's lightest model's numbers, so it is never trusted
    with harder work than the weakest known model. A release the catalog has
    already moved past is added as deprecated, superseded by its relative, so
    it is visible but never chosen ahead of the current one.
    """
    result = list(catalog)
    for agent in _available_models.agents():
        known = [spec for spec in catalog if spec.agent == agent]
        names = {_available_models.canonical(name) for spec in known
                 for name in (spec.model, spec.model_id or "")}
        for found in _available_models.discovered(agent):
            if _available_models.canonical(found.value) in names:
                continue
            names.add(_available_models.canonical(found.value))
            relative = _available_models.closest_relative(
                found.value, ((spec.model, spec) for spec in known)
            )
            spec = _inferred_spec(
                agent, found, relative, known,
                (evidence or {}).get(_available_models.canonical(found.value), ()),
            )
            # Equal scores are decided by catalog order, so a newer release
            # goes ahead of the model it was inferred from: on a tie the
            # current release wins, never the one it replaced.
            peer = relative[0] if relative and not relative[1] else None
            position = result.index(peer) if peer in result else len(result)
            result.insert(position, spec)
            known.append(spec)
    return tuple(result)


def _inferred_spec(agent, found, relative, known, measured=()) -> ModelSpec:
    peer, older = relative if relative else (None, False)
    if "available_models" in _available_models._calibration_policy():
        older = False  # Retirement now comes from validated lifecycle evidence.
    if peer is None and known:
        peer = min(known, key=lambda spec: (spec.relative_capability, spec.relative_cost))
    efforts = tuple(found.efforts) or (peer.supported_efforts if peer else ("low", "medium", "high"))
    unmeasured = BenchmarkEntry(None, None, None, None, None, None, None, "HEURISTIC")
    basis = f"inferred from {peer.model}" if peer else "assumed from defaults (no catalogued relative)"
    # Dollar prices come from the versioned pricing catalog (else the active
    # calibration's feed), not from the relative: a model with no price is
    # scored as if it were expensive, so a cheaper new release would otherwise
    # lose to the older, priced one.
    resolution = _model_pricing.resolve_price(found.value, provider=agent)
    price = resolution.price if resolution.priced else None
    input_cost = price.input_per_million if price else None
    output_cost = price.output_per_million if price else None
    reasoning_cost = price.reasoning_per_million if price else None
    price_note = (
        "Unpriced until the pricing catalog or the model data feed prices it." if not price
        else f"Priced from the model data feed ({price.rate_id})." if price.rate_id.startswith("calibration/")
        else f"Priced from the pricing catalog ({price.rate_id})."
    )
    return ModelSpec(
        provider=peer.provider if peer else agent,
        agent=agent,
        model=found.value,
        model_id=found.value,
        active=True,
        recommended=False,
        deprecated=older,
        superseded_by=peer.model if older and peer else None,
        supported_efforts=efforts,
        strengths=peer.strengths if peer else frozenset(),
        weaknesses=peer.weaknesses if peer else frozenset(),
        # Measured evidence, when a calibration holds any, outranks the relative.
        relative_capability=measured_capability(measured) or (peer.relative_capability if peer else 3),
        relative_cost=peer.relative_cost if peer else 3,
        relative_token_efficiency=peer.relative_token_efficiency if peer else 3,
        relative_latency=peer.relative_latency if peer else 3,
        benchmarks={effort: unmeasured for effort in efforts},
        benchmark_source=None,
        benchmark_date=None,
        notes=f"Discovered from the {agent} CLI; capability and cost {basis}"
              + ("; capability ranked from measured Intelligence Index. " if measured else ". ")
              + f"{price_note}",
        input_cost=input_cost,
        output_cost=output_cost,
        reasoning_cost=reasoning_cost,
        intelligence_by_effort=_clean_intelligence(measured),
    )


def _parse_model(entry: Any) -> ModelSpec:
    if not isinstance(entry, dict):
        raise ModelRouterConfigError("each models.yaml entry must be a mapping")
    try:
        benchmarks_raw = entry.get("benchmarks") or {}
        benchmarks = {
            str(effort): _parse_benchmark(values) for effort, values in benchmarks_raw.items()
        }
        return ModelSpec(
            provider=str(entry["provider"]),
            agent=str(entry["agent"]),
            model=str(entry["model"]),
            model_id=entry.get("model_id"),
            active=bool(entry.get("active", True)),
            recommended=bool(entry.get("recommended", False)),
            deprecated=bool(entry.get("deprecated", False)),
            superseded_by=entry.get("superseded_by"),
            supported_efforts=tuple(str(item) for item in entry.get("supported_efforts") or ()),
            strengths=frozenset(str(item) for item in entry.get("strengths") or ()),
            weaknesses=frozenset(str(item) for item in entry.get("weaknesses") or ()),
            relative_capability=int(entry["relative_capability"]),
            relative_cost=int(entry["relative_cost"]),
            relative_token_efficiency=int(entry["relative_token_efficiency"]),
            relative_latency=int(entry["relative_latency"]),
            benchmarks=benchmarks,
            benchmark_source=entry.get("benchmark_source"),
            benchmark_date=entry.get("benchmark_date"),
            notes=str(entry.get("notes") or ""),
            input_cost=_optional_float(entry.get("input_cost")),
            output_cost=_optional_float(entry.get("output_cost")),
            reasoning_cost=_optional_float(entry.get("reasoning_cost")),
            intelligence_by_effort=_clean_intelligence(entry.get("intelligence_by_effort")),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ModelRouterConfigError(f"malformed models.yaml entry {entry!r}: {error}") from error


def _parse_benchmark(values: Any) -> BenchmarkEntry:
    if not isinstance(values, dict):
        raise ModelRouterConfigError("each benchmarks entry must be a mapping")
    return BenchmarkEntry(
        coding_agent_index=_optional_float(values.get("coding_agent_index")),
        deep_swe=_optional_float(values.get("deep_swe")),
        terminal_bench=_optional_float(values.get("terminal_bench")),
        swe_atlas_qna=_optional_float(values.get("swe_atlas_qna")),
        benchmark_cost_per_task=_optional_float(values.get("benchmark_cost_per_task")),
        benchmark_tokens_per_task=_optional_float(values.get("benchmark_tokens_per_task")),
        benchmark_runtime_minutes=_optional_float(values.get("benchmark_runtime_minutes")),
        data_quality=str(values.get("data_quality") or "HEURISTIC"),
    )


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _parse_weight_sets(raw: Any) -> tuple[dict[str, float], dict[str, float]]:
    """Return (cost-off weights, cost-on weights) from routing-rules.yaml.

    Accepts either the two-set mapping (``cost_consideration_off`` /
    ``cost_consideration_on``) or a legacy flat weights dict. A flat dict is
    treated as the OFF set with cost/token zeroed, and as the ON set as-is, so
    older fixtures keep loading.
    """
    if not isinstance(raw, dict) or not raw:
        raise ModelRouterConfigError("routing-rules.yaml 'weights' must be a mapping")
    if "cost_consideration_off" in raw or "cost_consideration_on" in raw:
        off_raw = raw.get("cost_consideration_off")
        on_raw = raw.get("cost_consideration_on")
        if not isinstance(off_raw, dict) or not isinstance(on_raw, dict):
            raise ModelRouterConfigError(
                "weights.cost_consideration_off and weights.cost_consideration_on must both be mappings"
            )
        return (
            {str(key): float(value) for key, value in off_raw.items()},
            {str(key): float(value) for key, value in on_raw.items()},
        )
    flat = {str(key): float(value) for key, value in raw.items()}
    off_weights = dict(flat)
    off_weights["cost_efficiency"] = 0.0
    off_weights["token_efficiency"] = 0.0
    return off_weights, flat


def _uses_cost_consideration(request: RouteRequest) -> bool:
    """True when the UI Cost Consideration setting (or its cost_sensitive alias) is on."""
    return bool(request.cost_consideration_enabled or request.cost_sensitive)


def load_routing_rules(path: Path | None = None) -> RoutingRules:
    data = _read_yaml(path or (_config_dir() / "routing-rules.yaml"))
    if not isinstance(data, dict):
        raise ModelRouterConfigError("routing-rules.yaml must contain a top-level mapping")
    try:
        bands = tuple(
            ComplexityBand(
                level=str(row["level"]),
                ai_grade_range=(int(row["ai_grade_range"][0]), int(row["ai_grade_range"][1])),
                min_capability=int(row["min_capability"]),
            )
            for row in data["complexity_bands"]
        )
        groups = tuple(
            TaskTypeGroup(
                name=str(name),
                task_types=tuple(str(t) for t in row.get("task_types") or ()),
                benchmark_emphasis={str(k): float(v) for k, v in (row.get("benchmark_emphasis") or {}).items()},
                extra_weights={str(k): float(v) for k, v in (row.get("extra_weights") or {}).items()},
            )
            for name, row in data["task_type_groups"].items()
        )
        off_weights, on_weights = _parse_weight_sets(data["weights"])
        return RoutingRules(
            complexity_bands=bands,
            effort_ladder=tuple(str(e) for e in data["effort_ladder"]),
            min_effort_by_complexity={str(k): str(v) for k, v in data["min_effort_by_complexity"].items()},
            task_type_groups=groups,
            weights=off_weights,
            cost_consideration_weights=on_weights,
            sensitivity_boost=float(data["sensitivity_boost"]),
            overqualification_penalty_per_level=float(data["overqualification_penalty_per_level"]),
            unnecessary_reasoning_penalty_per_level=float(data["unnecessary_reasoning_penalty_per_level"]),
            cost_consideration_unnecessary_reasoning_multiplier=float(
                data.get("cost_consideration_unnecessary_reasoning_multiplier", 1.0)
            ),
            minimum_expected_success=float(data.get("minimum_expected_success", 0.8)),
            cost_optimization_quality_tolerance=float(data.get("cost_optimization_quality_tolerance", 0.03)),
            tie_break_margin=float(data["tie_break_margin"]),
            confidence_floor=float(data["confidence_floor"]),
            confidence_score_gap_scale=float(data["confidence_score_gap_scale"]),
        )
    except (KeyError, TypeError, ValueError, IndexError) as error:
        raise ModelRouterConfigError(f"malformed routing-rules.yaml: {error}") from error


def _band_for(complexity: str | int, rules: RoutingRules) -> ComplexityBand:
    if isinstance(complexity, int) or (isinstance(complexity, str) and complexity.strip().lstrip("-").isdigit()):
        grade = int(complexity)
        for band in rules.complexity_bands:
            if band.ai_grade_range[0] <= grade <= band.ai_grade_range[1]:
                return band
        raise ModelRouterError(f"complexity grade {grade} is outside the configured 1-10 scale")
    level = str(complexity).strip().upper()
    for band in rules.complexity_bands:
        if band.level == level:
            return band
    raise ModelRouterError(f"unknown complexity level {complexity!r}")


def _group_for(task_type: str, rules: RoutingRules) -> TaskTypeGroup:
    key = str(task_type).strip().lower()
    if key not in TASK_TYPES:
        raise ModelRouterError(f"unknown task type {task_type!r}")
    for group in rules.task_type_groups:
        if key in group.task_types:
            return group
    for group in rules.task_type_groups:
        if group.name == "default":
            return group
    raise ModelRouterConfigError("routing-rules.yaml has no 'default' task_type_groups entry")


def _task_fit(model: ModelSpec, task_type: str) -> float:
    keywords = _TASK_TYPE_KEYWORDS.get(task_type, frozenset())
    if not keywords:
        return 0.6
    overlap = len(model.strengths & keywords) / len(keywords)
    fit = 0.5 + 0.5 * min(1.0, overlap)
    weakness_hits = len(model.weaknesses & keywords)
    fit -= 0.15 * weakness_hits
    return max(0.1, min(1.0, fit))


class _BenchmarkNormalizer:
    """Min-max normalizes raw benchmark values across the whole catalog.

    Built once per ``route()`` call so every candidate's coding-capability
    score is comparable on the same 0..1 scale, cheapest/most-expensive
    catalog entries included or not. Measured cost and token totals are
    tracked separately and only applied at the exact effort they were
    recorded — never extrapolated to an unmeasured reasoning level.
    """

    def __init__(self, catalog: Sequence[ModelSpec]) -> None:
        self._ranges: dict[str, tuple[float, float]] = {}
        for field in _BENCHMARK_FIELDS:
            values = [
                getattr(entry, field)
                for model in catalog
                for entry in model.benchmarks.values()
                if getattr(entry, field) is not None
            ]
            if values:
                self._ranges[field] = (min(values), max(values))
        measured_costs: list[float] = []
        measured_tokens: list[float] = []
        for model in catalog:
            for entry in model.benchmarks.values():
                if entry.data_quality != "MEASURED":
                    continue
                if entry.benchmark_cost_per_task is not None:
                    measured_costs.append(entry.benchmark_cost_per_task)
                if entry.benchmark_tokens_per_task is not None:
                    measured_tokens.append(entry.benchmark_tokens_per_task)
        measured_costs.extend(
            cost for model in catalog for effort in model.supported_efforts
            if (cost := estimated_dollar_cost(model, effort)) is not None
        )
        if measured_costs:
            self._ranges["benchmark_cost_per_task"] = (min(measured_costs), max(measured_costs))
        if measured_tokens:
            self._ranges["benchmark_tokens_per_task"] = (min(measured_tokens), max(measured_tokens))

    def normalize(self, field: str, value: float | None) -> float | None:
        if value is None or field not in self._ranges:
            return None
        low, high = self._ranges[field]
        if high <= low:
            return 0.5
        return (value - low) / (high - low)

    def invert_normalize(self, field: str, value: float | None) -> float | None:
        """Lower measured cost/tokens maps to a higher efficiency score."""
        if value is None or field not in self._ranges:
            return None
        low, high = self._ranges[field]
        if high <= low:
            return 1.0
        return (high - value) / (high - low)


def _coding_capability(
    model: ModelSpec,
    effort: str,
    group: TaskTypeGroup,
    normalizer: _BenchmarkNormalizer,
) -> tuple[float, str]:
    """(score, data_quality) — HEURISTIC when no benchmark exists at this exact effort."""
    entry = model.benchmarks.get(effort)
    if entry is None:
        return model.relative_capability / 5.0, "HEURISTIC"
    weighted_values: list[tuple[float, float]] = []
    for field in _BENCHMARK_FIELDS:
        normalized = normalizer.normalize(field, getattr(entry, field))
        if normalized is None:
            continue
        weight = group.benchmark_emphasis.get(field, 1.0)
        weighted_values.append((normalized, weight))
    if not weighted_values:
        return model.relative_capability / 5.0, "HEURISTIC"
    total_weight = sum(weight for _, weight in weighted_values)
    score = sum(value * weight for value, weight in weighted_values) / total_weight
    return score, entry.data_quality


def _context_fit(model: ModelSpec, swe_atlas_norm: float | None) -> float:
    if swe_atlas_norm is not None:
        return swe_atlas_norm
    return model.relative_capability / 5.0


def _measured_entry(model: ModelSpec, effort: str) -> BenchmarkEntry | None:
    entry = model.benchmarks.get(effort)
    if entry is None or entry.data_quality != "MEASURED":
        return None
    return entry


def estimated_dollar_cost(model: ModelSpec, effort: str) -> float | None:
    """Exact-effort measured cost, else a disclosed token-budget estimate.

    Public API prices are USD per million tokens. This is a comparison
    scenario, not a prediction of CLI subscription billing or actual usage.
    """
    entry = _measured_entry(model, effort)
    if entry is not None and entry.benchmark_cost_per_task is not None:
        return entry.benchmark_cost_per_task
    resolved = _model_pricing.resolve_price(model.model, provider=model.agent, calibration_entry=dataclasses.asdict(model))
    if not resolved.priced:
        return None
    price = resolved.price
    reasoning_tokens = {"low": 0, "medium": 1000, "high": 3000, "xhigh": 6000, "max": 10000}.get(effort, 0)
    reasoning_price = price.reasoning_per_million if price.reasoning_per_million is not None else price.output_per_million
    return (4000 * price.input_per_million + 1000 * price.output_per_million + reasoning_tokens * reasoning_price) / 1_000_000


def estimated_tokens_per_task(model: ModelSpec, effort: str) -> float | None:
    """Measured benchmark tokens/task at this exact effort, or None."""
    entry = _measured_entry(model, effort)
    return None if entry is None else entry.benchmark_tokens_per_task


def _cost_efficiency(model: ModelSpec, effort: str, normalizer: _BenchmarkNormalizer) -> float:
    measured = estimated_dollar_cost(model, effort)
    inverted = normalizer.invert_normalize("benchmark_cost_per_task", measured)
    if inverted is not None:
        return inverted
    return (6 - model.relative_cost) / 5.0


def _token_efficiency(model: ModelSpec, effort: str, normalizer: _BenchmarkNormalizer) -> float:
    measured = estimated_tokens_per_task(model, effort)
    inverted = normalizer.invert_normalize("benchmark_tokens_per_task", measured)
    if inverted is not None:
        return inverted
    return model.relative_token_efficiency / 5.0


def _expected_success(model: ModelSpec, required_capability: int) -> float:
    """Capability sufficiency on 0..1, used for scoring and the cost-ON floor.

    Models that meet the band sit in a tight high band so a one-level
    capability gap (e.g. 0.92 vs 0.94) can still fall inside
    ``cost_optimization_quality_tolerance``. Models that fall short are
    scaled down well below ``minimum_expected_success``.
    """
    if required_capability <= 0:
        return 1.0
    if model.relative_capability >= required_capability:
        # Cap the bonus at one capability step so a frontier model is only
        # slightly ahead of an adequate one (0.94 vs 0.92). That keeps both
        # inside cost_optimization_quality_tolerance; extra levels of
        # overqualification must not push cheaper capable models out of the pool.
        bonus = 0.02 if model.relative_capability > required_capability else 0.0
        return 0.92 + bonus
    return max(0.15, model.relative_capability / required_capability) * 0.75


def _required_capability(band: ComplexityBand, request: RouteRequest) -> int:
    required = band.min_capability
    if str(request.quality_requirement).strip().lower() == "high":
        required = min(5, required + 1)
    if request.capability_requirements:
        # Existing catalog ranks are ordinal 1-5, not measured percentages.
        floor = float(request.capability_requirements.get("recommended_capability_floor", 0))
        required = max(required, min(5, math.ceil(floor / 20)))
        if request.complexity_vector.get("security_risk", 0) >= 70:
            required = max(required, 4)
    return required


def _required_min_effort(band: ComplexityBand, rules: RoutingRules, request: RouteRequest) -> str:
    floor = rules.min_effort_by_complexity[band.level]
    if str(request.quality_requirement).strip().lower() == "high":
        index = min(len(rules.effort_ladder) - 1, rules.effort_ladder.index(floor) + 1)
        floor = rules.effort_ladder[index]
    requested = request.capability_requirements.get("recommended_reasoning")
    if requested in rules.effort_ladder:
        floor = rules.effort_ladder[max(rules.effort_ladder.index(floor), rules.effort_ladder.index(requested))]
    return floor


def _score_candidate(
    model: ModelSpec,
    effort: str,
    *,
    request: RouteRequest,
    rules: RoutingRules,
    band: ComplexityBand,
    group: TaskTypeGroup,
    required_capability: int,
    min_effort: str,
    normalizer: _BenchmarkNormalizer,
    cheapest_cost: int | None = None,
) -> tuple[float, float]:
    """Return ``(score, expected_success)``. See SKILL.md for the formula."""
    coding_capability, data_quality = _coding_capability(model, effort, group, normalizer)
    swe_atlas_norm = normalizer.normalize(
        "swe_atlas_qna",
        (model.benchmarks.get(effort) or BenchmarkEntry(*([None] * 7), "HEURISTIC")).swe_atlas_qna,
    )
    cost_on = _uses_cost_consideration(request)
    expected_success = _expected_success(model, required_capability)
    samples = [item for item in request.historical_performance if item.get("model") == model.model]
    if len(samples) >= 3:
        # Twenty prior observations shrink sparse history; influence is capped.
        successes = sum(bool(item.get("success")) for item in samples)
        observed = (20 * expected_success + successes) / (20 + len(samples))
        expected_success += max(-.08, min(.03, observed - expected_success))

    components = {
        "expected_success": expected_success,
        "task_fit": _task_fit(model, request.task_type),
        "coding_capability": coding_capability,
        "context_fit": _context_fit(model, swe_atlas_norm),
        "cost_efficiency": _cost_efficiency(model, effort, normalizer),
        "token_efficiency": _token_efficiency(model, effort, normalizer),
        "latency": model.relative_latency / 5.0,
        "reliability": coding_capability * (1.0 if data_quality == "MEASURED" else 0.85),
    }

    weights = rules.active_weights(cost_on)
    vector = request.complexity_vector
    if vector:
        weights["context_fit"] = weights.get("context_fit", 0) + .12 * vector.get("change_surface", 0) / 100
        weights["coding_capability"] = weights.get("coding_capability", 0) + .08 * vector.get("implementation_complexity", 0) / 100
        weights["reliability"] = weights.get("reliability", 0) + .08 * max(vector.get("architecture_risk", 0), vector.get("security_risk", 0)) / 100
    for key, bonus in group.extra_weights.items():
        if not cost_on and key in {"cost_efficiency", "token_efficiency"}:
            continue
        weights[key] = weights.get(key, 0.0) + bonus
    if request.token_sensitive:
        current = weights.get("token_efficiency", 0.0)
        if current <= 0:
            weights["token_efficiency"] = rules.cost_consideration_weights.get("token_efficiency", 0.08)
        else:
            weights["token_efficiency"] = current * rules.sensitivity_boost
    if cost_on and not request.latency_sensitive:
        # Automatic cost-first routing: latency may only break a cost tie.
        weights["latency"] = 0.0
    elif request.latency_sensitive:
        weights["latency"] = weights.get("latency", 0.0) * rules.sensitivity_boost

    positive = sum(weights.get(key, 0.0) * value for key, value in components.items())

    overqualification = max(0, model.relative_capability - required_capability) * rules.overqualification_penalty_per_level
    if cost_on and cheapest_cost is not None and model.relative_cost <= cheapest_cost:
        # Cost-first: the cheapest capable model must not lose to a more
        # expensive just-capable peer because it is overqualified.
        overqualification = 0.0
    extra_effort_levels = max(0, rules.effort_ladder.index(effort) - rules.effort_ladder.index(min_effort))
    reasoning_penalty = rules.unnecessary_reasoning_penalty_per_level
    if cost_on:
        reasoning_penalty *= rules.cost_consideration_unnecessary_reasoning_multiplier
    unnecessary_reasoning = extra_effort_levels * reasoning_penalty

    return positive - overqualification - unnecessary_reasoning, expected_success


def is_priced(spec: ModelSpec) -> bool:
    """Whether the model's spend can be recorded: it has a price in the pricing
    catalog, or valid input and output prices of its own (a calibration row
    carries the feed's)."""
    entry = dataclasses.asdict(spec) if (spec.input_cost is not None or spec.output_cost is not None) else None
    return _model_pricing.resolve_price(spec.model, provider=spec.agent, calibration_entry=entry).priced


def _eligible_models(catalog: Sequence[ModelSpec], availability: RoutingAvailability) -> list[ModelSpec]:
    eligible = []
    for model in catalog:
        if not model.active:
            continue
        if availability.enabled_agents is not None and model.agent not in availability.enabled_agents:
            continue
        if model.model in availability.disabled_models:
            continue
        if availability.enabled_models is not None and model.model not in availability.enabled_models:
            continue
        eligible.append(model)
    return eligible


def route(
    request: RouteRequest,
    *,
    catalog: Sequence[ModelSpec] | None = None,
    rules: RoutingRules | None = None,
    availability: RoutingAvailability = RoutingAvailability(),
) -> RoutingDecision:
    """Score every eligible (model, effort) pair and return the best one.

    Raises ``ModelRouterError`` when nothing is eligible — every provider,
    model, or effort the request could use was filtered out by
    ``availability``. Callers should treat that as "cannot dynamically route
    this task", not silently pick something unavailable.
    """
    catalog = catalog if catalog is not None else load_model_catalog()
    rules = rules if rules is not None else load_routing_rules()
    band = _band_for(request.complexity, rules)
    group = _group_for(request.task_type, rules)
    required_capability = _required_capability(band, request)
    min_effort = _required_min_effort(band, rules, request)
    min_effort_index = rules.effort_ladder.index(min_effort)
    normalizer = _BenchmarkNormalizer(catalog)

    eligible = _eligible_models(catalog, availability)
    if request.capability_requirements:
        context_floor = {"low": 0, "medium": .4, "high": .7}.get(request.capability_requirements.get("context_requirement"), 0)
        eligible = [model for model in eligible if model.relative_capability >= required_capability
                    and max(_context_fit(model, normalizer.normalize("swe_atlas_qna", entry.swe_atlas_qna))
                            for entry in (list(model.benchmarks.values()) or [BenchmarkEntry(*([None] * 7), "HEURISTIC")])) >= context_floor]
    # The exemption from the over-qualification penalty is measured against the
    # cheapest model that can do this work. Measured against the cheapest model
    # overall (a model too weak for the task), it never applied, so a newer
    # release at the same price as its predecessor was penalized for being
    # more capable and lost to it.
    capable_costs = [model.relative_cost for model in eligible
                     if model.relative_capability >= required_capability]
    cheapest_cost = min(
        capable_costs or [model.relative_cost for model in eligible], default=None
    )
    scored: list[RoutingCandidate] = []
    for model in eligible:
        for effort in model.supported_efforts:
            if effort not in rules.effort_ladder:
                continue
            if rules.effort_ladder.index(effort) < min_effort_index:
                continue
            score, expected_success = _score_candidate(
                model,
                effort,
                request=request,
                rules=rules,
                band=band,
                group=group,
                required_capability=required_capability,
                min_effort=min_effort,
                normalizer=normalizer,
                cheapest_cost=cheapest_cost,
            )
            scored.append(
                RoutingCandidate(
                    model=model,
                    effort=effort,
                    score=score,
                    expected_success=expected_success,
                    estimated_cost=estimated_dollar_cost(model, effort),
                    relative_cost=model.relative_cost,
                    relative_latency=model.relative_latency,
                )
            )

    if not scored:
        raise ModelRouterError(
            f"no eligible provider/model/effort combination for {request.task_type} at {band.level}"
        )

    cost_on = _uses_cost_consideration(request)
    pool = scored
    if cost_on:
        sufficient = [c for c in scored if c.expected_success >= rules.minimum_expected_success]
        if request.capability_requirements and not sufficient:
            raise ModelRouterError("no candidate meets repository-aware expected-success requirements")
        if sufficient:
            best_success = max(candidate.expected_success for candidate in sufficient)
            close = [
                candidate
                for candidate in sufficient
                if best_success - candidate.expected_success <= rules.cost_optimization_quality_tolerance
            ]
            pool = close or sufficient

    pool = sorted(pool, key=lambda candidate: candidate.score, reverse=True)
    top_score = pool[0].score
    tied = pool if request.capability_requirements and cost_on else [c for c in pool if top_score - c.score <= rules.tie_break_margin]
    if cost_on:
        # Dollar estimates only order candidates when every one has a measured
        # rate; an unmeasured price is unknown, not infinite, so the catalog's
        # relative cost tier orders a mixed pool.
        dollars_first = bool(request.capability_requirements) and all(
            c.estimated_cost is not None for c in tied)
        # Cost, then effort, then latency. A faster model cannot beat a cheaper
        # adequately capable one solely because it is faster.
        tied.sort(
            key=lambda c: (
                c.estimated_cost if dollars_first else (c.relative_cost if c.relative_cost else c.model.relative_cost),
                c.relative_cost if c.relative_cost else c.model.relative_cost,
                rules.effort_ladder.index(c.effort),
                c.relative_latency if c.relative_latency else c.model.relative_latency,
                -c.expected_success,
                -c.score,
            )
        )
    else:
        tied.sort(
            key=lambda c: (
                -c.model.relative_capability,
                -c.score,
                rules.effort_ladder.index(c.effort),
            )
        )
    if availability.preferred_provider:
        preferred = [c for c in tied if c.model.provider == availability.preferred_provider]
        if preferred and (
            not cost_on
            or all(c.relative_cost == preferred[0].relative_cost for c in tied)
        ):
            tied = preferred
    winner = tied[0]

    remaining = [c for c in scored if c is not winner]
    remaining.sort(key=lambda candidate: candidate.score, reverse=True)
    alternatives = tuple(candidate.as_dict() for candidate in remaining[:2])
    snapshot = tuple(candidate.as_dict() for candidate in sorted(scored, key=lambda item: item.score, reverse=True)[:12])
    native_scores = [candidate.score for candidate in scored]
    lowest, highest = min(native_scores), max(native_scores)
    if highest <= lowest:
        normalized = 1.0
    else:
        normalized = (winner.score - lowest) / (highest - lowest)

    # Confidence measures how close a *realistic* alternative was, not the
    # score of a strictly more expensive (cost-on) or ineligible model. A
    # one-unit cost-rank bump on a dominated bystander must not change the
    # reported decision, including confidence.
    confidence_peers = remaining
    if cost_on:
        winner_cost = winner.relative_cost if winner.relative_cost else winner.model.relative_cost
        confidence_peers = [
            candidate
            for candidate in pool
            if candidate is not winner
            and (candidate.relative_cost if candidate.relative_cost else candidate.model.relative_cost)
            <= winner_cost
        ]
        confidence_peers.sort(key=lambda candidate: candidate.score, reverse=True)
    runner_up_score = confidence_peers[0].score if confidence_peers else winner.score
    gap = max(0.0, winner.score - runner_up_score)
    confidence = min(1.0, rules.confidence_floor + gap / rules.confidence_score_gap_scale)

    reason = _explain(winner, remaining, request, cost_on)
    return RoutingDecision(
        provider=winner.model.provider,
        agent=winner.model.agent,
        model=winner.model.model,
        effort=winner.effort,
        complexity=band.level,
        task_type=request.task_type,
        confidence=round(confidence, 4),
        reason=reason,
        alternatives=alternatives,
        cost_consideration_enabled=cost_on,
        native_score=winner.score,
        normalized_score=normalized,
        candidates=snapshot,
        estimated_cost=winner.estimated_cost,
    )


def _effort_label(effort: str) -> str:
    labels = {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "XHigh", "max": "Max"}
    return labels.get(effort, effort)


def _candidate_label(candidate: RoutingCandidate) -> str:
    return f"{candidate.model.model} {_effort_label(candidate.effort)}"


def _explain(
    winner: RoutingCandidate,
    remaining: Sequence[RoutingCandidate],
    request: RouteRequest,
    cost_on: bool,
) -> str:
    task_label = request.task_type.replace("_", " ")
    if not cost_on:
        return (
            f"{_candidate_label(winner)} has the strongest task fit for this {task_label} "
            "workload; cost was not considered in routing."
        )
    pricier = [
        candidate
        for candidate in remaining
        if candidate.model.relative_cost > winner.model.relative_cost
        or (
            candidate.model.model == winner.model.model
            and candidate.effort != winner.effort
        )
    ]
    if pricier:
        alternative = max(
            pricier,
            key=lambda candidate: (candidate.expected_success, candidate.model.relative_cost),
        )
        return (
            f"{_candidate_label(winner)} is expected to meet the task requirements while offering "
            f"materially lower estimated cost and token usage than {_candidate_label(alternative)}."
        )
    return (
        f"{_candidate_label(winner)} is expected to meet the task requirements at the lowest "
        "estimated cost among capable options."
    )
