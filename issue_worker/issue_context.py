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
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

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
# Matched against a right-stripped line, so no quantifier overlaps another and matching is
# linear; the closing "#" run of an ATX heading is trimmed afterwards, not in the pattern.
_HEADING = re.compile(r"^\s{0,3}(?:#{1,6}\s+(?P<h>\S.*)|\*\*(?P<b>[^*\n]{2,80})\*\*:?|(?P<c>[A-Za-z][A-Za-z /&-]{2,60}):)$")
# Longer lines are prose, never headings; skipping them also bounds work per line.
MAX_HEADING_LINE_CHARS = 200
# CommonMark fenced code: 3+ backticks or tildes; a backtick fence's info string has no backtick.
_FENCE_OPEN = re.compile(r"^ {0,3}(?P<f>`{3,}(?=[^`]*$)|~{3,})")


def _closes_fence(line: str, fence: str) -> bool:
    """A closing fence repeats the opening character at least as many times, with nothing after it."""
    return re.match(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*$", line) is not None

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
            except (TypeError, ValueError, OverflowError):
                number = default
            return min(HARD_MAX_CHARS, max(low, number))

        try:
            timeout = float(self.summary_timeout_seconds)
            if not math.isfinite(timeout):
                raise ValueError(timeout)
            timeout = min(30.0, max(0.5, timeout))
        except (TypeError, ValueError, OverflowError):
            timeout = 5.0
        try:
            retries = min(3, max(0, int(self.summary_retries)))
        except (TypeError, ValueError, OverflowError):
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
    current_level = 0
    fence: str | None = None
    for line in text.splitlines():
        # Inside fenced code a "# comment" or "Usage:" line is code, never a heading.
        if fence is not None:
            in_code = True
            if _closes_fence(line, fence):
                fence = None
        else:
            opening = _FENCE_OPEN.match(line)
            fence = opening.group("f") if opening else None
            in_code = fence is not None
        if in_code:
            if current and line.strip():
                sections.setdefault(current, []).append(line.rstrip())
            continue
        match = _HEADING.match(line.rstrip()) if len(line) <= MAX_HEADING_LINE_CHARS else None
        if match:
            atx = match.group("h")
            if atx:
                atx = atx.rstrip("#").rstrip() or atx
            title = atx or match.group("b") or match.group("c") or ""
            level = len(line.lstrip()) - len(line.lstrip().lstrip("#")) if match.group("h") else 0
            if current and level > current_level > 0:
                # A sub-heading belongs to the section it sits under.
                sections.setdefault(current, []).append(line.strip())
                continue
            kind = _classify(title)
            if current and not match.group("h") and kind is None:
                # An unrecognised bold or "Label:" line is content, not a new section.
                sections.setdefault(current, []).append(line.strip())
                continue
            current = kind
            current_level = level
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


CONTEXT_HEADER = "[context: long issue description summarized; original excerpts follow]"


def render_parts(parts: Sequence[tuple[str, str]]) -> str:
    return "\n\n".join([CONTEXT_HEADER, *(f"[{label}]\n{body}" for label, body in parts)])


def fit_parts(parts: Sequence[tuple[str, str]], limit: int) -> str:
    """Render ``parts`` within ``limit`` characters without dropping any of them.

    The generated summary yields first; only then are the original excerpts
    shortened, each in proportion, so every excerpt the metadata names survives.
    """
    text = render_parts(parts)
    if len(text) <= limit:
        return text
    parts = list(parts)
    fixed = len(render_parts([(label, "") for label, _ in parts]))
    room = max(0, limit - fixed)
    if parts and parts[0][0] == "summary":
        excerpt_total = sum(len(body) for _, body in parts[1:])
        parts[0] = ("summary", _clip(parts[0][1], max(0, room - excerpt_total)) if room > excerpt_total else "")
        if not parts[0][1]:
            parts = parts[1:]
            fixed = len(render_parts([(label, "") for label, _ in parts]))
            room = max(0, limit - fixed)
    total = sum(len(body) for _, body in parts)
    if total > room:
        parts = [(label, _clip(body, max(4, len(body) * room // total))) for label, body in parts]
    return render_parts(parts)[:limit]


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
    overview = " ".join(clean.split())
    metadata["sections"] = [key for key in SECTION_KEYS if key in sections]
    if summarizer is not None:
        summary, source = _run_summarizer(summarizer, sections, cfg.max_summary_chars, cfg)
    else:
        summary, source = deterministic_summary(sections, cfg.max_summary_chars), "deterministic"
    if not summary.strip():
        # No recognisable headings: summarise the prose itself, bounded.
        summary = _clip("overview: " + overview, cfg.max_summary_chars)

    budget = cfg.max_excerpt_chars
    # Head and tail are reserved first so long sections cannot crowd them out, but
    # they shrink with the configured cap so it stays a real bound.
    edge = min(HEAD_TAIL_CHARS, max(10, (budget - 30) // 4))
    head = clean[:edge]
    tail = clean[-edge:]
    used = len("[beginning]\n") + len(head) + len("\n\n[end]\n") + len(tail)
    chosen: list[str] = ["beginning"]
    excerpts: list[tuple[str, str]] = [("beginning", head)]
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
        excerpts.append((key, piece))
    chosen.append("end")
    excerpts.append(("end", tail))

    parts = ([("summary", summary)] if summary else []) + excerpts
    text = _clip(render_parts(parts), cfg.max_summary_chars + cfg.max_excerpt_chars + 200)
    metadata.update(
        truncated=True,
        summarized=bool(summary),
        complete=False,
        summarySource=source if summary else "none",
        excerpts=chosen,
        sentLength=len(text),
    )
    return {"text": text, "metadata": metadata, "parts": parts}
