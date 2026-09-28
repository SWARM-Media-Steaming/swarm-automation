"""Read-only aggregation behind Feedback's **Usage & cost** tab (issue #295).

Everything here runs in SQLite, over the same app-wide database the rest of
execution history uses. The desktop never receives the ``ai_token_usage``
table: it receives a filtered summary, coverage counts, one page of grouped
rows and one page of invocation detail, all computed by these queries. That
is a hard requirement rather than an optimization — a fleet with a year of
history has more invocation rows than the UI could sensibly hold, and
aggregating in JavaScript would also make the repository filter a client-side
concern, which is exactly the isolation bug the other Feedback tabs already
avoid by aggregating server-side.

Three semantics worth stating once, because everything below depends on them:

**Activity time, not write time.** Usage events are persisted in one batch
when a work-round ends, so ``created_at`` can be minutes or hours after the
call it describes, and a routing call made before the execution row existed
shares a batch with calls from much later. Every date filter and every time
bucket therefore uses the invocation's own ``started_at`` (falling back to
``completed_at``, and only then to ``created_at`` for rows old enough to have
neither).

**Missing is not zero.** ``SUM`` over a column that is NULL in every matching
row returns NULL, and that NULL is carried all the way to the UI, which
renders it as *unavailable*. Nothing here coalesces a token count to 0 — a
provider that did not report output tokens did not report zero output tokens.
Each nullable sum is paired with a ``COUNT`` of how many rows actually
reported it, so a total can always say what it is a total *of*.

**Cost is an estimate with provenance.** ``estimated_cost`` is whatever
``model_pricing`` computed when the call happened, at the rate effective
then, and it is read back verbatim. Nothing recosts a stored row, so
correcting the catalog never rewrites history.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence


#: Group-by dimensions the query API supports. The Feedback selector lists
#: a subset of these (everything except ``outcome``); ``outcome`` is still a
#: first-class grouping so a caller can break totals down by success vs.
#: failure rather than only filter to one or the other. The first entry is
#: the fallback for an unknown value.
GROUP_BY_KEYS: tuple[str, ...] = (
    "issue",
    "model",
    "provider",
    "grade",
    "effort",
    "agent",
    "prompt",
    "outcome",
    "repository",
    "day",
    "week",
    "month",
)

#: Sortable aggregate columns, mapped to the SELECT alias they order by.
SORT_KEYS: tuple[str, ...] = (
    "group",
    "issues",
    "invocations",
    "input",
    "cached",
    "reasoning",
    "output",
    "total",
    "cost",
)

#: Every coverage status a recorded invocation can have. These are what make
#: "we have no data" visibly different from "there was nothing to spend".
COVERAGE_STATUSES: tuple[str, ...] = (
    "complete",
    "tokens_only",
    "partial",
    "unreported",
    "failed",
)

#: Neither table is ever paged more than this at a time.
USAGE_PAGE_SIZE = 25
#: Facet lists are bounded too: a filter dropdown with ten thousand issues in
#: it is not a filter. The UI's own search narrows beyond this.
FACET_LIMIT = 250
#: Execution ids are bound one parameter each, so the per-execution lookups
#: below batch them. Feedback only ever asks for a page of ten, but the
#: unpaged history export can hand over a whole repository at once.
_ID_BATCH = 400

_OUTCOMES = ("all", "success", "failure")

# --- Shared SQL fragments --------------------------------------------------

_JOIN = (
    "FROM ai_token_usage u "
    "LEFT JOIN ai_executions e ON e.execution_id = u.execution_id"
)

#: The invocation's own activity timestamp. See the module docstring: the
#: batch ``created_at`` is a persistence detail and must never be the date a
#: report filters or buckets on.
_ACTIVITY = "COALESCE(NULLIF(u.started_at, ''), NULLIF(u.completed_at, ''), u.created_at)"
#: As a plain ``YYYY-MM-DD``. ``date()`` understands an ISO timestamp with an
#: offset and normalizes it; the ``substr`` fallback keeps a row with an
#: unparseable timestamp in its own day rather than dropping it entirely.
_ACTIVITY_DATE = f"COALESCE(date({_ACTIVITY}), substr({_ACTIVITY}, 1, 10))"

#: A router call is recorded before its execution row exists and is linked
#: when the batch is persisted, so its repository/issue can come from either
#: side of the join.
_REPOSITORY = "COALESCE(NULLIF(u.repository, ''), e.repository, '')"
_ISSUE_NUMBER = "COALESCE(NULLIF(u.issue_number, 0), e.issue_number, 0)"
_ISSUE_KEY = f"({_REPOSITORY} || '#' || {_ISSUE_NUMBER})"
# History is independent of Dynamic Model Routing. An execution with an empty
# or non-JSON ``routing_decision`` (the default when the router did not run)
# must still be queryable — ``json_extract`` on '' raises "malformed JSON"
# and would abort the whole Usage & cost tab.
_ROUTING_JSON = (
    "CASE WHEN e.routing_decision IS NOT NULL AND json_valid(e.routing_decision) "
    "THEN e.routing_decision ELSE NULL END"
)
_GRADE = f"COALESCE(json_extract({_ROUTING_JSON}, '$.prompt_grade'), '')"
#: Success vs. failure of the invocation itself. Matches the outcome *filter*
#: (``success = 0`` is failure; anything else is success) so grouping by
#: outcome and then drilling into one bucket is the same partition the
#: filter would have produced.
_OUTCOME = "CASE WHEN u.success = 0 THEN 'failure' ELSE 'success' END"

#: Order matters: a failed call is reported as failed even if it also came
#: back with partial usage, and an invocation with no usage fields at all is
#: "unreported" rather than "partial". ``tokens_only`` is the specific case of
#: complete provider totals with no matching price, which is what makes an
#: unpriced model visible instead of looking free.
_COVERAGE = """CASE
    WHEN u.success = 0 THEN 'failed'
    WHEN u.total_tokens IS NULL AND u.input_tokens IS NULL AND u.output_tokens IS NULL
        THEN 'unreported'
    WHEN u.total_tokens IS NULL OR u.input_tokens IS NULL OR u.output_tokens IS NULL
        THEN 'partial'
    WHEN u.estimated_cost IS NULL THEN 'tokens_only'
    ELSE 'complete'
END"""

#: A row that recorded no token fields at all may still carry a leftover
#: ``estimated_cost = 0.0`` from older pricing. Those zeros must not enter
#: the estimated-cost total or the priced-invocation count — $0.00 on an
#: unreported call is a lie. Genuine zeros keep a non-NULL token field.
_HAS_TOKEN_USAGE = (
    "NOT (u.total_tokens IS NULL AND u.input_tokens IS NULL AND u.output_tokens IS NULL)"
)
_PRICED_COST = f"CASE WHEN {_HAS_TOKEN_USAGE} THEN u.estimated_cost END"
_COST_SELECT = f"SUM({_PRICED_COST}), COUNT({_PRICED_COST})"

_GROUP_EXPRESSIONS: dict[str, str] = {
    "issue": _ISSUE_KEY,
    "model": "COALESCE(NULLIF(u.model, ''), '')",
    "provider": "COALESCE(NULLIF(u.provider, ''), '')",
    "grade": _GRADE,
    "effort": "COALESCE(NULLIF(u.reasoning_effort, ''), '')",
    "agent": "COALESCE(NULLIF(u.agent_type, ''), '')",
    "prompt": "COALESCE(NULLIF(u.prompt_type, ''), '')",
    "outcome": _OUTCOME,
    "repository": _REPOSITORY,
    "day": _ACTIVITY_DATE,
    # %W is a Monday-based week-of-year, which is what the day bucket's
    # ISO dates already imply.
    "week": f"strftime('%Y-W%W', {_ACTIVITY_DATE})",
    "month": f"substr({_ACTIVITY_DATE}, 1, 7)",
}

_SORT_EXPRESSIONS: dict[str, str] = {
    "group": "group_value",
    "issues": "issues",
    "invocations": "invocations",
    "input": "input_tokens",
    "cached": "cached_input_tokens",
    "reasoning": "reasoning_tokens",
    "output": "output_tokens",
    "total": "total_tokens",
    "cost": "estimated_cost",
}


def normalize_group_by(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in GROUP_BY_KEYS else GROUP_BY_KEYS[0]


def normalize_sort(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in SORT_KEYS else "cost"


def normalize_direction(value: Any) -> str:
    return "asc" if str(value or "").strip().lower() == "asc" else "desc"


def normalize_outcome(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in _OUTCOMES else "all"


def normalize_coverage(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in COVERAGE_STATUSES else ""


def clamp_limit(value: Any, default: int = USAGE_PAGE_SIZE) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return default
    if size < 1:
        return default
    return min(size, USAGE_PAGE_SIZE)


def clamp_offset(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


class UsageFilters:
    """One combinable filter set, normalized once and reused by every query.

    Kept as a class rather than a bag of keyword arguments because the same
    predicate list has to build the summary, the coverage counts, the grouped
    page and the invocation page — any of them drifting apart would make the
    report disagree with itself.
    """

    def __init__(
        self,
        repositories: Sequence[str] | str | None = None,
        *,
        start_date: str = "",
        end_date: str = "",
        issue_number: Any = None,
        grade: str = "",
        provider: str = "",
        model: str = "",
        effort: str = "",
        agent_type: str = "",
        prompt_type: str = "",
        outcome: str = "",
        coverage: str = "",
        execution_id: str = "",
        search: str = "",
    ) -> None:
        values = [repositories] if isinstance(repositories, str) else list(repositories or [])
        self.repositories = list(
            dict.fromkeys(str(value).strip() for value in values if str(value).strip())
        )
        self.start_date = str(start_date or "").strip()[:10]
        self.end_date = str(end_date or "").strip()[:10]
        self.issue_number = _as_issue_number(issue_number)
        self.grade = str(grade or "").strip()[:8]
        self.provider = str(provider or "").strip()[:60]
        self.model = str(model or "").strip()[:120]
        self.effort = str(effort or "").strip()[:40]
        self.agent_type = str(agent_type or "").strip()[:60]
        self.prompt_type = str(prompt_type or "").strip()[:60]
        self.outcome = normalize_outcome(outcome)
        self.coverage = normalize_coverage(coverage)
        self.execution_id = str(execution_id or "").strip()[:64]
        self.search = str(search or "").strip()[:200]

    def repository_clause(self) -> tuple[str, list[Any]]:
        """Only the repository predicate.

        Separate from the rest so facets can be scoped to the repositories in
        view without also being narrowed by the filters the user is choosing
        between — a dropdown that removes its own remaining options as soon
        as one is picked is not usable.
        """
        if not self.repositories:
            return "", []
        slots = ", ".join("?" for _ in self.repositories)
        return f"{_REPOSITORY} IN ({slots})", list(self.repositories)

    def where(self) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        repository_clause, repository_params = self.repository_clause()
        if repository_clause:
            clauses.append(repository_clause)
            params.extend(repository_params)
        if self.start_date:
            clauses.append(f"{_ACTIVITY_DATE} >= ?")
            params.append(self.start_date)
        if self.end_date:
            clauses.append(f"{_ACTIVITY_DATE} <= ?")
            params.append(self.end_date)
        if self.issue_number is not None:
            clauses.append(f"{_ISSUE_NUMBER} = ?")
            params.append(self.issue_number)
        if self.grade:
            clauses.append(f"{_GRADE} = ?")
            params.append(self.grade)
        if self.provider:
            clauses.append("LOWER(u.provider) = ?")
            params.append(self.provider.lower())
        if self.model:
            clauses.append("u.model = ?")
            params.append(self.model)
        if self.effort:
            clauses.append("u.reasoning_effort = ?")
            params.append(self.effort)
        if self.agent_type:
            clauses.append("u.agent_type = ?")
            params.append(self.agent_type)
        if self.prompt_type:
            clauses.append("u.prompt_type = ?")
            params.append(self.prompt_type)
        if self.outcome == "success":
            clauses.append("u.success = 1")
        elif self.outcome == "failure":
            clauses.append("u.success = 0")
        if self.coverage:
            clauses.append(f"({_COVERAGE}) = ?")
            params.append(self.coverage)
        if self.execution_id:
            clauses.append("u.execution_id = ?")
            params.append(self.execution_id)
        if self.search:
            pattern = "%" + self.search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append(
                "("
                f"CAST({_ISSUE_NUMBER} AS TEXT) LIKE ? ESCAPE '\\' "
                "OR LOWER(COALESCE(e.issue_title, '')) LIKE ? ESCAPE '\\' "
                "OR LOWER(u.provider) LIKE ? ESCAPE '\\' "
                "OR LOWER(u.model) LIKE ? ESCAPE '\\'"
                ")"
            )
            params.extend([pattern] * 4)
        if not clauses:
            return "", []
        return " WHERE " + " AND ".join(clauses), params

    def to_dict(self) -> dict[str, Any]:
        """Echoed back to the UI so a page can redraw its own filter state."""
        return {
            "repositories": list(self.repositories),
            "startDate": self.start_date,
            "endDate": self.end_date,
            "issueNumber": self.issue_number,
            "grade": self.grade,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "agentType": self.agent_type,
            "promptType": self.prompt_type,
            "outcome": self.outcome,
            "coverage": self.coverage,
            "executionId": self.execution_id,
            "search": self.search,
        }


def _as_issue_number(value: Any) -> int | None:
    text = str(value if value is not None else "").strip().lstrip("#")
    if not text:
        return None
    try:
        number = int(text)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else round(float(value), 6)


# Every nullable token column, as ``SUM`` (the total, NULL when nothing
# reported it) paired with ``COUNT`` (how many rows did). The pair is what
# keeps "nobody reported this" distinguishable from "everybody reported 0".
_TOKEN_COLUMNS = (
    ("input", "u.input_tokens"),
    ("cached", "u.cached_input_tokens"),
    ("cacheRead", "u.cache_read_tokens"),
    ("cacheWrite", "u.cache_write_tokens"),
    ("reasoning", "u.reasoning_tokens"),
    ("output", "u.output_tokens"),
    ("total", "u.total_tokens"),
)


def _token_select() -> str:
    parts = []
    for _, column in _TOKEN_COLUMNS:
        parts.append(f"SUM({column})")
        parts.append(f"COUNT({column})")
    return ", ".join(parts)


def _token_values(row: Sequence[Any], start: int) -> tuple[dict[str, Any], int]:
    tokens: dict[str, Any] = {}
    index = start
    for name, _ in _TOKEN_COLUMNS:
        tokens[f"{name}Tokens"] = _optional_int(row[index])
        tokens[f"{name}Reported"] = int(row[index + 1] or 0)
        index += 2
    return tokens, index


_COVERAGE_SELECT = ", ".join(
    f"SUM(CASE WHEN ({_COVERAGE}) = '{status}' THEN 1 ELSE 0 END)"
    for status in COVERAGE_STATUSES
)


def _coverage_values(row: Sequence[Any], start: int) -> tuple[dict[str, int], int]:
    counts = {}
    for offset, status in enumerate(COVERAGE_STATUSES):
        counts[status] = int(row[start + offset] or 0)
    return counts, start + len(COVERAGE_STATUSES)


def _summary(connection: sqlite3.Connection, filters: UsageFilters) -> dict[str, Any]:
    where, params = filters.where()
    row = connection.execute(
        "SELECT COUNT(*), "
        f"COUNT(DISTINCT {_ISSUE_KEY}), "
        "COUNT(DISTINCT NULLIF(u.execution_id, '')), "
        f"{_COST_SELECT}, "
        f"{_token_select()}, {_COVERAGE_SELECT}, "
        "COUNT(DISTINCT NULLIF(u.currency, '')), "
        "MIN(NULLIF(u.currency, '')) "
        f"{_JOIN}{where}",
        params,
    ).fetchone()
    invocations = int(row[0] or 0)
    tokens, index = _token_values(row, 5)
    coverage, index = _coverage_values(row, index)
    currencies = int(row[index] or 0)
    currency = str(row[index + 1] or "USD")
    summary: dict[str, Any] = {
        "invocations": invocations,
        "issues": int(row[1] or 0),
        "executions": int(row[2] or 0),
        "estimatedCost": _optional_float(row[3]),
        "pricedInvocations": int(row[4] or 0),
        # Shown next to every estimated cost: a total over 3 of 11 priced
        # invocations means something very different from one over 11 of 11.
        "currency": currency,
        "mixedCurrencies": currencies > 1,
    }
    summary.update(tokens)
    return {"summary": summary, "coverage": coverage}


def _group_rows(
    connection: sqlite3.Connection,
    filters: UsageFilters,
    *,
    group_by: str,
    sort: str,
    direction: str,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    expression = _GROUP_EXPRESSIONS[group_by]
    where, params = filters.where()
    total = int(
        connection.execute(
            f"SELECT COUNT(*) FROM (SELECT {expression} AS group_value {_JOIN}{where} "
            "GROUP BY group_value)",
            params,
        ).fetchone()[0]
        or 0
    )
    if total == 0:
        offset = 0
    elif offset >= total:
        offset = ((total - 1) // limit) * limit
    order_column = _SORT_EXPRESSIONS[sort]
    # NULLs always sort last, whichever direction is asked for: an
    # unavailable value is not a small value, and floating it to the top of a
    # "most expensive first" list would be actively misleading.
    order = f"{order_column} IS NULL, {order_column} {direction.upper()}, group_value ASC"
    # The issue grouping needs the issue's own context (title, URL, execution)
    # for its label and cross-links; MAX picks one deterministically.
    rows = connection.execute(
        f"SELECT {expression} AS group_value, "
        f"COUNT(DISTINCT {_ISSUE_KEY}) AS issues, COUNT(*) AS invocations, "
        f"SUM({_PRICED_COST}) AS estimated_cost, COUNT({_PRICED_COST}), "
        f"{_token_select()}, {_COVERAGE_SELECT}, "
        "MAX(COALESCE(e.issue_title, '')), "
        f"MAX({_REPOSITORY}), MAX({_ISSUE_NUMBER}), MAX(COALESCE(e.issue_url, '')), "
        "MAX(COALESCE(u.execution_id, '')) "
        f"{_JOIN}{where} GROUP BY group_value ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    results = []
    for row in rows:
        tokens, index = _token_values(row, 5)
        coverage, index = _coverage_values(row, index)
        entry: dict[str, Any] = {
            "group": str(row[0] or ""),
            "issues": int(row[1] or 0),
            "invocations": int(row[2] or 0),
            "estimatedCost": _optional_float(row[3]),
            "pricedInvocations": int(row[4] or 0),
            "coverage": coverage,
            "issueTitle": str(row[index] or ""),
            "repository": str(row[index + 1] or ""),
            "issueNumber": int(row[index + 2] or 0),
            "issueUrl": str(row[index + 3] or ""),
            "executionId": str(row[index + 4] or ""),
        }
        entry.update(tokens)
        results.append(entry)
    return {"rows": results, "total": total, "offset": offset, "limit": limit}


_DETAIL_COLUMNS = (
    "u.id",
    "u.execution_id",
    f"{_REPOSITORY}",
    f"{_ISSUE_NUMBER}",
    "COALESCE(e.issue_title, '')",
    "COALESCE(e.issue_url, '')",
    "COALESCE(e.final_status, '')",
    _GRADE,
    "u.agent_type",
    "u.prompt_type",
    "u.provider",
    "u.model",
    "u.reasoning_effort",
    "u.attempt_number",
    "u.input_tokens",
    "u.cached_input_tokens",
    "u.cache_read_tokens",
    "u.cache_write_tokens",
    "u.reasoning_tokens",
    "u.output_tokens",
    "u.total_tokens",
    "u.estimated_cost",
    "u.currency",
    "u.pricing_status",
    "u.pricing_version",
    "u.pricing_rate_id",
    "u.pricing_source",
    "u.duration_ms",
    "u.success",
    "u.error_type",
    _ACTIVITY,
    "u.completed_at",
    _COVERAGE,
)

_DETAIL_KEYS = (
    "id",
    "executionId",
    "repository",
    "issueNumber",
    "issueTitle",
    "issueUrl",
    "executionStatus",
    "grade",
    "agentType",
    "promptType",
    "provider",
    "model",
    "reasoningEffort",
    "attemptNumber",
    "inputTokens",
    "cachedInputTokens",
    "cacheReadTokens",
    "cacheWriteTokens",
    "reasoningTokens",
    "outputTokens",
    "totalTokens",
    "estimatedCost",
    "currency",
    "pricingStatus",
    "pricingVersion",
    "pricingRateId",
    "pricingSource",
    "durationMs",
    "success",
    "errorType",
    "startedAt",
    "completedAt",
    "coverage",
)

_DETAIL_INT_KEYS = {
    "issueNumber",
    "attemptNumber",
    "inputTokens",
    "cachedInputTokens",
    "cacheReadTokens",
    "cacheWriteTokens",
    "reasoningTokens",
    "outputTokens",
    "totalTokens",
    "durationMs",
}


def _detail_row(row: Sequence[Any]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in zip(_DETAIL_KEYS, row):
        if key in _DETAIL_INT_KEYS:
            record[key] = _optional_int(value)
        elif key == "estimatedCost":
            record[key] = _optional_float(value)
        elif key == "success":
            record[key] = bool(value)
        else:
            record[key] = "" if value is None else str(value)
    # ``issueNumber`` is a real identifier rather than a measurement; 0 means
    # "not linked to an issue", not "issue zero".
    if not record.get("issueNumber"):
        record["issueNumber"] = 0
    if not record.get("attemptNumber"):
        record["attemptNumber"] = 1
    return record


def _detail_rows(
    connection: sqlite3.Connection,
    filters: UsageFilters,
    *,
    group_by: str,
    group_value: str | None,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    where, params = filters.where()
    if group_value is not None:
        expression = _GROUP_EXPRESSIONS[group_by]
        joiner = " AND " if where else " WHERE "
        where = f"{where}{joiner}{expression} = ?"
        params = [*params, group_value]
    total = int(
        connection.execute(f"SELECT COUNT(*) {_JOIN}{where}", params).fetchone()[0] or 0
    )
    if total == 0:
        offset = 0
    elif offset >= total:
        offset = ((total - 1) // limit) * limit
    rows = connection.execute(
        f"SELECT {', '.join(_DETAIL_COLUMNS)} {_JOIN}{where} "
        f"ORDER BY {_ACTIVITY} DESC, u.rowid DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return {
        "rows": [_detail_row(row) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
        "groupValue": group_value,
    }


def _facet(
    connection: sqlite3.Connection,
    expression: str,
    where: str,
    params: Sequence[Any],
) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"SELECT {expression} AS value, COUNT(*) {_JOIN}{where} "
        "GROUP BY value HAVING value <> '' ORDER BY COUNT(*) DESC, value ASC LIMIT ?",
        (*params, FACET_LIMIT),
    ).fetchall()
    return [{"value": str(row[0]), "count": int(row[1] or 0)} for row in rows]


def _facets(connection: sqlite3.Connection, filters: UsageFilters) -> dict[str, Any]:
    clause, params = filters.repository_clause()
    where = f" WHERE {clause}" if clause else ""
    issues = connection.execute(
        f"SELECT {_ISSUE_NUMBER} AS number, MAX({_REPOSITORY}), "
        "MAX(COALESCE(e.issue_title, '')), MAX(COALESCE(e.issue_url, '')), COUNT(*) "
        f"{_JOIN}{where} GROUP BY number, {_REPOSITORY} HAVING number > 0 "
        f"ORDER BY MAX({_ACTIVITY}) DESC LIMIT ?",
        (*params, FACET_LIMIT),
    ).fetchall()
    return {
        "repositories": _facet(connection, _REPOSITORY, where, params),
        "providers": _facet(connection, "COALESCE(u.provider, '')", where, params),
        "models": _facet(connection, "COALESCE(u.model, '')", where, params),
        "agentTypes": _facet(connection, "COALESCE(u.agent_type, '')", where, params),
        "promptTypes": _facet(connection, "COALESCE(u.prompt_type, '')", where, params),
        "efforts": _facet(connection, "COALESCE(u.reasoning_effort, '')", where, params),
        "grades": _facet(connection, _GRADE, where, params),
        "issues": [
            {
                "number": int(row[0] or 0),
                "repository": str(row[1] or ""),
                "title": str(row[2] or ""),
                "url": str(row[3] or ""),
                "count": int(row[4] or 0),
            }
            for row in issues
        ],
        "coverage": list(COVERAGE_STATUSES),
        "groupBy": list(GROUP_BY_KEYS),
    }


def _availability(connection: sqlite3.Connection, filters: UsageFilters) -> dict[str, Any]:
    """Whether there is *any* usage, and any execution, in these repositories.

    This is what lets the UI tell three different empty states apart: a
    database with no telemetry at all (explain that usage is recorded for
    local runs from #280 onward), executions that predate or never produced
    telemetry (*usage unavailable*), and a filter that simply matches nothing.
    """
    clause, params = filters.repository_clause()
    usage_where = f" WHERE {clause}" if clause else ""
    usage = int(
        connection.execute(f"SELECT COUNT(*) {_JOIN}{usage_where}", params).fetchone()[0] or 0
    )
    execution_clause = ""
    execution_params: list[Any] = []
    if filters.repositories:
        slots = ", ".join("?" for _ in filters.repositories)
        execution_clause = f" WHERE repository IN ({slots})"
        execution_params = list(filters.repositories)
    executions = int(
        connection.execute(
            f"SELECT COUNT(*) FROM ai_executions{execution_clause}", execution_params
        ).fetchone()[0]
        or 0
    )
    without_usage = int(
        connection.execute(
            "SELECT COUNT(*) FROM ai_executions e"
            f"{execution_clause} "
            f"{'AND' if execution_clause else 'WHERE'} NOT EXISTS ("
            "SELECT 1 FROM ai_token_usage u WHERE u.execution_id = e.execution_id)",
            execution_params,
        ).fetchone()[0]
        or 0
    )
    return {
        "hasAnyUsage": usage > 0,
        "hasAnyActivity": executions > 0,
        "executionsWithoutUsage": without_usage,
    }


def build_usage_report(
    connection: sqlite3.Connection,
    filters: UsageFilters,
    *,
    group_by: str = "issue",
    sort: str = "cost",
    direction: str = "desc",
    group_offset: int = 0,
    detail_offset: int = 0,
    limit: int = USAGE_PAGE_SIZE,
    group_value: str | None = None,
    include_details: bool = True,
) -> dict[str, Any]:
    """The whole Usage & cost payload for one filter/grouping selection.

    Read-only by construction: every statement here is a ``SELECT``. Nothing
    in this module writes, recosts or repairs a row, so a report can never
    alter the telemetry it is describing.
    """
    group_by = normalize_group_by(group_by)
    sort = normalize_sort(sort)
    direction = normalize_direction(direction)
    limit = clamp_limit(limit)
    totals = _summary(connection, filters)
    payload: dict[str, Any] = {
        "groupBy": group_by,
        "sort": sort,
        "direction": direction,
        "filters": filters.to_dict(),
        "groups": _group_rows(
            connection,
            filters,
            group_by=group_by,
            sort=sort,
            direction=direction,
            offset=clamp_offset(group_offset),
            limit=limit,
        ),
        "invocations": (
            _detail_rows(
                connection,
                filters,
                group_by=group_by,
                group_value=group_value,
                offset=clamp_offset(detail_offset),
                limit=limit,
            )
            if include_details
            else {"rows": [], "total": 0, "offset": 0, "limit": limit, "groupValue": group_value}
        ),
        "facets": _facets(connection, filters),
    }
    payload.update(totals)
    payload.update(_availability(connection, filters))
    return payload


def _batches(values: list[str]) -> list[list[str]]:
    return [values[index:index + _ID_BATCH] for index in range(0, len(values), _ID_BATCH)]


def usage_summaries_for_executions(
    connection: sqlite3.Connection, execution_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Per-execution usage headline for the Execution History cards.

    One grouped query for the whole page rather than one per card. An
    execution with no recorded usage is simply absent from the result, which
    is what the card renders as *usage unavailable* — deliberately different
    from an execution that recorded invocations costing nothing.
    """
    ids = [str(value) for value in execution_ids if value]
    if not ids:
        return {}
    rows = []
    for batch in _batches(ids):
        slots = ", ".join("?" for _ in batch)
        rows.extend(
            connection.execute(
                "SELECT u.execution_id, COUNT(*), SUM(u.total_tokens), COUNT(u.total_tokens), "
                f"{_COST_SELECT}, "
                f"{_COVERAGE_SELECT} "
                f"{_JOIN} WHERE u.execution_id IN ({slots}) GROUP BY u.execution_id",
                batch,
            ).fetchall()
        )
    summaries: dict[str, dict[str, Any]] = {}
    for row in rows:
        coverage, _ = _coverage_values(row, 6)
        summaries[str(row[0])] = {
            "invocations": int(row[1] or 0),
            "totalTokens": _optional_int(row[2]),
            "totalTokensReported": int(row[3] or 0),
            "estimatedCost": _optional_float(row[4]),
            "pricedInvocations": int(row[5] or 0),
            "coverage": coverage,
        }
    return summaries


def usage_records_for_executions(
    connection: sqlite3.Connection, execution_ids: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    """Every invocation of the given executions, oldest first.

    Bounded by the caller: Execution History only ever asks for the ten
    executions on the page it is about to draw.
    """
    ids = [str(value) for value in execution_ids if value]
    if not ids:
        return {}
    rows = []
    for batch in _batches(ids):
        slots = ", ".join("?" for _ in batch)
        rows.extend(
            connection.execute(
                f"SELECT {', '.join(_DETAIL_COLUMNS)} {_JOIN} "
                f"WHERE u.execution_id IN ({slots}) "
                f"ORDER BY u.execution_id, {_ACTIVITY}, u.rowid",
                batch,
            ).fetchall()
        )
    records: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        record = _detail_row(row)
        records.setdefault(record["executionId"], []).append(record)
    return records


def empty_report(group_by: str = "issue") -> dict[str, Any]:
    """The shape the desktop gets when the database file does not exist yet.

    Identical in shape to a real report so the UI has exactly one rendering
    path, and honest about why it is empty: no usage, and no activity either.
    """
    group_by = normalize_group_by(group_by)
    limit = USAGE_PAGE_SIZE
    return {
        "groupBy": group_by,
        "sort": "cost",
        "direction": "desc",
        "filters": UsageFilters().to_dict(),
        "summary": {
            "invocations": 0,
            "issues": 0,
            "executions": 0,
            "estimatedCost": None,
            "pricedInvocations": 0,
            "currency": "USD",
            "mixedCurrencies": False,
            **{f"{name}Tokens": None for name, _ in _TOKEN_COLUMNS},
            **{f"{name}Reported": 0 for name, _ in _TOKEN_COLUMNS},
        },
        "coverage": {status: 0 for status in COVERAGE_STATUSES},
        "groups": {"rows": [], "total": 0, "offset": 0, "limit": limit},
        "invocations": {
            "rows": [],
            "total": 0,
            "offset": 0,
            "limit": limit,
            "groupValue": None,
        },
        "facets": {
            "repositories": [],
            "providers": [],
            "models": [],
            "agentTypes": [],
            "promptTypes": [],
            "efforts": [],
            "grades": [],
            "issues": [],
            "coverage": list(COVERAGE_STATUSES),
            "groupBy": list(GROUP_BY_KEYS),
        },
        "hasAnyUsage": False,
        "hasAnyActivity": False,
        "executionsWithoutUsage": 0,
    }
