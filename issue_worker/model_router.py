"""Reusable Dynamic Model Router (issue #195).

The router's job is choosing a provider/model/reasoning-effort combination
reasonably expected to complete a given task successfully. When the UI Cost
Consideration setting is on (``RouteRequest.cost_consideration_enabled``), it
additionally prefers the least expensive, least token-intensive combination
that still clears a minimum expected-success bar — never simply the strongest
model available, and never a cheaper model that is not expected to succeed.
When that setting is off, dollar cost and token consumption have no weight.
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
from pathlib import Path
from typing import Any, Sequence

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
    cost_consideration_enabled: bool = False
    cost_sensitive: bool = False
    token_sensitive: bool = False
    latency_sensitive: bool = False
    quality_requirement: str = "normal"


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

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.model.provider,
            "agent": self.model.agent,
            "model": self.model.model,
            "effort": self.effort,
            "score": round(self.score, 4),
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
    cost_consideration_enabled: bool = False

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
        catalog.append(_parse_model(entry))
    return tuple(catalog)


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
    """Measured benchmark dollars/task at this exact effort, or None.

    Relative cost ranks are not converted into a fabricated dollar figure.
    """
    entry = _measured_entry(model, effort)
    return None if entry is None else entry.benchmark_cost_per_task


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
    return required


def _required_min_effort(band: ComplexityBand, rules: RoutingRules, request: RouteRequest) -> str:
    floor = rules.min_effort_by_complexity[band.level]
    if str(request.quality_requirement).strip().lower() == "high":
        index = min(len(rules.effort_ladder) - 1, rules.effort_ladder.index(floor) + 1)
        return rules.effort_ladder[index]
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
) -> tuple[float, float]:
    """Return ``(score, expected_success)``. See SKILL.md for the formula."""
    coding_capability, data_quality = _coding_capability(model, effort, group, normalizer)
    swe_atlas_norm = normalizer.normalize(
        "swe_atlas_qna",
        (model.benchmarks.get(effort) or BenchmarkEntry(*([None] * 7), "HEURISTIC")).swe_atlas_qna,
    )
    cost_on = _uses_cost_consideration(request)
    expected_success = _expected_success(model, required_capability)

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
    if request.latency_sensitive:
        weights["latency"] = weights.get("latency", 0.0) * rules.sensitivity_boost

    positive = sum(weights.get(key, 0.0) * value for key, value in components.items())

    overqualification = max(0, model.relative_capability - required_capability) * rules.overqualification_penalty_per_level
    extra_effort_levels = max(0, rules.effort_ladder.index(effort) - rules.effort_ladder.index(min_effort))
    reasoning_penalty = rules.unnecessary_reasoning_penalty_per_level
    if cost_on:
        reasoning_penalty *= rules.cost_consideration_unnecessary_reasoning_multiplier
    unnecessary_reasoning = extra_effort_levels * reasoning_penalty

    return positive - overqualification - unnecessary_reasoning, expected_success


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

    scored: list[RoutingCandidate] = []
    for model in _eligible_models(catalog, availability):
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
            )
            scored.append(
                RoutingCandidate(
                    model=model, effort=effort, score=score, expected_success=expected_success
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
    tied = [c for c in pool if top_score - c.score <= rules.tie_break_margin]
    if cost_on:
        tied.sort(key=lambda c: (c.model.relative_cost, rules.effort_ladder.index(c.effort)))
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
        if preferred:
            tied = preferred
    winner = tied[0]

    remaining = [c for c in scored if c is not winner]
    remaining.sort(key=lambda candidate: candidate.score, reverse=True)
    alternatives = tuple(candidate.as_dict() for candidate in remaining[:2])

    runner_up_score = remaining[0].score if remaining else winner.score
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
