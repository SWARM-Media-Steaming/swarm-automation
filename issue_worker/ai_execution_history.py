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
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = 5
PROMPT_TEMPLATE_VERSION = "issue-worker-v1"
# Feedback shows one page of executions. Callers cannot raise this to dump
# the whole history through the paged query.
PAGE_SIZE = 10
# Grade points on a 4.0 scale, best first. Keep the keys in sync with
# ``PROMPT_GRADES`` in ``dynamic_router.py`` (a test checks this).
GRADE_POINTS = {
    "A+": 4.0,
    "A": 4.0,
    "A-": 3.7,
    "B+": 3.3,
    "B": 3.0,
    "B-": 2.7,
    "C+": 2.3,
    "C": 2.0,
    "C-": 1.7,
    "D+": 1.3,
    "D": 1.0,
    "D-": 0.7,
    "F": 0.0,
}
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
    "adversarial_filed_findings",
    "security_findings",
    "security_filed_findings",
)

# The AI platform that graded and routed an issue, and the one that was picked
# to work it, as comparable lowercase provider keys. The worked platform comes
# from the decision when it is there and from the row's own provider column
# otherwise, so imported and pre-routing rows still line up.
_ROUTER_KEY_SQL = "LOWER(COALESCE(json_extract(routing_decision, '$.router_provider'), ''))"
_ROUTER_MODEL_SQL = "COALESCE(json_extract(routing_decision, '$.router_model'), '')"
_WORKED_KEY_SQL = (
    "LOWER(COALESCE(NULLIF(json_extract(routing_decision, '$.provider'), ''), ai_provider, ''))"
)

# Provider keys the router filter accepts. Anything else is treated as "no
# filter" rather than reaching SQL.
_PROVIDER_KEY_LIMIT = 40
_PROVIDER_KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*")

# Migration 3 columns on ``ai_executions``. ``capacity_consumed_percent`` is an
# honest approximation, not a metered cost: it is the drop in the provider's
# remaining-quota percentage between the start and the end of the work-round,
# read from the same ``*_capacity`` probes routing already performs. Nothing in
# this codebase meters tokens or dollars, so anything showing this value must
# say it is an approximation.
_MIGRATION_3_COLUMNS = (
    ("adversarial_round_count", "INTEGER NOT NULL DEFAULT 0"),
    ("adversarial_outcome", "TEXT NOT NULL DEFAULT ''"),
    ("capacity_consumed_percent", "REAL"),
)

# Migration 4 records the non-blocking issues independently filed by an
# adversarial tester.  Unlike the checkpoint marker list, this is user-facing
# execution history: each entry has the finding title and the resulting issue
# URL so it remains useful after the worker's in-progress state is gone.
_MIGRATION_4_COLUMNS = (
    ("adversarial_filed_findings", "TEXT NOT NULL DEFAULT '[]'"),
)

# Migration 5 adds the adversarial cybersecurity review alongside UAT. The two
# stages share one loop (``adversarial_core.py``) and therefore one round
# table; ``stage`` is what tells a UAT round apart from a security round, so
# the table's uniqueness moves from (execution, round) to
# (execution, stage, round) and the table has to be rebuilt to widen it.
# ``security_review_status`` is deliberately separate from
# ``security_outcome``: a review that could not execute must never be
# indistinguishable from one that ran and found nothing.
_MIGRATION_5_COLUMNS = (
    ("security_outcome", "TEXT NOT NULL DEFAULT ''"),
    ("security_review_status", "TEXT NOT NULL DEFAULT ''"),
    ("security_review_error", "TEXT NOT NULL DEFAULT ''"),
    ("security_round_count", "INTEGER NOT NULL DEFAULT 0"),
    ("security_findings", "TEXT NOT NULL DEFAULT '{}'"),
    ("security_filed_findings", "TEXT NOT NULL DEFAULT '[]'"),
)

# Every value ``security_review_status`` may hold. ``""`` means the stage never
# ran for this work-round. ``FAILED`` covers both a review that could not
# execute and one that ended with unresolved findings — the accompanying
# ``security_outcome``/``security_review_error`` say which.
SECURITY_REVIEW_STATUSES = (
    "PASS",
    "FIXED",
    "FINDINGS_CREATED",
    "FAILED",
)

# Every value ``adversarial_outcome`` may hold. ``""`` means the work-round
# predates the loop or never reached it; ``disabled`` means it ran with the
# setting off. The loop in ``adversarial_uat.py`` writes these outcomes.
ADVERSARIAL_OUTCOMES = (
    "clean_first_pass",
    "resolved_after_n",
    "cap_hit",
    "disabled",
)

#: Which adversarial agent produced a round row. ``uat`` is the historical
#: default so rows written before the cybersecurity stage existed keep meaning
#: exactly what they meant.
ADVERSARIAL_STAGE_SLUGS = ("uat", "security")

_ADVERSARIAL_ROUND_COLUMNS = (
    "stage",
    "round_number",
    "fixer_provider",
    "fixer_model",
    "tester_provider",
    "tester_model",
    "tests_added",
    "tests_modified",
    "tests_failing_before",
    "tests_failing_after",
    "disputed",
    "dispute_resolution",
    "started_at",
    "completed_at",
    "duration_seconds",
    "findings_found",
    "findings_fixed",
    "findings_filed",
    "severity_counts",
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


def normalize_grade(grade: str) -> str:
    """A router grade such as ``B-``, or ``""`` when the value is not one."""
    text = str(grade or "").strip()
    return text if text in GRADE_POINTS else ""


def normalize_provider_key(provider: str) -> str:
    """A provider key such as ``claude``, or ``""`` when it is not one.

    Provider keys come from ``KNOWN_PROVIDERS`` in the worker, so this only has
    to accept that shape rather than know the open set of names.
    """
    text = str(provider or "").strip().lower()[:_PROVIDER_KEY_LIMIT]
    return text if _PROVIDER_KEY_PATTERN.fullmatch(text) else ""


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


def _repository_filter(repositories: Sequence[str] | str | None) -> tuple[str, list[str]]:
    """Optional SQL predicate for one or more repositories.

    A string remains accepted for callers using the old single-repository API.
    An empty sequence deliberately returns no predicate: Feedback's default is
    the app-wide database, queried as one global aggregate.
    """
    values = [repositories] if isinstance(repositories, str) else list(repositories or [])
    names = list(dict.fromkeys(sanitize_text(value) for value in values if str(value).strip()))
    if not names:
        return "", []
    slots = ", ".join("?" for _ in names)
    return f"repository IN ({slots})", names


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
            if 3 not in applied and 3 not in {
                row[0] for row in database.execute("SELECT version FROM schema_migrations")
            }:
                columns = {
                    row[1] for row in database.execute("PRAGMA table_info(ai_executions)")
                }
                for name, definition in _MIGRATION_3_COLUMNS:
                    if name not in columns:
                        database.execute(
                            f"ALTER TABLE ai_executions ADD COLUMN {name} {definition}"
                        )
                database.executescript(
                    """
                    -- Created in the widened, stage-aware shape migration 5
                    -- introduced, so a database that first appears after that
                    -- migration never needs the rebuild below.
                    CREATE TABLE IF NOT EXISTS adversarial_rounds (
                        round_id TEXT PRIMARY KEY,
                        execution_id TEXT NOT NULL
                            REFERENCES ai_executions(execution_id) ON DELETE CASCADE,
                        stage TEXT NOT NULL DEFAULT 'uat',
                        round_number INTEGER NOT NULL,
                        fixer_provider TEXT NOT NULL DEFAULT '',
                        fixer_model TEXT NOT NULL DEFAULT '',
                        tester_provider TEXT NOT NULL DEFAULT '',
                        tester_model TEXT NOT NULL DEFAULT '',
                        tests_added INTEGER NOT NULL DEFAULT 0,
                        tests_modified INTEGER NOT NULL DEFAULT 0,
                        tests_failing_before INTEGER NOT NULL DEFAULT 0,
                        tests_failing_after INTEGER NOT NULL DEFAULT 0,
                        disputed INTEGER NOT NULL DEFAULT 0,
                        dispute_resolution TEXT NOT NULL DEFAULT '',
                        started_at TEXT NOT NULL DEFAULT '',
                        completed_at TEXT NOT NULL DEFAULT '',
                        duration_seconds REAL,
                        findings_found INTEGER NOT NULL DEFAULT 0,
                        findings_fixed INTEGER NOT NULL DEFAULT 0,
                        findings_filed INTEGER NOT NULL DEFAULT 0,
                        severity_counts TEXT NOT NULL DEFAULT '{}',
                        UNIQUE(execution_id, stage, round_number)
                    );
                    CREATE INDEX IF NOT EXISTS adversarial_rounds_execution_idx
                        ON adversarial_rounds(execution_id, stage, round_number);
                    """
                )
                database.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                    (3,),
                )
            if 4 not in applied and 4 not in {
                row[0] for row in database.execute("SELECT version FROM schema_migrations")
            }:
                columns = {
                    row[1] for row in database.execute("PRAGMA table_info(ai_executions)")
                }
                for name, definition in _MIGRATION_4_COLUMNS:
                    if name not in columns:
                        database.execute(
                            f"ALTER TABLE ai_executions ADD COLUMN {name} {definition}"
                        )
                database.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                    (4,),
                )
            if 5 not in applied and 5 not in {
                row[0] for row in database.execute("SELECT version FROM schema_migrations")
            }:
                columns = {
                    row[1] for row in database.execute("PRAGMA table_info(ai_executions)")
                }
                for name, definition in _MIGRATION_5_COLUMNS:
                    if name not in columns:
                        database.execute(
                            f"ALTER TABLE ai_executions ADD COLUMN {name} {definition}"
                        )
                round_columns = {
                    row[1] for row in database.execute("PRAGMA table_info(adversarial_rounds)")
                }
                if round_columns and "stage" not in round_columns:
                    # SQLite cannot widen a table-level UNIQUE constraint in
                    # place, and (execution_id, round_number) has to become
                    # (execution_id, stage, round_number) or a security round 0
                    # would collide with the UAT round 0 of the same execution.
                    # Rebuild, backfilling every existing row as a UAT round.
                    database.executescript(
                        """
                        CREATE TABLE adversarial_rounds_v5 (
                            round_id TEXT PRIMARY KEY,
                            execution_id TEXT NOT NULL
                                REFERENCES ai_executions(execution_id) ON DELETE CASCADE,
                            stage TEXT NOT NULL DEFAULT 'uat',
                            round_number INTEGER NOT NULL,
                            fixer_provider TEXT NOT NULL DEFAULT '',
                            fixer_model TEXT NOT NULL DEFAULT '',
                            tester_provider TEXT NOT NULL DEFAULT '',
                            tester_model TEXT NOT NULL DEFAULT '',
                            tests_added INTEGER NOT NULL DEFAULT 0,
                            tests_modified INTEGER NOT NULL DEFAULT 0,
                            tests_failing_before INTEGER NOT NULL DEFAULT 0,
                            tests_failing_after INTEGER NOT NULL DEFAULT 0,
                            disputed INTEGER NOT NULL DEFAULT 0,
                            dispute_resolution TEXT NOT NULL DEFAULT '',
                            started_at TEXT NOT NULL DEFAULT '',
                            completed_at TEXT NOT NULL DEFAULT '',
                            duration_seconds REAL,
                            findings_found INTEGER NOT NULL DEFAULT 0,
                            findings_fixed INTEGER NOT NULL DEFAULT 0,
                            findings_filed INTEGER NOT NULL DEFAULT 0,
                            severity_counts TEXT NOT NULL DEFAULT '{}',
                            UNIQUE(execution_id, stage, round_number)
                        );
                        INSERT INTO adversarial_rounds_v5 (
                            round_id, execution_id, stage, round_number, fixer_provider,
                            fixer_model, tester_provider, tester_model, tests_added,
                            tests_modified, tests_failing_before, tests_failing_after,
                            disputed, dispute_resolution, started_at, completed_at,
                            duration_seconds
                        )
                        SELECT round_id, execution_id, 'uat', round_number, fixer_provider,
                               fixer_model, tester_provider, tester_model, tests_added,
                               tests_modified, tests_failing_before, tests_failing_after,
                               disputed, dispute_resolution, started_at, completed_at,
                               duration_seconds
                        FROM adversarial_rounds;
                        DROP TABLE adversarial_rounds;
                        ALTER TABLE adversarial_rounds_v5 RENAME TO adversarial_rounds;
                        CREATE INDEX IF NOT EXISTS adversarial_rounds_execution_idx
                            ON adversarial_rounds(execution_id, stage, round_number);
                        """
                    )
                database.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                    (5,),
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
            "adversarial_round_count",
            "adversarial_outcome",
            "capacity_consumed_percent",
            "adversarial_filed_findings",
            "security_outcome",
            "security_review_status",
            "security_review_error",
            "security_round_count",
            "security_findings",
            "security_filed_findings",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unsupported execution fields: {sorted(unknown)}")
        serialized: dict[str, Any] = {}
        for key, value in fields.items():
            if key in {"files_changed", "commit_shas", "operational_notes", "warnings_errors"}:
                serialized[key] = json.dumps(sanitize_values(value))
            elif key in {"adversarial_filed_findings", "security_filed_findings"}:
                serialized[key] = _serialize_adversarial_filed_findings(value)
            elif key == "security_findings":
                serialized[key] = _serialize_security_findings(value)
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

    def final_statuses_for_issue(self, repository: str, issue_number: int) -> list[str]:
        """`final_status` of every recorded attempt for one issue, newest first.

        Used by the worker's orphan-branch reconciliation to tell a terminal
        no-code outcome from a code completion or a run still in flight."""
        with self.connect() as database:
            return [
                str(row[0])
                for row in database.execute(
                    "SELECT final_status FROM ai_executions "
                    "WHERE repository = ? AND issue_number = ? "
                    "ORDER BY attempt_number DESC",
                    (sanitize_text(repository), int(issue_number)),
                )
            ]

    def record_adversarial_round(self, execution_id: str, round_values: dict[str, Any]) -> None:
        """Insert (or replace) one adversarial fix/test round of an execution.

        A round is identified by ``(execution_id, round_number)`` so a retried
        scheduler tick that re-reports the same round updates it rather than
        appending a duplicate — the same idempotency rule the issue comments
        follow.
        """
        values: dict[str, Any] = {}
        for column in _ADVERSARIAL_ROUND_COLUMNS:
            value = round_values.get(column)
            if column in {"round_number", "tests_added", "tests_modified",
                          "tests_failing_before", "tests_failing_after",
                          "findings_found", "findings_fixed", "findings_filed"}:
                values[column] = int(value or 0)
            elif column == "disputed":
                values[column] = 1 if value else 0
            elif column == "duration_seconds":
                values[column] = None if value is None else float(value)
            elif column == "severity_counts":
                values[column] = sanitize_text(value) or "{}"
            elif column == "stage":
                values[column] = sanitize_text(value) or "uat"
            else:
                values[column] = sanitize_text(value)
        if values["stage"] not in ADVERSARIAL_STAGE_SLUGS:
            raise ValueError(f"Unknown adversarial stage: {values['stage']}")
        columns = ", ".join(("round_id", "execution_id", *_ADVERSARIAL_ROUND_COLUMNS))
        slots = ", ".join("?" for _ in range(len(_ADVERSARIAL_ROUND_COLUMNS) + 2))
        if not 0 <= values["round_number"] <= 6:
            raise ValueError("Adversarial round must be from 0 (assessment) through 6")
        assignments = ", ".join(f"{column}=excluded.{column}" for column in _ADVERSARIAL_ROUND_COLUMNS)
        with self.connect() as database:
            database.execute(
                f"INSERT INTO adversarial_rounds ({columns}) VALUES ({slots}) "
                f"ON CONFLICT(execution_id, stage, round_number) DO UPDATE SET {assignments}",
                (str(uuid.uuid4()), execution_id, *(values[column] for column in _ADVERSARIAL_ROUND_COLUMNS)),
            )

    def adversarial_rounds_for(self, execution_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        """Every recorded round of the given executions, oldest round first."""
        ids = [str(value) for value in execution_ids if value]
        if not ids:
            return {}
        slots = ", ".join("?" for _ in ids)
        rounds: dict[str, list[dict[str, Any]]] = {}
        with self.connect() as database:
            for row in database.execute(
                f"SELECT * FROM adversarial_rounds WHERE execution_id IN ({slots}) "
                "ORDER BY execution_id, stage DESC, round_number",
                ids,
            ):
                record = dict(row)
                record["disputed"] = bool(record.get("disputed"))
                rounds.setdefault(str(record["execution_id"]), []).append(record)
        return rounds

    def adversarial_summary(
        self, repositories: Sequence[str] | str | None = None, *, search: str = ""
    ) -> dict[str, Any]:
        """How the adversarial UAT loop is performing for the selected repositories.

        Only work-rounds that actually ran the loop are counted (``disabled``
        and rows written before the loop existed are not), so the percentages
        answer "when the loop runs, how often does it settle cleanly?" rather
        than being diluted by every issue worked with it switched off.
        """
        repository_clause, repository_params = _repository_filter(repositories)
        clause, search_params = _search_filter(search)
        conditions = ["adversarial_outcome <> ''", "adversarial_outcome <> 'disabled'"]
        if repository_clause:
            conditions.insert(0, repository_clause)
        where = " WHERE " + " AND ".join(conditions) + clause
        params = [*repository_params, *search_params]
        with self.connect() as database:
            row = database.execute(
                "SELECT COUNT(*), AVG(adversarial_round_count), "
                "SUM(adversarial_outcome = 'clean_first_pass'), "
                "SUM(adversarial_outcome = 'cap_hit'), "
                "AVG(capacity_consumed_percent) "
                f"FROM ai_executions{where}",
                params,
            ).fetchone()
            tests = database.execute(
                "SELECT COALESCE(SUM(tests_added), 0) FROM adversarial_rounds "
                "WHERE stage = 'uat' AND execution_id IN ("
                f"SELECT execution_id FROM ai_executions{where})",
                params,
            ).fetchone()
        loops = int(row[0] or 0)
        if not loops:
            return {
                "loops": 0,
                "averageRounds": None,
                "cleanFirstPassPercent": None,
                "capHitPercent": None,
                "averageCapacityConsumedPercent": None,
                "testsAdded": 0,
            }
        capacity = row[4]
        return {
            "loops": loops,
            "averageRounds": round(float(row[1] or 0.0), 2),
            "cleanFirstPassPercent": round(int(row[2] or 0) * 100 / loops, 1),
            "capHitPercent": round(int(row[3] or 0) * 100 / loops, 1),
            "averageCapacityConsumedPercent": (
                None if capacity is None else round(float(capacity), 2)
            ),
            "testsAdded": int(tests[0] or 0),
        }

    def security_summary(
        self, repositories: Sequence[str] | str | None = None, *, search: str = ""
    ) -> dict[str, Any]:
        """How the adversarial cybersecurity review is performing.

        Counted the same way as ``adversarial_summary``: only work-rounds that
        actually ran the review. ``failedPercent`` deliberately separates a
        review that could not execute or left findings unresolved from a clean
        pass, so the dashboard can never present a broken reviewer as a secure
        codebase.
        """
        repository_clause, repository_params = _repository_filter(repositories)
        clause, search_params = _search_filter(search)
        conditions = ["security_outcome <> ''", "security_outcome <> 'disabled'"]
        if repository_clause:
            conditions.insert(0, repository_clause)
        where = " WHERE " + " AND ".join(conditions) + clause
        params = [*repository_params, *search_params]
        with self.connect() as database:
            row = database.execute(
                "SELECT COUNT(*), AVG(security_round_count), "
                "SUM(security_review_status = 'PASS'), "
                "SUM(security_review_status = 'FIXED'), "
                "SUM(security_review_status = 'FINDINGS_CREATED'), "
                "SUM(security_review_status = 'FAILED') "
                f"FROM ai_executions{where}",
                params,
            ).fetchone()
            rounds = database.execute(
                "SELECT COALESCE(SUM(findings_found), 0), COALESCE(SUM(findings_fixed), 0), "
                "COALESCE(SUM(tests_added), 0) FROM adversarial_rounds "
                "WHERE stage = 'security' AND execution_id IN ("
                f"SELECT execution_id FROM ai_executions{where})",
                params,
            ).fetchone()
        reviews = int(row[0] or 0)
        if not reviews:
            return _empty_security_summary()
        return {
            "reviews": reviews,
            "averageRounds": round(float(row[1] or 0.0), 2),
            "passPercent": round(int(row[2] or 0) * 100 / reviews, 1),
            "fixedPercent": round(int(row[3] or 0) * 100 / reviews, 1),
            "findingsCreatedPercent": round(int(row[4] or 0) * 100 / reviews, 1),
            "failedPercent": round(int(row[5] or 0) * 100 / reviews, 1),
            "findingsFound": int(rounds[0] or 0),
            "findingsFixed": int(rounds[1] or 0),
            "testsAdded": int(rounds[2] or 0),
        }

    def page_for_repository(
        self,
        repositories: Sequence[str] | str | None = None,
        *,
        search: str = "",
        limit: int = PAGE_SIZE,
        offset: int = 0,
        sort: str = "recent",
    ) -> tuple[list[sqlite3.Row], int, int, int]:
        """One page of executions, newest first, plus the filtered total.

        ``LIMIT``/``OFFSET`` are applied in SQLite. An offset past the end is
        pulled back to the last page so the caller still receives rows that
        exist. Returns ``(rows, total, offset, limit)``.
        """
        limit = clamp_page_size(limit)
        offset = clamp_offset(offset)
        order = {"rounds_asc": "adversarial_round_count ASC, started_at DESC, attempt_number DESC",
                 "rounds_desc": "adversarial_round_count DESC, started_at DESC, attempt_number DESC"}.get(
                     sort, "started_at DESC, attempt_number DESC")
        repository_clause, repository_params = _repository_filter(repositories)
        clause, search_params = _search_filter(search)
        where = f" WHERE {repository_clause}" if repository_clause else " WHERE 1 = 1"
        params = [*repository_params, *search_params]
        with self.connect() as database:
            total = int(
                database.execute(
                    f"SELECT COUNT(*) FROM ai_executions{where}{clause}",
                    params,
                ).fetchone()[0]
            )
            if total == 0:
                offset = 0
            elif offset >= total:
                offset = ((total - 1) // limit) * limit
            rows = list(
                database.execute(
                    f"SELECT * FROM ai_executions{where}"
                    f"{clause} ORDER BY {order}, execution_id "
                    "LIMIT ? OFFSET ?",
                    (*params, limit, offset),
                )
            )
        return rows, total, offset, limit


    def graded_for_repository(
        self,
        repositories: Sequence[str] | str | None = None,
        *,
        search: str = "",
        grade: str = "",
        router: str = "",
        router_model: str = "",
        limit: int = PAGE_SIZE,
        offset: int = 0,
    ) -> dict[str, Any]:
        """One page of prompt grades, newest first, plus two summaries.

        A row counts as graded when its routing decision carries a real
        ``prompt_grade`` (routing fallbacks and runs without routing do not).
        ``search`` matches the same columns as execution history.

        Three nested filters, each deliberately applied to a different part of
        the result so a panel is never the thing that hides its own options:

        * ``search`` narrows everything.
        * ``router`` and ``router_model`` keep only issues graded by that AI
          platform/model. They narrow the page *and* the grade summary — the
          grade distribution of one router is the interesting comparison —
          but not ``routerMatrix``, so another router can always be picked.
        * ``grade`` keeps only that letter (for example ``B-``) on the page;
          neither summary narrows, so the chart can switch filters.

        ``LIMIT``/``OFFSET`` run in SQLite, and an offset past the end snaps to
        the last page. Only the columns the grades view shows are read, so
        issue bodies and prompts never travel with a grade. Returns
        ``{records, total, offset, limit, summary, routerMatrix}``.
        """
        limit = clamp_page_size(limit)
        offset = clamp_offset(offset)
        selected = normalize_grade(grade)
        selected_router = normalize_provider_key(router)
        selected_router_model = normalize_router_model(router_model)
        search_clause, search_params = _search_filter(search)
        grade_names = list(GRADE_POINTS)
        grade_slots = ", ".join("?" for _ in grade_names)
        repository_clause, repository_params = _repository_filter(repositories)
        conditions = ["routing_decision <> ''"]
        if repository_clause:
            conditions.insert(0, repository_clause)
        where = (
            " WHERE "
            + " AND ".join(conditions)
            + f" AND json_extract(routing_decision, '$.prompt_grade') IN ({grade_slots})"
            + search_clause
        )
        params: list[Any] = [*repository_params, *grade_names, *search_params]
        grade_where = where
        grade_params = list(params)
        if selected_router:
            grade_where += f" AND {_ROUTER_KEY_SQL} = ?"
            grade_params.append(selected_router)
        if selected_router_model:
            grade_where += f" AND {_ROUTER_MODEL_SQL} = ?"
            grade_params.append(selected_router_model)
        page_where = grade_where
        page_params = list(grade_params)
        if selected:
            page_where += " AND json_extract(routing_decision, '$.prompt_grade') = ?"
            page_params.append(selected)
        columns = (
            "repository, issue_number, issue_title, issue_url, attempt_number, started_at, "
            "ai_provider, model, effort, final_status, routing_decision"
        )
        with self.connect() as database:
            counts = database.execute(
                "SELECT json_extract(routing_decision, '$.prompt_grade') AS grade, COUNT(*) "
                f"FROM ai_executions{grade_where} GROUP BY grade",
                grade_params,
            ).fetchall()
            pairs = database.execute(
                f"SELECT {_ROUTER_KEY_SQL} AS router, {_WORKED_KEY_SQL} AS worked, COUNT(*) "
                f"FROM ai_executions{where} GROUP BY router, worked",
                params,
            ).fetchall()
            models = database.execute(
                f"SELECT {_ROUTER_KEY_SQL} AS router, {_ROUTER_MODEL_SQL} AS model, COUNT(*) "
                f"FROM ai_executions{where} GROUP BY router, {_ROUTER_MODEL_SQL}",
                params,
            ).fetchall()
            total = int(
                database.execute(
                    f"SELECT COUNT(*) FROM ai_executions{page_where}",
                    page_params,
                ).fetchone()[0]
            )
            if total == 0:
                offset = 0
            elif offset >= total:
                offset = ((total - 1) // limit) * limit
            rows = list(
                database.execute(
                    f"SELECT {columns} FROM ai_executions{page_where} "
                    "ORDER BY started_at DESC, attempt_number DESC LIMIT ? OFFSET ?",
                    (*page_params, limit, offset),
                )
            )
        grades: list[str] = []
        for grade_name, count in counts:
            grades.extend([str(grade_name)] * int(count))
        return {
            "records": [row_to_dict(row) for row in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
            "summary": summarize_grades(grades),
            "routerMatrix": summarize_router_matrix(
                [
                    (str(router_key or ""), str(worked or ""), int(count))
                    for router_key, worked, count in pairs
                ],
                [
                    (str(router_key or ""), str(model or ""), int(count))
                    for router_key, model, count in models
                ],
            ),
        }


def summarize_grades(grades: list[str]) -> dict[str, Any]:
    """Distribution, mean grade points, and the letter nearest to that mean."""
    distribution = {grade: 0 for grade in GRADE_POINTS}
    for grade in grades:
        distribution[grade] += 1
    if not grades:
        return {
            "graded": 0,
            "averagePoints": None,
            "averageGrade": "",
            "distribution": distribution,
        }
    average = sum(GRADE_POINTS[grade] for grade in grades) / len(grades)
    # A+ and A share 4.0 points, so a mean can only ever resolve to A.
    nearest = min(
        (grade for grade in GRADE_POINTS if grade != "A+"),
        key=lambda grade: abs(GRADE_POINTS[grade] - average),
    )
    return {
        "graded": len(grades),
        "averagePoints": round(average, 2),
        "averageGrade": nearest,
        "distribution": distribution,
    }


def summarize_router_matrix(
    pairs: list[tuple[str, str, int]],
    model_pairs: list[tuple[str, str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Which AI platform each grading platform picked, as counts and percents.

    One entry per grading (router) platform, busiest first, each carrying the
    platforms it selected in descending count order. Percentages are of that
    router's own graded total, so a row answers "when Grok grades an issue, how
    often does it keep it?" directly. A router key the decision never recorded
    is kept under ``""`` rather than dropped — silently omitting rows would
    make the percentages lie about the total.
    """
    totals: dict[str, int] = {}
    selections: dict[str, dict[str, int]] = {}
    for router, worked, count in pairs:
        if count <= 0:
            continue
        totals[router] = totals.get(router, 0) + count
        bucket = selections.setdefault(router, {})
        bucket[worked] = bucket.get(worked, 0) + count
    models: dict[str, dict[str, int]] = {}
    for router, model, count in model_pairs or []:
        if count <= 0:
            continue
        bucket = models.setdefault(router, {})
        bucket[model] = bucket.get(model, 0) + count
    rows: list[dict[str, Any]] = []
    for router in sorted(totals, key=lambda key: (-totals[key], key)):
        graded = totals[router]
        chosen = selections[router]
        rows.append(
            {
                "router": router,
                "graded": graded,
                "models": [
                    {
                        "model": model,
                        "count": models[router][model],
                        "percent": round(models[router][model] * 100 / graded, 1),
                    }
                    for model in sorted(
                        models.get(router, {}),
                        key=lambda key: (-models[router][key], key),
                    )
                ],
                "selections": [
                    {
                        "provider": provider,
                        "count": chosen[provider],
                        "percent": round(chosen[provider] * 100 / graded, 1),
                    }
                    for provider in sorted(chosen, key=lambda key: (-chosen[key], key))
                ],
            }
        )
    return rows


def normalize_router_model(value: str) -> str:
    """Trim a router-model filter without treating model IDs as provider keys."""
    return sanitize_text(value).strip()[:120]


def _serialize_routing(value: Any) -> str:
    if not value:
        return ""
    if not isinstance(value, dict):
        raise ValueError("routing_decision must be an object")
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        cleaned[str(key)] = sanitize_text(item) if isinstance(item, str) else item
    return json.dumps(cleaned)


def _serialize_adversarial_filed_findings(value: Any) -> str:
    """Sanitize the small, user-facing receipt for separately filed UAT bugs."""
    if not isinstance(value, list):
        raise ValueError("adversarial_filed_findings must be a list")
    cleaned = []
    for finding in value:
        if not isinstance(finding, dict):
            raise ValueError("adversarial_filed_findings entries must be objects")
        url = sanitize_text(finding.get("url", ""))
        if not re.search(r"/issues/[0-9]+$", url):
            url = ""
        cleaned.append({
            "title": sanitize_text(finding.get("title", "")),
            "url": url,
        })
    return json.dumps(cleaned)


def _serialize_security_findings(value: Any) -> str:
    """Sanitize the structured cybersecurity review metadata.

    Findings quote real code, so every string goes through the same secret
    scrubber the rest of the history uses before it is persisted.
    """
    if not value:
        return "{}"
    if not isinstance(value, dict):
        raise ValueError("security_findings must be an object")

    def clean(item: Any) -> Any:
        if isinstance(item, str):
            return sanitize_text(item)
        if isinstance(item, dict):
            return {str(key): clean(entry) for key, entry in item.items()}
        if isinstance(item, list):
            return [clean(entry) for entry in item]
        return item

    return json.dumps(clean(value))


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


def attach_adversarial_rounds(
    repository: "ExecutionHistoryRepository", records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Give each record its ``adversarial_rounds`` list (possibly empty)."""
    rounds = repository.adversarial_rounds_for(
        [str(record.get("execution_id") or "") for record in records]
    )
    for record in records:
        record["adversarial_rounds"] = rounds.get(str(record.get("execution_id") or ""), [])
    return records


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
            existing = False
            if existing_id:
                with self.repository.connect() as database:
                    existing = database.execute(
                        "SELECT 1 FROM ai_executions WHERE execution_id = ?", (existing_id,)
                    ).fetchone() is not None
            if existing:
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

    def final_statuses(self, repository: str, issue_number: int) -> list[str]:
        """Recorded attempt statuses for one issue, newest first.

        Empty when history is disabled or unreadable, so callers treat missing
        history as "no evidence" rather than as evidence of anything."""
        if not self.repository:
            return []
        try:
            return self.repository.final_statuses_for_issue(repository, issue_number)
        except sqlite3.Error as error:
            self.error = sanitize_text(error)
            return []

    def adversarial_round(self, round_values: dict[str, Any]) -> None:
        """Record one adversarial fix/test round; a no-op when history is off."""
        if self.repository and self.execution_id:
            try:
                self.repository.record_adversarial_round(self.execution_id, round_values)
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


def _empty_adversarial_summary() -> dict[str, Any]:
    return {
        "loops": 0,
        "averageRounds": None,
        "cleanFirstPassPercent": None,
        "capHitPercent": None,
        "averageCapacityConsumedPercent": None,
        "testsAdded": 0,
    }


def _empty_security_summary() -> dict[str, Any]:
    return {
        "reviews": 0,
        "averageRounds": None,
        "passPercent": None,
        "fixedPercent": None,
        "findingsCreatedPercent": None,
        "failedPercent": None,
        "findingsFound": 0,
        "findingsFixed": 0,
        "testsAdded": 0,
    }


def _empty_page() -> dict[str, Any]:
    return {
        "records": [],
        "total": 0,
        "offset": 0,
        "limit": PAGE_SIZE,
        "adversarial": _empty_adversarial_summary(),
        "security": _empty_security_summary(),
    }


def main(argv: list[str] | None = None) -> int:
    """`python3 ai_execution_history.py --db PATH [--repository OWNER/NAME ...]`.

    Without paging flags, prints the repository's executions as a JSON array
    on stdout. `--limit`, `--offset`, or `--search` instead print one page
    object (`records`/`total`/`offset`/`limit`/`adversarial`/`security`) of at most 10
    rows — the desktop Feedback view always uses that form so it never
    receives the full history. Every record carries its `adversarial_rounds`,
    and `adversarial` summarizes the adversarial UAT loop across the whole
    (searched) repository, not just the page.

    `--grades` instead prints one page of prompt grades (the router's grade
    of each issue's original prompt, with the reason and complexity) plus a
    summary of every graded execution in the current search — the Feedback
    view's grades panel. `--search` filters that page the same way it filters
    execution history. `--grade` keeps the page to one letter; the summary
    still includes every grade in the search. `--router` and `--router-model`
    keep the page and grade summary to issues graded by one AI platform/model;
    `routerMatrix` still covers every platform in the search so another can be
    chosen.

    `--import-from-github` instead scans that repository's full GitHub issue
    backlog (open and closed) and adds a synthetic `imported` row for any
    issue with no existing execution history row, printing a JSON summary
    object (`totalIssues`/`imported`/`skipped`) instead of the row array —
    the Feedback view's "Import from GitHub" action.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to the SQLite database file.")
    parser.add_argument(
        "--repository",
        action="append",
        default=[],
        help="owner/name to filter by; repeat for multiple repositories, or omit for all.",
    )
    parser.add_argument("--sort", choices=("recent", "rounds_asc", "rounds_desc"), default="recent")
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
        "--grades",
        action="store_true",
        help="Print one page of prompt grades plus a summary of every grade instead.",
    )
    parser.add_argument(
        "--grade",
        default="",
        help="With --grades, return only this prompt grade (for example B-). The summary still counts every grade.",
    )
    parser.add_argument(
        "--router",
        default="",
        help=(
            "With --grades, return only issues graded and routed by this AI platform "
            "(for example claude). The router matrix still covers every platform."
        ),
    )
    parser.add_argument(
        "--router-model",
        default="",
        help=(
            "With --grades, return only issues graded by this exact router model. "
            "The router matrix still covers every platform and model."
        ),
    )
    parser.add_argument(
        "--search",
        default="",
        help="Case-insensitive match on issue number, title, provider, model, branch, or status.",
    )
    args = parser.parse_args(argv)

    database_path = Path(args.db).expanduser()
    repository_names = [sanitize_text(value) for value in args.repository if value.strip()]
    paging = _page_requested(args.limit, args.offset, args.search) or args.sort != "recent"

    if args.import_from_github:
        if len(repository_names) != 1:
            print(json.dumps({"error": "Import requires exactly one --repository."}))
            return 1
        repository_name = repository_names[0]
        repository = ExecutionHistoryRepository(database_path)
        try:
            issues = fetch_github_issues(args.gh_bin, repository_name)
        except (RuntimeError, ValueError) as error:
            print(json.dumps({"error": str(error)}))
            return 1
        json.dump(import_missing_issues(repository, repository_name, issues), sys.stdout)
        return 0

    if args.grades:
        if not database_path.is_file():
            json.dump(
                {**_empty_page(), "summary": summarize_grades([]), "routerMatrix": []},
                sys.stdout,
            )
            return 0
        json.dump(
            ExecutionHistoryRepository(database_path).graded_for_repository(
                repository_names,
                search=args.search,
                grade=args.grade,
                router=args.router,
                router_model=args.router_model,
                limit=PAGE_SIZE if args.limit is None else args.limit,
                offset=args.offset,
            ),
            sys.stdout,
        )
        return 0

    if not database_path.is_file():
        if paging:
            json.dump(_empty_page(), sys.stdout)
        else:
            print("[]")
        return 0

    repository = ExecutionHistoryRepository(database_path)
    if not paging:
        if len(repository_names) != 1:
            parser.error("unpaged export requires exactly one --repository")
        rows = repository.for_repository(repository_names[0])
        json.dump(
            attach_adversarial_rounds(repository, [row_to_dict(row) for row in rows]),
            sys.stdout,
        )
        return 0

    rows, total, offset, limit = repository.page_for_repository(
        repository_names,
        sort=args.sort,
        search=args.search,
        limit=PAGE_SIZE if args.limit is None else args.limit,
        offset=args.offset,
    )
    json.dump(
        {
            "records": attach_adversarial_rounds(
                repository, [row_to_dict(row) for row in rows]
            ),
            "total": total,
            "offset": offset,
            "limit": limit,
            "adversarial": repository.adversarial_summary(
                repository_names, search=args.search
            ),
            "security": repository.security_summary(
                repository_names, search=args.search
            ),
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
