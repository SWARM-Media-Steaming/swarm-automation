#!/usr/bin/env python3
"""Per-repository interactive architecture documentation (issue #338).

One canonical, structured documentation model per repository is kept in the
application state directory (never in the monitored repository). The desktop
UI renders persona-specific views from that single model. After completed
issue work the worker runs a *bounded* documentation-impact review:

1. deterministic impact signals decide whether an AI pass is worthwhile at all;
2. the AI returns a small structured patch (never HTML) for the affected
   entities only;
3. the patch is validated and redacted before it is stored;
4. the patch is applied (and ``documentedThrough`` advanced) only when the
   change is in the canonical integration branch - otherwise it stays
   *pending* and is never presented as current architecture.

Every statement carries provenance (observed / inferred / ai_generated /
human), confidence and evidence. Secrets, ``.env`` content and private URLs
are filtered from both the prompt and anything stored or rendered.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

SCHEMA_VERSION = 1

PERSONAS = ("engineer", "architect", "security", "product", "executive")

# Ordered section vocabulary; the renderer hides sections without entities.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("system_context", "System context"),
    ("architecture", "Runtime / component architecture"),
    ("data_flows", "Data flows"),
    ("components", "Components and responsibilities"),
    ("technologies", "Technologies and dependencies"),
    ("data_stores", "Data stores and important schemas"),
    ("integrations", "External integrations"),
    ("security", "Security boundaries and controls"),
    ("deployment", "Deployment / runtime model"),
    ("testing_ci", "Testing and CI"),
    ("decisions", "Architecture decisions and trade-offs"),
    ("risks", "Risks, assumptions and open questions"),
)
SECTION_KEYS = frozenset(key for key, _ in SECTIONS)

KINDS = frozenset(
    {
        "purpose", "component", "flow", "technology", "datastore", "integration",
        "control", "deployment", "test", "decision", "risk", "assumption", "question",
    }
)
PROVENANCE = ("observed", "inferred", "ai_generated", "human")
EVIDENCE_TYPES = frozenset({"path", "symbol", "issue", "pull_request", "commit", "test"})

MAX_OPERATIONS = 25
MAX_ENTITIES = 400
MAX_PENDING = 50
MAX_REVIEWS = 100
MAX_LIST = 12
MAX_EVIDENCE = 8
MAX_TEXT = 600
MAX_NAME = 120
MAX_PROMPT_DIFF_CHARS = 12000
MAX_PROMPT_FILES = 200

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_LABEL = r"\1[REDACTED]"
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|token|basic)\s+)[^\s]+"), _LABEL),
    (re.compile(
        r"(?i)\b((?:api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|client[_-]?secret|"
        r"password|passwd|secret|token|credential|private[_-]?key)\w*\s*[=:]\s*)[^\s,;]+"), _LABEL),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b"), "[REDACTED]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b"), "[REDACTED]"),
    (re.compile(r"-----BEGIN [^-]*-----.*?(?:-----END [^-]*-----|\Z)", re.DOTALL), "[REDACTED]"),
    # URLs (credentialed or not) can leak private hosts and infrastructure identifiers.
    (re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s)\]>\"']+"), "[url]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[REDACTED]"),
    (re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9+/_-])"), "[REDACTED]"),
)


def redact(value: Any, limit: int = MAX_TEXT) -> str:
    """Strip credential-shaped, URL and address content, then bound the length."""
    text = "" if value is None else str(value)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text).strip()
    return text[:limit]


_SENSITIVE_PATH_RE = re.compile(
    r"(?i)(?:^|/)(?:\.env(?:\..*)?|.*\.(?:pem|key|p12|pfx|jks|keystore|crt|cer|der|kdbx)|"
    r"id_(?:rsa|dsa|ecdsa|ed25519)|\.npmrc|\.netrc|\.pypirc|credentials(?:\..*)?|"
    r"secrets?(?:\..*)?|.*\.tfstate|.*\.tfvars)$"
)


def is_sensitive_path(path: str) -> bool:
    return bool(_SENSITIVE_PATH_RE.search(path.replace("\\", "/")))


def safe_repo_path(value: Any) -> str:
    """A repository-relative path usable as evidence, or ``""``."""
    text = str(value or "").strip().replace("\\", "/")
    if not text or len(text) > 200 or "\x00" in text or text.startswith("/") or "://" in text:
        return ""
    if re.match(r"^[A-Za-z]:", text):
        return ""
    parts = PurePosixPath(text).parts
    if not parts or any(part in ("..", ".") for part in parts):
        return ""
    if is_sensitive_path(text):
        return ""
    return text if redact(text, 200) == text else ""


# ---------------------------------------------------------------------------
# Deterministic impact signals
# ---------------------------------------------------------------------------

_SIGNAL_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dependencies", re.compile(
        r"(?i)(?:^|/)(?:cargo\.toml|cargo\.lock|package(?:-lock)?\.json|requirements[^/]*\.txt|pyproject\.toml|"
        r"poetry\.lock|go\.mod|pom\.xml|build\.gradle(?:\.kts)?|gemfile|composer\.json|[^/]+\.csproj)$")),
    ("schema_persistence", re.compile(
        r"(?i)(?:^|/)(?:migrations?|schema|schemas|db|database)/|\.sql$|(?:^|/)schema[^/]*\.[a-z]+$|"
        r"(?:^|/)models?\.(?:py|rs|ts|js)$")),
    ("api", re.compile(
        r"(?i)openapi|swagger|\.proto$|graphql|(?:^|/)(?:api|routes?|controllers?|handlers?|endpoints?)(?:/|\.[a-z]+$)")),
    ("authentication_authorization", re.compile(
        r"(?i)(?:^|[/_.-])(?:auth|oauth|authn|authz|login|session|permissions?|rbac|acl|iam|crypto|secrets?|tokens?)(?:[/_.-]|$)")),
    ("deployment", re.compile(
        r"(?i)(?:^|/)(?:dockerfile[^/]*|docker-compose[^/]*\.ya?ml|tauri\.conf\.json|helm|charts|k8s|kubernetes|"
        r"terraform|deploy|deployment|infra)(?:/|$)|\.tf$|\.dockerfile$")),
    ("ci", re.compile(r"(?i)(?:^|/)(?:\.github/workflows/|\.gitlab-ci\.ya?ml$|jenkinsfile$|azure-pipelines|\.circleci/)")),
    ("queues_messaging", re.compile(r"(?i)queue|kafka|rabbit|amqp|sqs|pubsub|celery|nats|scheduler|cron")),
)
_SOURCE_RE = re.compile(r"(?i)\.(?:py|rs|js|jsx|ts|tsx|go|java|kt|cs|rb|php|swift|c|cc|cpp|h|hpp)$")
_TEST_RE = re.compile(r"(?i)(?:^|/)(?:tests?|__tests__|spec)/|(?:^|/)test_[^/]*$|[._-]tests?\.[a-z]+$|\.spec\.[a-z]+$")


def parse_name_status(text: str) -> list[tuple[str, str]]:
    """``git diff --name-status`` lines -> ``[(status letter, path)]`` (new path for renames)."""
    entries: list[tuple[str, str]] = []
    for line in str(text or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0]:
            entries.append((parts[0][0], parts[-1].strip()))
    return entries


def impact_signals(changes: Iterable[tuple[str, str]]) -> list[str]:
    """Deterministic architectural-impact signals for a change set."""
    found: list[str] = []
    entries = [(status, path) for status, path in changes if path]
    for name, pattern in _SIGNAL_RULES:
        if any(pattern.search(path) and not _TEST_RE.search(path) for _status, path in entries):
            found.append(name)
    structural = [
        path for status, path in entries
        if status in ("A", "D", "R") and _SOURCE_RE.search(path) and not _TEST_RE.search(path)
    ]
    if structural:
        found.append("module_structure")
    if sum(1 for status, path in entries if status == "A" and _TEST_RE.search(path)) >= 3:
        found.append("major_tests")
    return found


# ---------------------------------------------------------------------------
# Validation of AI patches
# ---------------------------------------------------------------------------


class PatchError(ValueError):
    """The structured documentation patch is malformed or unsafe."""


def _confidence(value: Any) -> float:
    if isinstance(value, str):
        value = {"low": 0.4, "medium": 0.7, "high": 0.9}.get(value.strip().lower(), value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PatchError("confidence must be a number between 0 and 1") from None
    if number != number or not 0.0 <= number <= 1.0:
        raise PatchError("confidence must be between 0 and 1")
    return round(number, 2)


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        raise PatchError("text fields must be strings")
    return redact(value, limit)


def _string_list(value: Any, limit: int = MAX_TEXT) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise PatchError("expected a list of strings")
    return [item for item in (_text(entry, limit) for entry in value[:MAX_LIST]) if item]


def _id_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise PatchError("expected a list of entity ids")
    result = []
    for entry in value[:MAX_LIST]:
        if not isinstance(entry, str) or not _ID_RE.match(entry):
            raise PatchError("invalid entity id reference")
        result.append(entry)
    return result


def _evidence(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise PatchError("evidence must be a list")
    items: list[dict[str, Any]] = []
    for raw in value[:MAX_EVIDENCE]:
        if not isinstance(raw, dict):
            raise PatchError("evidence entries must be objects")
        kind = raw.get("type")
        if kind not in EVIDENCE_TYPES:
            raise PatchError("unknown evidence type")
        entry: dict[str, Any] = {"type": kind}
        reference = raw.get("ref")
        if kind in ("path", "test"):
            path = safe_repo_path(reference)
            if not path:
                continue  # unsafe or sensitive paths are dropped, not stored
            entry["ref"] = path
        elif kind in ("issue", "pull_request"):
            if not isinstance(reference, (int, str)) or not re.fullmatch(r"#?\d{1,9}", str(reference)):
                continue
            entry["ref"] = str(reference).lstrip("#")
        elif kind == "commit":
            if not isinstance(reference, str) or not _SHA_RE.match(reference.lower()):
                continue
            entry["ref"] = reference.lower()
        else:  # symbol
            symbol = _text(reference, 120)
            if not symbol or "[REDACTED]" in symbol:
                continue
            entry["ref"] = symbol
        line = raw.get("line")
        if isinstance(line, int) and not isinstance(line, bool) and 0 < line < 10_000_000:
            entry["line"] = line
        label = _text(raw.get("label"), 120)
        if label:
            entry["label"] = label
        items.append(entry)
    return items


_ENTITY_FIELDS = frozenset(
    {
        "id", "section", "kind", "name", "summary", "personas", "responsibilities", "depends_on",
        "technologies", "steps", "risks", "evidence", "provenance", "confidence",
    }
)


def validate_entity(raw: Any, *, allow_human: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PatchError("entity must be an object")
    unknown = set(raw) - _ENTITY_FIELDS
    if unknown:
        raise PatchError(f"unknown entity fields: {', '.join(sorted(str(item)[:30] for item in unknown))}")
    identifier = raw.get("id")
    if not isinstance(identifier, str) or not _ID_RE.match(identifier):
        raise PatchError("entity id must be lowercase letters, digits, '.', '_' or '-'")
    section = raw.get("section")
    if section not in SECTION_KEYS:
        raise PatchError("unknown section")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise PatchError("unknown entity kind")
    provenance = raw.get("provenance")
    if provenance not in PROVENANCE:
        raise PatchError("provenance must be observed, inferred, ai_generated or human")
    if provenance == "human" and not allow_human:
        raise PatchError("AI output cannot claim human-authored provenance")
    name = _text(raw.get("name"), MAX_NAME)
    if not name:
        raise PatchError("entity needs a name")
    personas_raw = raw.get("personas") or {}
    if not isinstance(personas_raw, dict):
        raise PatchError("personas must be an object")
    personas: dict[str, str] = {}
    for persona, text in personas_raw.items():
        if persona not in PERSONAS:
            raise PatchError("unknown persona")
        cleaned = _text(text)
        if cleaned:
            personas[persona] = cleaned
    evidence = _evidence(raw.get("evidence"))
    if provenance == "observed" and not evidence:
        raise PatchError("observed statements need evidence")
    return {
        "id": identifier,
        "section": section,
        "kind": kind,
        "name": name,
        "summary": _text(raw.get("summary")),
        "personas": personas,
        "responsibilities": _string_list(raw.get("responsibilities")),
        "depends_on": [item for item in _id_list(raw.get("depends_on")) if item != identifier],
        "technologies": _string_list(raw.get("technologies"), 80),
        "steps": _id_list(raw.get("steps")),
        "risks": _string_list(raw.get("risks")),
        "evidence": evidence,
        "provenance": provenance,
        "confidence": _confidence(raw.get("confidence")),
    }


def validate_review(raw: Any) -> dict[str, Any]:
    """Validate the AI's structured review; returns a normalized review."""
    if not isinstance(raw, dict):
        raise PatchError("review must be a JSON object")
    unknown = set(raw) - {"impact", "reason", "confidence", "operations"}
    if unknown:
        raise PatchError("unknown review fields")
    impact = raw.get("impact")
    if impact not in ("none", "update"):
        raise PatchError("impact must be 'none' or 'update'")
    reason = _text(raw.get("reason"), 300)
    confidence = _confidence(raw.get("confidence", 0.7))
    operations: list[dict[str, Any]] = []
    if impact == "update":
        ops = raw.get("operations")
        if not isinstance(ops, list) or not ops:
            raise PatchError("an update needs operations")
        if len(ops) > MAX_OPERATIONS:
            raise PatchError(f"at most {MAX_OPERATIONS} operations are allowed")
        for op in ops:
            if not isinstance(op, dict) or op.get("op") not in ("upsert", "remove"):
                raise PatchError("operation must be upsert or remove")
            if op["op"] == "upsert":
                if set(op) - {"op", "entity"}:
                    raise PatchError("unknown operation fields")
                operations.append({"op": "upsert", "entity": validate_entity(op.get("entity"))})
            else:
                if set(op) - {"op", "id"}:
                    raise PatchError("unknown operation fields")
                identifier = op.get("id")
                if not isinstance(identifier, str) or not _ID_RE.match(identifier):
                    raise PatchError("invalid entity id")
                operations.append({"op": "remove", "id": identifier})
    return {"impact": impact, "reason": reason, "confidence": confidence, "operations": operations}


def parse_review(text: str) -> dict[str, Any]:
    """Extract and validate the JSON object in an AI response."""
    body = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", body, re.DOTALL)
    if fenced:
        body = fenced.group(1)
    else:
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            raise PatchError("no JSON object in the documentation review")
        body = body[start:end + 1]
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as error:
        raise PatchError(f"documentation review is not valid JSON: {error.msg}") from None
    return validate_review(raw)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def build_prompt(
    *, repository: str, issue_number: int, issue_title: str, signals: list[str],
    name_status: list[tuple[str, str]], diff_text: str, existing: dict[str, dict[str, Any]],
) -> str:
    """Bounded, redacted prompt. Only file names and a filtered diff are included."""
    files = [
        f"{status} {path}" for status, path in name_status[:MAX_PROMPT_FILES]
        if not is_sensitive_path(path) and safe_repo_path(path)
    ]
    catalog = [
        f"- {entity['id']} ({entity['section']}/{entity['kind']}, {entity['provenance']}): {entity['name']}"
        for entity in list(existing.values())[:80]
    ]
    sections = ", ".join(sorted(SECTION_KEYS))
    return (
        "You are maintaining structured architecture documentation for a software repository. "
        "Do NOT use tools, read files or edit anything. Reply with a single JSON object only.\n\n"
        f"Repository: {redact(repository, 120)}\n"
        f"Issue #{int(issue_number)}: {redact(issue_title, 200)}\n"
        f"Deterministic impact signals: {', '.join(signals) or 'none'}\n\n"
        "Decide whether this completed change alters the architecture (components, data flows, technologies, "
        "data stores, integrations, security controls, deployment, testing/CI, decisions, risks). "
        'If not, reply {"impact":"none","reason":"...","confidence":0.8}.\n'
        'Otherwise reply {"impact":"update","reason":"...","confidence":0.0-1.0,"operations":[...]} with at most '
        f"{MAX_OPERATIONS} operations, touching only affected entities:\n"
        '  {"op":"upsert","entity":{"id","section","kind","name","summary","personas":{"engineer|architect|security|'
        'product|executive":"..."},"responsibilities":[],"depends_on":[ids],"technologies":[],"steps":[ids],'
        '"risks":[],"evidence":[{"type":"path|symbol|issue|pull_request|commit|test","ref":"...","line":n}],'
        '"provenance":"observed|inferred|ai_generated","confidence":0.0-1.0}}\n'
        '  {"op":"remove","id":"..."}\n'
        f"Sections: {sections}. Ids are lowercase [a-z0-9._-], max 64 chars. 'observed' statements need path/symbol/"
        "test evidence. Never include secret values, keys, tokens, .env content, credentials, hostnames, URLs or "
        "IP addresses; describe roles and file names instead. Never claim human provenance.\n\n"
        "Existing entities (update by reusing the id):\n" + ("\n".join(catalog) or "(none yet)") + "\n\n"
        "Changed files:\n" + ("\n".join(files) or "(none)") + "\n\n"
        "Filtered diff (redacted, truncated):\n" + redact(diff_text, MAX_PROMPT_DIFF_CHARS) + "\n"
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _slug(repository: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", str(repository))[:80].strip(".") or "repo"
    return f"{cleaned}-{hashlib.sha256(str(repository).encode()).hexdigest()[:8]}"


def _empty(repository: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "repository": redact(repository, 200),
        "documentedThrough": "",
        "documentedAt": "",
        "seeded": False,
        "entities": {},
        "pending": [],
        "reviews": [],
        "updatedAt": "",
    }


class ArchitectureStore:
    """JSON snapshot keyed by repository under the application state directory."""

    def __init__(self, state_dir: Path | str, repository: str):
        self.repository = str(repository)
        self.path = Path(state_dir) / f"{_slug(self.repository)}.json"

    # -- persistence -------------------------------------------------------
    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _empty(self.repository)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
            return _empty(self.repository)
        base = _empty(self.repository)
        base.update({key: data[key] for key in base if key in data})
        if not isinstance(base["entities"], dict):
            base["entities"] = {}
        for key in ("pending", "reviews"):
            if not isinstance(base[key], list):
                base[key] = []
        return base

    def save(self, snapshot: dict[str, Any]) -> None:
        snapshot["updatedAt"] = now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".arch-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(snapshot, stream, indent=1, sort_keys=True)
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    # -- mutation ----------------------------------------------------------
    @staticmethod
    def apply_operations(
        snapshot: dict[str, Any], operations: list[dict[str, Any]], context: dict[str, Any],
    ) -> int:
        """Apply validated operations; human-authored entities are never overwritten."""
        entities = snapshot["entities"]
        applied = 0
        for op in operations:
            if op["op"] == "remove":
                current = entities.get(op["id"])
                if current is None or current.get("provenance") == "human":
                    continue
                del entities[op["id"]]
                applied += 1
                continue
            entity = dict(op["entity"])
            current = entities.get(entity["id"])
            if current is not None and current.get("provenance") == "human":
                continue
            if current is None and len(entities) >= MAX_ENTITIES:
                continue
            history = list((current or {}).get("history") or [])[-4:]
            history.append({key: context.get(key, "") for key in ("issue", "commit", "at")})
            entity["history"] = history
            entity["updatedBy"] = history[-1]
            entities[entity["id"]] = entity
            applied += 1
        return applied

    def _remember_review(self, snapshot: dict[str, Any], record: dict[str, Any]) -> None:
        snapshot["reviews"] = (snapshot["reviews"] + [record])[-MAX_REVIEWS:]

    def record_review(
        self, *, issue_number: int, issue_title: str, commit: str, review: dict[str, Any],
        signals: list[str], merged: bool, branch: str = "", pull_request: str = "",
    ) -> dict[str, Any]:
        """Persist the outcome of a completed documentation-impact review."""
        commit = commit.lower()
        if not _SHA_RE.match(commit):
            raise PatchError("a full or abbreviated commit sha is required")
        snapshot = self.load()
        at = now_iso()
        context = {"issue": int(issue_number), "commit": commit, "at": at}
        base = {
            "issue": int(issue_number),
            "title": redact(issue_title, 120),
            "commit": commit,
            "at": at,
            "signals": [redact(item, 40) for item in signals][:12],
            "reason": review.get("reason", ""),
            "confidence": review.get("confidence", 0.0),
            "branch": redact(branch, 80),
            "pullRequest": pull_request if re.fullmatch(r"\d{1,9}", str(pull_request or "")) else "",
        }
        if review["impact"] == "none":
            record = dict(base, status="no_change", state="current" if merged else "pending")
            if merged:
                snapshot["documentedThrough"], snapshot["documentedAt"] = commit, at
            self._remember_review(snapshot, record)
        elif merged:
            applied = self.apply_operations(snapshot, review["operations"], context)
            snapshot["documentedThrough"], snapshot["documentedAt"] = commit, at
            self._remember_review(snapshot, dict(base, status="updated", state="current", applied=applied))
        else:
            pending = dict(base, status="updated", state="pending", operations=review["operations"])
            snapshot["pending"] = (snapshot["pending"] + [pending])[-MAX_PENDING:]
            self._remember_review(
                snapshot, dict(base, status="updated", state="pending", applied=0))
        self.save(snapshot)
        return snapshot

    def record_failure(self, *, issue_number: int, commit: str, error: str) -> None:
        """A failed/paused/quota-limited review changes nothing but the audit trail."""
        snapshot = self.load()
        self._remember_review(snapshot, {
            "issue": int(issue_number), "commit": commit.lower() if _SHA_RE.match(commit.lower()) else "",
            "at": now_iso(), "status": "failed", "state": "not_applied", "reason": redact(error, 200),
            "signals": [], "confidence": 0.0,
        })
        self.save(snapshot)

    def reconcile(self, is_merged: Callable[[str], bool]) -> int:
        """Promote pending patches whose commit has reached the canonical branch."""
        snapshot = self.load()
        if not snapshot["pending"]:
            return 0
        remaining, promoted = [], 0
        for record in snapshot["pending"]:
            try:
                merged = bool(is_merged(record.get("commit", "")))
            except Exception:  # noqa: BLE001 - an unknown state must stay pending
                merged = False
            if not merged:
                remaining.append(record)
                continue
            at = now_iso()
            try:
                operations = validate_review(
                    {"impact": "update", "reason": "", "confidence": record.get("confidence", 0.5),
                     "operations": record.get("operations") or []})["operations"]
            except PatchError:
                operations = []
            self.apply_operations(
                snapshot, operations, {"issue": record.get("issue", 0), "commit": record.get("commit", ""), "at": at})
            snapshot["documentedThrough"], snapshot["documentedAt"] = record.get("commit", ""), at
            for review in snapshot["reviews"]:
                if review.get("commit") == record.get("commit") and review.get("state") == "pending":
                    review["state"] = "current"
            promoted += 1
        snapshot["pending"] = remaining
        self.save(snapshot)
        return promoted

    # -- baseline ----------------------------------------------------------
    def seed_from_workspace(self, root: Path | str) -> bool:
        """Deterministic, privacy-safe baseline: file/directory names only."""
        snapshot = self.load()
        if snapshot["entities"] or snapshot["seeded"]:
            return False
        entities = seed_entities(Path(root))
        if not entities:
            return False
        snapshot["entities"] = {entity["id"]: entity for entity in entities}
        snapshot["seeded"] = True
        self.save(snapshot)
        return True

    # -- read model --------------------------------------------------------
    def view(self, *, enabled: bool = True) -> dict[str, Any]:
        snapshot = self.load()
        entities = sorted(snapshot["entities"].values(), key=lambda item: (item["section"], item["name"].lower()))
        pending = [
            {
                "issue": record.get("issue"), "title": record.get("title", ""), "commit": record.get("commit", ""),
                "at": record.get("at", ""), "reason": record.get("reason", ""),
                "confidence": record.get("confidence", 0.0), "pullRequest": record.get("pullRequest", ""),
                "touches": [
                    op["entity"]["name"] if op["op"] == "upsert" else op["id"]
                    for op in record.get("operations", [])
                ][:MAX_LIST],
            }
            for record in snapshot["pending"]
        ]
        if not entities:
            freshness = "empty"
        elif pending:
            freshness = "pending"
        elif snapshot["documentedThrough"]:
            freshness = "current"
        else:
            freshness = "baseline"
        return {
            "schema": SCHEMA_VERSION,
            "enabled": bool(enabled),
            "repository": snapshot["repository"],
            "freshness": freshness,
            "documentedThrough": snapshot["documentedThrough"],
            "documentedAt": snapshot["documentedAt"],
            "updatedAt": snapshot["updatedAt"],
            "sections": [{"key": key, "title": title} for key, title in SECTIONS],
            "personas": list(PERSONAS),
            "entities": entities,
            "pending": pending,
            "reviews": list(reversed(snapshot["reviews"]))[:20],
        }


_MANIFESTS: tuple[tuple[str, str], ...] = (
    ("Cargo.toml", "Rust (Cargo)"), ("package.json", "Node.js (npm)"), ("pyproject.toml", "Python (pyproject)"),
    ("requirements.txt", "Python (pip)"), ("go.mod", "Go modules"), ("pom.xml", "Java (Maven)"),
    ("build.gradle", "Gradle"), ("build.gradle.kts", "Gradle (Kotlin DSL)"), ("Gemfile", "Ruby (Bundler)"),
    ("composer.json", "PHP (Composer)"), ("Dockerfile", "Docker"), ("docker-compose.yml", "Docker Compose"),
    ("tauri.conf.json", "Tauri"),
)
_SKIP_DIRS = frozenset({
    "node_modules", "target", "dist", "build", "vendor", "venv", "env", "__pycache__", "coverage", "out", "gen",
})


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-.")[:56] or "item"


def seed_entities(root: Path) -> list[dict[str, Any]]:
    """Names-only baseline entities. Contents of source files are never read."""
    entities: list[dict[str, Any]] = []
    if not root.is_dir():
        return entities
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return entities

    def add(section: str, kind: str, identifier: str, name: str, summary: str, provenance: str,
            evidence_path: str, confidence: float) -> None:
        path = safe_repo_path(evidence_path)
        entities.append({
            "id": f"seed.{identifier}"[:64], "section": section, "kind": kind, "name": redact(name, MAX_NAME),
            "summary": redact(summary), "personas": {}, "responsibilities": [], "depends_on": [],
            "technologies": [], "steps": [], "risks": [],
            "evidence": [{"type": "path", "ref": path}] if path else [], "provenance": provenance,
            "confidence": confidence,
        })

    readme = next((root / name for name in ("README.md", "README.rst", "README.txt", "README") if (root / name).is_file()), None)
    if readme:
        try:
            with readme.open("r", encoding="utf-8", errors="replace") as stream:
                head = stream.read(4000)
        except OSError:
            head = ""
        paragraph = next((block.strip() for block in re.split(r"\n\s*\n", head)
                          if block.strip() and not block.lstrip().startswith(("#", "!", "[", "<", "|", "```"))), "")
        if paragraph:
            add("system_context", "purpose", "purpose", "Repository purpose", " ".join(paragraph.split()),
                "observed", readme.name, 0.6)
    for filename, label in _MANIFESTS:
        if (root / filename).is_file():
            add("technologies", "technology", _slugify(label), label, f"Detected from {filename}.",
                "observed", filename, 0.9)
    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        add("testing_ci", "test", "github-actions", "GitHub Actions workflows", "CI workflows are defined in the repository.",
            "observed", ".github/workflows", 0.9)
    for child in children:
        if (not child.is_dir() or child.name.startswith(".") or child.name in _SKIP_DIRS
                or child.is_symlink() or is_sensitive_path(child.name)):
            continue
        add("components", "component", f"dir-{_slugify(child.name)}", child.name,
            f"Top-level directory `{child.name}`; its role has not been documented yet.", "inferred", child.name, 0.4)
        if len([e for e in entities if e["kind"] == "component"]) >= 20:
            break
    return entities


def _git_output(workspace: Path, *arguments: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(workspace), *arguments], capture_output=True, text=True, timeout=30, check=False)
    return result.stdout if result.returncode == 0 else ""


def collect_change(workspace: Path, base: str, commit: str) -> tuple[list[tuple[str, str]], str]:
    """Changed files (name-status) and a diff limited to non-sensitive source files."""
    changes = parse_name_status(_git_output(workspace, "diff", "--name-status", base, commit))
    safe = [path for _status, path in changes if not is_sensitive_path(path) and safe_repo_path(path)]
    diff = ""
    if safe:
        diff = _git_output(workspace, "diff", "--unified=1", "--no-color", base, commit, "--", *safe[:60])
    return changes, diff[:MAX_PROMPT_DIFF_CHARS * 2]


# ---------------------------------------------------------------------------
# Worker integration
# ---------------------------------------------------------------------------


class ArchitectureDocsMixin:
    """Documentation-impact review run by the issue worker after completed work."""

    def architecture_store(self) -> ArchitectureStore:
        directory = Path(self.config.execution_history_db).parent / "architecture_docs"
        return ArchitectureStore(directory, self.config.github_repository)

    def commit_on_integration_branch(self, commit: str) -> bool:
        """True once ``commit`` is reachable from the remote integration branch."""
        if not _SHA_RE.match(str(commit).lower()):
            return False
        remote = f"{self.config.remote_name}/{self.config.integration_branch}"
        self.git("fetch", self.config.remote_name, self.config.integration_branch, check=False)
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            [self.config.git_bin, "-C", str(self.config.repo_dir), "merge-base", "--is-ancestor", commit, remote],
            capture_output=True, check=False,
        ).returncode == 0

    def run_architecture_docs_review(
        self, *, base_sha: str, commit_sha: str, merged: bool, pull_request_url: str, branch: str,
    ) -> str:
        """Bounded documentation-impact review. Never raises; returns a short status.

        Statuses: ``disabled``, ``no_signals``, ``no_change``, ``updated``, ``pending``, ``skipped``, ``failed``.
        """
        from swarm_issue_worker import WorkerError, log  # local import: avoids a cycle at load time

        if not self.config.architecture_docs_enabled or not self.issue:
            return "disabled"
        store = self.architecture_store()
        try:
            store.reconcile(self.commit_on_integration_branch)
            name_status, diff_text = collect_change(Path(self.config.repo_dir), base_sha, commit_sha)
            signals = impact_signals(name_status)
            pr_match = re.search(r"/pull/(\d+)", pull_request_url or "")
            pull_request = pr_match.group(1) if pr_match else ""
            common = dict(issue_number=self.issue.number, issue_title=self.issue.title, commit=commit_sha,
                          signals=signals, merged=merged, branch=branch, pull_request=pull_request)
            if not signals:
                store.record_review(
                    review={"impact": "none", "reason": "No architectural impact signals in the changed files.",
                            "confidence": 0.9, "operations": []}, **common)
                log(f"Architecture documentation for issue #{self.issue.number}: no impact signals; nothing to update.")
                return "no_signals"
            if self.worktree_status():
                log("WARNING: Architecture documentation review skipped: the checkout has uncommitted changes.")
                return "skipped"
            existing = store.load()["entities"]
            prompt = build_prompt(
                repository=self.config.github_repository, issue_number=self.issue.number,
                issue_title=self.issue.title, signals=signals, name_status=name_status,
                diff_text=diff_text, existing=existing)
            review = self._run_documentation_pass(prompt)
            store.record_review(review=review, **common)
            status = review["impact"] == "none" and "no_change" or (merged and "updated" or "pending")
            log(f"Architecture documentation for issue #{self.issue.number}: {status}.")
            return status
        except (PatchError, WorkerError, OSError, ValueError) as error:
            log(f"WARNING: Architecture documentation review did not complete: {redact(error, 200)}")
            try:
                store.record_failure(issue_number=self.issue.number, commit=commit_sha, error=str(error))
            except Exception:  # noqa: BLE001
                pass
            return "failed"
        except Exception as error:  # noqa: BLE001 - documentation must never break delivery
            log(f"WARNING: Architecture documentation review failed: {redact(error, 200)}")
            return "failed"

    def _run_documentation_pass(self, prompt: str) -> dict[str, Any]:
        """One fresh, tool-free AI session; any repository edit is discarded."""
        import dataclasses
        import uuid
        from swarm_issue_worker import WorkerError

        assert self.choice
        original = self.choice
        self.choice = dataclasses.replace(original, session_id=str(uuid.uuid4()), resume=False)
        try:
            self.issue_images = []
            status = self.run_ai(prompt, activity="reviewing architecture documentation impact")
            output = ""
            if self.ai_output_file.exists():
                output = self.ai_output_file.read_text(encoding="utf-8", errors="replace")
            if status != 0 or not output.strip():
                raise WorkerError("the documentation review session did not produce a result")
        finally:
            self.choice = original
        if self.worktree_status():
            self.git("checkout", "--", ".", check=False)
            self.git("clean", "-fdq", check=False)
            raise WorkerError("the documentation review modified the repository; result discarded")
        return parse_review(output)


# ---------------------------------------------------------------------------
# CLI used by the desktop app
# ---------------------------------------------------------------------------


def handle_action(payload: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    repository = str(payload.get("repository") or "").strip()
    if not repository:
        return {"error": "A repository is required."}
    store = ArchitectureStore(state_dir, repository)
    enabled = bool(payload.get("enabled"))
    workspace = str(payload.get("workspace") or "")
    action = payload.get("action") or "view"
    if action != "view":
        return {"error": "Unsupported action."}
    if enabled and workspace:
        store.seed_from_workspace(workspace)
    return store.view(enabled=enabled)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--action", default="view", choices=("view",))
    args = parser.parse_args(argv)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        payload.setdefault("action", args.action)
        result = handle_action(payload, Path(args.state_dir).expanduser())
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"error": redact(error, 300)}))
        return 1
    print(json.dumps(result))
    return 0 if not result.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
