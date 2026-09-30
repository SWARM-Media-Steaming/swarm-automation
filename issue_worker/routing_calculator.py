"""Routing calculator: what would Dynamic Model Routing pick for these inputs?

The desktop app's "Try the router" dialog calls this. It takes the values the
router itself works from — task type, complexity 1-10, risk, and the AI tool —
and returns the model and reasoning effort the worker would run, by calling the
same functions the worker calls (``dynamic_router.scored_tier``,
``candidate_catalog``, ``latest_release``) over the same catalog: the active
calibration when one exists, plus the models the provider CLIs report. Nothing
here is a second implementation of routing, so the answer cannot drift from the
real one.

What it deliberately leaves out: the AI router's own reading of an issue (its
grade, task type and complexity are *inputs* here) and Jev's adjustments, which
need a live issue. The optional "suggested model" input reproduces the one place
the AI router's opinion changes the outcome — a valid suggestion is honoured.

Usage::

    routing_calculator.py defaults [--providers claude,codex]
    routing_calculator.py describe [--providers claude,codex]
    routing_calculator.py simulate --input JSON [--providers ...]

Both accept ``--available-models JSON`` and ``--allow-usage-credit-models``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import available_models
import dynamic_router
import model_router

PROVIDER_NAMES = {"claude": "Claude", "codex": "Codex", "grok": "Grok"}
RISKS = ("low", "medium", "high")
ALTERNATIVES_SHOWN = 6

_TASK_TYPE_HINTS = {
    "mechanical_edit": "Renames, formatting, search-and-replace style changes",
    "documentation": "Docs, comments, READMEs",
    "test_generation": "Writing or extending tests",
    "simple_bug_fix": "A small, well-understood bug",
    "feature": "A normal feature or enhancement",
    "complex_feature": "A feature that spans many parts of the system",
    "refactor": "Restructuring code without changing behaviour",
    "large_refactor": "A refactor across many files or modules",
    "debugging": "Finding and fixing a defect",
    "deep_debugging": "A hard, elusive, or cross-system defect",
    "architecture": "Design decisions and system structure",
    "planning": "Breaking work down, roadmaps",
    "code_review": "Reviewing changes",
    "security_analysis": "Finding or assessing vulnerabilities",
    "performance_analysis": "Profiling and speed-ups",
    "infrastructure": "Build, deploy, cloud configuration",
    "devops": "CI/CD and operational tooling",
    "repository_analysis": "Understanding an unfamiliar codebase",
    "multi_repository": "Work that touches several repositories",
    "research": "Investigating options or unknowns",
    "general_reasoning": "Anything that does not fit the rest",
}


class CalculatorError(ValueError):
    """An input the calculator cannot use; the message is shown to the user."""


def _label(value: str) -> str:
    return str(value).replace("_", " ").capitalize()


def _providers(keys: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for key in keys:
        key = str(key).strip().lower()
        if key in PROVIDER_NAMES and key not in seen:
            seen.append(key)
    return seen or list(PROVIDER_NAMES)


def _catalog_info() -> dict[str, Any]:
    """Which catalog routing is reading, in words a person can use."""
    path = dynamic_router.active_calibration_catalog_path()
    if path is not None:
        try:
            calibration = json.loads(Path(path).read_text(encoding="utf-8")).get("calibration") or {}
            version = str(calibration.get("version") or "")
            if version:
                return {
                    "source": "calibration",
                    "label": f"Active calibration {version}",
                    "version": version,
                }
        except (OSError, ValueError):
            pass
    return {"source": "bundled", "label": "Bundled model catalog", "version": ""}


def defaults(*, providers: Sequence[str], allow_usage_credit_models: bool) -> dict[str, Any]:
    """Starting worker and router settings per AI tool, from the live catalog."""
    return {
        "defaults": {
            key: dynamic_router.suggested_defaults(
                key, allow_usage_credit_models=allow_usage_credit_models
            )
            for key in _providers(providers)
        }
    }


def describe(*, providers: Sequence[str]) -> dict[str, Any]:
    """Everything the dialog needs to draw its controls."""
    rules = model_router.load_routing_rules()
    bands = [
        {
            "level": band.level,
            "label": _label(band.level),
            "from": band.ai_grade_range[0],
            "to": band.ai_grade_range[1],
        }
        for band in rules.complexity_bands
    ]
    return {
        "taskTypes": [
            {"value": value, "label": _label(value), "hint": _TASK_TYPE_HINTS.get(value, "")}
            for value in model_router.TASK_TYPES
        ],
        "complexity": {"min": 1, "max": 10, "bands": bands},
        "risks": [
            {"value": "low", "label": "Low", "hint": "Easy to undo, little depends on it"},
            {"value": "medium", "label": "Medium", "hint": "Normal production code"},
            {"value": "high", "label": "High", "hint": "Security, data, money, or hard to reverse; raises the quality bar"},
        ],
        "providers": [{"key": key, "name": PROVIDER_NAMES[key]} for key in _providers(providers)],
        "efforts": [{"value": key, "label": label} for key, label in dynamic_router.EFFORT_LABELS.items()],
        "catalog": _catalog_info(),
    }


def _band_label(complexity: int) -> str:
    for band in model_router.load_routing_rules().complexity_bands:
        if band.ai_grade_range[0] <= complexity <= band.ai_grade_range[1]:
            return _label(band.level)
    return ""


def _read_inputs(raw: Mapping[str, Any]) -> dict[str, Any]:
    try:
        complexity = int(raw.get("complexity"))
    except (TypeError, ValueError):
        raise CalculatorError("Complexity must be a whole number from 1 to 10.") from None
    if not 1 <= complexity <= 10:
        raise CalculatorError("Complexity must be a whole number from 1 to 10.")
    risk = str(raw.get("risk") or "medium").strip().lower()
    if risk not in RISKS:
        raise CalculatorError("Risk must be low, medium, or high.")
    task_type = dynamic_router._normalize_task_type(str(raw.get("taskType") or ""))
    return {
        "complexity": complexity,
        "risk": risk,
        "task_type": task_type,
        "provider": str(raw.get("provider") or "").strip().lower(),
        "suggested_model": str(raw.get("suggestedModel") or "").strip(),
        "suggested_effort": str(raw.get("suggestedEffort") or "").strip(),
    }


def _decide(
    key: str,
    inputs: Mapping[str, Any],
    tiers: Mapping[str, Sequence[Any]] | None,
    *,
    allow_usage_credit_models: bool,
) -> dict[str, Any]:
    """The worker's model-and-effort decision for one AI tool."""
    name = PROVIDER_NAMES[key]
    # Reference tiers are derived from the live catalog; a caller-supplied table
    # is only for tests that pin one.
    reference = tuple((tiers or {}).get(key) or dynamic_router.derived_routing_tiers(
        key, allow_usage_credit_models=allow_usage_credit_models))
    candidate = dynamic_router.RouterCandidate(
        key=key, name=name, tiers=reference, strengths="",
        usage_remaining=None, excluded_models=(),
    )
    complexity, risk, task_type = inputs["complexity"], inputs["risk"], inputs["task_type"]
    steps = [
        f"Complexity {complexity}/10 is the {_band_label(complexity)} band"
        + (", and high risk raises the quality bar one notch." if risk == "high" else "."),
    ]
    try:
        tier_model, tier_effort, tier_explanation, decision = dynamic_router.scored_tier(
            candidate, complexity, task_type, risk,
            routing_optimization="cost",
            allow_usage_credit_models=allow_usage_credit_models,
        )
    except dynamic_router.RouterError as error:
        return {"provider": key, "providerName": name, "error": str(error)}

    offered = {
        entry.model
        for entry in dynamic_router.candidate_catalog(
            candidate, allow_usage_credit_models=allow_usage_credit_models
        )
    }
    if decision is not None:
        steps.append(
            f"{len(decision.candidates)} model and effort combinations were scored on "
            "expected success, capability, and cost; the lowest-cost one that is capable enough wins."
        )
    else:
        steps.append("The band's reference tier decided, because scoring was not available.")

    model, effort, source = tier_model, tier_effort, "scored" if decision is not None else "tier"
    explanation = tier_explanation
    suggested = inputs["suggested_model"]
    if suggested and suggested in offered:
        model = suggested
        effort = dynamic_router._normalize_effort(inputs["suggested_effort"]) or tier_effort
        source = "suggested"
        explanation = dynamic_router.describe_model_choice(
            candidate, model, effort, complexity, optimization="cost"
        )
        steps.append(f"The suggested model {suggested} is one {name} can run, so it is used as suggested.")
    elif suggested:
        steps.append(
            f"The suggested model {suggested} is not one {name} can run here, so the scored pick stands."
        )

    upgrade = dynamic_router.latest_release(
        key, model, effort, allow_usage_credit_models=allow_usage_credit_models
    )
    upgrade_info = None
    if upgrade is not None:
        upgrade_info = {"from": upgrade.previous, "to": upgrade.model, "reason": upgrade.reason}
        steps.append(
            f"{upgrade.previous} was moved up to {upgrade.model}: {upgrade.reason}."
        )
        model = upgrade.model

    alternatives = []
    if decision is not None:
        for row in decision.candidates[:ALTERNATIVES_SHOWN]:
            alternatives.append({
                "model": row.get("model"),
                "modelLabel": dynamic_router.display_model_name(str(row.get("model") or "")),
                "effort": row.get("effort"),
                "effortLabel": dynamic_router.display_effort(str(row.get("effort") or "")),
                "score": row.get("score"),
                "expectedSuccess": row.get("expected_success"),
                "estimatedCost": row.get("estimated_cost"),
                "relativeCost": row.get("relative_cost"),
            })
    return {
        "provider": key,
        "providerName": name,
        "model": model,
        "modelLabel": dynamic_router.display_model_name(model),
        "effort": effort,
        "effortLabel": dynamic_router.display_effort(effort),
        "source": source,
        "explanation": explanation,
        "steps": steps,
        "upgrade": upgrade_info,
        "alternatives": alternatives,
        "band": _band_label(complexity),
    }


def simulate(
    raw_inputs: Mapping[str, Any],
    *,
    providers: Sequence[str],
    tiers: Mapping[str, Sequence[Any]] | None = None,
    allow_usage_credit_models: bool,
) -> dict[str, Any]:
    """Decide for the chosen AI tool, or for every enabled tool when none is chosen."""
    inputs = _read_inputs(raw_inputs)
    keys = _providers(providers)
    if inputs["provider"]:
        if inputs["provider"] not in keys:
            raise CalculatorError(f"{inputs['provider']} is not an enabled AI tool.")
        keys = [inputs["provider"]]
    return {
        "inputs": {
            "complexity": inputs["complexity"],
            "risk": inputs["risk"],
            "taskType": inputs["task_type"],
            "provider": inputs["provider"],
            "suggestedModel": inputs["suggested_model"],
            "suggestedEffort": inputs["suggested_effort"],
        },
        "results": [
            _decide(key, inputs, tiers, allow_usage_credit_models=allow_usage_credit_models)
            for key in keys
        ],
        "catalog": _catalog_info(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("describe", "simulate", "defaults"):
        command = sub.add_parser(name)
        command.add_argument("--providers", default="", help="Comma-separated enabled AI tool keys.")
        command.add_argument("--tiers", default="", help="Ignored. Reference tiers are computed from the live catalog.")
        command.add_argument("--available-models", default="", help="JSON of models each provider CLI reports.")
        command.add_argument("--allow-usage-credit-models", action="store_true")
        if name == "simulate":
            command.add_argument("--input", required=True, help="JSON object of calculator inputs.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    available_models.configure(
        args.available_models, allow_usage_credit_models=bool(args.allow_usage_credit_models)
    )
    providers = [part for part in args.providers.split(",") if part.strip()]
    try:
        if args.action == "describe":
            result = describe(providers=providers)
        elif args.action == "defaults":
            result = defaults(providers=providers, allow_usage_credit_models=bool(args.allow_usage_credit_models))
        else:
            try:
                raw = json.loads(args.input)
            except ValueError:
                raise CalculatorError("The inputs were not valid JSON.") from None
            if not isinstance(raw, dict):
                raise CalculatorError("The inputs must be a JSON object.")
            result = simulate(
                raw,
                providers=providers,
                allow_usage_credit_models=bool(args.allow_usage_credit_models),
            )
    except (CalculatorError, ValueError, dynamic_router.RouterError, model_router.ModelRouterError) as error:
        print(json.dumps({"error": str(error)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
