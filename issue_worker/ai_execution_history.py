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
from typing import Any, Iterable, Mapping, Sequence

import usage_report


SCHEMA_VERSION = 9
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
    "execution_id",
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
    "adversarial_unresolved",
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
    "epoch_number",
    "round_in_epoch",
    "fixer_effort",
    "tester_effort",
    "escalation_reason",
    "patch_fingerprint",
    "repeated_patch",
    "failure_fingerprint",
    "repeated_failure",
    "progress",
    "merge_policy",
    "failing_suites",
    "open_findings",
    "usage_json",
)

# Migration 9 (issue #305). Strict mode renews three-round epochs, so a round
# number is no longer capped at 6, and each closed epoch is its own row.
_MIGRATION_9_EXECUTION_COLUMNS = (
    ("adversarial_epoch_count", "INTEGER NOT NULL DEFAULT 0"),
    ("security_epoch_count", "INTEGER NOT NULL DEFAULT 0"),
    ("adversarial_merge_policy", "TEXT NOT NULL DEFAULT ''"),
    ("adversarial_delivery", "TEXT NOT NULL DEFAULT ''"),
    ("adversarial_unresolved", "TEXT NOT NULL DEFAULT '{}'"),
    ("promotion_status", "TEXT NOT NULL DEFAULT ''"),
    ("promotion_url", "TEXT NOT NULL DEFAULT ''"),
)

_MIGRATION_9_ROUND_COLUMNS = (
    ("epoch_number", "INTEGER NOT NULL DEFAULT 1"),
    ("round_in_epoch", "INTEGER NOT NULL DEFAULT 0"),
    ("fixer_effort", "TEXT NOT NULL DEFAULT ''"),
    ("tester_effort", "TEXT NOT NULL DEFAULT ''"),
    ("escalation_reason", "TEXT NOT NULL DEFAULT ''"),
    ("patch_fingerprint", "TEXT NOT NULL DEFAULT ''"),
    ("repeated_patch", "INTEGER NOT NULL DEFAULT 0"),
    ("failure_fingerprint", "TEXT NOT NULL DEFAULT ''"),
    ("repeated_failure", "INTEGER NOT NULL DEFAULT 0"),
    ("progress", "TEXT NOT NULL DEFAULT ''"),
    ("merge_policy", "TEXT NOT NULL DEFAULT ''"),
    ("failing_suites", "TEXT NOT NULL DEFAULT '[]'"),
    ("open_findings", "TEXT NOT NULL DEFAULT '[]'"),
    ("usage_json", "TEXT NOT NULL DEFAULT '{}'"),
)

# A sanity ceiling, not a product cap. Strict epochs keep counting; a corrupt
# round number still must not land in the table.
_MAX_ADVERSARIAL_ROUND = 10000

# History filters. An execution written before the delivery column existed is
# classified from its outcome: a clean pass was verified, and a cap hit was
# the old always-best-effort delivery.
_VERIFIED_CLEAN_SQL = (
    "(adversarial_delivery = 'verified_clean' OR "
    "(adversarial_delivery = '' AND adversarial_outcome IN ('clean_first_pass', 'resolved_after_n')))"
)
_BEST_EFFORT_SQL = (
    "(adversarial_delivery = 'best_effort' OR "
    "(adversarial_delivery = '' AND adversarial_outcome = 'cap_hit'))"
)
_DELIVERY_FILTERS = {
    "verified_clean": _VERIFIED_CLEAN_SQL,
    "best_effort": _BEST_EFFORT_SQL,
}

_ROUND_JSON_COLUMNS = ("failing_suites", "open_findings", "usage_json")
_EPOCH_JSON_COLUMNS = (
    "next_escalation", "fixers", "failing_suites", "findings", "disputes",
    "patch_fingerprints", "no_progress", "usage_json",
)

# Migration 6 adds per-prompt AI token usage (issue #280). Unlike the other
# migrations, ``ai_token_usage`` rows are not tied to ``ai_executions`` by a
# foreign key: a dynamic-routing call can happen before ``ai_executions`` even
# has a row for this attempt (routing runs before ``start_execution_history``),
# so ``execution_id`` here is a plain, indexed column rather than an enforced
# reference — a row with an execution_id that does not (yet) resolve is still
# useful telemetry, never a constraint violation that could break an
# otherwise-successful AI call.
_TOKEN_USAGE_COLUMNS = (
    "execution_id",
    "repository",
    "issue_number",
    "workflow_run_id",
    "agent_run_id",
    "prompt_id",
    "provider",
    "model",
    "reasoning_effort",
    "agent_type",
    "prompt_type",
    "attempt_number",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_input_tokens",
    "total_tokens",
    "estimated_cost",
    "currency",
    "started_at",
    "completed_at",
    "duration_ms",
    "success",
    "error_type",
    "cache_read_tokens",
    "cache_write_tokens",
    "pricing_status",
    "pricing_version",
    "pricing_rate_id",
    "pricing_source",
    "input_rate_per_million",
    "cached_input_rate_per_million",
    "cache_write_rate_per_million",
    "output_rate_per_million",
)

# Migration 7 columns on ``ai_token_usage`` (issue #295). Two groups:
#
# * ``cache_read_tokens``/``cache_write_tokens`` split the single
#   ``cached_input_tokens`` figure, because a cache write is a metered
#   operation billed at a premium while a cache read is the discount — a
#   report that cannot tell them apart cannot price them.
# * The pricing-provenance columns record *which* catalog entry produced
#   ``estimated_cost`` and at what rates. They exist so a stored estimate can
#   be explained and reproduced later, and so correcting the catalog never
#   silently restates history: nothing recomputes a persisted estimate.
#   ``pricing_status`` says why a row has no cost (unknown model, ambiguous
#   alias, no effective rate) rather than leaving "unpriced" and "free"
#   indistinguishable.
_MIGRATION_8_COLUMNS = (
    ("cache_read_tokens", "INTEGER"),
    ("cache_write_tokens", "INTEGER"),
    ("pricing_status", "TEXT NOT NULL DEFAULT ''"),
    ("pricing_version", "TEXT NOT NULL DEFAULT ''"),
    ("pricing_rate_id", "TEXT NOT NULL DEFAULT ''"),
    ("pricing_source", "TEXT NOT NULL DEFAULT ''"),
    ("input_rate_per_million", "REAL"),
    ("cached_input_rate_per_million", "REAL"),
    ("cache_write_rate_per_million", "REAL"),
    ("output_rate_per_million", "REAL"),
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


# Nested analytics payloads must never keep raw prompts, CLI transcripts, or
# credential-shaped keys. Matching is case-insensitive on the key name.
_SENSITIVE_STRUCTURED_KEYS = frozenset(
    {
        "prompt",
        "body",
        "raw",
        "raw_prompt",
        "cli",
        "cli_output",
        "stdout",
        "stderr",
        "transcript",
        "api_key",
        "access_token",
        "auth_token",
        "password",
        "secret",
        "token",
        "authorization",
        "credentials",
        "private_key",
    }
)
_SENSITIVE_KEY_FRAGMENTS = (
    "prompt",
    "secret",
    "password",
    "api_key",
    "credential",
    "authorization",
    "private_key",
)


def sanitize_structured(value: Any) -> Any:
    """Drop sensitive keys and redact secrets in nested JSON-like values."""
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            lowered = name.strip().lower().replace("-", "_")
            if lowered in _SENSITIVE_STRUCTURED_KEYS or any(
                fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS
            ):
                continue
            cleaned[name] = sanitize_structured(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [sanitize_structured(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize_text(value)


def _json_sanitized(value: Any) -> str:
    return sanitize_text(json.dumps(sanitize_structured(value)))


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

    The term must sit inside issue number, title, provider, model, branch,
    status, or execution id. `%`, `_`, and `\\` are matched literally. Issue
    body and prompt text are not searched.
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


def _optional_number(value: Any) -> float | None:
    """A finite number from a filter field, or None when blank/invalid."""
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


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
                        epoch_number INTEGER NOT NULL DEFAULT 1,
                        round_in_epoch INTEGER NOT NULL DEFAULT 0,
                        fixer_effort TEXT NOT NULL DEFAULT '',
                        tester_effort TEXT NOT NULL DEFAULT '',
                        escalation_reason TEXT NOT NULL DEFAULT '',
                        patch_fingerprint TEXT NOT NULL DEFAULT '',
                        repeated_patch INTEGER NOT NULL DEFAULT 0,
                        failure_fingerprint TEXT NOT NULL DEFAULT '',
                        repeated_failure INTEGER NOT NULL DEFAULT 0,
                        progress TEXT NOT NULL DEFAULT '',
                        merge_policy TEXT NOT NULL DEFAULT '',
                        failing_suites TEXT NOT NULL DEFAULT '[]',
                        open_findings TEXT NOT NULL DEFAULT '[]',
                        usage_json TEXT NOT NULL DEFAULT '{}',
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
            if 6 not in applied and 6 not in {
                row[0] for row in database.execute("SELECT version FROM schema_migrations")
            }:
                database.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS ai_token_usage (
                        id TEXT PRIMARY KEY,
                        execution_id TEXT NOT NULL DEFAULT '',
                        repository TEXT NOT NULL DEFAULT '',
                        issue_number INTEGER NOT NULL DEFAULT 0,
                        workflow_run_id TEXT NOT NULL DEFAULT '',
                        agent_run_id TEXT NOT NULL DEFAULT '',
                        prompt_id TEXT NOT NULL DEFAULT '',
                        provider TEXT NOT NULL DEFAULT '',
                        model TEXT NOT NULL DEFAULT '',
                        reasoning_effort TEXT NOT NULL DEFAULT '',
                        agent_type TEXT NOT NULL DEFAULT '',
                        prompt_type TEXT NOT NULL DEFAULT '',
                        attempt_number INTEGER NOT NULL DEFAULT 1,
                        input_tokens INTEGER,
                        output_tokens INTEGER,
                        reasoning_tokens INTEGER,
                        cached_input_tokens INTEGER,
                        total_tokens INTEGER,
                        estimated_cost REAL,
                        currency TEXT NOT NULL DEFAULT 'USD',
                        started_at TEXT NOT NULL DEFAULT '',
                        completed_at TEXT NOT NULL DEFAULT '',
                        duration_ms INTEGER,
                        success INTEGER NOT NULL DEFAULT 1,
                        error_type TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS ai_token_usage_execution_idx
                        ON ai_token_usage(execution_id);
                    CREATE INDEX IF NOT EXISTS ai_token_usage_issue_idx
                        ON ai_token_usage(repository, issue_number);
                    CREATE INDEX IF NOT EXISTS ai_token_usage_agent_idx
                        ON ai_token_usage(agent_type, provider, model);
                    """
                )
                database.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                    (6,),
                )
            if 7 not in applied and 7 not in {
                row[0] for row in database.execute("SELECT version FROM schema_migrations")
            }:
                database.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS jev_decisions (
                        decision_id TEXT PRIMARY KEY,
                        execution_id TEXT NOT NULL DEFAULT '',
                        issue_number INTEGER NOT NULL DEFAULT 0,
                        repository TEXT NOT NULL DEFAULT '',
                        decision_type TEXT NOT NULL DEFAULT '',
                        input_fingerprint TEXT NOT NULL DEFAULT '',
                        decision TEXT NOT NULL DEFAULT '',
                        confidence REAL,
                        scores TEXT NOT NULL DEFAULT '{}',
                        reason_codes TEXT NOT NULL DEFAULT '[]',
                        latency_ms REAL,
                        provider TEXT NOT NULL DEFAULT 'jev',
                        model TEXT NOT NULL DEFAULT '',
                        version TEXT NOT NULL DEFAULT '',
                        estimated_cost REAL,
                        created_at TEXT NOT NULL DEFAULT '',
                        fallback_used TEXT NOT NULL DEFAULT '',
                        swarm_action TEXT NOT NULL DEFAULT '',
                        recommendation_accepted INTEGER,
                        workflow_outcome TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        error_type TEXT NOT NULL DEFAULT '',
                        jev_enabled INTEGER NOT NULL DEFAULT 0,
                        llm_calls_avoided INTEGER NOT NULL DEFAULT 0,
                        estimated_tokens_avoided INTEGER,
                        estimated_dollar_savings REAL
                    );
                    CREATE INDEX IF NOT EXISTS jev_decisions_execution_idx
                        ON jev_decisions(execution_id, created_at);
                    CREATE INDEX IF NOT EXISTS jev_decisions_issue_idx
                        ON jev_decisions(repository, issue_number);
                    CREATE INDEX IF NOT EXISTS jev_decisions_type_idx
                        ON jev_decisions(decision_type, source);
                    CREATE TABLE IF NOT EXISTS jev_score_comparisons (
                        comparison_id TEXT PRIMARY KEY,
                        execution_id TEXT NOT NULL DEFAULT '',
                        repository TEXT NOT NULL DEFAULT '',
                        issue_number INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT '',
                        jev_status TEXT NOT NULL DEFAULT 'disabled',
                        baseline_native_score REAL,
                        baseline_normalized_score REAL,
                        baseline_prompt_grade TEXT NOT NULL DEFAULT '',
                        baseline_complexity INTEGER,
                        baseline_task_type TEXT NOT NULL DEFAULT '',
                        baseline_risk TEXT NOT NULL DEFAULT '',
                        baseline_context TEXT NOT NULL DEFAULT '',
                        baseline_provider TEXT NOT NULL DEFAULT '',
                        baseline_model TEXT NOT NULL DEFAULT '',
                        baseline_effort TEXT NOT NULL DEFAULT '',
                        baseline_candidates TEXT NOT NULL DEFAULT '[]',
                        baseline_inputs TEXT NOT NULL DEFAULT '{}',
                        jev_native_score REAL,
                        jev_normalized_score REAL,
                        jev_component_scores TEXT NOT NULL DEFAULT '{}',
                        jev_reason_codes TEXT NOT NULL DEFAULT '[]',
                        jev_confidence REAL,
                        jev_model TEXT NOT NULL DEFAULT '',
                        jev_version TEXT NOT NULL DEFAULT '',
                        modified_native_score REAL,
                        modified_normalized_score REAL,
                        modified_provider TEXT NOT NULL DEFAULT '',
                        modified_model TEXT NOT NULL DEFAULT '',
                        modified_effort TEXT NOT NULL DEFAULT '',
                        modified_candidates TEXT NOT NULL DEFAULT '[]',
                        score_delta_absolute REAL,
                        score_delta_percent REAL,
                        routing_changed INTEGER NOT NULL DEFAULT 0,
                        estimated_jev_cost REAL,
                        estimated_total_cost REAL,
                        estimated_llm_calls_avoided INTEGER NOT NULL DEFAULT 0,
                        estimated_tokens_avoided INTEGER,
                        estimated_dollar_savings REAL,
                        latency_ms REAL,
                        workflow_outcome TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS jev_score_comparisons_execution_idx
                        ON jev_score_comparisons(execution_id);
                    CREATE INDEX IF NOT EXISTS jev_score_comparisons_status_idx
                        ON jev_score_comparisons(jev_status, repository, created_at);
                    """
                )
                database.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                    (7,),
                )
            if 8 not in applied and 8 not in {
                row[0] for row in database.execute("SELECT version FROM schema_migrations")
            }:
                columns = {
                    row[1] for row in database.execute("PRAGMA table_info(ai_token_usage)")
                }
                for name, definition in _MIGRATION_8_COLUMNS:
                    if name not in columns:
                        database.execute(
                            f"ALTER TABLE ai_token_usage ADD COLUMN {name} {definition}"
                        )
                # The Usage & cost report filters and buckets by the
                # invocation's own activity time, never by the later batch
                # ``created_at``, so that is the column that needs the index.
                database.execute(
                    "CREATE INDEX IF NOT EXISTS ai_token_usage_started_idx "
                    "ON ai_token_usage(started_at)"
                )
                database.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                    (8,),
                )
            if 9 not in applied and 9 not in {
                row[0] for row in database.execute("SELECT version FROM schema_migrations")
            }:
                columns = {
                    row[1] for row in database.execute("PRAGMA table_info(ai_executions)")
                }
                for name, definition in _MIGRATION_9_EXECUTION_COLUMNS:
                    if name not in columns:
                        database.execute(
                            f"ALTER TABLE ai_executions ADD COLUMN {name} {definition}"
                        )
                round_table = database.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'adversarial_rounds'"
                ).fetchone()
                if round_table is not None:
                    round_columns = {
                        row[1] for row in database.execute("PRAGMA table_info(adversarial_rounds)")
                    }
                    for name, definition in _MIGRATION_9_ROUND_COLUMNS:
                        if name not in round_columns:
                            database.execute(
                                f"ALTER TABLE adversarial_rounds ADD COLUMN {name} {definition}"
                            )
                database.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS adversarial_epochs (
                        epoch_id TEXT PRIMARY KEY,
                        execution_id TEXT NOT NULL
                            REFERENCES ai_executions(execution_id) ON DELETE CASCADE,
                        stage TEXT NOT NULL,
                        epoch_number INTEGER NOT NULL,
                        first_round INTEGER NOT NULL DEFAULT 0,
                        last_round INTEGER NOT NULL DEFAULT 0,
                        merge_policy TEXT NOT NULL DEFAULT '',
                        outcome TEXT NOT NULL DEFAULT '',
                        progress INTEGER NOT NULL DEFAULT 0,
                        stalled_epochs INTEGER NOT NULL DEFAULT 0,
                        escalation_reason TEXT NOT NULL DEFAULT '',
                        next_escalation TEXT NOT NULL DEFAULT '{}',
                        fixers TEXT NOT NULL DEFAULT '[]',
                        failing_suites TEXT NOT NULL DEFAULT '[]',
                        findings TEXT NOT NULL DEFAULT '[]',
                        disputes TEXT NOT NULL DEFAULT '[]',
                        tests_added INTEGER NOT NULL DEFAULT 0,
                        patch_fingerprints TEXT NOT NULL DEFAULT '[]',
                        failure_fingerprint TEXT NOT NULL DEFAULT '',
                        no_progress TEXT NOT NULL DEFAULT '{}',
                        started_at TEXT NOT NULL DEFAULT '',
                        completed_at TEXT NOT NULL DEFAULT '',
                        duration_seconds REAL,
                        usage_json TEXT NOT NULL DEFAULT '{}',
                        UNIQUE(execution_id, stage, epoch_number)
                    );
                    CREATE INDEX IF NOT EXISTS adversarial_epochs_execution_idx
                        ON adversarial_epochs(execution_id, stage, epoch_number);
                    """
                )
                database.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                    (9,),
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
            "adversarial_epoch_count",
            "security_epoch_count",
            "adversarial_merge_policy",
            "adversarial_delivery",
            "adversarial_unresolved",
            "promotion_status",
            "promotion_url",
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
            elif key == "adversarial_unresolved":
                serialized[key] = _serialize_json(value if value else {}, empty="{}")
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

        A round is identified by ``(execution_id, stage, round_number)`` so a
        retried scheduler tick that re-reports the same round updates it rather
        than appending a duplicate — the same idempotency rule the issue
        comments follow. Strict-mode epochs count past the original six rounds.
        """
        payload = dict(round_values)
        if "open_findings" not in payload and "findings" in payload:
            payload["open_findings"] = payload["findings"]
        if "usage_json" not in payload and "usage" in payload:
            payload["usage_json"] = payload["usage"]
        values: dict[str, Any] = {}
        for column in _ADVERSARIAL_ROUND_COLUMNS:
            value = payload.get(column)
            if column in {"round_number", "tests_added", "tests_modified",
                          "tests_failing_before", "tests_failing_after",
                          "findings_found", "findings_fixed", "findings_filed",
                          "epoch_number", "round_in_epoch"}:
                values[column] = int(value or 0)
            elif column in {"disputed", "repeated_patch", "repeated_failure"}:
                values[column] = 1 if value else 0
            elif column == "duration_seconds":
                values[column] = None if value is None else float(value)
            elif column == "severity_counts":
                values[column] = sanitize_text(value) or "{}"
            elif column == "stage":
                values[column] = sanitize_text(value) or "uat"
            elif column in {"failing_suites", "open_findings"}:
                values[column] = _serialize_json([] if value is None else value, empty="[]")
            elif column == "usage_json":
                values[column] = _serialize_json({} if value is None else value, empty="{}")
            else:
                values[column] = sanitize_text(value)
        if "epoch_number" not in payload:
            number = values["round_number"]
            values["epoch_number"] = 1 if number <= 3 else (number - 1) // 3 + 1
        if "round_in_epoch" not in payload:
            number = values["round_number"]
            values["round_in_epoch"] = 0 if number <= 0 else (number - 1) % 3 + 1
        if values["stage"] not in ADVERSARIAL_STAGE_SLUGS:
            raise ValueError(f"Unknown adversarial stage: {values['stage']}")
        columns = ", ".join(("round_id", "execution_id", *_ADVERSARIAL_ROUND_COLUMNS))
        slots = ", ".join("?" for _ in range(len(_ADVERSARIAL_ROUND_COLUMNS) + 2))
        if not 0 <= values["round_number"] <= _MAX_ADVERSARIAL_ROUND:
            raise ValueError("Adversarial round must be from 0 (assessment) upward")
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
                record["repeated_patch"] = bool(record.get("repeated_patch"))
                record["repeated_failure"] = bool(record.get("repeated_failure"))
                _decode_json_fields(record, _ROUND_JSON_COLUMNS)
                rounds.setdefault(str(record["execution_id"]), []).append(record)
        return rounds

    def record_adversarial_epoch(self, execution_id: str, summary: dict[str, Any]) -> None:
        """Insert or replace one closed adversarial epoch.

        Identified by ``(execution_id, stage, epoch_number)`` so a resumed
        scheduler tick that re-closes the same epoch updates it.
        """
        stage = sanitize_text(summary.get("stage")) or "uat"
        if stage not in ADVERSARIAL_STAGE_SLUGS:
            raise ValueError(f"Unknown adversarial stage: {stage}")
        epoch_number = int(summary.get("epoch_number") or 0)
        if not 1 <= epoch_number <= _MAX_ADVERSARIAL_ROUND:
            raise ValueError("Adversarial epoch must be 1 or greater")
        duration = summary.get("duration_seconds")
        values = (
            str(uuid.uuid4()),
            execution_id,
            stage,
            epoch_number,
            int(summary.get("first_round") or 0),
            int(summary.get("last_round") or 0),
            sanitize_text(summary.get("merge_policy")),
            sanitize_text(summary.get("outcome")),
            1 if summary.get("progress") else 0,
            int(summary.get("stalled_epochs") or 0),
            sanitize_text(summary.get("escalation_reason")),
            _serialize_json(summary.get("next_escalation") or {}, empty="{}"),
            _serialize_json(summary.get("fixers") or [], empty="[]"),
            _serialize_json(summary.get("failing_suites") or [], empty="[]"),
            _serialize_json(summary.get("findings") or [], empty="[]"),
            _serialize_json(summary.get("disputes") or [], empty="[]"),
            int(summary.get("tests_added") or 0),
            _serialize_json(summary.get("patch_fingerprints") or [], empty="[]"),
            sanitize_text(summary.get("failure_fingerprint")),
            _serialize_json(summary.get("no_progress") or {}, empty="{}"),
            sanitize_text(summary.get("started_at")),
            sanitize_text(summary.get("completed_at")),
            None if duration is None else float(duration),
            _serialize_json(summary.get("usage") if summary.get("usage") is not None else summary.get("usage_json") or {}, empty="{}"),
        )
        assignments = ", ".join(
            f"{column}=excluded.{column}" for column in (
                "first_round", "last_round", "merge_policy", "outcome", "progress",
                "stalled_epochs", "escalation_reason", "next_escalation", "fixers",
                "failing_suites", "findings", "disputes", "tests_added",
                "patch_fingerprints", "failure_fingerprint", "no_progress",
                "started_at", "completed_at", "duration_seconds", "usage_json",
            )
        )
        with self.connect() as database:
            database.execute(
                f"""INSERT INTO adversarial_epochs (
                    epoch_id, execution_id, stage, epoch_number, first_round, last_round,
                    merge_policy, outcome, progress, stalled_epochs, escalation_reason,
                    next_escalation, fixers, failing_suites, findings, disputes, tests_added,
                    patch_fingerprints, failure_fingerprint, no_progress, started_at,
                    completed_at, duration_seconds, usage_json
                ) VALUES ({", ".join("?" for _ in values)})
                ON CONFLICT(execution_id, stage, epoch_number) DO UPDATE SET {assignments}""",
                values,
            )

    def adversarial_epochs_for(self, execution_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        """Every recorded epoch of the given executions, oldest epoch first."""
        ids = [str(value) for value in execution_ids if value]
        if not ids:
            return {}
        slots = ", ".join("?" for _ in ids)
        epochs: dict[str, list[dict[str, Any]]] = {}
        with self.connect() as database:
            for row in database.execute(
                f"SELECT * FROM adversarial_epochs WHERE execution_id IN ({slots}) "
                "ORDER BY execution_id, stage DESC, epoch_number",
                ids,
            ):
                record = dict(row)
                record["progress"] = bool(record.get("progress"))
                _decode_json_fields(record, _EPOCH_JSON_COLUMNS)
                epochs.setdefault(str(record["execution_id"]), []).append(record)
        return epochs

    def adversarial_summary(
        self, repositories: Sequence[str] | str | None = None, *, search: str = "",
        delivery: str = "all",
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
        delivery_sql = _delivery_filter_sql(delivery)
        if delivery_sql:
            conditions.append(delivery_sql)
        where = " WHERE " + " AND ".join(conditions) + clause
        params = [*repository_params, *search_params]
        with self.connect() as database:
            row = database.execute(
                "SELECT COUNT(*), AVG(adversarial_round_count), "
                "SUM(adversarial_outcome = 'clean_first_pass'), "
                "SUM(adversarial_outcome = 'cap_hit'), "
                "AVG(capacity_consumed_percent), "
                f"SUM({_VERIFIED_CLEAN_SQL}), SUM({_BEST_EFFORT_SQL}) "
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
            return _empty_adversarial_summary()
        capacity = row[4]
        verified = int(row[5] or 0)
        best_effort = int(row[6] or 0)
        return {
            "loops": loops,
            "averageRounds": round(float(row[1] or 0.0), 2),
            "cleanFirstPassPercent": round(int(row[2] or 0) * 100 / loops, 1),
            "capHitPercent": round(int(row[3] or 0) * 100 / loops, 1),
            "averageCapacityConsumedPercent": (
                None if capacity is None else round(float(capacity), 2)
            ),
            "testsAdded": int(tests[0] or 0),
            "verifiedCleanCount": verified,
            "bestEffortCount": best_effort,
            "verifiedCleanPercent": round(verified * 100 / loops, 1),
            "bestEffortPercent": round(best_effort * 100 / loops, 1),
        }

    def security_summary(
        self, repositories: Sequence[str] | str | None = None, *, search: str = "",
        delivery: str = "all",
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
        # A review attempt is counted by whether a verdict was recorded at
        # all, not by whether a round completed: a reviewer failure writes
        # security_review_status without ever setting security_outcome (that
        # column only exists for rounds that actually reached an outcome), so
        # filtering on security_outcome silently drops failed attempts from
        # the failure rate. security_review_status stays '' for both "this
        # stage never ran" and "disabled", so a single check on it covers both.
        conditions = ["security_review_status <> ''"]
        if repository_clause:
            conditions.insert(0, repository_clause)
        delivery_sql = _delivery_filter_sql(delivery)
        if delivery_sql:
            conditions.append(delivery_sql)
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

    def record_token_usage_batch(
        self, execution_id: str, repository: str, issue_number: int, events: Sequence[dict[str, Any]]
    ) -> None:
        """Persist every accumulated per-prompt usage event of one work-round.

        Called once, when the work-round reaches a terminal state (see
        ``finish_execution_history``) rather than after each individual AI
        call: some calls (dynamic routing) happen before this execution's own
        ``ai_executions`` row exists, so batching at the end is what lets
        every event still carry the now-known ``execution_id`` without
        blocking on write ordering. ``INSERT OR IGNORE`` on the event's own
        stable id makes a repeated flush of the same (already-persisted)
        state safe — a retried scheduler tick can never duplicate a row.
        """
        if not events:
            return
        columns = ", ".join(("id", "created_at", *_TOKEN_USAGE_COLUMNS))
        slots = ", ".join("?" for _ in range(len(_TOKEN_USAGE_COLUMNS) + 2))
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        rows = []
        for event in events:
            event_id = sanitize_text(event.get("id")) or str(uuid.uuid4())
            values = [event_id, now]
            for column in _TOKEN_USAGE_COLUMNS:
                if column == "execution_id":
                    values.append(execution_id)
                elif column == "repository":
                    values.append(sanitize_text(repository))
                elif column == "issue_number":
                    values.append(int(issue_number or 0))
                elif column in {
                    "attempt_number", "input_tokens", "output_tokens", "reasoning_tokens",
                    "cached_input_tokens", "cache_read_tokens", "cache_write_tokens",
                    "total_tokens", "duration_ms",
                }:
                    raw_value = event.get(column)
                    values.append(None if raw_value is None else int(raw_value))
                elif column in {
                    "estimated_cost", "input_rate_per_million",
                    "cached_input_rate_per_million", "cache_write_rate_per_million",
                    "output_rate_per_million",
                }:
                    raw_value = event.get(column)
                    values.append(None if raw_value is None else float(raw_value))
                elif column == "success":
                    values.append(1 if event.get(column, True) else 0)
                elif column == "currency":
                    values.append(sanitize_text(event.get(column)) or "USD")
                else:
                    values.append(sanitize_text(event.get(column)))
            rows.append(tuple(values))
        with self.connect() as database:
            database.executemany(
                f"INSERT OR IGNORE INTO ai_token_usage ({columns}) VALUES ({slots})",
                rows,
            )

    def token_usage_for_execution(self, execution_id: str) -> list[dict[str, Any]]:
        """Every recorded invocation of one execution, in the order they ran."""
        with self.connect() as database:
            rows = database.execute(
                "SELECT * FROM ai_token_usage WHERE execution_id = ? ORDER BY created_at, rowid",
                (execution_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def token_usage_for_issue(self, repository: str, issue_number: int) -> list[dict[str, Any]]:
        """Every recorded invocation across every attempt of one issue."""
        with self.connect() as database:
            rows = database.execute(
                "SELECT * FROM ai_token_usage WHERE repository = ? AND issue_number = ? "
                "ORDER BY created_at, rowid",
                (sanitize_text(repository), int(issue_number)),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_jev_decision(self, payload: Mapping[str, Any]) -> None:
        """Persist one sanitized Jev/fallback decision. Never stores raw prompts."""
        now = payload.get("created_at") or dt.datetime.now().astimezone().isoformat(timespec="seconds")
        scores = payload.get("scores") if isinstance(payload.get("scores"), dict) else {}
        reason_codes = payload.get("reason_codes") or payload.get("reasonCodes") or []
        accepted = payload.get("recommendation_accepted")
        if accepted is None:
            accepted = payload.get("accepted")
        row = (
            sanitize_text(payload.get("decision_id")) or str(uuid.uuid4()),
            sanitize_text(payload.get("execution_id")),
            int(payload.get("issue_number") or 0),
            sanitize_text(payload.get("repository")),
            sanitize_text(payload.get("decision_type") or payload.get("decisionType")),
            sanitize_text(payload.get("input_fingerprint") or payload.get("inputFingerprint")),
            sanitize_text(payload.get("decision")),
            _optional_float(payload.get("confidence")),
            _json_sanitized(scores),
            _json_sanitized([sanitize_text(item) for item in list(reason_codes)[:24]]),
            _optional_float(payload.get("latency_ms") or payload.get("latencyMs")),
            sanitize_text(payload.get("provider") or "jev") or "jev",
            sanitize_text(payload.get("model")),
            sanitize_text(payload.get("version")),
            _optional_float(payload.get("estimated_cost") or payload.get("estimatedCost")),
            sanitize_text(now),
            sanitize_text(payload.get("fallback_used") or payload.get("fallbackUsed")),
            sanitize_text(payload.get("swarm_action") or payload.get("swarmAction")),
            None if accepted is None else (1 if accepted else 0),
            sanitize_text(payload.get("workflow_outcome") or payload.get("workflowOutcome")),
            sanitize_text(payload.get("source")),
            sanitize_text(payload.get("error_type") or payload.get("errorType")),
            1 if payload.get("jev_enabled", payload.get("source") == "jev") else 0,
            int(payload.get("llm_calls_avoided") or payload.get("llmCallsAvoided") or 0),
            None
            if payload.get("estimated_tokens_avoided", payload.get("estimatedTokensAvoided")) is None
            else int(payload.get("estimated_tokens_avoided") or payload.get("estimatedTokensAvoided") or 0),
            _optional_float(payload.get("estimated_dollar_savings") or payload.get("estimatedDollarSavings")),
        )
        with self.connect() as database:
            database.execute(
                """INSERT OR REPLACE INTO jev_decisions (
                    decision_id, execution_id, issue_number, repository, decision_type,
                    input_fingerprint, decision, confidence, scores, reason_codes,
                    latency_ms, provider, model, version, estimated_cost, created_at,
                    fallback_used, swarm_action, recommendation_accepted, workflow_outcome,
                    source, error_type, jev_enabled, llm_calls_avoided,
                    estimated_tokens_avoided, estimated_dollar_savings
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                row,
            )

    def record_jev_score_comparison(self, payload: Mapping[str, Any]) -> None:
        """Store distinct baseline, Jev, and modified scores for one execution."""
        now = payload.get("created_at") or dt.datetime.now().astimezone().isoformat(timespec="seconds")
        baseline = payload.get("baseline") if isinstance(payload.get("baseline"), dict) else {}
        jev = payload.get("jev") if isinstance(payload.get("jev"), dict) else {}
        modified = payload.get("modified") if isinstance(payload.get("modified"), dict) else {}
        delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
        comparison_id = sanitize_text(payload.get("comparison_id")) or str(uuid.uuid4())
        row = (
            comparison_id,
            sanitize_text(payload.get("execution_id")),
            sanitize_text(payload.get("repository")),
            int(payload.get("issue_number") or 0),
            sanitize_text(now),
            sanitize_text(payload.get("jev_status") or payload.get("status") or "disabled") or "disabled",
            _optional_float(baseline.get("native_score")),
            _optional_float(baseline.get("normalized_score")),
            sanitize_text(baseline.get("prompt_grade")),
            baseline.get("complexity"),
            sanitize_text(baseline.get("task_type")),
            sanitize_text(baseline.get("risk")),
            sanitize_text(baseline.get("context_requirement") or baseline.get("context")),
            sanitize_text(baseline.get("provider")),
            sanitize_text(baseline.get("model")),
            sanitize_text(baseline.get("effort")),
            _json_sanitized(baseline.get("candidates") or []),
            _json_sanitized(baseline.get("inputs") or {}),
            _optional_float(jev.get("native_score") if jev else None),
            _optional_float(jev.get("normalized_score") if jev else None),
            _json_sanitized((jev or {}).get("scores") or (jev or {}).get("component_scores") or {}),
            _json_sanitized((jev or {}).get("reason_codes") or (jev or {}).get("reasonCodes") or []),
            _optional_float((jev or {}).get("confidence")),
            sanitize_text((jev or {}).get("model")),
            sanitize_text((jev or {}).get("version")),
            _optional_float(modified.get("native_score")),
            _optional_float(modified.get("normalized_score")),
            sanitize_text(modified.get("provider")),
            sanitize_text(modified.get("model")),
            sanitize_text(modified.get("effort")),
            _json_sanitized(modified.get("candidates") or []),
            _optional_float(delta.get("absolute")),
            _optional_float(delta.get("percent")),
            1 if (delta.get("routing_changed") or payload.get("routing_changed")) else 0,
            _optional_float(payload.get("estimated_jev_cost")),
            _optional_float(payload.get("estimated_total_cost")),
            int(payload.get("estimated_llm_calls_avoided") or 0),
            None if payload.get("estimated_tokens_avoided") is None else int(payload.get("estimated_tokens_avoided") or 0),
            _optional_float(payload.get("estimated_dollar_savings")),
            _optional_float(payload.get("latency_ms")),
            sanitize_text(payload.get("workflow_outcome")),
        )
        with self.connect() as database:
            database.execute(
                """INSERT OR REPLACE INTO jev_score_comparisons (
                    comparison_id, execution_id, repository, issue_number, created_at, jev_status,
                    baseline_native_score, baseline_normalized_score, baseline_prompt_grade,
                    baseline_complexity, baseline_task_type, baseline_risk, baseline_context,
                    baseline_provider, baseline_model, baseline_effort, baseline_candidates,
                    baseline_inputs, jev_native_score, jev_normalized_score, jev_component_scores,
                    jev_reason_codes, jev_confidence, jev_model, jev_version,
                    modified_native_score, modified_normalized_score, modified_provider,
                    modified_model, modified_effort, modified_candidates, score_delta_absolute,
                    score_delta_percent, routing_changed, estimated_jev_cost, estimated_total_cost,
                    estimated_llm_calls_avoided, estimated_tokens_avoided, estimated_dollar_savings,
                    latency_ms, workflow_outcome
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                row,
            )

    def finish_jev_outcomes(self, execution_id: str, outcome: str) -> None:
        """Stamp the eventual workflow outcome onto Jev rows for later calibration."""
        if not execution_id:
            return
        status = sanitize_text(outcome)
        with self.connect() as database:
            database.execute(
                "UPDATE jev_decisions SET workflow_outcome = ? WHERE execution_id = ? AND workflow_outcome = ''",
                (status, execution_id),
            )
            database.execute(
                "UPDATE jev_score_comparisons SET workflow_outcome = ? WHERE execution_id = ? AND workflow_outcome = ''",
                (status, execution_id),
            )

    def jev_feedback(
        self,
        repositories: Sequence[str] | str | None = None,
        *,
        search: str = "",
        jev_status: str = "",
        provider: str = "",
        outcome: str = "",
        routing_changed: str = "",
        created_after: str = "",
        created_before: str = "",
        min_delta: float | str | None = None,
        max_cost: float | str | None = None,
        limit: int = PAGE_SIZE,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Filterable Jev score comparisons joined to execution outcomes.

        ``provider`` matches as a substring of any provider/model name;
        ``created_after`` / ``created_before``
        (``YYYY-MM-DD``, both inclusive), ``min_delta`` (absolute score change) and
        ``max_cost`` (Jev cost; rows with no recorded cost are kept) run in SQL
        so they see every page of history, not just the current one.
        """
        limit = clamp_page_size(limit)
        offset = clamp_offset(offset)
        repository_clause, repository_params = _repository_filter(repositories)
        conditions = []
        params: list[Any] = []
        if repository_clause:
            conditions.append(repository_clause.replace("repository", "c.repository", 1))
            params.extend(repository_params)
        term = normalize_search(search)
        if term:
            escaped = term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            conditions.append(
                "("
                "CAST(c.issue_number AS TEXT) LIKE ? ESCAPE '\\' OR "
                "LOWER(COALESCE(e.issue_title, '')) LIKE ? ESCAPE '\\' OR "
                "LOWER(COALESCE(e.ai_provider, c.modified_provider, '')) LIKE ? ESCAPE '\\' OR "
                "LOWER(COALESCE(e.model, c.modified_model, '')) LIKE ? ESCAPE '\\' OR "
                "LOWER(COALESCE(e.final_status, c.workflow_outcome, '')) LIKE ? ESCAPE '\\'"
                ")"
            )
            params.extend([pattern, pattern, pattern, pattern, pattern])
        status = str(jev_status or "").strip().lower()
        if status in {"enabled", "disabled", "unavailable", "timeout", "malformed", "authentication", "low_confidence", "fallback"}:
            conditions.append("c.jev_status = ?")
            params.append(status)
        provider_match = normalize_search(provider).lower()
        if provider_match:
            escaped = provider_match.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(
                "LOWER(COALESCE(c.modified_provider, '') || ' ' || COALESCE(c.modified_model, '') || ' ' "
                "|| COALESCE(c.baseline_provider, '') || ' ' || COALESCE(c.baseline_model, '') || ' ' "
                "|| COALESCE(c.jev_model, '') || ' ' "
                "|| COALESCE(e.ai_provider, '')) LIKE ? ESCAPE '\\'"
            )
            params.append(f"%{escaped}%")
        outcome_key = sanitize_text(outcome)
        if outcome_key:
            conditions.append("COALESCE(e.final_status, c.workflow_outcome) = ?")
            params.append(outcome_key)
        if str(routing_changed).strip().lower() in {"1", "true", "yes"}:
            conditions.append("c.routing_changed = 1")
        elif str(routing_changed).strip().lower() in {"0", "false", "no"}:
            conditions.append("c.routing_changed = 0")
        after = str(created_after or "").strip()[:10]
        if after:
            conditions.append("substr(c.created_at, 1, 10) >= ?")
            params.append(after)
        before = str(created_before or "").strip()[:10]
        if before:
            conditions.append("substr(c.created_at, 1, 10) <= ?")
            params.append(before)
        minimum_delta = _optional_number(min_delta)
        if minimum_delta is not None:
            conditions.append("ABS(COALESCE(c.score_delta_absolute, 0)) >= ?")
            params.append(minimum_delta)
        maximum_cost = _optional_number(max_cost)
        if maximum_cost is not None:
            conditions.append("(c.estimated_jev_cost IS NULL OR c.estimated_jev_cost <= ?)")
            params.append(maximum_cost)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        join = (
            "FROM jev_score_comparisons c "
            "LEFT JOIN ai_executions e ON e.execution_id = c.execution_id"
        )
        with self.connect() as database:
            tables = {
                row[0]
                for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "jev_score_comparisons" not in tables:
                return _empty_jev_feedback(limit)
            total = int(database.execute(f"SELECT COUNT(*) {join}{where}", params).fetchone()[0])
            if total == 0:
                offset = 0
            elif offset >= total:
                offset = ((total - 1) // limit) * limit
            rows = list(
                database.execute(
                    "SELECT c.*, e.issue_title, e.issue_url, e.final_status, e.ai_provider, "
                    "e.model AS execution_model, e.effort AS execution_effort, e.started_at AS execution_started "
                    f"{join}{where} ORDER BY c.created_at DESC LIMIT ? OFFSET ?",
                    (*params, limit, offset),
                )
            )
            aggregates = database.execute(
                "SELECT COUNT(*) AS comparisons, "
                "AVG(c.baseline_normalized_score) AS avg_baseline, "
                "AVG(c.jev_normalized_score) AS avg_jev, "
                "AVG(c.modified_normalized_score) AS avg_modified, "
                "AVG(c.score_delta_absolute) AS avg_delta, "
                "AVG(c.latency_ms) AS avg_latency, "
                "SUM(c.estimated_jev_cost) AS jev_cost, "
                "SUM(CASE WHEN c.routing_changed = 1 THEN 1 ELSE 0 END) AS routing_changes, "
                "SUM(CASE WHEN c.jev_status IN ('unavailable', 'timeout', 'malformed', 'authentication', 'low_confidence', 'fallback') THEN 1 ELSE 0 END) AS fallbacks, "
                "SUM(CASE WHEN c.jev_status = 'disabled' THEN 1 ELSE 0 END) AS disabled, "
                "SUM(CASE WHEN c.jev_status = 'enabled' THEN 1 ELSE 0 END) AS enabled, "
                "SUM(c.estimated_llm_calls_avoided) AS llm_calls_avoided, "
                "SUM(c.estimated_tokens_avoided) AS tokens_avoided, "
                "SUM(CASE WHEN COALESCE(e.final_status, c.workflow_outcome) IN ('completed', 'accepted', 'delivered') THEN COALESCE(c.estimated_dollar_savings, 0) ELSE 0 END) AS dollar_savings, "
                "SUM(CASE WHEN COALESCE(e.final_status, c.workflow_outcome) IN ('completed', 'accepted', 'delivered') THEN 1 ELSE 0 END) AS completed "
                f"{join}{where}",
                params,
            ).fetchone()
            outcomes = list(
                database.execute(
                    "SELECT COALESCE(e.final_status, c.workflow_outcome, '') AS outcome, COUNT(*) "
                    f"{join}{where} GROUP BY outcome",
                    params,
                )
            )
            decisions = database.execute(
                "SELECT COUNT(*) AS calls, AVG(confidence) AS avg_confidence, "
                "SUM(latency_ms) AS latency, SUM(estimated_cost) AS cost, "
                "SUM(CASE WHEN fallback_used <> '' THEN 1 ELSE 0 END) AS fallbacks, "
                "SUM(llm_calls_avoided) AS llm_calls_avoided "
                "FROM jev_decisions"
                + (
                    " WHERE repository IN (" + ", ".join("?" for _ in repository_params) + ")"
                    if repository_params
                    else ""
                ),
                repository_params,
            ).fetchone()
        records = [_jev_comparison_row(row) for row in rows]
        agg = dict(aggregates) if aggregates else {}
        completed = int(agg.get("completed") or 0)
        comparisons = int(agg.get("comparisons") or 0)
        jev_cost = float(agg.get("jev_cost") or 0)
        return {
            "records": records,
            "total": total,
            "offset": offset,
            "limit": limit,
            "summary": {
                "comparisons": comparisons,
                "averageBaseline": agg.get("avg_baseline"),
                "averageJev": agg.get("avg_jev"),
                "averageModified": agg.get("avg_modified"),
                "averageDelta": agg.get("avg_delta"),
                "averageLatencyMs": agg.get("avg_latency"),
                "jevCost": jev_cost,
                "routingChanges": int(agg.get("routing_changes") or 0),
                "fallbackCount": int(agg.get("fallbacks") or 0),
                "fallbackRate": (int(agg.get("fallbacks") or 0) / comparisons) if comparisons else 0.0,
                "disabledCount": int(agg.get("disabled") or 0),
                "enabledCount": int(agg.get("enabled") or 0),
                "llmCallsAvoided": int(agg.get("llm_calls_avoided") or 0),
                "estimatedTokensAvoided": int(agg.get("tokens_avoided") or 0),
                "estimatedDollarSavings": float(agg.get("dollar_savings") or 0),
                "completed": completed,
                "completionRate": (completed / comparisons) if comparisons else None,
                "costPerCompleted": (jev_cost / completed) if completed else None,
                "outcomes": {str(name or "unknown"): int(count) for name, count in outcomes},
                "decisions": {
                    "calls": int(decisions["calls"] or 0) if decisions else 0,
                    "averageConfidence": decisions["avg_confidence"] if decisions else None,
                    "latencyMs": decisions["latency"] if decisions else 0,
                    "estimatedCost": decisions["cost"] if decisions else 0,
                    "fallbacks": int(decisions["fallbacks"] or 0) if decisions else 0,
                    "llmCallsAvoided": int(decisions["llm_calls_avoided"] or 0) if decisions else 0,
                },
            },
        }

    def token_usage_totals(
        self,
        repositories: Sequence[str] | str | None = None,
        *,
        agent_type: str = "",
        provider: str = "",
        model: str = "",
        prompt_type: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> dict[str, Any]:
        """Aggregate token/cost totals, optionally filtered by any combination
        of agent type, provider, model, prompt type, and an ``started_at``
        date range — the shape a future usage dashboard needs without any
        schema change (issue #280 item 10)."""
        repository_clause, params = _repository_filter(repositories)
        conditions = [repository_clause] if repository_clause else []
        for column, value in (
            ("agent_type", agent_type),
            ("provider", provider),
            ("model", model),
            ("prompt_type", prompt_type),
        ):
            text = sanitize_text(value)
            if text:
                conditions.append(f"{column} = ?")
                params.append(text)
        if start_date:
            conditions.append("substr(started_at, 1, 10) >= ?")
            params.append(sanitize_text(start_date))
        if end_date:
            conditions.append("substr(started_at, 1, 10) <= ?")
            params.append(sanitize_text(end_date))
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as database:
            row = database.execute(
                "SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), "
                "SUM(reasoning_tokens), SUM(cached_input_tokens), "
                "SUM(total_tokens), SUM(estimated_cost) "
                f"FROM ai_token_usage{where}",
                params,
            ).fetchone()
        return {
            "invocations": int(row[0] or 0),
            "inputTokens": int(row[1]) if row[1] is not None else None,
            "outputTokens": int(row[2]) if row[2] is not None else None,
            "reasoningTokens": int(row[3]) if row[3] is not None else None,
            "cachedInputTokens": int(row[4]) if row[4] is not None else None,
            "totalTokens": int(row[5]) if row[5] is not None else None,
            "estimatedCost": round(float(row[6]), 6) if row[6] is not None else None,
        }

    def usage_report(
        self,
        repositories: Sequence[str] | str | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        """One Usage & cost payload (issue #295).

        Read-only, filtered, grouped and paginated entirely in SQLite — see
        ``usage_report`` for why none of that may move into the desktop. The
        filter keyword arguments are the ones ``UsageFilters`` accepts; the
        grouping/paging ones are ``build_usage_report``'s.
        """
        filter_fields = {
            "start_date", "end_date", "issue_number", "grade", "provider", "model",
            "effort", "agent_type", "prompt_type", "outcome", "coverage",
            "execution_id", "search",
        }
        filters = usage_report.UsageFilters(
            repositories,
            **{key: value for key, value in options.items() if key in filter_fields},
        )
        query = {key: value for key, value in options.items() if key not in filter_fields}
        with self.connect() as database:
            return usage_report.build_usage_report(database, filters, **query)

    def usage_summaries_for_executions(
        self, execution_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Per-execution usage headline for the Execution History cards."""
        with self.connect() as database:
            return usage_report.usage_summaries_for_executions(database, execution_ids)

    def usage_records_for_executions(
        self, execution_ids: Sequence[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Every invocation of the given executions, for card expansion."""
        with self.connect() as database:
            return usage_report.usage_records_for_executions(database, execution_ids)

    def page_for_repository(
        self,
        repositories: Sequence[str] | str | None = None,
        *,
        search: str = "",
        limit: int = PAGE_SIZE,
        offset: int = 0,
        sort: str = "recent",
        delivery: str = "all",
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
        delivery_sql = _delivery_filter_sql(delivery)
        if delivery_sql:
            where += f" AND {delivery_sql}"
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


def _json_sanitize(item: Any) -> Any:
    if isinstance(item, str):
        return sanitize_text(item)
    if isinstance(item, dict):
        return {str(key): _json_sanitize(entry) for key, entry in item.items()}
    if isinstance(item, list):
        return [_json_sanitize(entry) for entry in item]
    if isinstance(item, (int, float, bool)) or item is None:
        return item
    return sanitize_text(item)


def _serialize_json(value: Any, *, empty: str) -> str:
    if value is None or value == "":
        return empty
    return json.dumps(_json_sanitize(value))


def _decode_json_fields(record: dict[str, Any], columns: Sequence[str]) -> None:
    for column in columns:
        raw = record.get(column)
        if isinstance(raw, str) and raw:
            try:
                record[column] = json.loads(raw)
            except ValueError:
                pass


def _delivery_filter_sql(delivery: str) -> str:
    key = str(delivery or "all").strip().lower()
    if key in {"", "all"}:
        return ""
    try:
        return _DELIVERY_FILTERS[key]
    except KeyError as error:
        raise ValueError(f"Unknown delivery filter: {delivery}") from error


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


def attach_adversarial_epochs(
    repository: "ExecutionHistoryRepository", records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Give each record its ``adversarial_epochs`` list (possibly empty)."""
    epochs = repository.adversarial_epochs_for(
        [str(record.get("execution_id") or "") for record in records]
    )
    for record in records:
        record["adversarial_epochs"] = epochs.get(str(record.get("execution_id") or ""), [])
    return records


def attach_token_usage(
    repository: "ExecutionHistoryRepository", records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Give each record its usage headline and its individual invocations.

    Two queries for the whole page rather than two per card. An execution
    with no telemetry gets ``token_usage_summary = None`` rather than a row
    of zeroes: an imported issue, or a run from before #280, has *no* usage
    information, which the card must show as unavailable instead of free.
    """
    ids = [str(record.get("execution_id") or "") for record in records]
    summaries = repository.usage_summaries_for_executions(ids)
    invocations = repository.usage_records_for_executions(ids)
    for record in records:
        execution_id = str(record.get("execution_id") or "")
        record["token_usage_summary"] = summaries.get(execution_id)
        record["token_usage"] = invocations.get(execution_id, [])
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

    def adversarial_epoch(self, summary: dict[str, Any]) -> None:
        """Record one closed adversarial epoch; a no-op when history is off."""
        if self.repository and self.execution_id:
            try:
                self.repository.record_adversarial_epoch(self.execution_id, summary)
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

    def token_usage_batch(
        self, repository_name: str, issue_number: int, events: Sequence[dict[str, Any]]
    ) -> None:
        """Persist this work-round's accumulated per-prompt usage events; a
        no-op when history is off. Telemetry failure here must never surface
        as an error to the caller — see issue #280 item 15."""
        if self.repository and self.execution_id and events:
            try:
                self.repository.record_token_usage_batch(
                    self.execution_id, repository_name, issue_number, events
                )
            except sqlite3.Error as error:
                self.error = sanitize_text(error)

    def record_jev_decision(self, payload: Mapping[str, Any]) -> None:
        if self.repository:
            try:
                if self.execution_id and not payload.get("execution_id"):
                    payload = {**payload, "execution_id": self.execution_id}
                self.repository.record_jev_decision(payload)
            except sqlite3.Error as error:
                self.error = sanitize_text(error)

    def record_jev_score_comparison(self, payload: Mapping[str, Any]) -> None:
        if self.repository:
            try:
                if self.execution_id and not payload.get("execution_id"):
                    payload = {**payload, "execution_id": self.execution_id}
                self.repository.record_jev_score_comparison(payload)
            except sqlite3.Error as error:
                self.error = sanitize_text(error)

    def finish_jev_outcomes(self, outcome: str) -> None:
        if self.repository and self.execution_id:
            try:
                self.repository.finish_jev_outcomes(self.execution_id, outcome)
            except sqlite3.Error as error:
                self.error = sanitize_text(error)


def _page_requested(limit: int | None, offset: int, search: str, delivery: str = "all") -> bool:
    return (
        limit is not None or offset != 0 or bool(normalize_search(search))
        or str(delivery or "all").strip().lower() not in {"", "all"}
    )


def _empty_adversarial_summary() -> dict[str, Any]:
    return {
        "loops": 0,
        "averageRounds": None,
        "cleanFirstPassPercent": None,
        "capHitPercent": None,
        "averageCapacityConsumedPercent": None,
        "testsAdded": 0,
        "verifiedCleanCount": 0,
        "bestEffortCount": 0,
        "verifiedCleanPercent": None,
        "bestEffortPercent": None,
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


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _empty_jev_feedback(limit: int = PAGE_SIZE) -> dict[str, Any]:
    return {
        "records": [],
        "total": 0,
        "offset": 0,
        "limit": clamp_page_size(limit),
        "summary": {
            "comparisons": 0,
            "averageBaseline": None,
            "averageJev": None,
            "averageModified": None,
            "averageDelta": None,
            "averageLatencyMs": None,
            "jevCost": 0.0,
            "routingChanges": 0,
            "fallbackCount": 0,
            "fallbackRate": 0.0,
            "disabledCount": 0,
            "enabledCount": 0,
            "llmCallsAvoided": 0,
            "estimatedTokensAvoided": 0,
            "estimatedDollarSavings": 0.0,
            "completed": 0,
            "completionRate": None,
            "costPerCompleted": None,
            "outcomes": {},
            "decisions": {
                "calls": 0,
                "averageConfidence": None,
                "latencyMs": 0,
                "estimatedCost": 0,
                "fallbacks": 0,
                "llmCallsAvoided": 0,
            },
        },
    }


def _jev_comparison_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("baseline_candidates", "baseline_inputs", "jev_component_scores", "jev_reason_codes", "modified_candidates"):
        raw = data.get(key)
        if isinstance(raw, str) and raw:
            try:
                data[key] = json.loads(raw)
            except json.JSONDecodeError:
                data[key] = [] if "codes" in key or "candidates" in key else {}
        elif raw in ("", None):
            data[key] = [] if "codes" in key or "candidates" in key else {}
    status = str(data.get("jev_status") or "disabled")
    jev_present = status == "enabled" and data.get("jev_normalized_score") is not None
    return {
        "comparisonId": data.get("comparison_id"),
        "executionId": data.get("execution_id"),
        "repository": data.get("repository"),
        "issueNumber": data.get("issue_number"),
        "issueTitle": data.get("issue_title") or "",
        "issueUrl": data.get("issue_url") or "",
        "createdAt": data.get("created_at"),
        "jevStatus": status,
        "jevPresent": jev_present,
        "baselineOnly": status == "disabled",
        "baselineNativeScore": data.get("baseline_native_score"),
        "baselineScore": data.get("baseline_normalized_score"),
        "baselinePromptGrade": data.get("baseline_prompt_grade") or "",
        "baselineComplexity": data.get("baseline_complexity"),
        "baselineTaskType": data.get("baseline_task_type") or "",
        "jevScore": data.get("jev_normalized_score") if jev_present else None,
        "jevNativeScore": data.get("jev_native_score") if jev_present else None,
        "jevConfidence": data.get("jev_confidence") if jev_present else None,
        "jevComponentScores": data.get("jev_component_scores") if jev_present else None,
        "jevReasonCodes": data.get("jev_reason_codes") if jev_present else [],
        "jevModel": data.get("jev_model") or "",
        "jevVersion": data.get("jev_version") or "",
        "modifiedScore": data.get("modified_normalized_score"),
        "modifiedNativeScore": data.get("modified_native_score"),
        "scoreDelta": data.get("score_delta_absolute"),
        "scoreDeltaPercent": data.get("score_delta_percent"),
        "routingChanged": bool(data.get("routing_changed")),
        "baselineProvider": data.get("baseline_provider") or "",
        "baselineModel": data.get("baseline_model") or "",
        "baselineEffort": data.get("baseline_effort") or "",
        "modifiedProvider": data.get("modified_provider") or data.get("ai_provider") or "",
        "modifiedModel": data.get("modified_model") or data.get("execution_model") or "",
        "modifiedEffort": data.get("modified_effort") or data.get("execution_effort") or "",
        "baselineCandidates": data.get("baseline_candidates") or [],
        "modifiedCandidates": data.get("modified_candidates") or [],
        "estimatedJevCost": data.get("estimated_jev_cost"),
        "estimatedTotalCost": data.get("estimated_total_cost"),
        "estimatedLlmCallsAvoided": data.get("estimated_llm_calls_avoided") or 0,
        "estimatedTokensAvoided": data.get("estimated_tokens_avoided"),
        "estimatedDollarSavings": data.get("estimated_dollar_savings"),
        "latencyMs": data.get("latency_ms"),
        "outcome": data.get("final_status") or data.get("workflow_outcome") or "",
        "fallback": status in {"unavailable", "timeout", "malformed", "authentication", "low_confidence", "fallback"},
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

    `--usage` instead prints the Usage & cost report: filtered summary totals,
    reporting-coverage counts, one page of aggregate rows for `--group-by`, one
    page of individual invocation records, and the stable filter facets. Every
    filter (`--start-date`/`--end-date`, `--issue-number`, `--grade`,
    `--provider`, `--model`, `--effort`, `--agent-type`, `--prompt-type`,
    `--outcome`, `--coverage`, `--execution-id`, `--search`) combines, and all
    of the filtering, grouping and paging happens in SQLite — the desktop never
    receives the usage table itself. Dates match the invocation's own activity
    timestamp, never the later batch-persistence time.

    `--validate-pricing` instead validates the versioned pricing catalog and
    exits non-zero if any rate is malformed, negative, or covered by two
    overlapping effective windows.

    `--import-from-github` instead scans that repository's full GitHub issue
    backlog (open and closed) and adds a synthetic `imported` row for any
    issue with no existing execution history row, printing a JSON summary
    object (`totalIssues`/`imported`/`skipped`) instead of the row array —
    the Feedback view's "Import from GitHub" action.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default="",
        help="Path to the SQLite database file. Required except with --validate-pricing.",
    )
    parser.add_argument(
        "--repository",
        action="append",
        default=[],
        help="owner/name to filter by; repeat for multiple repositories, or omit for all.",
    )
    parser.add_argument("--sort", choices=("recent", "rounds_asc", "rounds_desc"), default="recent")
    parser.add_argument(
        "--delivery",
        choices=("all", "verified_clean", "best_effort"),
        default="all",
        help="Keep verified-clean merges or best-effort merges. Default is every execution.",
    )
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
    parser.add_argument(
        "--usage",
        action="store_true",
        help="Print the Usage & cost report (summary, coverage, grouped rows, invocations).",
    )
    parser.add_argument(
        "--group-by",
        default="issue",
        choices=usage_report.GROUP_BY_KEYS,
        help="With --usage, the aggregate table's grouping dimension.",
    )
    parser.add_argument("--usage-sort", default="cost", choices=usage_report.SORT_KEYS)
    parser.add_argument("--usage-direction", default="desc", choices=("asc", "desc"))
    parser.add_argument("--group-offset", type=int, default=0)
    parser.add_argument("--detail-offset", type=int, default=0)
    parser.add_argument(
        "--group-value",
        default=None,
        help="With --usage, drill the invocation list down to one aggregate row.",
    )
    parser.add_argument("--start-date", default="", help="With --usage, earliest activity date (YYYY-MM-DD).")
    parser.add_argument("--end-date", default="", help="With --usage, latest activity date (YYYY-MM-DD).")
    parser.add_argument("--issue-number", default="", help="With --usage, one issue number.")
    parser.add_argument("--provider", default="", help="With --usage, one provider.")
    parser.add_argument("--model", default="", help="With --usage, one model.")
    parser.add_argument("--effort", default="", help="With --usage, one reasoning effort.")
    parser.add_argument("--agent-type", default="", help="With --usage, one agent type/stage.")
    parser.add_argument("--prompt-type", default="", help="With --usage, one prompt type.")
    parser.add_argument(
        "--outcome", default="all", choices=("all", "success", "failure"),
        help="With --usage, keep only successful or only failed invocations.",
    )
    parser.add_argument(
        "--coverage", default="", help="With --usage, one reporting-coverage status.",
    )
    parser.add_argument(
        "--execution-id", default="", help="With --usage, one execution's invocations.",
    )
    parser.add_argument(
        "--validate-pricing",
        action="store_true",
        help="Validate the pricing catalog and exit; non-zero if anything is wrong.",
    )
    parser.add_argument(
        "--jev-feedback",
        action="store_true",
        help="Print Jev score comparisons and aggregate effectiveness metrics.",
    )
    parser.add_argument("--jev-status", default="", help="With --jev-feedback, filter by Jev status.")
    parser.add_argument("--jev-provider", default="", help="With --jev-feedback, filter by provider.")
    parser.add_argument("--jev-outcome", default="", help="With --jev-feedback, filter by workflow outcome.")
    parser.add_argument(
        "--jev-routing-changed",
        default="",
        help="With --jev-feedback, filter to routing changes (true/false).",
    )
    parser.add_argument("--jev-from", default="", help="With --jev-feedback, earliest date (YYYY-MM-DD).")
    parser.add_argument("--jev-to", default="", help="With --jev-feedback, latest date (YYYY-MM-DD).")
    parser.add_argument("--jev-min-delta", default="", help="With --jev-feedback, minimum absolute score change.")
    parser.add_argument("--jev-max-cost", default="", help="With --jev-feedback, maximum Jev cost.")
    args = parser.parse_args(argv)

    if args.validate_pricing:
        import model_pricing

        problems = model_pricing.validate_catalog()
        json.dump(
            {"summary": model_pricing.catalog_summary(), "problems": problems}, sys.stdout
        )
        return 1 if problems else 0

    if not args.db:
        parser.error("--db is required")

    database_path = Path(args.db).expanduser()
    repository_names = [sanitize_text(value) for value in args.repository if value.strip()]
    paging = _page_requested(args.limit, args.offset, args.search, args.delivery) or args.sort != "recent"

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

    if args.usage:
        if not database_path.is_file():
            json.dump(usage_report.empty_report(args.group_by), sys.stdout)
            return 0
        json.dump(
            ExecutionHistoryRepository(database_path).usage_report(
                repository_names,
                start_date=args.start_date,
                end_date=args.end_date,
                issue_number=args.issue_number,
                grade=args.grade,
                provider=args.provider,
                model=args.model,
                effort=args.effort,
                agent_type=args.agent_type,
                prompt_type=args.prompt_type,
                outcome=args.outcome,
                coverage=args.coverage,
                execution_id=args.execution_id,
                search=args.search,
                group_by=args.group_by,
                sort=args.usage_sort,
                direction=args.usage_direction,
                group_offset=args.group_offset,
                detail_offset=args.detail_offset,
                limit=usage_report.USAGE_PAGE_SIZE,
                group_value=args.group_value,
            ),
            sys.stdout,
        )
        return 0

    if args.jev_feedback:
        if not database_path.is_file():
            json.dump(_empty_jev_feedback(), sys.stdout)
            return 0
        json.dump(
            ExecutionHistoryRepository(database_path).jev_feedback(
                repository_names,
                search=args.search,
                jev_status=args.jev_status,
                provider=args.jev_provider,
                outcome=args.jev_outcome,
                routing_changed=args.jev_routing_changed,
                created_after=args.jev_from,
                created_before=args.jev_to,
                min_delta=args.jev_min_delta,
                max_cost=args.jev_max_cost,
                limit=PAGE_SIZE if args.limit is None else args.limit,
                offset=args.offset,
            ),
            sys.stdout,
        )
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
            attach_token_usage(
                repository,
                attach_adversarial_epochs(
                    repository,
                    attach_adversarial_rounds(repository, [row_to_dict(row) for row in rows]),
                ),
            ),
            sys.stdout,
        )
        return 0

    rows, total, offset, limit = repository.page_for_repository(
        repository_names,
        sort=args.sort,
        search=args.search,
        limit=PAGE_SIZE if args.limit is None else args.limit,
        offset=args.offset,
        delivery=args.delivery,
    )
    json.dump(
        {
            "records": attach_token_usage(
                repository,
                attach_adversarial_epochs(
                    repository,
                    attach_adversarial_rounds(repository, [row_to_dict(row) for row in rows]),
                ),
            ),
            "total": total,
            "offset": offset,
            "limit": limit,
            "adversarial": repository.adversarial_summary(
                repository_names, search=args.search, delivery=args.delivery
            ),
            "security": repository.security_summary(
                repository_names, search=args.search, delivery=args.delivery
            ),
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
