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
is invented: an uncatalogued model stays unpriced until the pricing catalog or
the active calibration's feed prices it.

Everything here degrades to "no discovery": bad input, or no input, leaves the
checked-in catalogs exactly as they were.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import re
import threading
from pathlib import Path
from typing import Any, Callable, Collection, Iterable, Mapping, Sequence

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


# Retirements: older releases and the successor that replaces each. The one
# list is shared with the desktop app, which embeds the same file; an entry is
# in force only while its successor is offered and priced (``blacklist``).
BLACKLIST_PATH = Path(__file__).resolve().parent.parent / "skills" / "model-router" / "model-blacklist.json"
BUNDLED_CATALOG_PATH = BLACKLIST_PATH.parent / "models.yaml"

_lock = threading.Lock()
_models: dict[str, tuple[DiscoveredModel, ...]] = {}
_allow_usage_credit_models = False
_configured_version = None


def configure(raw: Any, *, allow_usage_credit_models: bool = False) -> int:
    """Replace the discovered list. Returns how many models were accepted.

    ``raw`` is the app's JSON — ``{agent: [{"value": ..., "label": ...,
    "efforts": [...], "defaultEffort": ..., "requiresUsageCredits": ...}]}`` —
    or the equivalent mapping. Anything malformed is skipped, and an empty or
    unparseable value clears discovery so the checked-in catalogs apply.
    """
    global _models, _allow_usage_credit_models, _configured_version
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
                # Retired models stay recorded: whether a retirement is in
                # force can change at run time (``discovered`` filters it).
                if model is not None and model.value not in seen:
                    seen.add(model.value)
                    models.append(model)
            accepted[key] = tuple(models)
    import model_pricing
    version = model_pricing.calibration_document().get("version")
    with _lock:
        _configured_version = version
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


@functools.lru_cache(maxsize=1)
def listed_retirements() -> dict[str, str]:
    """``{canonical model: successor}`` exactly as the shared JSON lists them.

    Every entry is conditional (see ``blacklist``). The successor is ``""``
    when none is named. An unreadable or malformed file yields no entries:
    routing must degrade, never stall, on a bad list.
    """
    try:
        rows = json.loads(BLACKLIST_PATH.read_text(encoding="utf-8")).get("models") or []
    except (OSError, ValueError, AttributeError):
        return {}
    listed: dict[str, str] = {}
    for row in rows:
        name = canonical(str(row.get("model") or "")) if isinstance(row, Mapping) else ""
        if name:
            listed[name] = str(row.get("superseded_by") or "").strip()
    return listed


@functools.lru_cache(maxsize=1)
def bundled_releases() -> frozenset[str]:
    """Canonical names of the current releases the bundled ``models.yaml`` ships.

    The app treats these as offered even when a CLI list omits them, exactly as
    routing always has. ``src/tools.rs`` reads the same file the same way.
    """
    import model_router_yaml

    try:
        data = model_router_yaml.load(BUNDLED_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    rows = data.get("models") if isinstance(data, Mapping) else None
    return frozenset(
        canonical(str(row.get("model") or "")) for row in rows or ()
        if isinstance(row, Mapping) and row.get("model")
        and row.get("active") is True and row.get("deprecated") is not True
    )


def _calibration_policy() -> Mapping[str, Any]:
    import model_pricing

    policy = model_pricing.calibration_document().get("model_policy")
    return policy if isinstance(policy, Mapping) else {}


def recorded_models() -> dict[str, tuple[str, ...]]:
    """CLI lists the desktop app recorded with the active calibration.

    A long-running scheduler keeps the list it started with; the calibration
    carries the app's newer report, so a model a CLI starts offering reaches
    routing on the next refresh without restarting anything.
    """
    recorded = _calibration_policy().get("available_models")
    if not isinstance(recorded, Mapping):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for agent, names in recorded.items():
        if isinstance(agent, str) and isinstance(names, Sequence) and not isinstance(names, (str, bytes)):
            result[agent.strip().lower()] = tuple(str(name) for name in names if isinstance(name, str) and name)
    return result


def offered_models() -> dict[str, set[str]]:
    """CLI evidence is authoritative once a filtered calibration is published.

    Bundled releases are only the legacy offline fallback, before any refresh
    has recorded a CLI report. They never override an explicit calibration list.
    """
    names = {agent: {canonical(row.value) for row in rows} for agent, rows in _discovered_rows().items()}
    if "available_models" not in _calibration_policy():
        import model_pricing
        for name in bundled_releases():
            price = model_pricing.resolve_price(name)
            if price.priced:
                names.setdefault(price.price.provider, set()).add(name)
    return names


def retirement_in_force(
    successor: str, *, offered: Mapping[str, Collection[str]], price_provider: Callable[[str], str],
) -> bool:
    """Whether a listed or derived retirement applies now.

    It applies only once its successor is offered and priced (static catalog or feed). An entry naming no successor
    is an outright operator ban and always applies.
    """
    if not successor:
        return True
    agent = price_provider(successor)
    return bool(agent) and canonical(successor) in {canonical(name) for name in offered.get(agent, ())}


def _price_provider(model: str) -> str:
    import model_pricing

    price = model_pricing.resolve_price(model)
    return price.price.provider if price.priced else ""


def active_retirements(
    *,
    offered: Mapping[str, Collection[str]] | None = None,
    price_provider: Callable[[str], str] | None = None,
    derived: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """``{canonical model: successor}`` for every retirement in force.

    The shared JSON plus the active calibration's derived supersessions, each
    kept only while ``retirement_in_force``. A dormant entry leaves its model
    fully routable. ``offered``/``price_provider``/``derived`` default to the live
    process state; the calibration passes the state it is about to publish.
    """
    offered = offered_models() if offered is None else offered
    price_provider = price_provider or _price_provider
    if derived is None:
        recorded = _calibration_policy().get("derived_supersessions")
        derived = recorded if isinstance(recorded, Mapping) else {}
    entries = {old: new for old, new in listed_retirements().items()
               if retirement_in_force(new, offered=offered, price_provider=price_provider)}
    for old, new in derived.items():
        if (isinstance(old, str) and isinstance(new, str) and old and new
                and retirement_in_force(new, offered=offered, price_provider=price_provider)):
            # A dormant explicit relationship cannot hide an eligible later
            # release derived from the feed (for example, a skipped release).
            entries.setdefault(canonical(old), new)
    active = entries
    for old, new in active.copy().items():
        seen = {old}
        while canonical(new) in active and canonical(new) not in seen:
            seen.add(canonical(new))
            new = active[canonical(new)]
        if canonical(new) not in seen:
            active[old] = new
    return active


def blacklist() -> dict[str, str]:
    """``{canonical model: successor}`` for every model that must not be used now.

    A retirement is conditional: it is in force only while its successor is
    offered and priced (``active_retirements``). Python and ``src/tools.rs``
    apply the same rule to the same JSON and calibration publication.
    """
    return active_retirements()


def is_blacklisted(slug: str) -> bool:
    """Whether ``slug`` (a dated alias counts as its model) is retired now."""
    return canonical(slug) in blacklist()


def blacklist_successor(slug: str) -> str:
    """The model that replaces a blacklisted ``slug``, or ``""``."""
    return blacklist().get(canonical(slug), "")


def replace_blacklisted(slug: str) -> str:
    """``slug`` itself, or its successor when it is blacklisted and has one."""
    return blacklist_successor(slug) or slug if is_blacklisted(slug) else slug


def reset() -> None:
    """Forget discovery (tests, and a worker started without the app)."""
    configure({})


def _discovered_rows() -> dict[str, tuple[DiscoveredModel, ...]]:
    """Current CLI rows, refreshed from a newer calibration for long-lived workers.

    Never union old and new reports: doing so would keep withdrawn models alive.
    A caller configured after publication has the freshest report. A publication
    arriving after configure replaces those names, preserving metadata on matches.
    """
    import model_pricing

    with _lock:
        rows = dict(_models)
        configured_version = _configured_version
    recorded = recorded_models()
    changed = model_pricing.calibration_document().get("version") != configured_version
    if changed and "available_models" in _calibration_policy():
        # This is a replacement report, including an empty report or omitted
        # providers. Never keep a withdrawn provider's stale discovery rows.
        rows = {agent: rows.get(agent, ()) for agent in recorded}
    for agent, names in recorded.items():
        if agent not in rows or changed:
            metadata = {canonical(row.value): row for row in rows.get(agent, ())}
            rows[agent] = tuple(metadata.get(canonical(name), DiscoveredModel(agent, name)) for name in names)
    return rows


def cli_offers(agent: str, model: str) -> bool:
    """Check a current CLI report; preserve the offline catalog when none exists."""
    rows = _discovered_rows()
    if "available_models" not in _calibration_policy():
        return canonical(model) in bundled_releases() or any(
            canonical(row.value) == canonical(model) for row in rows.get(agent, ()))
    return any(canonical(row.value) == canonical(model) for row in rows.get(agent, ()))


def discovered(agent: str | None = None) -> tuple[DiscoveredModel, ...]:
    """Models a CLI offers, without any whose retirement is in force."""
    rows = _discovered_rows()
    found = (rows.get(str(agent).strip().lower(), ()) if agent is not None
             else tuple(model for models in rows.values() for model in models))
    retired = blacklist()
    return tuple(model for model in found if canonical(model.value) not in retired)


def agents() -> tuple[str, ...]:
    return tuple(_discovered_rows())


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
