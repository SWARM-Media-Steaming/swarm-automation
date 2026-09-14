"""Durable, sanitized AI execution history for the issue worker.

The repository deliberately uses Python's built-in SQLite driver: it keeps the
worker dependency-free and gives callers a small persistence abstraction that
can also support a future prompt-feedback uploader.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
PROMPT_TEMPLATE_VERSION = "issue-worker-v1"

# Columns persisted as JSON-encoded text; the desktop UI wants them decoded
# back into real arrays/objects rather than doubly-encoded strings.
_JSON_COLUMNS = (
    "reasoning_config",
    "files_changed",
    "commit_shas",
    "operational_notes",
    "warnings_errors",
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
                    (SCHEMA_VERSION,),
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
                    application_version, prompt_template_version, updated_at, operational_notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?, ?)""",
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
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unsupported execution fields: {sorted(unknown)}")
        serialized: dict[str, Any] = {}
        for key, value in fields.items():
            if key in {"files_changed", "commit_shas", "operational_notes", "warnings_errors"}:
                serialized[key] = json.dumps(sanitize_values(value))
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
        """Every execution for one repository, newest first — the desktop UI's feed."""
        with self.connect() as database:
            return list(
                database.execute(
                    "SELECT * FROM ai_executions WHERE repository = ? "
                    "ORDER BY started_at DESC, attempt_number DESC",
                    (repository,),
                )
            )


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
    return record


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


def main(argv: list[str] | None = None) -> int:
    """`python3 ai_execution_history.py --db PATH --repository OWNER/NAME`.

    Prints the repository's executions as a JSON array on stdout — the
    desktop app's Feedback view shells out to this the same way it already
    shells out to the other worker scripts for one-off, read-only queries.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to the SQLite database file.")
    parser.add_argument("--repository", required=True, help="owner/name to filter by.")
    args = parser.parse_args(argv)

    database_path = Path(args.db).expanduser()
    if not database_path.is_file():
        print("[]")
        return 0

    repository = ExecutionHistoryRepository(database_path)
    rows = repository.for_repository(sanitize_text(args.repository))
    json.dump([row_to_dict(row) for row in rows], sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
