"""Model retirement shared by the calibration refresh and live routing (issue #374).

Two retirement sources, one rule:

* ``skills/model-router/model-blacklist.json`` lists known successor
  relationships (read by ``available_models`` and ``src/tools.rs``).
* ``derived_supersessions`` retires an older release of the same provider and
  family when a newer one is offered by its CLI, priced, no dearer than
  ``UPGRADE_PRICE_TOLERANCE`` and not measurably weaker than
  ``UPGRADE_SCORE_MARGIN`` at any effort both were measured at.

Either kind is in force only while its successor is offered and priced
(``available_models.retirement_in_force``). ``policy_snapshot`` publishes the
evidence with every calibration so the worker and the desktop app apply the
same rule without re-deriving it. Retired rows are marked inactive and
deprecated with ``superseded_by``; they are never deleted.
"""

from __future__ import annotations

from typing import Any, Collection, Iterable, Mapping

import available_models as _available
import model_pricing as _pricing

# A newer release may cost at most this much more per token than the one it
# replaces. Zero would forbid a rounding difference; a real price increase is
# a different decision than "use the latest". Shared with
# ``dynamic_router.latest_release``.
UPGRADE_PRICE_TOLERANCE = 1.05
# A measured score this far below the older release's blocks the replacement:
# the default is "newer is better", and only evidence overrides it.
UPGRADE_SCORE_MARGIN = 1.0


def row_price(row: Mapping[str, Any]) -> _pricing.ModelPrice | None:
    """The price in force for one calibration row: static catalog first, then its feed."""
    resolution = _pricing.resolve_price(
        str(row.get("model") or ""), provider=str(row.get("agent") or row.get("provider") or ""),
        calibration_entry=dict(row),
    )
    return resolution.price if resolution.priced else None


def _scores(row: Mapping[str, Any]) -> dict[str, float]:
    scores = row.get("intelligence_by_effort")
    if not isinstance(scores, Mapping):
        return {}
    return {str(effort): float(value) for effort, value in scores.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)}


def replaces(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    """Whether ``new`` is a priced, no-dearer, not-weaker later release of ``old``."""
    agent = str(old.get("agent") or "")
    if not agent or agent != str(new.get("agent") or ""):
        return False
    old_provider = str(old.get("provider") or "").strip().lower()
    new_provider = str(new.get("provider") or "").strip().lower()
    # Same provider and same agent. A row that names neither provider is compared
    # on the agent alone; a disagreement never retires across vendors.
    if old_provider and new_provider and old_provider != new_provider:
        return False
    family, version = _available.family_and_version(str(old.get("model") or ""))
    new_family, new_version = _available.family_and_version(str(new.get("model") or ""))
    if not version or family != new_family or new_version <= version:
        return False
    old_price, new_price = row_price(old), row_price(new)
    if old_price is None or new_price is None:
        return False
    if (new_price.input_per_million > old_price.input_per_million * UPGRADE_PRICE_TOLERANCE
            or new_price.output_per_million > old_price.output_per_million * UPGRADE_PRICE_TOLERANCE):
        return False
    old_scores, new_scores = _scores(old), _scores(new)
    return all(new_scores[effort] >= old_scores[effort] - UPGRADE_SCORE_MARGIN
               for effort in old_scores.keys() & new_scores.keys())


def supersessions(rows: Iterable[Mapping[str, Any]], offered: Mapping[str, Collection[str]] | Collection[str]) -> dict[str, str]:
    """``derived_supersessions`` for a CLI report (``{agent: [models]}``) or a name list."""
    if isinstance(offered, Mapping):
        names: list[str] = []
        for value in offered.values():
            if isinstance(value, str):
                names.append(value)
            elif isinstance(value, Collection) and not isinstance(value, (str, bytes)):
                names.extend(str(name) for name in value if isinstance(name, str))
        offered_names: Collection[str] = names
    else:
        offered_names = offered
    return derived_supersessions(rows, offered=offered_names)


def derived_supersessions(rows: Iterable[Mapping[str, Any]], *, offered: Collection[str]) -> dict[str, str]:
    """``{old model: newest successor}`` among ``rows`` under the release rule.

    A successor must be offered (canonical name in ``offered``), active and not
    deprecated by its own definition. Missing CLI evidence or a missing price
    never retires anything.
    """
    usable = [row for row in rows if isinstance(row, Mapping) and row.get("model") and row.get("agent")]
    offered = {_available.canonical(name) for name in offered}
    successors = [row for row in usable
                  if _available.canonical(str(row["model"])) in offered
                  and row.get("active", True) is True and not row.get("deprecated")]
    result: dict[str, str] = {}
    for old in usable:
        newer = [new for new in successors if new is not old and replaces(old, new)]
        if newer:
            newest = max(newer, key=lambda row: (_available.family_and_version(str(row["model"]))[1], str(row["model"])))
            result[str(old["model"])] = str(newest["model"])
    return dict(sorted(result.items()))


def priced_models(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Every model name with a price: the static catalog plus feed-priced rows."""
    names = {name for entry in _pricing.PRICING_CATALOG
             if _pricing.resolve_price(entry.model, provider=entry.provider).priced
             for name in entry.model_names}
    names.update(_available.canonical(str(row["model"])) for row in rows
                 if isinstance(row, Mapping) and row.get("model") and row_price(row) is not None)
    return sorted(names)


def policy_snapshot(
    rows: Iterable[Mapping[str, Any]], available: Mapping[str, Collection[str]] | None,
) -> dict[str, Any]:
    """Retirement evidence published atomically with a calibration.

    ``available`` is the raw CLI report the refresh ran with (``None`` when the
    caller passed none). Only when CLI evidence is absent do bundled current releases supply the
    offline fallback. An explicit empty CLI report offers nothing. ``retirements``
    is the in-force result under exactly this evidence, used to mark rows and
    to log and diff transitions; live readers re-apply the rule to their own
    CLI evidence.
    """
    rows = [row for row in rows if isinstance(row, Mapping)]
    recorded = {str(agent): sorted({str(name) for name in names if isinstance(name, str) and name})
                for agent, names in (available or {}).items()}
    # An explicit CLI report always wins over bundled metadata.
    offered = {_available.canonical(name) for names in recorded.values() for name in names}
    if available is None:
        offered |= set(_available.bundled_releases())
    prices = set(priced_models(rows))
    # Restore dormant listed peers before comparing releases. Otherwise their
    # checked-in deprecated flag would prevent them replacing an older release.
    explicit = _available.active_retirements(offered=offered, priced=lambda name: _available.canonical(name) in prices, derived={})
    apply_retirements(rows, explicit, offered=offered)
    derived = derived_supersessions(rows, offered=offered)
    retirements = _available.active_retirements(
        offered=offered, priced=lambda model: _available.canonical(model) in prices, derived=derived,
    )
    policy: dict[str, Any] = {
        "priced_models": sorted(prices),
        "derived_supersessions": derived,
        "retirements": dict(sorted(retirements.items())),
    }
    if available is not None:
        policy["available_models"] = recorded
    return policy


def snapshot(rows: Iterable[Mapping[str, Any]], available: Mapping[str, Collection[str]] | None = None) -> dict[str, Any]:
    """Alias used by the Rust/Python agreement check. See ``policy_snapshot``."""
    return policy_snapshot(rows, available)


def apply_retirements(
    rows: Iterable[dict[str, Any]], retirements: Mapping[str, str], *, offered: Collection[str] | None = None,
) -> None:
    """Mark in-force retirements inactive, and restore a dormant listed one the CLI offers.

    The checked-in catalogs keep a retired peer as inactive and deprecated.
    That flag must not stick while the successor is absent or unpriced: if the
    CLI still offers the predecessor, the row is routable again. ``offered`` is
    the CLI report only (not bundled releases). Unknown CLI evidence (``None``)
    restores nothing.
    """
    cli = {_available.canonical(name) for name in (offered or ())}
    listed = set(_available.listed_retirements())
    for row in rows:
        name = _available.canonical(str(row.get("model") or ""))
        successor = retirements.get(name)
        if successor:
            row.update(active=False, recommended=False, deprecated=True, superseded_by=successor)
        elif offered is not None and name in listed and name in cli:
            row.update(active=True, deprecated=False, superseded_by=None)
