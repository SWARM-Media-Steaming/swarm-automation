"""Model Routing Calibration (issue #205).

A deterministic service that refreshes the model/pricing/benchmark data
Dynamic Model Routing's scoring engine (``model_router.py``) uses, without
ever depending on an external source being reachable and without ever
silently replacing what is already active.

Pipeline (``ModelCalibrationService.refresh``): fetch -> validate -> normalize
-> diff against the active calibration -> recalculate routing metrics (via the
real ``model_router.route`` engine) -> simulate a regression check -> store a
*proposed* calibration. A proposed calibration only becomes active when a
caller explicitly activates it (``activate``, used for promotion and rollback)
or when ``activation_policy="auto"`` and the simulation reports no regression.

Every entry point (manual refresh, startup refresh, scheduled job, or an
AI-agent-triggered refresh) calls ``ModelCalibrationService.refresh`` with a
different ``initiated_by`` — never a separate reimplementation.
"""

from __future__ import annotations

import argparse
import calendar
import fcntl
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence

import model_data_sources as _sources
import model_router as _model_router
import model_router_yaml as _model_router_yaml
from ai_execution_history import sanitize_text

ALGORITHM_VERSION = "1.0"
DEFAULT_MIN_REFRESH_INTERVAL_HOURS = 6.0
FAILED_RETRY_BACKOFF_HOURS = 0.25
MAX_ERROR_LENGTH = 2000
MAX_HISTORY_ENTRIES = 30
STALE_REFRESH_SECONDS = 30 * 60
ALLOWED_INITIATORS = ("STARTUP", "USER", "SCHEDULED", "AI_AGENT")
ALLOWED_SOURCES = ("local", "models_dev", "artificial_analysis", "json")
ROUTER_FIELDS = (
    "provider",
    "agent",
    "model",
    "model_id",
    "active",
    "recommended",
    "deprecated",
    "superseded_by",
    "supported_efforts",
    "strengths",
    "weaknesses",
    "relative_capability",
    "relative_cost",
    "relative_token_efficiency",
    "relative_latency",
    "benchmarks",
    "benchmark_source",
    "benchmark_date",
    "notes",
    "input_cost",
    "output_cost",
    "reasoning_cost",
)
TOKEN_COST_ASSUMPTIONS = {
    "input_tokens": 4000,
    "output_tokens": 1000,
    "reasoning_tokens": {"low": 0, "medium": 1000, "high": 3000, "xhigh": 6000, "max": 10000},
    "unit": "USD per million tokens",
}

STATUS_ACTIVE = "ACTIVE"
STATUS_CANDIDATE = "CANDIDATE"
STATUS_DISCOVERED = "DISCOVERED"
STATUS_DEPRECATED = "DEPRECATED"
STATUS_DISABLED = "DISABLED"
ROUTABLE_STATUSES = (STATUS_ACTIVE, STATUS_CANDIDATE)

PROGRESS_STAGES = (
    ("fetching", "Fetching model data..."),
    ("validating", "Validating..."),
    ("comparing", "Comparing models..."),
    ("recalculating", "Recalculating routing..."),
    ("simulating", "Running routing simulation..."),
    ("complete", "Complete"),
)

WORKLOAD_CATEGORIES: tuple[tuple[str, str, int, str], ...] = (
    ("simple_coding", "Simple coding task", 2, "mechanical_edit"),
    ("complex_coding", "Complex debugging", 7, "deep_debugging"),
    ("architecture", "Architecture design", 8, "architecture"),
    ("general_tasks", "Simple text transformation", 1, "general_reasoning"),
    ("agentic_coding", "Agentic multi-step coding", 7, "large_refactor"),
)

_VERSION_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{3}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
_UNSAFE_TEXT_RE = re.compile(r'["\n\r]')


class CalibrationError(Exception):
    """Base class for every error this module raises deliberately."""


class CalibrationSourceError(CalibrationError):
    """The configured model-data source could not be reached."""


class CalibrationValidationError(CalibrationError):
    """The source responded, but its data is not usable."""


class CalibrationBusyError(CalibrationError):
    """A refresh is already running."""


def default_models_yaml_path() -> Path:
    return Path(__file__).resolve().parent.parent / "skills" / "model-router" / "models.yaml"


def fetch_local_source(path: Path | None = None) -> list[Any]:
    target = path or default_models_yaml_path()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as error:
        raise CalibrationSourceError(f"could not read local model catalog: {error}") from error
    try:
        data = _model_router_yaml.load(text)
    except _model_router_yaml.YamlError as error:
        raise CalibrationValidationError(f"could not parse local model catalog: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise CalibrationValidationError("local catalog must contain a top-level 'models' list")
    return data["models"]


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number < 0 or number > 1_000_000:
        return None
    return number


def _overlay_keys(entry: dict[str, Any]) -> set[tuple[str, str]]:
    provider = str(entry.get("provider") or "").strip().lower()
    keys = set()
    model = str(entry.get("model") or "").strip().lower()
    model_id = str(entry.get("model_id") or "").strip().lower()
    if provider and model:
        keys.add((provider, model))
    if provider and model_id:
        keys.add((provider, model_id))
    return keys


def _merge_observations(previous: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Coalesce one feed's observations, rejecting order-dependent values."""
    combined = dict(previous)
    for field, value in incoming.items():
        if field in combined and combined[field] != value:
            if isinstance(combined[field], dict) and isinstance(value, dict):
                value = _merge_observations(combined[field], value)
            else:
                # Do not include untrusted identities or source values in errors.
                raise CalibrationValidationError(
                    "Model source contains conflicting observations for the same model identity."
                )
        combined[field] = value
    return combined


def merge_overlay(
    local_entries: Sequence[dict[str, Any]], overlay_rows: Sequence[dict[str, Any]],
    *, previous: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply remote pricing/benchmark patches onto the bundled catalog.

    A feed is a set, not a list: which model identity a row's (provider,
    model[, model_id]) keys resolve to — and whether two rows contradict each
    other — is decided from the whole feed's declared aliases before any row
    is applied, so permuting a feed's row order can never change the verdict,
    the published prices, or which name a newly discovered model is published
    under. Two rows are the same identity if they share a key directly, if a
    third row ties their keys together (even transitively), or if either
    resolves to the same existing catalog entry. Unmatched overlay rows become
    DISCOVERED candidates and are never made routable by this merge alone.
    """
    merged = [dict(entry) for entry in local_entries if isinstance(entry, dict)]
    previous_models = _calibration_models(previous)
    lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in merged:
        prior = previous_models.get(f"{entry.get('provider')}/{entry.get('model')}", {})
        # Feeds are overlays, so an omitted or invalid observation cannot erase
        # the last known value. Router capability/eligibility still comes from
        # the local definition, never from a public pricing feed.
        for field in ("input_cost", "output_cost", "reasoning_cost", "external_evaluations", "speed", "latency_seconds", "release_date"):
            if prior.get(field) is not None:
                entry[field] = prior[field]
        # A missing retirement observation is not permission to re-enable.
        if prior.get("deprecated"):
            entry["deprecated"] = True
        for key in _overlay_keys(entry):
            lookup[key] = entry

    # Pass 1: parse every row and union the keys it names as one identity.
    # Union-find connectivity depends only on which keys are tied together by
    # the row set, never on the order rows are visited in.
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(key: tuple[str, str]) -> tuple[str, str]:
        parent.setdefault(key, key)
        root = key
        while parent[root] != root:
            root = parent[root]
        while parent[key] != root:
            parent[key], key = root, parent[key]
        return root

    def union(a: tuple[str, str], b: tuple[str, str]) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    parsed_rows: list[tuple[set[tuple[str, str]], dict[str, Any]]] = []
    usable_rows = 0
    for raw in overlay_rows:
        if not isinstance(raw, dict):
            continue
        provider = raw.get("provider")
        model = raw.get("model") or raw.get("model_id")
        if not isinstance(provider, str) or not isinstance(model, str):
            continue
        if not provider.strip() or not model.strip():
            continue
        observations: dict[str, Any] = {}
        for field in ("input_cost", "output_cost", "reasoning_cost", "speed", "latency_seconds"):
            parsed = _finite_float(raw.get(field))
            if parsed is not None:
                observations[field] = parsed
        evaluations = raw.get("evaluations")
        if isinstance(evaluations, dict):
            cleaned = {
                sanitize_text(str(name))[:80]: parsed
                for name, value in list(evaluations.items())[:20]
                if (parsed := _finite_float(value)) is not None
            }
            if cleaned:
                observations["external_evaluations"] = cleaned
        release = sanitize_text(str(raw.get("release_date") or "")).strip()[:40]
        if release:
            observations["release_date"] = release
        if isinstance(raw.get("deprecated"), bool):
            observations["deprecated"] = raw["deprecated"]
        if not observations:
            continue
        usable_rows += 1
        keys = _overlay_keys(raw)
        keys_list = list(keys)
        for key in keys_list:
            find(key)
        for key in keys_list[1:]:
            union(keys_list[0], key)
        parsed_rows.append((keys, observations))
    if not usable_rows:
        raise CalibrationValidationError("Model source contained no usable model pricing, benchmark, or performance data.")

    # Pass 2: group every row's keys by its final component, independent of
    # row order. A component resolves to whichever single catalog entry any
    # of its keys already names; two components that each resolve to that
    # same catalog entry (one directly, one only via a shared alias key) are
    # the same published identity and must not be validated independently.
    component_keys: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for keys, _ in parsed_rows:
        root = find(next(iter(keys)))
        component_keys.setdefault(root, set()).update(keys)

    group_key_for_root: dict[tuple[str, str], tuple[str, Any]] = {}
    group_target: dict[tuple[str, Any], dict[str, Any] | None] = {}
    group_all_keys: dict[tuple[str, Any], set[tuple[str, str]]] = {}
    for root, keys in component_keys.items():
        targets = {id(lookup[key]): lookup[key] for key in keys if key in lookup}
        if len(targets) > 1:
            raise CalibrationValidationError("Model source contains ambiguous model aliases.")
        target = next(iter(targets.values()), None)
        group_key = ("catalog", id(target)) if target is not None else ("new", root)
        group_key_for_root[root] = group_key
        group_target.setdefault(group_key, target)
        group_all_keys.setdefault(group_key, set()).update(keys)

    # Pass 3: accumulate every row's observations into its resolved group.
    # Coalescing here (rather than per-component) is what makes a row reached
    # only through an alias, or resolved through a pre-existing catalog
    # alias, still conflict with a contradictory row for the same identity.
    combined_by_group: dict[tuple[str, Any], dict[str, Any]] = {}
    for keys, observations in parsed_rows:
        group_key = group_key_for_root[find(next(iter(keys)))]
        combined_by_group[group_key] = _merge_observations(
            combined_by_group.get(group_key, {}), observations
        )

    discovered: list[dict[str, Any]] = []
    for group_key, target in group_target.items():
        all_keys = group_all_keys[group_key]
        if target is None:
            canonical_provider, canonical_model = min(all_keys)
            target = {
                "provider": canonical_provider[:80],
                "model": canonical_model[:120],
                "status": STATUS_DISCOVERED,
                "active": False,
            }
            discovered.append(target)
        combined = combined_by_group.get(group_key, {})
        for key in all_keys:
            lookup[key] = target
        # A public feed cannot re-enable a retired catalog model. Still compare
        # explicit false observations above so contradictory rows fail validation.
        target.update({field: value for field, value in combined.items()
                       if field != "deprecated" or value is True})
    return merged, discovered


def _model_spec_to_dict(spec: "_model_router.ModelSpec") -> dict[str, Any]:
    """A JSON-safe, fully round-trippable copy of a parsed model entry."""
    return {
        "provider": spec.provider,
        "agent": spec.agent,
        "model": spec.model,
        "model_id": spec.model_id,
        "active": spec.active,
        "recommended": spec.recommended,
        "deprecated": spec.deprecated,
        "superseded_by": spec.superseded_by,
        "supported_efforts": list(spec.supported_efforts),
        "strengths": sorted(spec.strengths),
        "weaknesses": sorted(spec.weaknesses),
        "relative_capability": spec.relative_capability,
        "relative_cost": spec.relative_cost,
        "relative_token_efficiency": spec.relative_token_efficiency,
        "relative_latency": spec.relative_latency,
        "benchmarks": {
            effort: {
                "coding_agent_index": entry.coding_agent_index,
                "deep_swe": entry.deep_swe,
                "terminal_bench": entry.terminal_bench,
                "swe_atlas_qna": entry.swe_atlas_qna,
                "benchmark_cost_per_task": entry.benchmark_cost_per_task,
                "benchmark_tokens_per_task": entry.benchmark_tokens_per_task,
                "benchmark_runtime_minutes": entry.benchmark_runtime_minutes,
                "data_quality": entry.data_quality,
            }
            for effort, entry in spec.benchmarks.items()
        },
        "benchmark_source": spec.benchmark_source,
        "benchmark_date": spec.benchmark_date,
        "notes": spec.notes,
        "input_cost": spec.input_cost,
        "output_cost": spec.output_cost,
        "reasoning_cost": spec.reasoning_cost,
    }


def _entry_text_fields(entry: dict[str, Any]) -> Sequence[str]:
    return (
        entry.get("notes") or "",
        entry.get("benchmark_source") or "",
        entry.get("benchmark_date") or "",
        entry.get("model_id") or "",
        entry.get("superseded_by") or "",
        entry.get("release_date") or "",
    )


def _calibration_model_entry(
    spec: "_model_router.ModelSpec", raw: dict[str, Any],
) -> dict[str, Any]:
    """Normalize all published inputs, including observations outside ModelSpec.

    Duplicate validation must compare the same values that publication uses;
    otherwise a later row can silently overwrite performance or source data.
    """
    entry = _model_spec_to_dict(spec)
    if raw.get("release_date"):
        entry["release_date"] = sanitize_text(str(raw["release_date"]))[:40]
    for field in ("speed", "latency_seconds"):
        if raw.get(field) is not None:
            entry[field] = _finite_float(raw[field])
    if raw.get("external_evaluations"):
        entry["external_evaluations"] = raw["external_evaluations"]
    return entry


def _validate_and_parse(raw_entries: Any) -> tuple[list["_model_router.ModelSpec"], list[str]]:
    if not isinstance(raw_entries, list) or not raw_entries:
        raise CalibrationValidationError("source data did not contain any model entries")
    specs: list[_model_router.ModelSpec] = []
    warnings: list[str] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for index, raw in enumerate(raw_entries):
        try:
            spec = _model_router._parse_model(raw)
        except _model_router.ModelRouterConfigError as error:
            warnings.append(f"entry {index}: {error}")
            continue
        if not _TOKEN_RE.match(spec.provider) or not _TOKEN_RE.match(spec.model):
            warnings.append(f"entry {index}: provider/model has unsupported characters")
            continue
        entry_dict = _calibration_model_entry(spec, raw)
        if any(_UNSAFE_TEXT_RE.search(str(text)) for text in _entry_text_fields(entry_dict)):
            warnings.append(f"entry {index}: a text field contains an unsupported character")
            continue
        key = (spec.provider, spec.model)
        if key in seen:
            if seen[key] != entry_dict:
                raise CalibrationValidationError(
                    "Catalog contains conflicting entries for the same model identity."
                )
            warnings.append(f"entry {index}: duplicate model {spec.provider}/{spec.model}")
            continue
        seen[key] = entry_dict
        specs.append(spec)
    if not specs:
        raise CalibrationValidationError(
            "no valid model entries after validation: " + "; ".join(warnings[:5])
        )
    return specs, warnings


def _cost_efficiency(entry: dict[str, Any]) -> float:
    cost = max(1, int(entry["relative_cost"]))
    return round(entry["relative_capability"] / cost, 2)


def _best_benchmark(entry: dict[str, Any], field: str) -> tuple[float | None, str]:
    fallback: float | None = None
    for benchmark in entry["benchmarks"].values():
        value = benchmark.get(field)
        if value is None:
            continue
        if benchmark.get("data_quality") == "MEASURED":
            return value, "MEASURED"
        if fallback is None:
            fallback = value
    return fallback, "HEURISTIC"


def _status_for(
    entry: dict[str, Any],
    prev_by_key: dict[str, dict[str, Any]],
    approved_keys: frozenset[str],
    *,
    has_previous: bool,
) -> str:
    """DISCOVERED is a review gate, not a one-refresh delay: once assigned it
    is carried forward on every later refresh regardless of what else in the
    calibration changes, until ``approve_discovered_model`` explicitly
    approves that key. Without this, "present in the immediately preceding
    calibration" reads as approval, so any unrelated refresh ages a
    never-reviewed model straight into the live routing catalog.
    """
    if entry["deprecated"]:
        return STATUS_DEPRECATED
    if not entry["active"]:
        return STATUS_DISABLED
    if entry["key"] in approved_keys:
        return STATUS_ACTIVE if entry["recommended"] else STATUS_CANDIDATE
    prev = prev_by_key.get(entry["key"])
    if has_previous and prev is None:
        return STATUS_DISCOVERED
    if prev is not None and prev.get("status") == STATUS_DISCOVERED:
        return STATUS_DISCOVERED
    return STATUS_ACTIVE if entry["recommended"] else STATUS_CANDIDATE


def _weights_summary(routing_optimization: str) -> dict[str, str]:
    """Automatic routing is always cost-first after capability gates."""
    _ = routing_optimization
    return {
        "capability": "high",
        "task_fit": "high",
        "cost_efficiency": "high",
        "performance": "medium",
        "reliability": "medium",
    }


def routing_mode_label(routing_optimization: str) -> str:
    """Saved ``best``/``quality`` labels migrate to cost-aware automatic routing."""
    _ = routing_optimization
    return "cost_aware"


def recalculate_routing(
    specs: Sequence["_model_router.ModelSpec"], *, routing_optimization: str
) -> dict[str, dict[str, Any]]:
    """Example routing decisions, computed with the real scoring engine."""
    cost_on = True
    _ = routing_optimization
    try:
        rules = _model_router.load_routing_rules()
    except _model_router.ModelRouterConfigError:
        rules = None
    routing: dict[str, dict[str, Any]] = {}
    for key, label, complexity, task_type in WORKLOAD_CATEGORIES:
        request = _model_router.RouteRequest(
            task_type=task_type,
            complexity=complexity,
            cost_consideration_enabled=cost_on,
            cost_sensitive=cost_on,
        )
        try:
            kwargs: dict[str, Any] = {"catalog": specs}
            if rules is not None:
                kwargs["rules"] = rules
            decision = _model_router.route(request, **kwargs)
            routing[key] = {
                "label": label,
                "provider": decision.provider,
                "agent": decision.agent,
                "model": decision.model,
                "effort": decision.effort,
                "confidence": decision.confidence,
                "reason": decision.reason,
            }
        except _model_router.ModelRouterError as error:
            routing[key] = {
                "label": label,
                "provider": None,
                "agent": None,
                "model": None,
                "effort": None,
                "confidence": None,
                "reason": sanitize_text(str(error))[:400],
            }
    return routing


def _routing_costs(
    routing: dict[str, Any], models_by_key: dict[str, dict[str, Any]]
) -> dict[str, float]:
    """Per-workload dollar estimates; ordinal cost ranks are never dollars."""
    costs: dict[str, float] = {}
    for category, decision in routing.items():
        provider, model = decision.get("provider"), decision.get("model")
        if not provider or not model:
            continue
        spec = models_by_key.get(f"{provider}/{model}")
        if not spec:
            continue
        try:
            parsed = _model_router._parse_model(spec)
            dollar = _model_router.estimated_dollar_cost(parsed, str(decision.get("effort") or "medium"))
        except _model_router.ModelRouterConfigError:
            dollar = None
        if dollar is not None:
            costs[category] = dollar
    return costs


def _average_routing_cost(
    routing: dict[str, Any], models_by_key: dict[str, dict[str, Any]]
) -> float | None:
    costs = _routing_costs(routing, models_by_key)
    return round(sum(costs.values()) / len(costs), 4) if costs else None


def _benchmark_value_changes(previous: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare named benchmark values for both review summaries and history.

    External evaluations remain in their source's units and namespace; they
    need review even when the effort-specific router benchmarks are unchanged.
    """
    fields = ("coding_score", "agentic_score", "reasoning_score", "relative_capability")
    before = {field: previous.get(field) for field in fields}
    after = {field: new.get(field) for field in fields}
    for entry, values in ((previous, before), (new, after)):
        for effort, benchmark in (entry.get("benchmarks") or {}).items():
            values.update(
                (f"benchmarks.{effort}.{name}", value)
                for name, value in benchmark.items()
            )
        values.update(
            (f"external_evaluations.{name}", value)
            for name, value in (entry.get("external_evaluations") or {}).items()
        )
    return [
        {"field": field, "previous": before.get(field), "new": after.get(field)}
        for field in sorted(before.keys() | after.keys())
        if before.get(field) != after.get(field)
    ]


def _calibration_models(calibration: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Include external discoveries in comparison and history, not live routing."""
    calibration = calibration or {}
    return {
        model.get("key") or f"{model['provider']}/{model['model']}": model
        for model in list(calibration.get("discovered_models") or []) + list(calibration.get("models") or [])
    }


def diff_calibrations(previous: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    prev_models = _calibration_models(previous)
    new_models = _calibration_models(new)
    # Newly discovered *this round* -- drives has_meaningful_change and the
    # notification gate, so a model that was already awaiting review before
    # this refresh does not repeatedly count as "new" or re-trigger a notice.
    newly_discovered = sorted(key for key in new_models if key not in prev_models)
    removed_models = sorted(key for key in prev_models if key not in new_models)
    # Every model still sitting in DISCOVERED status, whether or not it was
    # discovered this round -- so the change summary keeps surfacing an
    # unreviewed model instead of only mentioning it once and then going
    # silent about it forever.
    pending_review = sorted(
        key for key, model in new_models.items() if model.get("status") == STATUS_DISCOVERED
    )
    discovered = sorted(set(newly_discovered) | set(pending_review))

    pricing_changes: list[dict[str, Any]] = []
    benchmark_changes: list[dict[str, Any]] = []
    performance_changes: list[dict[str, Any]] = []
    routing_input_changes: list[dict[str, Any]] = []
    # A model already present last refresh whose `status` alone moves (most
    # notably DISCOVERED -> ACTIVE/CANDIDATE right after approve_discovered_
    # model) must count as meaningful on its own. Without this, the operator
    # sequence of "review, approve, refresh" with nothing else changed
    # computes has_meaningful_change=False, and _refresh_locked discards the
    # correctly-recalculated new_calibration instead of ever persisting it --
    # the approval silently never takes effect.
    status_changes: list[dict[str, Any]] = []
    for key, model in new_models.items():
        prev = prev_models.get(key)
        if prev is not None and prev.get("status") != model.get("status"):
            status_changes.append(
                {
                    "key": key,
                    "provider": model["provider"],
                    "model": model["model"],
                    "previous_status": prev.get("status"),
                    "new_status": model.get("status"),
                }
            )
    for key, model in new_models.items():
        prev = prev_models.get(key)
        if prev is None:
            continue
        # The representative workloads cannot cover every task or constraint.
        # Preserve eligibility and task-fit changes even for unselected models.
        for field in ("agent", "model_id", "active", "recommended", "deprecated",
                      "superseded_by", "supported_efforts", "strengths", "weaknesses"):
            if prev.get(field) != model.get(field):
                routing_input_changes.append({
                    "key": key, "provider": model["provider"], "model": model["model"],
                    "field": field, "previous": prev.get(field), "new": model.get(field),
                })
        price_fields = ("relative_cost", "input_cost", "output_cost", "reasoning_cost")
        if any(prev.get(field) != model.get(field) for field in price_fields):
            pricing_changes.append(
                {
                    "key": key,
                    "provider": model["provider"],
                    "model": model["model"],
                    "previous_cost_rank": prev.get("relative_cost"),
                    "new_cost_rank": model.get("relative_cost"),
                    "previous_input_cost": prev.get("input_cost"),
                    "new_input_cost": model.get("input_cost"),
                    "previous_output_cost": prev.get("output_cost"),
                    "new_output_cost": model.get("output_cost"),
                    "previous_reasoning_cost": prev.get("reasoning_cost"),
                    "new_reasoning_cost": model.get("reasoning_cost"),
                }
            )
        for change in _benchmark_value_changes(prev, model):
            benchmark_changes.append(
                {
                    "key": key,
                    "provider": model["provider"],
                    "model": model["model"],
                    **change,
                }
            )
        for field in ("speed", "latency_seconds", "relative_latency", "relative_token_efficiency"):
            if prev.get(field) != model.get(field):
                performance_changes.append({
                    "key": key, "provider": model["provider"], "model": model["model"],
                    "field": field, "previous": prev.get(field), "new": model.get(field),
                })

    prev_routing = (previous or {}).get("routing", {})
    new_routing = new.get("routing", {})
    routing_changes: list[dict[str, Any]] = []
    for key, decision in new_routing.items():
        old_decision = prev_routing.get(key)
        old_pick = (old_decision or {}).get("model")
        old_effort = (old_decision or {}).get("effort")
        new_pick = decision.get("model")
        new_effort = decision.get("effort")
        if old_pick != new_pick or old_effort != new_effort:
            routing_changes.append(
                {
                    "category": key,
                    "label": decision.get("label"),
                    "previous": {"model": old_pick, "effort": old_effort} if old_decision else None,
                    "new": {"model": new_pick, "effort": new_effort},
                }
            )

    routed_model_keys = sorted(
        {
            f"{decision.get('provider')}/{decision.get('model')}"
            for decision in list(prev_routing.values()) + list(new_routing.values())
            if decision.get("provider") and decision.get("model")
        }
    )

    before_costs = _routing_costs(prev_routing, prev_models)
    after_costs = _routing_costs(new_routing, new_models)
    # Both averages must describe the same workload sample. Learning a price
    # for previously unpriced work cannot masquerade as a routing saving.
    comparable = sorted(before_costs.keys() & after_costs.keys())
    cost_before = round(sum(before_costs[key] for key in comparable) / len(comparable), 4) if comparable else None
    cost_after = round(sum(after_costs[key] for key in comparable) / len(comparable), 4) if comparable else None
    cost_change_percent = None
    if cost_before not in (None, 0) and cost_after is not None:
        cost_change_percent = round((cost_after - cost_before) / cost_before * 100, 1)

    has_change = bool(
        newly_discovered or removed_models or pricing_changes or benchmark_changes
        or performance_changes or routing_changes or status_changes or routing_input_changes
    )
    return {
        "has_meaningful_change": has_change,
        "discovered_models": discovered,
        "newly_discovered_models": newly_discovered,
        "removed_models": removed_models,
        "pending_review_models": pending_review,
        "routed_model_keys": routed_model_keys,
        "pricing_changes": pricing_changes,
        "benchmark_changes": benchmark_changes,
        "performance_changes": performance_changes,
        "routing_input_changes": routing_input_changes,
        "routing_changes": routing_changes,
        "status_changes": status_changes,
        "estimated_cost_before": cost_before,
        "estimated_cost_after": cost_after,
        "estimated_cost_change_percent": cost_change_percent,
        "cost_comparison_categories": comparable,
        "models_checked": len(new_models),
        "capability": "Not yet simulated",
    }


def run_simulation(
    previous: dict[str, Any] | None, new: dict[str, Any], diff: dict[str, Any]
) -> dict[str, Any]:
    prev_models = {m["key"]: m for m in (previous or {}).get("models", [])}
    new_models = {m["key"]: m for m in new["models"]}
    prev_routing = (previous or {}).get("routing", {})
    new_routing = new.get("routing", {})

    regressions: list[dict[str, str]] = []
    for key, decision in new_routing.items():
        provider, model = decision.get("provider"), decision.get("model")
        old_decision = prev_routing.get(key) or {}
        old_provider, old_model = old_decision.get("provider"), old_decision.get("model")
        if not provider or not model:
            # Only a regression if a workload that used to route somewhere
            # now routes nowhere. No previous calibration (a first-ever
            # bootstrap) or a workload that already lacked coverage is not
            # a regression -- it is a coverage gap, not something that used
            # to work and stopped.
            if old_provider and old_model:
                regressions.append(
                    {"category": key, "reason": "no eligible model for this workload"}
                )
            continue
        if not old_provider or not old_model:
            continue
        new_capability = new_models.get(f"{provider}/{model}", {}).get("relative_capability")
        old_capability = prev_models.get(f"{old_provider}/{old_model}", {}).get("relative_capability")
        if new_capability is not None and old_capability is not None and new_capability < old_capability - 1:
            regressions.append(
                {
                    "category": key,
                    "reason": f"capability dropped from {old_capability} to {new_capability}",
                }
            )

    capability_note = (
        "Capability reduction detected" if regressions else "No meaningful reduction detected"
    )
    diff["capability"] = capability_note
    return {
        "regression_ok": not regressions,
        "regressions": regressions,
        "estimated_cost_before": diff["estimated_cost_before"],
        "estimated_cost_after": diff["estimated_cost_after"],
        "estimated_cost_change_percent": diff["estimated_cost_change_percent"],
        "capability": capability_note,
    }


def iso_now(timestamp: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp if timestamp is not None else time.time()))


def _parse_iso(text: str) -> float:
    return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                     prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
        tmp = Path(handle.name)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_bytes(path, json.dumps(payload, indent=2, sort_keys=True, allow_nan=False).encode("utf-8"))


def _is_calibration(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and bool(_VERSION_RE.fullmatch(str(payload.get("version") or "")))
        and isinstance(payload.get("models"), list)
        and all(isinstance(model, dict) for model in payload["models"])
        and isinstance(payload.get("routing"), dict)
    )


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CalibrationError(f"could not read {path}: {error}") from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise CalibrationError(f"could not parse {path}: {error}") from error


def _version_for(timestamp: float, existing_versions: set[str]) -> str:
    date = time.strftime("%Y-%m-%d", time.gmtime(timestamp))
    # Pruning retains the newest version. Never fill holes left by pruning or
    # rollback, including when the system clock moves back to an earlier date.
    latest = max((v for v in existing_versions if _VERSION_RE.fullmatch(v)), default="")
    date = max(date, latest[:10])
    sequence = int(latest[-3:]) + 1 if latest.startswith(date + "-") else 1
    if sequence > 999:
        raise CalibrationError("exhausted calibration versions for today")
    return f"{date}-{sequence:03d}"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _append_history(previous: dict[str, Any] | None, entry: dict[str, Any], now_ts: float) -> None:
    prev = previous or {}
    pricing_history = list(prev.get("pricing_history") or [])
    benchmark_history = list(prev.get("benchmark_history") or [])
    stamp = iso_now(now_ts)
    for field in ("input_cost", "output_cost", "reasoning_cost", "relative_cost"):
        if prev.get(field) != entry.get(field) and (prev.get(field) is not None or entry.get(field) is not None):
            pricing_history.append(
                {"at": stamp, "field": field, "previous": prev.get(field), "new": entry.get(field)}
            )
    benchmark_history.extend(
        {"at": stamp, **change} for change in _benchmark_value_changes(prev, entry)
    )
    entry["pricing_history"] = pricing_history[-20:]
    entry["benchmark_history"] = benchmark_history[-20:]


def notification_for(result: dict[str, Any], *, consecutive_failures: int = 0) -> dict[str, Any]:
    """Whether a startup/scheduled refresh should surface a user-facing notice."""
    status = result.get("status")
    diff = result.get("diff") or {}
    if status == "failed":
        if consecutive_failures >= 3:
            return {
                "should_notify": True,
                "kind": "error",
                "message": "Model data refresh keeps failing. Existing routing configuration remains active.",
            }
        return {"should_notify": False, "kind": "error", "message": ""}
    if status != "changed":
        return {"should_notify": False, "kind": "info", "message": ""}
    reasons = []
    # Only models newly discovered *this* round page the user -- one already
    # awaiting review from an earlier refresh does not re-notify every
    # startup just because it is still unreviewed (see discovered_models'
    # broader, non-notifying use in the diff for the ongoing summary).
    discovered = diff.get("newly_discovered_models") or []
    if discovered:
        reasons.append(f"{len(discovered)} new model{'s' if len(discovered) != 1 else ''} discovered")
    # A pricing change is only "significant" enough to notify about if it (a)
    # touches a model some workload is actually routed to, and (b) moves a
    # real published dollar price -- not just the catalog's 1-5 ordinal
    # `relative_cost` rank, which by itself changes no dollar estimate and no
    # routing decision. Both are needed: the smallest possible rank bump on a
    # model that some workload happens to route to still changes nothing
    # real about routing and must not page the user (issue: "Only surface a
    # meaningful notification when ... significant pricing changes occur").
    routed_keys = set(diff.get("routed_model_keys") or [])
    pricing = [
        item
        for item in (diff.get("pricing_changes") or [])
        if item.get("key") in routed_keys
        and (
            item.get("previous_input_cost") != item.get("new_input_cost")
            or item.get("previous_output_cost") != item.get("new_output_cost")
            or item.get("previous_reasoning_cost") != item.get("new_reasoning_cost")
        )
    ]
    if pricing:
        reasons.append(f"{len(pricing)} pricing change{'s' if len(pricing) != 1 else ''}")
    routing = diff.get("routing_changes") or []
    if routing:
        reasons.append(f"{len(routing)} routing change{'s' if len(routing) != 1 else ''}")
    if result.get("activated") is False and diff.get("has_meaningful_change"):
        reasons.append("manual review is required")
    if not reasons:
        return {"should_notify": False, "kind": "info", "message": ""}
    return {
        "should_notify": True,
        "kind": "info",
        "message": "Model routing update: " + "; ".join(reasons) + ".",
    }


def explain_update(active: dict[str, Any] | None, proposed: dict[str, Any] | None, diff: dict[str, Any] | None) -> dict[str, Any]:
    """Grounded explanation of a stored calibration diff. Invents no numbers."""
    diff = diff or {}
    answers: list[dict[str, str]] = []
    for key in diff.get("removed_models") or []:
        answers.append({
            "question": f"Why is {key} absent from the updated catalog?",
            "answer": "The model is no longer present in the refreshed catalog. Activating this calibration removes it from routing eligibility.",
        })
    routing_changes = diff.get("routing_changes") or []
    proposed_routing = (proposed or {}).get("routing") or {}
    for change in routing_changes:
        category = change.get("category")
        decision = proposed_routing.get(category) or {}
        previous = (change.get("previous") or {}).get("model")
        new_model = (change.get("new") or {}).get("model")
        reason = decision.get("reason") or "The stored routing simulation recorded this change."
        answers.append(
            {
                "question": f"Why did {change.get('label') or category} change?",
                "answer": (
                    f"{previous or 'none'} → {new_model or 'none'}. {reason}"
                ),
            }
        )
    for change in (diff.get("pricing_changes") or [])[:8]:
        details = []
        for field, label in (("input_cost", "Input price"), ("output_cost", "Output price"),
                             ("reasoning_cost", "Reasoning price"), ("cost_rank", "Cost rank")):
            before, after = change.get(f"previous_{field}"), change.get(f"new_{field}")
            if before == after:
                continue
            detail = f"{label} {before if before is not None else 'unavailable'} → {after if after is not None else 'unavailable'}"
            if field != "cost_rank":
                if before and after is not None:
                    detail += f" ({round((after - before) / before * 100, 1):+}%)"
                detail += " USD per million tokens"
            details.append(detail + ".")
        answers.append(
            {
                "question": f"Is the price change for {change.get('key')} significant?",
                "answer": " ".join(details),
            }
        )
    for change in (diff.get("performance_changes") or [])[:8]:
        answers.append({
            "question": f"What performance data changed for {change.get('key')}?",
            "answer": f"{change.get('field')}: {change.get('previous')} → {change.get('new')}.",
        })
    for change in (diff.get("routing_input_changes") or [])[:8]:
        answers.append({
            "question": f"What routing input changed for {change.get('key')}?",
            "answer": f"{change.get('field')}: {change.get('previous')} → {change.get('new')}. "
                      "This can affect tasks beyond the representative routing examples.",
        })
    for change in (diff.get("benchmark_changes") or [])[:8]:
        before = change.get("previous")
        after = change.get("new")
        answers.append(
            {
                "question": f"What benchmark changed for {change.get('key')}?",
                "answer": (
                    f"{change.get('field')}: "
                    f"{before if before is not None else 'unavailable'} → "
                    f"{after if after is not None else 'unavailable'}. "
                    "Benchmark results are comparative data, not guarantees of task quality."
                ),
            }
        )
    discovered = diff.get("discovered_models") or []
    if discovered:
        answers.append(
            {
                "question": "What new model looks competitive?",
                "answer": (
                    "Newly discovered models: "
                    + ", ".join(discovered[:8])
                    + ". Discovery does not make a model available for routing until it is activated."
                ),
            }
        )
    cost_delta = diff.get("estimated_cost_change_percent")
    if cost_delta is not None:
        direction = "decreased" if cost_delta < 0 else "increased"
        answers.append(
            {
                "question": "Why did expected cost change?",
                "answer": (
                    f"Estimated average routing cost {direction} by {abs(cost_delta)}% "
                    f"({diff.get('estimated_cost_before')} → {diff.get('estimated_cost_after')})."
                ),
            }
        )
    if not answers:
        answers.append(
            {
                "question": "Did anything change?",
                "answer": "Stored calibration data shows no meaningful pricing, benchmark, or routing changes.",
            }
        )
    return {
        "grounded": True,
        "active_version": (active or {}).get("version"),
        "proposed_version": (proposed or {}).get("version"),
        "answers": answers,
        "summary": (
            f"{len(diff.get('discovered_models') or [])} new models, "
            f"{len(diff.get('removed_models') or [])} removed models, "
            f"{len(diff.get('pricing_changes') or [])} pricing changes, "
            f"{len(diff.get('benchmark_changes') or [])} benchmark changes, "
            f"{len(routing_changes)} routing changes."
        ),
    }


class ModelCalibrationService:
    """One app-wide calibration state directory. See module docstring."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.history_dir = self.state_dir / "history"
        self._lock_handle = None

    @property
    def state_path(self) -> Path:
        return self.state_dir / "calibration_state.json"

    @property
    def active_path(self) -> Path:
        return self.state_dir / "calibration_active.json"

    @property
    def proposed_path(self) -> Path:
        return self.state_dir / "calibration_proposed.json"

    @property
    def catalog_override_path(self) -> Path:
        return self.state_dir / "active_catalog.json"

    @property
    def progress_path(self) -> Path:
        return self.state_dir / "refresh_progress.json"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "refresh.lock"

    def load_state(self) -> dict[str, Any]:
        # A torn/unparseable file here must degrade to "no recorded state"
        # rather than take down every read (status_report) and the one
        # in-app recovery action (a forced manual refresh) that both start
        # by loading it. See test_model_calibration_state_resilience.py.
        try:
            payload = _read_json(self.state_path)
            return payload if isinstance(payload, dict) else {}
        except CalibrationError:
            return {}

    def save_state(self, state: dict[str, Any]) -> None:
        _atomic_write_json(self.state_path, state)

    def _load_published_active(self) -> dict[str, Any] | None:
        """Read a coherent publication, including its filtered routing models."""
        try:
            catalog = _read_json(self.catalog_override_path)
            active = catalog.get("calibration") if isinstance(catalog, dict) else None
            if _is_calibration(active) and catalog.get("models") == self._router_models(active):
                for model in catalog["models"]:
                    _model_router._parse_model(model)
                return active
        except (CalibrationError, _model_router.ModelRouterConfigError):
            pass
        return None

    def load_active(self) -> dict[str, Any] | None:
        # Version and routable models share one atomic publication. The old
        # active file is retained as a compatibility/recovery copy, not an
        # independent pointer that can get ahead of the live router.
        active = self._load_published_active()
        if active:
            return active
        try:
            active = _read_json(self.active_path)
            return active if _is_calibration(active) else None
        except CalibrationError:
            return None

    def load_proposed(self) -> dict[str, Any] | None:
        try:
            payload = _read_json(self.proposed_path)
            return payload if _is_calibration(payload) else None
        except CalibrationError:
            return None

    def load_progress(self) -> dict[str, Any] | None:
        try:
            payload = _read_json(self.progress_path)
        except CalibrationError:
            return None
        return payload if isinstance(payload, dict) else None

    def _set_progress(self, stage: str, label: str) -> None:
        try:
            _atomic_write_json(
                self.progress_path,
                {"stage": stage, "label": label, "updated_at": iso_now()},
            )
        except OSError:
            # Progress is advisory; it must not turn a committed activation
            # into a failed refresh or prevent recovery from a storage error.
            pass

    def _clear_progress(self) -> None:
        try:
            self.progress_path.unlink(missing_ok=True)
        except OSError:
            pass

    def acquire_lock(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # The persistent lock inode serializes all writers. The separate JSON
        # marker is only for status: creating/deleting it cannot race to steal
        # the lock, and the OS releases ownership if a process crashes.
        handle = (self.state_dir / ".calibration.lock").open("a+b")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise CalibrationBusyError("A model data refresh is already running.") from error
        except OSError:
            handle.close()
            raise
        try:
            _atomic_write_json(self.lock_path, {"pid": os.getpid(), "created_at": time.time()})
        except OSError:
            handle.close()
            raise
        self._lock_handle = handle

    def release_lock(self) -> None:
        handle, self._lock_handle = self._lock_handle, None
        if handle is not None:
            try:
                self.lock_path.unlink(missing_ok=True)
            finally:
                handle.close()

    def list_history(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.history_dir.is_dir():
            return []
        entries: list[dict[str, Any]] = []
        for path in sorted(self.history_dir.glob("*.json"), reverse=True):
            try:
                payload = _read_json(path)
            except CalibrationError:
                continue
            if not _is_calibration(payload):
                continue
            entries.append(
                {
                    "version": payload.get("version"),
                    "created_at": payload.get("created_at"),
                    "initiated_by": payload.get("initiated_by"),
                    "source": payload.get("source"),
                    "model_counts": payload.get("model_counts"),
                    "regression_ok": payload.get("regression_ok"),
                }
            )
            if len(entries) >= max(0, limit):
                break
        return entries

    def _write_history(self, calibration: dict[str, Any]) -> None:
        version = calibration["version"]
        if not _VERSION_RE.match(version):
            raise CalibrationError(f"invalid calibration version {version!r}")
        _atomic_write_json(self.history_dir / f"{version}.json", calibration)
        self._prune_history(protected_versions={version})

    def _prune_history(self, *, protected_versions: set[str] | None = None) -> None:
        if not self.history_dir.is_dir():
            return
        paths = sorted(self.history_dir.glob("*.json"))
        protected = set(protected_versions or ())
        protected.add(self.load_state().get("previous_active_version"))
        for payload in (self.load_active(), self.load_proposed()):
            if payload:
                protected.add(payload["version"])
        excess = len(paths) - MAX_HISTORY_ENTRIES
        removable = [path for path in paths if path.stem not in protected]
        for path in removable[: max(0, excess)]:
            path.unlink(missing_ok=True)

    def _existing_versions(self) -> set[str]:
        versions = set()
        for payload in (self.load_active(), self.load_proposed()):
            if payload and payload.get("version"):
                versions.add(str(payload["version"]))
        if self.history_dir.is_dir():
            for path in self.history_dir.glob("*.json"):
                versions.add(path.stem)
        return versions

    def _find_calibration(self, version: str) -> dict[str, Any]:
        if not _VERSION_RE.match(str(version or "")):
            raise CalibrationError("invalid calibration version")
        proposed = self.load_proposed()
        if proposed and proposed.get("version") == version:
            return proposed
        active = self.load_active()
        if active and active.get("version") == version:
            return active
        historical = _read_json(self.history_dir / f"{version}.json")
        if _is_calibration(historical):
            return historical
        raise CalibrationError(f"no calibration found for version {version}")

    def _router_models(self, calibration: dict[str, Any]) -> list[dict[str, Any]]:
        models = []
        for entry in calibration.get("models") or []:
            if entry.get("status") not in ROUTABLE_STATUSES:
                continue
            models.append({field: entry.get(field) for field in ROUTER_FIELDS})
        return models

    def _write_catalog_override(self, calibration: dict[str, Any]) -> None:
        _atomic_write_json(self.catalog_override_path, {
            "models": self._router_models(calibration), "calibration": calibration,
        })

    def activate(self, version: str, *, initiated_by: str = "USER") -> dict[str, Any]:
        """Promote a proposed or historical calibration to active."""
        self.acquire_lock()
        try:
            return self._activate_locked(version, initiated_by=initiated_by)
        finally:
            self.release_lock()

    def _activate_locked(self, version: str, *, initiated_by: str) -> dict[str, Any]:
        calibration = self._find_calibration(version)
        previous = self.load_active()
        if previous:
            self._write_history(previous)
            # Upgrade a legacy catalog before touching its compatibility copy.
            if self._load_published_active() is None:
                self._write_catalog_override(previous)
        state = self.load_state()
        state["active_version"] = calibration["version"]
        if previous and previous["version"] != calibration["version"]:
            state["previous_active_version"] = previous["version"]
        if state.get("proposed_version") == calibration["version"]:
            state["proposed_version"] = None
        state["last_activated_at"] = iso_now()
        state["last_activated_by"] = _normalize_initiator(initiated_by)
        backups = {path: path.read_bytes() if path.exists() else None
                   for path in (self.active_path, self.state_path)}
        written = []
        try:
            _atomic_write_json(self.active_path, calibration)
            written.append(self.active_path)
            self.save_state(state)
            written.append(self.state_path)
            # Commit last: load_active and the router now see the same version.
            self._write_catalog_override(calibration)
        except OSError:
            for path in reversed(written):
                if backups[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write_bytes(path, backups[path])
            raise
        return calibration

    def approve_discovered_model(self, key: str, *, initiated_by: str = "USER") -> dict[str, Any]:
        """Explicitly clear a model out of DISCOVERED so the next refresh can
        assign it a normal ACTIVE/CANDIDATE status. Discovery alone never
        does this -- see `_status_for`'s docstring and issue #205's "does not
        automatically make it available for routing". Takes effect on the
        next refresh, matching how a whole calibration version's `activate`
        already works -- there is no separate instant-apply path.
        """
        self.acquire_lock()
        try:
            return self._approve_discovered_locked(key, initiated_by=initiated_by)
        finally:
            self.release_lock()

    def _approve_discovered_locked(self, key: str, *, initiated_by: str) -> dict[str, Any]:
        key = str(key or "").strip()
        if "/" not in key or _UNSAFE_TEXT_RE.search(key):
            raise CalibrationError(f"invalid model key {key!r}")
        state = self.load_state()
        approved = set(state.get("approved_models") or [])
        approved.add(key)
        state["approved_models"] = sorted(approved)
        state["last_approved_at"] = iso_now()
        state["last_approved_by"] = _normalize_initiator(initiated_by)
        self.save_state(state)
        return {"approved_models": state["approved_models"]}

    def _record_failure(
        self, state: dict[str, Any], attempted_at: str, source_status: str, error: Any, initiated_by: str
    ) -> dict[str, Any]:
        message = sanitize_text(str(error))[:MAX_ERROR_LENGTH]
        consecutive = int(state.get("consecutive_failures") or 0) + 1
        state["last_attempted_at"] = attempted_at
        state["last_attempted_status"] = "failed"
        state["last_source_status"] = source_status
        state["last_error"] = message
        state["consecutive_failures"] = consecutive
        self.save_state(state)
        result = {
            "status": "failed",
            "source_status": source_status,
            "error": message,
            "attempted_at": attempted_at,
            "initiated_by": initiated_by,
        }
        result["notification"] = notification_for(result, consecutive_failures=consecutive)
        return result

    def _build_calibration(
        self,
        raw_entries: Any,
        *,
        previous: dict[str, Any] | None,
        initiated_by: str,
        now_ts: float,
        source: dict[str, Any],
        routing_optimization: str,
        existing_versions: set[str],
        discovered_models: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        specs, warnings = _validate_and_parse(raw_entries)
        prev_by_key = _calibration_models(previous)
        approved_keys = frozenset(self.load_state().get("approved_models") or [])
        models: list[dict[str, Any]] = []
        raw_by_key = {}
        for raw in raw_entries:
            if isinstance(raw, dict) and raw.get("provider") and raw.get("model"):
                raw_by_key[f"{raw['provider']}/{raw['model']}"] = raw
        for spec in specs:
            extra = raw_by_key.get(f"{spec.provider}/{spec.model}") or {}
            entry = _calibration_model_entry(spec, extra)
            entry["key"] = f"{entry['provider']}/{entry['model']}"
            entry["status"] = _status_for(
                entry, prev_by_key, approved_keys, has_previous=previous is not None
            )
            entry["cost_efficiency"] = _cost_efficiency(entry)
            coding, coding_quality = _best_benchmark(entry, "coding_agent_index")
            agentic, _agentic_quality = _best_benchmark(entry, "deep_swe")
            reasoning, _reasoning_quality = _best_benchmark(entry, "swe_atlas_qna")
            entry["coding_score"] = coding
            entry["agentic_score"] = agentic
            entry["reasoning_score"] = reasoning
            entry["benchmark_quality"] = coding_quality if coding is not None else "HEURISTIC"
            entry["last_updated"] = entry.get("benchmark_date") or iso_now(now_ts)[:10]
            _append_history(prev_by_key.get(entry["key"]), entry, now_ts)
            models.append(entry)
        models.sort(key=lambda item: item["key"])

        counts = dict.fromkeys(
            (STATUS_ACTIVE, STATUS_CANDIDATE, STATUS_DISCOVERED, STATUS_DEPRECATED, STATUS_DISABLED), 0
        )
        for entry in models:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        discovered = list(discovered_models or [])
        for entry in discovered:
            entry["key"] = f"{entry['provider']}/{entry['model']}"
            entry["last_updated"] = iso_now(now_ts)
            _append_history(prev_by_key.get(entry["key"]), entry, now_ts)
        counts[STATUS_DISCOVERED] += len(discovered)

        routable_specs = [
            spec
            for spec in specs
            if any(
                model["provider"] == spec.provider
                and model["model"] == spec.model
                and model["status"] in ROUTABLE_STATUSES
                for model in models
            )
        ]
        return {
            "version": _version_for(now_ts, {v for v in existing_versions if v}),
            "created_at": iso_now(now_ts),
            "initiated_by": initiated_by,
            "algorithm_version": ALGORITHM_VERSION,
            "source": source,
            "routing_optimization": routing_optimization,
            "routing_mode": routing_mode_label(routing_optimization),
            "models": models,
            "discovered_models": discovered,
            "model_counts": counts,
            "routing": recalculate_routing(routable_specs, routing_optimization=routing_optimization),
            "weights": _weights_summary(routing_optimization),
            "token_cost_assumptions": TOKEN_COST_ASSUMPTIONS,
            "source_warnings": [sanitize_text(warning)[:400] for warning in warnings],
            "regression_ok": True,
        }

    def ensure_bootstrap(self, *, routing_optimization: str = "cost", now: float | None = None) -> dict[str, Any]:
        """Load the last known-good calibration, or seed it from the bundled catalog."""
        active = self._load_published_active()
        if active:
            return active
        self.acquire_lock()
        try:
            return self._bootstrap_locked(routing_optimization=routing_optimization, now=now)
        finally:
            self.release_lock()

    def _bootstrap_locked(self, *, routing_optimization: str, now: float | None) -> dict[str, Any]:
        published = self._load_published_active()
        if published:
            return published
        active = self.load_active()
        if active:
            self._write_catalog_override(active)
            return active
        now_ts = now if now is not None else time.time()
        bootstrap = self._build_calibration(
            fetch_local_source(),
            previous=None,
            initiated_by="STARTUP",
            now_ts=now_ts,
            source={"kind": "local", "url": None, "status": "local"},
            routing_optimization=routing_optimization,
            existing_versions=self._existing_versions(),
        )
        self._write_history(bootstrap)
        self._activate_locked(bootstrap["version"], initiated_by="STARTUP")
        return bootstrap

    def _should_skip_interval(
        self,
        state: dict[str, Any],
        *,
        now_ts: float,
        min_interval_hours: float,
        force: bool,
    ) -> dict[str, Any] | None:
        if force:
            return None
        last_attempt = state.get("last_attempted_at")
        last_status = state.get("last_attempted_status")
        if last_status == "failed" and last_attempt:
            try:
                failed_hours = (now_ts - _parse_iso(last_attempt)) / 3600.0
            except ValueError:
                failed_hours = None
            if failed_hours is not None and failed_hours < FAILED_RETRY_BACKOFF_HOURS:
                return {
                    "status": "skipped_interval",
                    "attempted_at": iso_now(now_ts),
                    "last_attempted_at": last_attempt,
                    "reason": "retry_backoff",
                }
        last_success = state.get("last_successful_at")
        if last_success:
            try:
                elapsed_hours = (now_ts - _parse_iso(last_success)) / 3600.0
            except ValueError:
                elapsed_hours = None
            if elapsed_hours is not None and elapsed_hours < max(0.0, float(min_interval_hours)):
                return {
                    "status": "skipped_interval",
                    "attempted_at": iso_now(now_ts),
                    "last_attempted_at": last_attempt,
                    "last_successful_at": last_success,
                }
        return None

    def _fetch_entries(
        self,
        *,
        source: str,
        source_url: str | None,
        fetch_fn: Callable[[], Any] | None,
        previous: dict[str, Any] | None = None,
    ) -> tuple[list[Any], list[dict[str, Any]], dict[str, Any]]:
        discovered: list[dict[str, Any]] = []
        if fetch_fn is not None:
            return fetch_fn(), discovered, {"kind": "custom", "url": source_url, "status": "ok"}
        kind = source if source in ALLOWED_SOURCES else "local"
        if kind == "local":
            return fetch_local_source(), discovered, {"kind": "local", "url": None, "status": "local"}
        local_entries = fetch_local_source()
        try:
            overlay, meta = _sources.fetch_source(kind, source_url or "")
        except _sources.SourceError as error:
            raise CalibrationSourceError(str(error)) from error
        if kind == "models_dev":
            overlay = list(overlay) + self._benchmark_overlay(meta)
        merged, discovered = merge_overlay(local_entries, overlay, previous=previous)
        return merged, discovered, meta

    @staticmethod
    def _benchmark_overlay(meta: dict[str, Any]) -> list[dict[str, Any]]:
        """Artificial Analysis benchmarks, when an API key is configured.

        models.dev stays the authority for prices and lifecycle, so the rows
        taken from Artificial Analysis are stripped to benchmark, speed and
        latency fields; two sources can then never contradict each other on a
        price. Any failure here is recorded in ``meta["warnings"]`` and the
        models.dev data is used on its own — benchmarks are an enhancement,
        never a reason for the whole refresh to fail.
        """
        if not os.environ.get(_sources.ARTIFICIAL_ANALYSIS_KEY_ENV):
            return []
        try:
            rows, _ = _sources.fetch_source("artificial_analysis")
        except _sources.SourceError as error:
            meta.setdefault("warnings", []).append(
                f"Artificial Analysis benchmarks unavailable: {sanitize_text(str(error))}"
            )
            return []
        keep = ("provider", "model", "source_id", "evaluations", "speed", "latency_seconds")
        meta["benchmarks"] = "artificial_analysis"
        return [{key: row[key] for key in keep if key in row} for row in rows]

    def refresh(
        self,
        *,
        source: str = "local",
        source_url: str | None = None,
        force: bool = False,
        run_calibration: bool = True,
        run_simulation_flag: bool = True,
        activation_policy: str = "manual",
        initiated_by: str = "USER",
        min_interval_hours: float = DEFAULT_MIN_REFRESH_INTERVAL_HOURS,
        routing_optimization: str = "cost",
        fetch_fn: Callable[[], Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """One calibration refresh shared by manual, startup, scheduled, and AI callers."""
        now_ts = now if now is not None else time.time()
        initiated_by = _normalize_initiator(initiated_by)
        try:
            self.acquire_lock()
        except CalibrationBusyError:
            return {
                "status": "already_running",
                "initiated_by": initiated_by,
                "attempted_at": iso_now(now_ts),
            }
        state_before = self.load_state()
        try:
            return self._refresh_locked(
                source=source,
                source_url=source_url,
                force=force,
                run_calibration=run_calibration,
                run_simulation_flag=run_simulation_flag,
                activation_policy=activation_policy,
                initiated_by=initiated_by,
                min_interval_hours=min_interval_hours,
                routing_optimization=routing_optimization,
                fetch_fn=fetch_fn,
                now_ts=now_ts,
            )
        except (OSError, CalibrationError) as error:
            state = self.load_state()
            state["last_successful_at"] = state_before.get("last_successful_at")
            return self._record_failure(state, iso_now(now_ts), "error", error, initiated_by)
        finally:
            try:
                self._clear_progress()
            finally:
                self.release_lock()

    def _refresh_locked(
        self,
        *,
        source: str,
        source_url: str | None,
        force: bool,
        run_calibration: bool,
        run_simulation_flag: bool,
        activation_policy: str,
        initiated_by: str,
        min_interval_hours: float,
        routing_optimization: str,
        fetch_fn: Callable[[], Any] | None,
        now_ts: float,
    ) -> dict[str, Any]:
        # External feeds are overlays of the offline catalog. Publish that
        # baseline before any network work, using the lock already held by
        # refresh. Reload state afterwards so its activation metadata cannot
        # be overwritten by a stale pre-bootstrap snapshot. Local/complete
        # catalogs can still supply their own initial offline baseline.
        if source in ("models_dev", "artificial_analysis", "json"):
            self._bootstrap_locked(routing_optimization=routing_optimization, now=now_ts)
        state = self.load_state()
        skipped = self._should_skip_interval(
            state, now_ts=now_ts, min_interval_hours=min_interval_hours, force=force
        )
        if skipped:
            skipped["initiated_by"] = initiated_by
            skipped["notification"] = notification_for(skipped)
            return skipped

        # Always compare with the last activated calibration. Bootstrap only
        # seeds an absent baseline or repairs its damaged routing publication.
        try:
            previous = self.load_active()
        except CalibrationError as error:
            return self._record_failure(state, iso_now(now_ts), "error", error, initiated_by)

        # A pending proposal is already-observed data, even though routing
        # still uses the active version. Repeated checks must not mint the
        # same proposal or rediscover its models. Tie it to its active base so
        # a rollback cannot reuse an unrelated proposal as the baseline.
        proposed = self.load_proposed()
        comparison = previous
        if (
            proposed
            and state.get("proposed_version") == proposed.get("version")
            and proposed.get("base_version") == (previous or {}).get("version")
        ):
            comparison = proposed

        attempted_at = iso_now(now_ts)
        self._set_progress(*PROGRESS_STAGES[0])
        try:
            raw_entries, discovered, source_meta = self._fetch_entries(
                source=source, source_url=source_url, fetch_fn=fetch_fn, previous=comparison
            )
        except CalibrationSourceError as error:
            return self._record_failure(state, attempted_at, "unavailable", error, initiated_by)
        except CalibrationValidationError as error:
            return self._record_failure(state, attempted_at, "error", error, initiated_by)

        self._set_progress(*PROGRESS_STAGES[1])
        try:
            new_calibration = self._build_calibration(
                raw_entries,
                previous=comparison,
                initiated_by=initiated_by,
                now_ts=now_ts,
                source=source_meta,
                routing_optimization=routing_optimization,
                existing_versions=self._existing_versions(),
                discovered_models=discovered,
            )
        except CalibrationValidationError as error:
            return self._record_failure(state, attempted_at, "error", error, initiated_by)

        self._set_progress(*PROGRESS_STAGES[2])
        diff = diff_calibrations(previous, new_calibration)
        observed_diff = diff_calibrations(comparison, new_calibration)
        diff["newly_discovered_models"] = observed_diff["newly_discovered_models"]
        simulation = None
        if run_calibration and run_simulation_flag:
            self._set_progress(*PROGRESS_STAGES[3])
            self._set_progress(*PROGRESS_STAGES[4])
            simulation = run_simulation(previous, new_calibration, diff)
            new_calibration["regression_ok"] = simulation["regression_ok"]
        else:
            new_calibration["regression_ok"] = True

        state["last_attempted_at"] = attempted_at
        state["last_successful_at"] = attempted_at
        state["last_source_status"] = source_meta.get("status")
        state["last_error"] = None
        state["last_diff"] = diff
        state["consecutive_failures"] = 0

        if previous is None:
            # Nothing was active yet, so there is no production calibration
            # to protect from a silent replacement: this successfully
            # fetched and validated data becomes the active baseline
            # immediately, regardless of activation_policy. Persist refresh
            # metadata before activation so publication is the final write.
            self._write_history(new_calibration)
            state["last_attempted_status"] = "changed"
            state["last_refresh_calibration_version"] = new_calibration["version"]
            self.save_state(state)
            self._activate_locked(new_calibration["version"], initiated_by=initiated_by)
            self._set_progress(*PROGRESS_STAGES[5])
            result = {
                "status": "changed",
                "initiated_by": initiated_by,
                "source_status": source_meta.get("status"),
                "source_warnings": list(source_meta.get("warnings") or []),
                "attempted_at": attempted_at,
                "diff": diff,
                "simulation": simulation,
                "calibration_version": new_calibration["version"],
                "activated": True,
                "models_checked": len(new_calibration["models"]),
            }
            result["notification"] = notification_for(result)
            return result

        if not run_calibration or not observed_diff["has_meaningful_change"] or not diff["has_meaningful_change"]:
            if run_calibration and not diff["has_meaningful_change"]:
                # The source returned to the active data; an older proposal
                # is no longer an update to offer. Its history remains intact.
                state["proposed_version"] = None
                self.proposed_path.unlink(missing_ok=True)
            state["last_attempted_status"] = "no_change"
            self.save_state(state)
            self._set_progress(*PROGRESS_STAGES[5])
            result = {
                "status": "no_change",
                "initiated_by": initiated_by,
                "source_status": source_meta.get("status"),
                "source_warnings": list(source_meta.get("warnings") or []),
                "attempted_at": attempted_at,
                "diff": diff,
                "simulation": simulation,
                "models_checked": len(new_calibration["models"]),
            }
            result["notification"] = notification_for(result)
            return result

        state["last_attempted_status"] = "changed"
        state["last_refresh_calibration_version"] = new_calibration["version"]
        new_calibration["base_version"] = (previous or {}).get("version")
        new_calibration["diff"] = diff
        new_calibration["simulation"] = simulation
        self._write_history(new_calibration)
        _atomic_write_json(self.proposed_path, new_calibration)
        state["proposed_version"] = new_calibration["version"]
        self.save_state(state)

        activated = False
        if activation_policy == "auto" and simulation is not None and simulation["regression_ok"]:
            self._activate_locked(new_calibration["version"], initiated_by=initiated_by)
            activated = True

        self._set_progress(*PROGRESS_STAGES[5])
        result = {
            "status": "changed",
            "initiated_by": initiated_by,
            "source_status": source_meta.get("status"),
                "source_warnings": list(source_meta.get("warnings") or []),
            "attempted_at": attempted_at,
            "diff": diff,
            "simulation": simulation,
            "calibration_version": new_calibration["version"],
            "activated": activated,
            "models_checked": len(new_calibration["models"]),
        }
        result["notification"] = notification_for(result)
        return result

    def analyze(self) -> dict[str, Any]:
        """Explain the latest stored diff using only stored calibration data."""
        state = self.load_state()
        proposed = self.load_proposed()
        pending = proposed if proposed and state.get("proposed_version") == proposed.get("version") else None
        return explain_update(self.load_active(), pending, (pending or {}).get("diff") or state.get("last_diff"))

    def status_report(self, *, routing_optimization: str = "cost") -> dict[str, Any]:
        try:
            self.ensure_bootstrap(routing_optimization=routing_optimization)
        except (CalibrationError, OSError):
            pass
        state = self.load_state()
        active = self.load_active()
        proposed = self.load_proposed()
        # Metadata/progress are advisory. Only readable calibration documents
        # can identify the live version or offer an update for activation.
        active_version = (active or {}).get("version")
        if not (
            proposed
            and proposed.get("version") == state.get("proposed_version")
            and proposed.get("version") != active_version
            and proposed.get("base_version") == active_version
        ):
            proposed = None
        proposed_version = (proposed or {}).get("version")
        progress = self.load_progress()
        refresh_running = False
        if self.lock_path.exists():
            try:
                payload = json.loads(self.lock_path.read_text(encoding="utf-8"))
                pid = int(payload.get("pid") or 0)
                created = float(payload.get("created_at") or 0)
                refresh_running = _pid_alive(pid) and time.time() - created < STALE_REFRESH_SECONDS
            except (OSError, ValueError, json.JSONDecodeError, TypeError):
                refresh_running = False
        counts = (active or {}).get("model_counts") or {}
        review_counts = (proposed or {}).get("model_counts") if proposed_version else counts
        return {
            "active_version": active_version,
            "proposed_version": proposed_version,
            "algorithm_version": (active or {}).get("algorithm_version", ALGORITHM_VERSION),
            "last_successful_refresh_at": state.get("last_successful_at"),
            "last_attempted_refresh_at": state.get("last_attempted_at"),
            "last_attempted_status": state.get("last_attempted_status"),
            "last_error": state.get("last_error"),
            "source_status": state.get("last_source_status"),
            "healthy": bool(active) and bool(active.get("regression_ok", True)),
            "has_newer_proposed": bool(proposed_version) and proposed_version != active_version,
            "active_calibration": active,
            "proposed_calibration": proposed,
            "last_diff": state.get("last_diff"),
            "last_refresh_calibration_version": state.get("last_refresh_calibration_version"),
            "last_activated_at": state.get("last_activated_at"),
            "last_activated_by": state.get("last_activated_by"),
            "history": self.list_history(10),
            "progress": progress,
            "refresh_running": refresh_running,
            "active_model_count": int(counts.get(STATUS_ACTIVE) or 0) + int(counts.get(STATUS_CANDIDATE) or 0),
            "discovered_model_count": int((review_counts or {}).get(STATUS_DISCOVERED) or 0),
            "routing_mode": (active or {}).get("routing_mode") or routing_mode_label(routing_optimization),
            "weights": (active or {}).get("weights") or _weights_summary(routing_optimization),
            "token_cost_assumptions": TOKEN_COST_ASSUMPTIONS,
            "catalog_override_path": str(self.catalog_override_path) if self.catalog_override_path.is_file() else None,
        }


def _normalize_initiator(value: str) -> str:
    text = str(value or "").strip().upper()[:32]
    return text if text in ALLOWED_INITIATORS else "USER"


def build_calibration_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    sub = parser.add_subparsers(dest="action", required=True)

    status = sub.add_parser("status", help="Print the current calibration status as JSON.")
    status.add_argument("--routing-optimization", default="cost", choices=["best", "cost"])

    refresh = sub.add_parser("refresh", help="Run one calibration refresh.")
    refresh.add_argument("--force", action="store_true")
    refresh.add_argument("--initiated-by", default="USER", choices=list(ALLOWED_INITIATORS))
    refresh.add_argument("--source", default="local", choices=list(ALLOWED_SOURCES))
    refresh.add_argument("--source-url", default=None)
    refresh.add_argument("--activation-policy", default="manual", choices=["manual", "auto"])
    refresh.add_argument("--min-interval-hours", type=float, default=DEFAULT_MIN_REFRESH_INTERVAL_HOURS)
    refresh.add_argument("--routing-optimization", default="cost", choices=["best", "cost"])
    refresh.add_argument("--no-simulation", action="store_true")
    refresh.add_argument("--no-calibration", action="store_true")

    activate = sub.add_parser("activate", help="Promote a calibration version to active.")
    activate.add_argument("version")
    activate.add_argument("--initiated-by", default="USER")

    approve = sub.add_parser(
        "approve", help="Approve a DISCOVERED model so the next refresh can make it routable."
    )
    approve.add_argument("key")
    approve.add_argument("--initiated-by", default="USER")

    history = sub.add_parser("history", help="List recent calibration versions.")
    history.add_argument("--limit", type=int, default=20)

    sub.add_parser("analyze", help="Explain the latest stored calibration diff.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_calibration_parser()
    args = parser.parse_args(argv)
    service = ModelCalibrationService(Path(args.state_dir))
    try:
        if args.action == "status":
            result: Any = service.status_report(routing_optimization=args.routing_optimization)
        elif args.action == "refresh":
            result = service.refresh(
                source=args.source,
                source_url=args.source_url,
                force=args.force,
                run_calibration=not args.no_calibration,
                run_simulation_flag=not args.no_simulation,
                activation_policy=args.activation_policy,
                initiated_by=args.initiated_by,
                min_interval_hours=args.min_interval_hours,
                routing_optimization=args.routing_optimization,
            )
        elif args.action == "activate":
            result = service.activate(args.version, initiated_by=args.initiated_by)
        elif args.action == "approve":
            result = service.approve_discovered_model(args.key, initiated_by=args.initiated_by)
        elif args.action == "analyze":
            result = service.analyze()
        else:
            result = {"history": service.list_history(args.limit)}
    except (CalibrationError, OSError) as error:
        print(json.dumps({"error": sanitize_text(str(error))}))
        return 1
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
