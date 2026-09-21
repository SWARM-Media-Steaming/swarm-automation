"""Durable, sanitized AI execution history for the issue worker.

The repository deliberately uses Python's built-in SQLite driver: it keeps the
worker dependency-free and gives callers a small persistence abstraction that
can also support a future prompt-feedback uploader.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import re
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 2
PROMPT_TEMPLATE_VERSION = "issue-worker-v1"
# Feedback shows one page of executions. Callers cannot raise this to dump
# the whole history through the paged query.
PAGE_SIZE = 10
_SEARCHABLE_TEXT_COLUMNS = (
    "issue_title",
    "ai_provider",
    "model",
    "branch_name",
    "final_status",
)

# Columns persisted as JSON-encoded text; the desktop UI wants them decoded
# back into real arrays/objects rather than doubly-encoded strings.
_JSON_COLUMNS = (
    "reasoning_config",
    "files_changed",
    "commit_shas",
    "operational_notes",
    "warnings_errors",
    "routing_decision",
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|token)\s+)[^\s]+"),
    re.compile(
        r"(?i)\b((?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
        r"\s*[=:]\s*)[^\s,;]+"
    ),
    re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
        r"sk-[A-Za-z0-9_-]{20,})\b"
    ),
    re.compile(
        r"-----BEGIN [^-]*(?:PRIVATE KEY|CREDENTIALS)[^-]*-----.*?-----END [^-]*-----",
        re.DOTALL,
    ),
)


def sanitize_text(value: Any) -> str:
    """Remove common credentials from arbitrary persisted/uploaded text."""
    text = "" if value is None else str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(
            lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]",
            text,
        )
    return text


def sanitize_values(values: Iterable[Any]) -> list[str]:
    return [sanitize_text(value) for value in values]


def normalize_search(search: str) -> str:
    """Trim a Feedback search and cap its length."""
    return str(search or "").strip()[:200]


def _search_filter(search: str) -> tuple[str, list[str]]:
    """SQL fragment for the Feedback search box.

    The term must sit inside issue number, title, provider, model, branch, or
    status. `%`, `_`, and `\\` are matched literally. Issue body and prompt
    text are not searched.
    """
    term = normalize_search(search)
    if not term:
        return "", []
    escaped = term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    clauses = ["CAST(issue_number AS TEXT) LIKE ? ESCAPE '\\'"]
    params = [pattern]
    for column in _SEARCHABLE_TEXT_COLUMNS:
        clauses.append(f"LOWER({column}) LIKE ? ESCAPE '\\'")
        params.append(pattern)
    return f" AND ({' OR '.join(clauses)})", params


def clamp_page_size(limit: int) -> int:
    try:
        size = int(limit)
    except (TypeError, ValueError):
        return PAGE_SIZE
    if size < 1:
        return PAGE_SIZE
    return min(size, PAGE_SIZE)


def clamp_offset(offset: int) -> int:
    try:
        value = int(offset)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


@dataclasses.dataclass(frozen=True)
class ExecutionStart:
    repository: str
    issue_number: int
    issue_url: str
    issue_title: str
    issue_body: str
    provider: str
    model: str
    effort: str
    branch_name: str
    application_version: str
    prompt_template_version: str = PROMPT_TEMPLATE_VERSION
    routing_decision: dict[str, Any] | None = None


class ExecutionHistoryRepository:
    """SQLite repository for lifecycle updates and future upload selection."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def migrate(self) -> None:
        with self.connect() as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            applied = {row[0] for row in database.execute("SELECT version FROM schema_migrations")}
            if 1 not in applied:
                database.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS ai_executions (
                        execution_id TEXT PRIMARY KEY,
                        repository TEXT NOT NULL,
                        issue_number INTEGER NOT NULL,
                        issue_url TEXT NOT NULL DEFAULT '',
                        issue_title TEXT NOT NULL,
                        original_issue_body TEXT NOT NULL,
                        effective_prompt TEXT NOT NULL DEFAULT '',
                        ai_provider TEXT NOT NULL,
                        model TEXT NOT NULL DEFAULT '',
                        effort TEXT NOT NULL DEFAULT '',
                        reasoning_config TEXT NOT NULL DEFAULT '{}',
                        started_at TEXT NOT NULL,
                        completed_at TEXT,
                        duration_seconds REAL,
                        requested_work_summary TEXT NOT NULL DEFAULT '',
                        changes_summary TEXT NOT NULL DEFAULT '',
                        files_changed TEXT NOT NULL DEFAULT '[]',
                        branch_name TEXT NOT NULL DEFAULT '',
                        commit_shas TEXT NOT NULL DEFAULT '[]',
                        pull_request_number INTEGER,
                        pull_request_url TEXT NOT NULL DEFAULT '',
                        operational_notes TEXT NOT NULL DEFAULT '[]',
                        warnings_errors TEXT NOT NULL DEFAULT '[]',
                        final_status TEXT NOT NULL,
                        attempt_number INTEGER NOT NULL,
                        application_version TEXT NOT NULL DEFAULT '',
                        prompt_template_version TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL,
                        uploaded_at TEXT,
                        upload_status TEXT NOT NULL DEFAULT 'never_uploaded',
                        upload_error TEXT NOT NULL DEFAULT '',
                        uploaded_record_updated_at TEXT,
                        reviewer_feedback TEXT NOT NULL DEFAULT '',
                        reviewer_feedback_at TEXT,
                        UNIQUE(repository, issue_number, attempt_number)
                    );
                    CREATE INDEX IF NOT EXISTS ai_executions_issue_idx
                        ON ai_executions(repository, issue_number, attempt_number);
                    CREATE INDEX IF NOT EXISTS ai_executions_upload_idx
                        ON ai_executions(upload_status, updated_at);
                    """
                )
                database.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                    (1,),
                )
            if 2 not in applied and 2 not in {
                row[0] for row in database.execute("SELECT version FROM schema_migrations")
            }:
                columns = {
                    row[1] for row in database.execute("PRAGMA table_info(ai_executions)")
                }
                if "routing_decision" not in columns:
                    database.execute(
                        "ALTER TABLE ai_executions ADD COLUMN routing_decision "
                        "TEXT NOT NULL DEFAULT ''"
                    )
                database.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                    (2,),
                )

    def create(self, start: ExecutionStart, started_at: str) -> str:
        execution_id = str(uuid.uuid4())
        repository = sanitize_text(start.repository)
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            attempt = database.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM ai_executions "
                "WHERE repository = ? AND issue_number = ?",
                (repository, start.issue_number),
            ).fetchone()[0]
            database.execute(
                """INSERT INTO ai_executions (
                    execution_id, repository, issue_number, issue_url, issue_title,
                    original_issue_body, ai_provider, model, effort, reasoning_config,
                    started_at, branch_name, final_status, attempt_number,
                    application_version, prompt_template_version, updated_at, operational_notes,
                    routing_decision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?, ?, ?)""",
                (
                    execution_id,
                    repository,
                    start.issue_number,
                    sanitize_text(start.issue_url),
                    sanitize_text(start.issue_title),
                    sanitize_text(start.issue_body),
                    sanitize_text(start.provider),
                    sanitize_text(start.model),
                    sanitize_text(start.effort),
                    json.dumps({"effort": sanitize_text(start.effort)}),
                    started_at,
                    sanitize_text(start.branch_name),
                    attempt,
                    sanitize_text(start.application_version),
                    sanitize_text(start.prompt_template_version),
                    started_at,
                    json.dumps(["Issue accepted"]),
                    _serialize_routing(start.routing_decision),
                ),
            )
        return execution_id

    def existing_issue_numbers(self, repository: str) -> set[int]:
        """Issue numbers this repository already has at least one row for.

        Used to skip issues the worker (or a previous import) already
        tracked, so importing never creates a second, misleadingly "real"
        looking row alongside genuine execution history.
        """
        with self.connect() as database:
            rows = database.execute(
                "SELECT DISTINCT issue_number FROM ai_executions WHERE repository = ?",
                (repository,),
            )
            return {int(row[0]) for row in rows}

    def import_issue(self, repository: str, issue: dict[str, Any], imported_at: str) -> str:
        """Record one pre-existing GitHub issue as a synthetic history row.

        Distinct from ``create``: this never represents an actual AI run, so
        it always lands as attempt 1 with ``final_status = 'imported'`` and no
        provider/model/effort — those fields would otherwise misrepresent
        work the automation never did.
        """
        execution_id = str(uuid.uuid4())
        repository = sanitize_text(repository)
        number = int(issue["number"])
        state = str(issue.get("state") or "open").lower()
        closed_at = issue.get("closedAt") or issue.get("closed_at")
        completed_at = str(closed_at) if state == "closed" and closed_at else None
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            attempt = database.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM ai_executions "
                "WHERE repository = ? AND issue_number = ?",
                (repository, number),
            ).fetchone()[0]
            database.execute(
                """INSERT INTO ai_executions (
                    execution_id, repository, issue_number, issue_url, issue_title,
                    original_issue_body, ai_provider, started_at, completed_at,
                    final_status, attempt_number, updated_at, operational_notes
                ) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, 'imported', ?, ?, ?)""",
                (
                    execution_id,
                    repository,
                    number,
                    sanitize_text(issue.get("url") or issue.get("html_url") or ""),
                    sanitize_text(issue.get("title") or ""),
                    sanitize_text(issue.get("body") or ""),
                    str(issue.get("createdAt") or issue.get("created_at") or imported_at),
                    completed_at,
                    attempt,
                    imported_at,
                    json.dumps([f"Imported from existing GitHub issue (state: {state})."]),
                ),
            )
        return execution_id

    def update(self, execution_id: str, updated_at: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "effective_prompt",
            "completed_at",
            "duration_seconds",
            "requested_work_summary",
            "changes_summary",
            "files_changed",
            "branch_name",
            "commit_shas",
            "pull_request_number",
            "pull_request_url",
            "operational_notes",
            "warnings_errors",
            "final_status",
            "upload_status",
            "upload_error",
            "uploaded_at",
            "uploaded_record_updated_at",
            "reviewer_feedback",
            "reviewer_feedback_at",
            "routing_decision",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unsupported execution fields: {sorted(unknown)}")
        serialized: dict[str, Any] = {}
        for key, value in fields.items():
            if key in {"files_changed", "commit_shas", "operational_notes", "warnings_errors"}:
                serialized[key] = json.dumps(sanitize_values(value))
            elif key == "routing_decision":
                serialized[key] = _serialize_routing(value)
            elif isinstance(value, str):
                serialized[key] = sanitize_text(value)
            else:
                serialized[key] = value
        serialized["updated_at"] = updated_at
        assignments = ", ".join(f"{key} = ?" for key in serialized)
        with self.connect() as database:
            database.execute(
                f"UPDATE ai_executions SET {assignments} WHERE execution_id = ?",
                (*serialized.values(), execution_id),
            )

    def append(self, execution_id: str, column: str, message: str, updated_at: str) -> None:
        if column not in {"operational_notes", "warnings_errors"}:
            raise ValueError(f"Unsupported append column: {column}")
        with self.connect() as database:
            row = database.execute(
                f"SELECT {column} FROM ai_executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if row is None:
                return
            values = json.loads(row[0])
            values.append(sanitize_text(message))
            database.execute(
                f"UPDATE ai_executions SET {column} = ?, updated_at = ? WHERE execution_id = ?",
                (json.dumps(values), updated_at, execution_id),
            )

    def pending_upload(self) -> list[sqlite3.Row]:
        """Records never uploaded, changed after upload, or with a failed upload."""
        with self.connect() as database:
            return list(
                database.execute(
                    """SELECT * FROM ai_executions
                   WHERE upload_status IN ('never_uploaded', 'failed')
                      OR uploaded_record_updated_at IS NULL
                      OR updated_at > uploaded_record_updated_at
                   ORDER BY started_at"""
                )
            )

    def with_reviewer_feedback(self) -> list[sqlite3.Row]:
        with self.connect() as database:
            return list(
                database.execute(
                    "SELECT * FROM ai_executions WHERE reviewer_feedback_at IS NOT NULL "
                    "ORDER BY reviewer_feedback_at"
                )
            )

    def for_repository(self, repository: str) -> list[sqlite3.Row]:
        """Every execution for one repository, newest first.

        The Feedback view uses ``page_for_repository`` so it does not pull
        this full list into the desktop UI.
        """
        with self.connect() as database:
            return list(
                database.execute(
                    "SELECT * FROM ai_executions WHERE repository = ? "
                    "ORDER BY started_at DESC, attempt_number DESC",
                    (repository,),
                )
            )

    def page_for_repository(
        self,
        repository: str,
        *,
        search: str = "",
        limit: int = PAGE_SIZE,
        offset: int = 0,
    ) -> tuple[list[sqlite3.Row], int, int, int]:
        """One page of executions, newest first, plus the filtered total.

        ``LIMIT``/``OFFSET`` are applied in SQLite. An offset past the end is
        pulled back to the last page so the caller still receives rows that
        exist. Returns ``(rows, total, offset, limit)``.
        """
        limit = clamp_page_size(limit)
        offset = clamp_offset(offset)
        clause, params = _search_filter(search)
        with self.connect() as database:
            total = int(
                database.execute(
                    f"SELECT COUNT(*) FROM ai_executions WHERE repository = ?{clause}",
                    (repository, *params),
                ).fetchone()[0]
            )
            if total == 0:
                offset = 0
            elif offset >= total:
                offset = ((total - 1) // limit) * limit
            rows = list(
                database.execute(
                    "SELECT * FROM ai_executions WHERE repository = ?"
                    f"{clause} ORDER BY started_at DESC, attempt_number DESC "
                    "LIMIT ? OFFSET ?",
                    (repository, *params, limit, offset),
                )
            )
        return rows, total, offset, limit


def _serialize_routing(value: Any) -> str:
    if not value:
        return ""
    if not isinstance(value, dict):
        raise ValueError("routing_decision must be an object")
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        cleaned[str(key)] = sanitize_text(item) if isinstance(item, str) else item
    return json.dumps(cleaned)


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """A `sqlite3.Row` as a plain, JSON-ready dict with JSON text columns decoded."""
    record = dict(row)
    for column in _JSON_COLUMNS:
        raw = record.get(column)
        if isinstance(raw, str) and raw:
            try:
                record[column] = json.loads(raw)
            except ValueError:
                pass
    if record.get("routing_decision") == "":
        record["routing_decision"] = None
    return record


def fetch_github_issues(gh_bin: str, repository: str) -> list[dict[str, Any]]:
    """Every open and closed issue (never pull requests — `gh issue list`
    already excludes those) for ``repository``, via the operator's own `gh`
    login rather than a provider's GitHub App bot — the same plain-`gh`
    pattern the desktop app already uses for other one-off, read-only GitHub
    lookups.
    """
    result = subprocess.run(
        [
            gh_bin, "issue", "list",
            "--repo", repository,
            "--state", "all",
            "--limit", "1000",
            "--json", "number,title,url,body,state,createdAt,closedAt",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(sanitize_text((result.stderr or result.stdout).strip() or "gh issue list failed"))
    return json.loads(result.stdout or "[]")


def import_missing_issues(
    repository: ExecutionHistoryRepository,
    repository_name: str,
    issues: list[dict[str, Any]],
    imported_at: str | None = None,
) -> dict[str, int]:
    """Add a synthetic `imported` row for every issue with no existing row.

    Issues already tracked (any attempt, any status) are left untouched —
    importing must never shadow or duplicate genuine execution history.
    """
    imported_at = imported_at or dt.datetime.now().astimezone().isoformat(timespec="seconds")
    existing = repository.existing_issue_numbers(repository_name)
    imported = 0
    for issue in issues:
        if int(issue["number"]) in existing:
            continue
        repository.import_issue(repository_name, issue, imported_at)
        imported += 1
    return {
        "totalIssues": len(issues),
        "imported": imported,
        "skipped": len(issues) - imported,
    }


class ExecutionHistoryService:
    """Optional facade so disabled history cannot affect issue processing."""

    def __init__(self, enabled: bool, database_path: Path) -> None:
        self.error = ""
        try:
            self.repository = ExecutionHistoryRepository(database_path) if enabled else None
        except sqlite3.Error as error:
            # History is observability, never a reason to change issue delivery.
            self.repository = None
            self.error = sanitize_text(error)
        self.execution_id = ""

    def start(self, value: ExecutionStart, now: str, existing_id: str = "") -> str:
        if not self.repository:
            return ""
        try:
            if existing_id:
                self.execution_id = existing_id
                self.note("Execution resumed", now)
            else:
                self.execution_id = self.repository.create(value, now)
        except sqlite3.Error as error:
            self.error = sanitize_text(error)
            self.execution_id = ""
        return self.execution_id

    def update(self, now: str, **fields: Any) -> None:
        if self.repository and self.execution_id:
            try:
                self.repository.update(self.execution_id, now, **fields)
            except sqlite3.Error as error:
                self.error = sanitize_text(error)

    def note(self, message: str, now: str) -> None:
        if self.repository and self.execution_id:
            try:
                self.repository.append(self.execution_id, "operational_notes", message, now)
            except sqlite3.Error as error:
                self.error = sanitize_text(error)

    def warning(self, message: str, now: str) -> None:
        if self.repository and self.execution_id:
            try:
                self.repository.append(self.execution_id, "warnings_errors", message, now)
            except sqlite3.Error as error:
                self.error = sanitize_text(error)


def _page_requested(limit: int | None, offset: int, search: str) -> bool:
    return limit is not None or offset != 0 or bool(normalize_search(search))


def _empty_page() -> dict[str, Any]:
    return {"records": [], "total": 0, "offset": 0, "limit": PAGE_SIZE}


def main(argv: list[str] | None = None) -> int:
    """`python3 ai_execution_history.py --db PATH --repository OWNER/NAME`.

    Without paging flags, prints the repository's executions as a JSON array
    on stdout. `--limit`, `--offset`, or `--search` instead print one page
    object (`records`/`total`/`offset`/`limit`) of at most 10 rows — the
    desktop Feedback view always uses that form so it never receives the
    full history.

    `--import-from-github` instead scans that repository's full GitHub issue
    backlog (open and closed) and adds a synthetic `imported` row for any
    issue with no existing execution history row, printing a JSON summary
    object (`totalIssues`/`imported`/`skipped`) instead of the row array —
    the Feedback view's "Import from GitHub" action.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to the SQLite database file.")
    parser.add_argument("--repository", required=True, help="owner/name to filter by.")
    parser.add_argument(
        "--import-from-github",
        action="store_true",
        help="Import open/closed GitHub issues with no existing history row, then exit.",
    )
    parser.add_argument(
        "--gh-bin", default="gh", help="Path to the gh CLI (only used with --import-from-github)."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"Page size. Clamped to {PAGE_SIZE}. Selects the paged JSON object.",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Number of matching executions to skip. Values past the end snap to the last page.",
    )
    parser.add_argument(
        "--search",
        default="",
        help="Case-insensitive match on issue number, title, provider, model, branch, or status.",
    )
    args = parser.parse_args(argv)

    database_path = Path(args.db).expanduser()
    repository_name = sanitize_text(args.repository)
    paging = _page_requested(args.limit, args.offset, args.search)

    if args.import_from_github:
        repository = ExecutionHistoryRepository(database_path)
        try:
            issues = fetch_github_issues(args.gh_bin, repository_name)
        except (RuntimeError, ValueError) as error:
            print(json.dumps({"error": str(error)}))
            return 1
        json.dump(import_missing_issues(repository, repository_name, issues), sys.stdout)
        return 0

    if not database_path.is_file():
        if paging:
            json.dump(_empty_page(), sys.stdout)
        else:
            print("[]")
        return 0

    repository = ExecutionHistoryRepository(database_path)
    if not paging:
        rows = repository.for_repository(repository_name)
        json.dump([row_to_dict(row) for row in rows], sys.stdout)
        return 0

    rows, total, offset, limit = repository.page_for_repository(
        repository_name,
        search=args.search,
        limit=PAGE_SIZE if args.limit is None else args.limit,
        offset=args.offset,
    )
    json.dump(
        {
            "records": [row_to_dict(row) for row in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
