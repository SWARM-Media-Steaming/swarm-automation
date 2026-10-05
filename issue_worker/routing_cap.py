"""Per-repository, per-provider ceiling on what dynamic routing may select.

The router still runs in full and its pick is recorded untouched. The cap is a
final clamp: when the pick costs more than the cap's model at the cap's effort
the cap's exact pair is used instead, otherwise the router's pick stands.

Cost is the router's own estimate (``model_router.estimated_dollar_cost``, which
folds the effort in), so there is no hand-written rank table here. A cap only
ever compares against candidates of its own provider. Manual selections and a
run with Dynamic Model Routing off never reach this module.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import available_models as _available_models
import dynamic_router as _dynamic_router
import model_router as _model_router

PROVIDER_KEYS = ("claude", "codex", "grok")


def parse_caps(raw: Any) -> dict[str, dict[str, str]]:
    """``{provider: {"model", "effort"}}`` from the ``--routing-caps`` JSON.

    Anything malformed, empty or for an unknown provider means "uncapped" for
    that provider; a bad value never stops a run.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return {}
    if not isinstance(raw, Mapping):
        return {}
    caps: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        provider = str(key).strip().lower()
        if provider not in PROVIDER_KEYS or not isinstance(value, Mapping):
            continue
        model = str(value.get("model") or "").strip()
        effort = str(value.get("effort") or "").strip().lower()
        if model and effort:
            caps[provider] = {"model": model, "effort": effort}
    return caps


def _spec(provider: str, model: str):
    try:
        catalog = _dynamic_router._routing_catalog()
    except _model_router.ModelRouterConfigError:
        return None
    return next((m for m in catalog if m.agent == provider and model in (m.model, m.model_id)), None)


def route_cost(provider: str, model: str, effort: str) -> float | None:
    """Estimated cost of one provider/model/effort route, or None when unknown."""
    profile = _dynamic_router.model_route_profile(provider, model, effort)
    return None if profile is None else profile[1]


def resolve_cap(
    provider: str, cap: Mapping[str, str] | None, *, allow_usage_credit_models: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """``(usable cap, note)``. A retired model is repaired, an unpriced one rejected."""
    if not cap:
        return None, ""
    model, effort = str(cap["model"]), str(cap["effort"])
    note = ""
    repaired = _available_models.replace_blacklisted(model)
    if repaired and repaired != model:
        note = f"Routing cap model {model} is retired; using its successor {repaired}."
        model = repaired
    spec = _spec(provider, model)
    if spec is None or not _model_router.is_priced(spec):
        return None, f"Routing cap {provider} {model} ignored: the model is unknown or has no price."
    if not allow_usage_credit_models and _dynamic_router.requires_usage_credits(spec.model):
        return None, f"Routing cap {provider} {model} ignored: it requires usage credits, which are off."
    if effort not in spec.supported_efforts:
        return None, f"Routing cap {provider} {model} ignored: effort {effort} is not supported."
    cost = _model_router.estimated_dollar_cost(spec, effort)
    if cost is None:
        return None, f"Routing cap {provider} {model} ignored: its cost cannot be estimated."
    return {"provider": provider, "model": spec.model, "effort": effort, "cost": cost,
            "repaired_from": str(cap["model"]) if model != str(cap["model"]) else ""}, note


def _below_floor(provider: str, model: str, effort: str, requirements: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The capability floor the cap cannot meet, with the expected-success gap."""
    try:
        floor = float((requirements or {}).get("recommended_capability_floor") or 0)
    except (TypeError, ValueError):
        return None
    spec = _spec(provider, model)
    if spec is None or floor <= 0:
        return None
    required = int(round(floor))
    # The router's own sufficiency measure, at the floor and at the cap's capability.
    success = _model_router._expected_success(spec, required)
    if spec.relative_capability >= required:
        return None
    return {"floor": required, "capability": spec.relative_capability,
            "expected_success": round(success, 3),
            "expected_success_gap": round(1.0 - success, 3)}


def clamp(
    provider: str, model: str, effort: str, caps: Mapping[str, Mapping[str, str]],
    *, allow_usage_credit_models: bool = False, requirements: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The cap record for this pick, or None when the provider is uncapped.

    ``applied`` says whether the cap's exact pair replaces the pick; the pick is
    kept in ``router_*`` and the outcome in ``final_*``.
    """
    key = str(provider).strip().lower()
    cap, note = resolve_cap(key, caps.get(key), allow_usage_credit_models=allow_usage_credit_models)
    if cap is None:
        return {"provider": key, "applied": False, "invalid": True, "note": note,
                "router_model": model, "router_effort": effort,
                "final_model": model, "final_effort": effort} if note else None
    router_cost = route_cost(key, model, effort)
    over = router_cost is None or router_cost > cap["cost"]
    record: dict[str, Any] = {
        "provider": key, "cap_model": cap["model"], "cap_effort": cap["effort"], "cap_cost": cap["cost"],
        "router_model": model, "router_effort": effort, "router_cost": router_cost,
        "applied": bool(over and (model, effort) != (cap["model"], cap["effort"])),
        "note": note,
    }
    if cap["repaired_from"]:
        record["repaired_from"] = cap["repaired_from"]
    if record["applied"]:
        record.update(final_model=cap["model"], final_effort=cap["effort"], final_cost=cap["cost"])
        gap = _below_floor(key, cap["model"], cap["effort"], requirements)
        if gap:
            record["below_capability_floor"] = gap
    else:
        record.update(final_model=model, final_effort=effort, final_cost=router_cost)
    return record


def _name(model: str) -> str:
    return _dynamic_router.display_model_name(model)


def describe(record: Mapping[str, Any] | None) -> str:
    """The Cap line for ``## Routing Decision`` (empty when the provider is uncapped)."""
    if not record:
        return ""
    if record.get("invalid"):
        return f"**Cap:** not applied — {record.get('note')}"
    cap = f"{_name(record['cap_model'])} {record['cap_effort']}"
    picked = f"{_name(record['router_model'])} {record['router_effort']}"
    if record.get("applied"):
        line = (f"**Cap:** Router selected {picked}; capped at {cap} (repository cap) → using "
                f"{_name(record['final_model'])} {record['final_effort']}")
        gap = record.get("below_capability_floor")
        if gap:
            line += (f"\n**Capped below capability floor:** floor {gap['floor']}/100, cap model capability "
                     f"{gap['capability']}/100, expected-success gap {gap['expected_success_gap']:.2f}. "
                     "The cap still wins.")
        return line
    return f"**Cap:** {cap} not applied (selected {picked} is lower cost)"


def log_line(issue_number: int, record: Mapping[str, Any]) -> str:
    """The one stable Overview line when a cap applies."""
    return (f"Routing cap for issue #{issue_number}: capped {record['provider']} "
            f"{record['router_model']} at {record['router_effort']} effort to "
            f"{record['final_model']} at {record['final_effort']} effort (repository cap).")


def clamp_choice(
    provider: str, model: str, effort: str, caps: Mapping[str, Mapping[str, str]],
    *, allow_usage_credit_models: bool = False,
) -> tuple[str, str, dict[str, Any] | None]:
    """``(model, effort, record)`` after the cap; unchanged when it does not apply."""
    record = clamp(provider, model, effort, caps, allow_usage_credit_models=allow_usage_credit_models)
    if record and record.get("applied"):
        return record["final_model"], record["final_effort"], record
    return model, effort, record


def upgrade_ceiling(provider: str, caps: Mapping[str, Mapping[str, str]],
                    *, allow_usage_credit_models: bool = False) -> float | None:
    """Cost a release upgrade must not exceed for ``provider``, or None when uncapped."""
    cap, _note = resolve_cap(str(provider).lower(), caps.get(str(provider).lower()),
                             allow_usage_credit_models=allow_usage_credit_models)
    return None if cap is None else cap["cost"]
