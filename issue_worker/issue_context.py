"""Tiered, bounded issue-description context for Jev issue-level decisions.

Short descriptions are sent whole. Longer ones become a structured package: a
bounded summary of the sections a decision depends on (requested change,
acceptance criteria, reproduction steps, constraints, components, dependencies,
security, testing, out of scope) plus targeted original excerpts, including the
beginning and end of the description. The default summarizer is deterministic;
a caller may inject a cheaper LLM-backed one, which is time-boxed, retried and
always falls back to the deterministic path. Everything is sanitized before it
is bounded, so a credential straddling a cut is still redacted.
"""

from __future__ import annotations

import concurrent.futures
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ai_execution_history import sanitize_text

CONTEXT_VERSION = 1
HARD_MAX_CHARS = 24000
EXPANDED_FACTOR = 2
HEAD_TAIL_CHARS = 400

SECTION_KEYS = (
    "requested_change",
    "acceptance_criteria",
    "reproduction_steps",
    "technical_constraints",
    "affected_components",
    "dependencies",
    "security",
    "testing",
    "out_of_scope",
)
# Sections whose original text is always worth quoting.
EXCERPT_KEYS = ("acceptance_criteria", "reproduction_steps", "security", "out_of_scope")

_SECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("acceptance_criteria", re.compile(r"acceptance|definition of done|success criteria|done when", re.I)),
    ("reproduction_steps", re.compile(r"reproduc|steps to|repro\b|how to reproduce", re.I)),
    ("out_of_scope", re.compile(r"out[- ]of[- ]scope|non[- ]goals?|not in scope|won'?t do", re.I)),
    ("security", re.compile(r"secur|vulnerab|threat|privacy|credential|auth(?:entication|orization)?\b", re.I)),
    ("testing", re.compile(r"\btest|verification|uat\b|qa\b", re.I)),
    ("dependencies", re.compile(r"dependenc|cross[- ]repo|blocked by|related (?:issues|repos)|prerequisite", re.I)),
    ("affected_components", re.compile(r"affected|components?|relevant (?:current )?implementation|files?\b|modules?", re.I)),
    ("technical_constraints", re.compile(r"constraint|requirement|limitation|compatib|must not|technical", re.I)),
    ("requested_change", re.compile(r"objective|goal|summary|description|problem|request|proposal|implementation prompt|feature|change", re.I)),
)
_HEADING = re.compile(r"^\s{0,3}(?:#{1,6}\s+(?P<h>.+?)\s*#*|\*\*(?P<b>[^*\n]{2,80})\*\*:?|(?P<c>[A-Za-z][A-Za-z /&-]{2,60}):)\s*$")

Summarizer = Callable[[Mapping[str, str], int], Any]


@dataclass(frozen=True)
class IssueContextSettings:
    max_raw_chars: int = 6000
    max_summary_chars: int = 1500
    max_excerpt_chars: int = 3000
    summary_timeout_seconds: float = 5.0
    summary_retries: int = 1

    def normalized(self) -> "IssueContextSettings":
        def bound(value: Any, default: int, low: int) -> int:
            try:
                number = int(value)
            except (TypeError, ValueError):
                number = default
            return min(HARD_MAX_CHARS, max(low, number))

        try:
            timeout = min(30.0, max(0.5, float(self.summary_timeout_seconds)))
        except (TypeError, ValueError):
            timeout = 5.0
        try:
            retries = min(3, max(0, int(self.summary_retries)))
        except (TypeError, ValueError):
            retries = 1
        return IssueContextSettings(
            bound(self.max_raw_chars, 6000, 200),
            bound(self.max_summary_chars, 1500, 100),
            bound(self.max_excerpt_chars, 3000, 100),
            timeout,
            retries,
        )

    def expanded(self) -> "IssueContextSettings":
        s = self.normalized()
        return IssueContextSettings(
            min(HARD_MAX_CHARS, s.max_raw_chars * EXPANDED_FACTOR),
            min(HARD_MAX_CHARS, s.max_summary_chars * EXPANDED_FACTOR),
            min(HARD_MAX_CHARS, s.max_excerpt_chars * EXPANDED_FACTOR),
            s.summary_timeout_seconds,
            s.summary_retries,
        )


def issue_context_settings_from(source: Any) -> IssueContextSettings:
    """Read the ``context_*`` fields from JevSettings (or any object/mapping)."""
    def get(name: str, default: Any) -> Any:
        value = source.get(name, default) if isinstance(source, Mapping) else getattr(source, name, default)
        return default if value is None else value

    defaults = IssueContextSettings()
    return IssueContextSettings(
        get("context_max_raw_chars", defaults.max_raw_chars),
        get("context_max_summary_chars", defaults.max_summary_chars),
        get("context_max_excerpt_chars", defaults.max_excerpt_chars),
        get("context_summary_timeout_seconds", defaults.summary_timeout_seconds),
        get("context_summary_retries", defaults.summary_retries),
    ).normalized()


def _classify(heading: str) -> str | None:
    for key, pattern in _SECTION_PATTERNS:
        if pattern.search(heading):
            return key
    return None


def extract_sections(text: str) -> dict[str, str]:
    """Group the description by recognised heading; repeated headings are joined."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            title = match.group("h") or match.group("b") or match.group("c") or ""
            current = _classify(title)
            continue
        if current and line.strip():
            sections.setdefault(current, []).append(line.rstrip())
    return {key: "\n".join(lines).strip() for key, lines in sections.items() if lines}


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def deterministic_summary(sections: Mapping[str, str], limit: int) -> str:
    present = [key for key in SECTION_KEYS if sections.get(key)]
    if not present:
        return ""
    share = max(40, limit // len(present))
    parts = [f"{key}: {_clip(' '.join(sections[key].split()), share - len(key) - 3)}" for key in present]
    return _clip("\n".join(parts), limit)


def _run_summarizer(
    summarizer: Summarizer, sections: Mapping[str, str], limit: int, settings: IssueContextSettings
) -> tuple[str, str]:
    """Returns (summary, source). Never raises; failure yields the deterministic summary."""
    attempts = settings.summary_retries + 1
    for _ in range(attempts):
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            value = pool.submit(summarizer, dict(sections), limit).result(timeout=settings.summary_timeout_seconds)
            text = sanitize_text(value if isinstance(value, str) else "")
            if text.strip():
                return _clip(text, limit), "generated"
        except Exception:  # noqa: BLE001 - timeout or summarizer failure both fall back
            pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    return deterministic_summary(sections, limit), "deterministic_after_summary_failure"


def build_issue_context(
    body: Any,
    settings: IssueContextSettings | None = None,
    *,
    summarizer: Summarizer | None = None,
) -> dict[str, Any]:
    """Return ``{"text": ..., "metadata": {...}}``; text is sanitized and bounded."""
    cfg = (settings or IssueContextSettings()).normalized()
    raw = "" if body is None else str(body)
    original_length = len(raw)
    clean = sanitize_text(raw).strip()
    metadata: dict[str, Any] = {
        "version": CONTEXT_VERSION,
        "originalLength": original_length,
        "truncated": False,
        "summarized": False,
        "complete": True,
        "summarySource": "none",
        "sections": [],
        "excerpts": [],
        "limits": {
            "maxRawChars": cfg.max_raw_chars,
            "maxSummaryChars": cfg.max_summary_chars,
            "maxExcerptChars": cfg.max_excerpt_chars,
            "summaryTimeoutSeconds": cfg.summary_timeout_seconds,
            "summaryRetries": cfg.summary_retries,
        },
    }
    if len(clean) <= cfg.max_raw_chars:
        metadata["excerpts"] = ["full_description"] if clean else []
        metadata["sentLength"] = len(clean)
        return {"text": clean, "metadata": metadata}

    sections = extract_sections(clean)
    metadata["sections"] = [key for key in SECTION_KEYS if key in sections]
    if summarizer is not None:
        summary, source = _run_summarizer(summarizer, sections, cfg.max_summary_chars, cfg)
    else:
        summary, source = deterministic_summary(sections, cfg.max_summary_chars), "deterministic"

    budget = cfg.max_excerpt_chars
    # Head and tail are reserved first so long sections cannot crowd them out, but
    # they shrink with the configured cap so it stays a real bound.
    edge = min(HEAD_TAIL_CHARS, max(10, (budget - 30) // 4))
    head = clean[:edge]
    tail = clean[-edge:]
    used = len("[beginning]\n") + len(head) + len("\n\n[end]\n") + len(tail)
    chosen: list[str] = ["beginning"]
    rendered: list[str] = [f"[beginning]\n{head}"]
    sections_left = [key for key in EXCERPT_KEYS if sections.get(key)]
    for index, key in enumerate(sections_left):
        label_cost = len(key) + 4  # "\n\n[key]\n"
        share = (budget - used) // (len(sections_left) - index)
        limit = share - label_cost
        if limit < 40:
            continue
        piece = _clip(sections[key], limit)
        used += label_cost + len(piece)
        chosen.append(key)
        rendered.append(f"[{key}]\n{piece}")
    chosen.append("end")
    rendered.append(f"[end]\n{tail}")

    text = "\n\n".join(
        part
        for part in (
            "[context: long issue description summarized; original excerpts follow]",
            f"[summary]\n{summary}" if summary else "",
            *rendered,
        )
        if part
    )
    text = _clip(text, cfg.max_summary_chars + cfg.max_excerpt_chars + 200)
    metadata.update(
        truncated=True,
        summarized=bool(summary),
        complete=False,
        summarySource=source if summary else "none",
        excerpts=chosen,
        sentLength=len(text),
    )
    return {"text": text, "metadata": metadata}
