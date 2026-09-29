"""Live model availability, as reported by each provider's own CLI.

The desktop app already asks every installed CLI which models it offers (the
Claude model catalog cache, ``codex debug models --bundled``, ``grok models``)
and passes that list to the worker as ``--available-models``. This module holds
it for the process so the routers and the decision engine work from what the
accounts can actually run instead of a hand-maintained list.

Discovery decides *which models exist*; the checked-in catalogs
(``skills/model-router/models.yaml`` and ``dynamic_router._MODEL_CATALOG``)
only supply *what is known about them*. A model that is discovered but not yet
catalogued is offered with metadata inferred from its closest catalogued
relative — same family, nearest version — and is marked as inferred. No price
is invented: an uncatalogued model stays unpriced until a real price is added.

Everything here degrades to "no discovery": bad input, or no input, leaves the
checked-in catalogs exactly as they were.
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
from typing import Any, Iterable, Mapping, Sequence

# Brand prefixes carry no family meaning ("claude-opus-5" is the opus family).
_BRANDS = frozenset({"claude", "gpt", "grok"})
_DATE_SUFFIX = re.compile(r"^\d{8,}$")
_VERSION_PART = re.compile(r"^\d+(?:\.\d+)*$")


@dataclasses.dataclass(frozen=True)
class DiscoveredModel:
    agent: str
    value: str
    label: str = ""
    efforts: tuple[str, ...] = ()
    default_effort: str = ""
    requires_usage_credits: bool = False


_lock = threading.Lock()
_models: dict[str, tuple[DiscoveredModel, ...]] = {}
_allow_usage_credit_models = False


def configure(raw: Any, *, allow_usage_credit_models: bool = False) -> int:
    """Replace the discovered list. Returns how many models were accepted.

    ``raw`` is the app's JSON — ``{agent: [{"value": ..., "label": ...,
    "efforts": [...], "defaultEffort": ..., "requiresUsageCredits": ...}]}`` —
    or the equivalent mapping. Anything malformed is skipped, and an empty or
    unparseable value clears discovery so the checked-in catalogs apply.
    """
    global _models, _allow_usage_credit_models
    parsed: Any = raw
    if isinstance(raw, (str, bytes)):
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        try:
            parsed = json.loads(text) if text.strip() else {}
        except ValueError:
            parsed = {}
    accepted: dict[str, tuple[DiscoveredModel, ...]] = {}
    if isinstance(parsed, Mapping):
        for agent, rows in parsed.items():
            key = str(agent).strip().lower()
            if not key or not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
                continue
            seen: set[str] = set()
            models: list[DiscoveredModel] = []
            for row in rows:
                model = _model_from(key, row)
                if model is not None and model.value not in seen:
                    seen.add(model.value)
                    models.append(model)
            if models:
                accepted[key] = tuple(models)
    with _lock:
        _models = accepted
        _allow_usage_credit_models = bool(allow_usage_credit_models)
    return sum(len(rows) for rows in accepted.values())


def _model_from(agent: str, row: Any) -> DiscoveredModel | None:
    if isinstance(row, str):
        row = {"value": row}
    if not isinstance(row, Mapping):
        return None
    value = str(row.get("value") or row.get("model") or row.get("id") or "").strip()
    if not value:
        return None
    efforts = row.get("efforts") or ()
    return DiscoveredModel(
        agent=agent,
        value=value,
        label=str(row.get("label") or row.get("name") or "").strip(),
        efforts=tuple(str(effort).strip() for effort in efforts if str(effort).strip())
        if isinstance(efforts, Sequence) and not isinstance(efforts, (str, bytes))
        else (),
        default_effort=str(row.get("defaultEffort") or row.get("default_effort") or "").strip(),
        requires_usage_credits=bool(
            row.get("requiresUsageCredits", row.get("requires_usage_credits", False))
        ),
    )


def reset() -> None:
    """Forget discovery (tests, and a worker started without the app)."""
    configure({})


def discovered(agent: str | None = None) -> tuple[DiscoveredModel, ...]:
    with _lock:
        if agent is not None:
            return _models.get(str(agent).strip().lower(), ())
        return tuple(model for rows in _models.values() for model in rows)


def agents() -> tuple[str, ...]:
    with _lock:
        return tuple(_models)


def allow_usage_credit_models() -> bool:
    return _allow_usage_credit_models


def canonical(slug: str) -> str:
    """The slug without a trailing release date: ``claude-haiku-4-5-20251001``
    and the catalogued ``claude-haiku-4-5`` name the same model."""
    parts = re.split(r"([-_])", str(slug or "").strip().lower())
    while len(parts) >= 3 and _DATE_SUFFIX.match(parts[-1]):
        parts = parts[:-2]
    return "".join(parts)


def family_and_version(slug: str) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """``claude-sonnet-5-5`` -> (("sonnet",), (5, 5)); ``gpt-5.6-luna`` ->
    (("luna",), (5, 6)); ``grok-4.7`` -> ((), (4, 7))."""
    family: list[str] = []
    version: list[int] = []
    for part in re.split(r"[-_]", canonical(slug)):
        if not part or part in _BRANDS:
            continue
        if _VERSION_PART.match(part):
            version.extend(int(piece) for piece in part.split("."))
        else:
            family.append(part)
    return tuple(family), tuple(version)


def closest_relative(slug: str, known: Iterable[tuple[str, Any]]) -> tuple[Any, bool] | None:
    """The catalogued entry ``slug`` should borrow its metadata from.

    Same family only, and the newest one that is not newer than ``slug`` — a
    release is most like its predecessor. Returns ``(entry, older)``; ``older``
    is true when every same-family entry is newer, i.e. ``slug`` is a release
    the catalog has already moved past. ``None`` when nothing shares a family.
    """
    family, version = family_and_version(slug)
    same = [(family_and_version(name)[1], entry) for name, entry in known
            if family_and_version(name)[0] == family]
    if not same:
        return None
    not_newer = [item for item in same if item[0] <= version]
    if not_newer:
        return max(not_newer, key=lambda item: item[0])[1], False
    return min(same, key=lambda item: item[0])[1], True


def display_label(model: DiscoveredModel) -> str:
    return model.label or model.value
