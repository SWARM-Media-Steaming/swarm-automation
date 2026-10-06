#!/usr/bin/env python3
"""The web backend's window onto the shared worker's read and compute logic.

``web/`` (the hosted REST API) does not re-implement execution history, usage
analytics, prompt grades, Jev feedback, architecture documentation or the routing
calculator. It runs this module once per request (``bridge.rs``): one JSON object
on **stdin** and one JSON object on stdout.

    {"op": "execution_history", "tenant": "t0123...", "args": {...}, "config": {...}}
    -> {"ok": true, "result": ...}
    -> {"ok": false, "code": "bad_request" | "unavailable" | "failed", "error": "..."}

``tenant`` is the only tenant the call can reach: storage is opened with
``storage_factory.open_storage("hosted")`` (settings come from ``SWARM_STORAGE_*``,
never from the request) and every read names that tenant first. Reads never
provision: a tenant without a history store answers like an empty desktop
database. Operations the hosted deployment cannot serve yet answer
``unavailable`` (HTTP 501) with the reason, rather than inventing data.

Standard library and sibling modules only, like the rest of ``issue_worker/``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

# ``python -I`` (how the backend starts this) leaves the script's directory off
# ``sys.path``; the siblings are this directory, so add exactly it.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import ai_execution_history as history  # noqa: E402
import architecture_docs  # noqa: E402
import available_models  # noqa: E402
import routing_calculator  # noqa: E402
import storage_factory  # noqa: E402
import usage_report  # noqa: E402
from storage import Storage, StorageError, validate_tenant  # noqa: E402

MAX_TEXT = 300
MAX_OFFSET = 10_000_000
HISTORY_SORTS = ("recent", "rounds_asc", "rounds_desc")
HISTORY_DELIVERY = ("all", "verified_clean", "best_effort")
PROVIDERS = ("claude", "codex", "grok")

# Operations whose hosted store or GitHub plumbing is a later step of #413. Each
# reason is shown to the user as the endpoint's 501 message.
UNAVAILABLE: dict[str, str] = {
    "import_execution_history": "Importing history from GitHub issues is not available on the hosted service yet. Hosted jobs record history directly.",
    "knowledge_status": "Engineering knowledge is not hosted yet: its index still lives in the desktop's local database.",
    "knowledge_refresh": "Engineering knowledge is not hosted yet: its index still lives in the desktop's local database.",
    "ask_swarm": "Engineering knowledge is not hosted yet: its index still lives in the desktop's local database.",
    "calibration_status": "Model calibration is not hosted yet: its snapshots still live in the desktop's local state directory.",
    "calibration_refresh": "Model calibration is not hosted yet: its snapshots still live in the desktop's local state directory.",
    "calibration_analyze": "Model calibration is not hosted yet: its snapshots still live in the desktop's local state directory.",
    "calibration_activate": "Model calibration is not hosted yet: its snapshots still live in the desktop's local state directory.",
    "diagnostics_run": "Diagnostics inspect a local checkout and are not available on the hosted service yet.",
    "diagnostics_file_issue": "Diagnostics inspect a local checkout and are not available on the hosted service yet.",
    "git_overview": "Branch overviews need a repository installation token on the bridge, which is not wired yet.",
    "merge_issue_branch": "Merging needs a repository installation token on the bridge, which is not wired yet.",
    "merge_integration_branch": "Merging needs a repository installation token on the bridge, which is not wired yet.",
    "branch_push_access": "Branch protection checks need a repository installation token on the bridge, which is not wired yet.",
    "grant_bot_branch_push": "Granting push access needs a repository installation token on the bridge, which is not wired yet.",
    "promotion_overview": "Promotion needs a repository installation token on the bridge, which is not wired yet.",
    "open_integration_pr": "Promotion needs a repository installation token on the bridge, which is not wired yet.",
    "promote_integration_branch": "Promotion needs a repository installation token on the bridge, which is not wired yet.",
}


class BridgeError(Exception):
    code = "failed"


class BadRequest(BridgeError):
    code = "bad_request"


class Unavailable(BridgeError):
    code = "unavailable"


# -- argument helpers ------------------------------------------------------------------------


def _text(args: Mapping[str, Any], name: str, default: str = "") -> str:
    value = args.get(name, default)
    if value is None:
        return default
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise BadRequest(f"{name} must be text.")
    text = str(value).strip()
    if len(text) > MAX_TEXT:
        raise BadRequest(f"{name} is too long.")
    return history.sanitize_text(text)


def _int(args: Mapping[str, Any], name: str, default: int = 0) -> int:
    value = args.get(name)
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be a whole number.") from None
    if number < 0 or number > MAX_OFFSET:
        raise BadRequest(f"{name} is out of range.")
    return number


def _choice(args: Mapping[str, Any], name: str, choices: tuple[str, ...], default: str) -> str:
    value = _text(args, name, default) or default
    if value not in choices:
        raise BadRequest(f"{name} must be one of: {', '.join(choices)}.")
    return value


def _mapping(args: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = args.get(name)
    if value is None or value == "":
        return {}
    if not isinstance(value, Mapping):
        raise BadRequest(f"{name} must be an object.")
    return value


def _repositories(args: Mapping[str, Any]) -> list[str]:
    names = args.get("repositories")
    if not isinstance(names, list):
        return []
    return [history.sanitize_text(name) for name in names if isinstance(name, str) and name.strip()]


# -- history-backed operations ----------------------------------------------------------------------


def _history(storage: Storage, tenant: str):
    """The tenant's history store, or ``None`` when it has none (never provisions)."""
    if not storage.has_execution_history(tenant):
        return None
    return storage.execution_history(tenant)


def op_execution_history(storage: Storage, tenant: str, args: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    names = _repositories(args)
    sort = _choice(args, "sort", HISTORY_SORTS, "recent")
    search = _text(args, "search")
    delivery = _choice(args, "delivery", HISTORY_DELIVERY, "all")
    offset = _int(args, "offset")
    store = _history(storage, tenant)
    if store is None:
        return history._empty_page()
    rows, total, offset, limit = store.page_for_repository(
        names, sort=sort, search=search, limit=history.PAGE_SIZE, offset=offset, delivery=delivery
    )
    records = [history.row_to_dict(row) for row in rows]
    records = history.attach_adversarial_rounds(store, records)
    records = history.attach_adversarial_epochs(store, records)
    return {
        "records": history.attach_token_usage(store, records),
        "total": total,
        "offset": offset,
        "limit": limit,
        "adversarial": store.adversarial_summary(names, search=search, delivery=delivery),
        "security": store.security_summary(names, search=search, delivery=delivery),
    }


def op_jev_feedback(storage: Storage, tenant: str, args: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    store = _history(storage, tenant)
    if store is None:
        return history._empty_jev_feedback()
    return store.jev_feedback(
        _repositories(args),
        search=_text(args, "search"),
        jev_status=_text(args, "jevStatus"),
        provider=_text(args, "provider"),
        outcome=_text(args, "outcome"),
        routing_changed=_text(args, "routingChanged"),
        created_after=_text(args, "createdAfter"),
        created_before=_text(args, "createdBefore"),
        min_delta=_text(args, "minDelta"),
        max_cost=_text(args, "maxCost"),
        limit=history.PAGE_SIZE,
        offset=_int(args, "offset"),
    )


def op_usage_report(storage: Storage, tenant: str, args: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    query = _mapping(args, "query")
    group_by = _choice(query, "groupBy", usage_report.GROUP_BY_KEYS, "issue")
    store = _history(storage, tenant)
    if store is None:
        return usage_report.empty_report(group_by)
    group_value = query.get("groupValue")
    return store.usage_report(
        _repositories(args),
        start_date=_text(query, "startDate"),
        end_date=_text(query, "endDate"),
        issue_number=_text(query, "issueNumber"),
        grade=_text(query, "grade"),
        provider=_text(query, "provider"),
        model=_text(query, "model"),
        effort=_text(query, "effort"),
        agent_type=_text(query, "agentType"),
        prompt_type=_text(query, "promptType"),
        outcome=_choice(query, "outcome", ("all", "success", "failure"), "all"),
        coverage=_text(query, "coverage"),
        execution_id=_text(query, "executionId"),
        search=_text(query, "search"),
        group_by=group_by,
        sort=_choice(query, "sort", usage_report.SORT_KEYS, "cost"),
        direction=_choice(query, "direction", ("asc", "desc"), "desc"),
        group_offset=_int(query, "groupOffset"),
        detail_offset=_int(query, "detailOffset"),
        limit=usage_report.USAGE_PAGE_SIZE,
        group_value=None if group_value is None else history.sanitize_text(str(group_value)[:MAX_TEXT]),
    )


def op_prompt_grades(storage: Storage, tenant: str, args: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    query = _mapping(args, "query")
    store = _history(storage, tenant)
    if store is None:
        return {**history._empty_page(), "summary": history.summarize_grades([]), "routerMatrix": []}
    return store.graded_for_repository(
        _repositories(args),
        search=_text(query, "search"),
        grade=_text(query, "grade"),
        router=_text(query, "router"),
        router_model=_text(query, "routerModel"),
        limit=history.PAGE_SIZE,
        offset=_int(query, "offset"),
    )


# -- documents and computation ------------------------------------------------------------------------


def op_architecture_docs(storage: Storage, tenant: str, args: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    repository = _text(args, "repository")
    if not repository:
        raise BadRequest("A repository is required.")
    configured = next(
        (repo for repo in config.get("repositories") or [] if isinstance(repo, Mapping) and repo.get("github_repository") == repository),
        None,
    )
    if configured is None:
        raise BadRequest("That repository is not configured.")
    store = architecture_docs.ArchitectureStore("", repository, storage=storage, tenant=tenant)
    # A hosted view never seeds from a workspace: there is no checkout here.
    return store.view(enabled=bool(configured.get("architecture_docs_enabled")))


def _providers(config: Mapping[str, Any]) -> list[str]:
    enabled = [
        str(provider.get("id", "")).lower()
        for provider in config.get("providers") or []
        if isinstance(provider, Mapping) and provider.get("enabled", True)
    ]
    return [key for key in enabled if key in PROVIDERS] or list(PROVIDERS)


def op_routing_describe(storage: Storage, tenant: str, args: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    available_models.configure("", allow_usage_credit_models=False)
    return routing_calculator.describe(providers=_providers(config))


def op_routing_simulate(storage: Storage, tenant: str, args: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    inputs = _mapping(args, "inputs")
    if not inputs:
        raise BadRequest("The inputs must be a JSON object.")
    available_models.configure("", allow_usage_credit_models=False)
    try:
        return routing_calculator.simulate(inputs, providers=_providers(config), allow_usage_credit_models=False)
    except routing_calculator.CalculatorError as error:
        raise BadRequest(str(error)) from None


# Operations that read the tenant's storage; the rest are pure computation.
STORAGE_OPS = frozenset({"execution_history", "jev_feedback", "usage_report", "prompt_grades", "architecture_docs"})

OPERATIONS: dict[str, Callable[[Storage, str, Mapping[str, Any], Mapping[str, Any]], Any]] = {
    "execution_history": op_execution_history,
    "jev_feedback": op_jev_feedback,
    "usage_report": op_usage_report,
    "prompt_grades": op_prompt_grades,
    "architecture_docs": op_architecture_docs,
    "routing_describe": op_routing_describe,
    "routing_simulate": op_routing_simulate,
}


def handle(request: Any, storage: Storage | Callable[[], Storage]) -> dict[str, Any]:
    """Run one request and return the response object (never raises)."""
    try:
        if not isinstance(request, Mapping):
            raise BadRequest("The request must be a JSON object.")
        op = str(request.get("op") or "")
        if op in UNAVAILABLE:
            raise Unavailable(UNAVAILABLE[op])
        handler = OPERATIONS.get(op)
        if handler is None:
            raise BadRequest("Unknown operation.")
        try:
            tenant = validate_tenant(str(request.get("tenant") or ""))
        except StorageError:
            raise BadRequest("A valid tenant is required.") from None
        args = request.get("args") if isinstance(request.get("args"), Mapping) else {}
        config = request.get("config") if isinstance(request.get("config"), Mapping) else {}
        opened = (storage() if callable(storage) else storage) if op in STORAGE_OPS else None
        return {"ok": True, "result": handler(opened, tenant, args, config)}
    except BridgeError as error:
        return {"ok": False, "code": error.code, "error": str(error)}
    except StorageError as error:
        return {"ok": False, "code": "failed", "error": architecture_docs.redact(f"Storage failed: {error}", MAX_TEXT)}
    except Exception as error:  # noqa: BLE001 - the backend needs an answer, not a traceback
        return {"ok": False, "code": "failed", "error": architecture_docs.redact(f"{type(error).__name__}: {error}", MAX_TEXT)}


def main(argv: list[str] | None = None) -> int:
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        request = None
    response = handle(request, lambda: storage_factory.open_storage("hosted"))
    sys.stdout.write(json.dumps(response, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
