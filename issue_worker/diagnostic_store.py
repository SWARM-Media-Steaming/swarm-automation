"""Durable storage for the "What's wrong?" diagnostic explainer (``diagnose.py``).

Shares the same SQLite file as ``ai_execution_history.py`` (both live under
the worker's state directory) but owns its own table and its own
migrations-tracking table (``diagnostic_schema_migrations``, not
``schema_migrations``), so the two modules' schemas evolve independently
without coordinating version numbers. A diagnostic explanation is not an
issue-delivery execution — it usually has no issue number, no branch, no
commit — so it does not belong in ``ai_executions``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from ai_execution_history import sanitize_text, sanitize_values


@dataclasses.dataclass(frozen=True)
class DiagnosticProblem:
    """One thing the playbook found wrong (or confirmed healthy), ready to persist."""

    repository: str
    signature: str
    source: str  # "canned" | "cache" | "ai" | "unavailable"
    provider: str = ""
    model: str = ""
    explanation: str = ""
    confidence: str = ""
    evidence: tuple[dict[str, str], ...] = ()
    actionable_items: tuple[str, ...] = ()
    is_bug: bool = False
    suggested_issue_title: str = ""
    suggested_issue_body: str = ""


class DiagnosticRepository:
    """SQLite repository for the diagnostic explainer's findings."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def migrate(self) -> None:
        with self.connect() as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS diagnostic_schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            applied = {
                row[0] for row in database.execute("SELECT version FROM diagnostic_schema_migrations")
            }
            if 1 not in applied:
                database.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS diagnostic_explanations (
                        problem_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        repository TEXT NOT NULL,
                        signature TEXT NOT NULL,
                        source TEXT NOT NULL,
                        provider TEXT NOT NULL DEFAULT '',
                        model TEXT NOT NULL DEFAULT '',
                        explanation TEXT NOT NULL DEFAULT '',
                        confidence TEXT NOT NULL DEFAULT '',
                        evidence TEXT NOT NULL DEFAULT '[]',
                        actionable_items TEXT NOT NULL DEFAULT '[]',
                        is_bug INTEGER NOT NULL DEFAULT 0,
                        suggested_issue_title TEXT NOT NULL DEFAULT '',
                        suggested_issue_body TEXT NOT NULL DEFAULT '',
                        filed_issue_url TEXT,
                        filed_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS diagnostic_explanations_signature_idx
                        ON diagnostic_explanations(signature, created_at);
                    CREATE INDEX IF NOT EXISTS diagnostic_explanations_run_idx
                        ON diagnostic_explanations(run_id);
                    """
                )
                database.execute(
                    "INSERT OR IGNORE INTO diagnostic_schema_migrations(version) VALUES (?)", (1,)
                )

    def insert(self, run_id: str, created_at: str, problem: DiagnosticProblem) -> str:
        problem_id = str(uuid.uuid4())
        evidence = [
            {"source": sanitize_text(entry.get("source", "")), "excerpt": sanitize_text(entry.get("excerpt", ""))}
            for entry in problem.evidence
        ]
        with self.connect() as database:
            database.execute(
                """INSERT INTO diagnostic_explanations (
                    problem_id, run_id, created_at, repository, signature, source,
                    provider, model, explanation, confidence, evidence, actionable_items,
                    is_bug, suggested_issue_title, suggested_issue_body
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    problem_id, run_id, created_at,
                    sanitize_text(problem.repository), problem.signature, problem.source,
                    sanitize_text(problem.provider), sanitize_text(problem.model),
                    sanitize_text(problem.explanation), problem.confidence,
                    json.dumps(evidence),
                    json.dumps(sanitize_values(problem.actionable_items)),
                    1 if problem.is_bug else 0,
                    sanitize_text(problem.suggested_issue_title),
                    sanitize_text(problem.suggested_issue_body),
                ),
            )
        return problem_id

    def find_by_signature(self, signature: str, max_age_seconds: int = 900) -> dict[str, Any] | None:
        """The newest explanation for this exact problem, if it's still fresh.

        The cutoff is computed in Python (not SQLite's ``datetime('now')``,
        which is UTC) because ``created_at`` is written with
        ``iso_timestamp()``'s local-timezone offset; both sides of the
        comparison must share that same convention to compare correctly as
        plain strings.
        """
        cutoff = (dt.datetime.now().astimezone() - dt.timedelta(seconds=max_age_seconds)).isoformat(
            timespec="seconds"
        )
        with self.connect() as database:
            row = database.execute(
                "SELECT * FROM diagnostic_explanations WHERE signature = ? AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT 1",
                (signature, cutoff),
            ).fetchone()
        return row_to_dict(row) if row else None

    def get(self, problem_id: str) -> dict[str, Any] | None:
        with self.connect() as database:
            row = database.execute(
                "SELECT * FROM diagnostic_explanations WHERE problem_id = ?", (problem_id,)
            ).fetchone()
        return row_to_dict(row) if row else None

    def mark_filed(self, problem_id: str, issue_url: str, filed_at: str) -> None:
        with self.connect() as database:
            database.execute(
                "UPDATE diagnostic_explanations SET filed_issue_url = ?, filed_at = ? WHERE problem_id = ?",
                (issue_url, filed_at, problem_id),
            )


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """A `sqlite3.Row` as a plain, JSON-ready dict with JSON text columns decoded."""
    record = dict(row)
    for column in ("evidence", "actionable_items"):
        raw = record.get(column)
        if isinstance(raw, str) and raw:
            try:
                record[column] = json.loads(raw)
            except ValueError:
                pass
    record["is_bug"] = bool(record.get("is_bug"))
    return record
