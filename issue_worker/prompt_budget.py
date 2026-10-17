"""Size guards that keep one oversized change from breaking every provider.

Two failures share a cause: a diff far larger than the change the issue asked
for (usually build output swept into a commit) is embedded whole in a prompt,
and the provider refuses it. The refusal is deterministic, so retrying only
repeats it until the issue is held. This module holds the pure pieces:

* ``bound_diff`` keeps an embedded patch under a character budget.
* ``generated_prefixes`` finds newly added build-output directories.
* ``parse_input_limit`` recognises a provider's "input too large" refusal.

Standard library only, like the rest of ``issue_worker/``.
"""
from __future__ import annotations

import re

# Well under the smallest provider input limit seen (Codex: 1,048,576 characters)
# so the rest of the prompt always fits beside the patch.
ADVERSARIAL_DIFF_MAX_CHARS = 400_000
# A change that stages more than this is not the change the issue described.
MAX_COMMIT_FILES = 500
MAX_COMMIT_CHANGED_LINES = 200_000

# Directories a toolchain regenerates. Only a *newly added* path under one is
# ever unstaged, so a repository that deliberately tracks one is unaffected.
GENERATED_DIRS = frozenset({
    "target", "node_modules", "__pycache__", ".gradle", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".venv", "venv",
})

INPUT_LIMIT_RE = re.compile(
    r"input exceeds the maximum length|input_too_large|prompt is too long|"
    r"context_length_exceeded|maximum context length|request too large",
    re.IGNORECASE,
)
_ACTUAL_RE = re.compile(r'"actual_chars"\s*:\s*(\d+)')
_MAX_RE = re.compile(r'"max_chars"\s*:\s*(\d+)|maximum length of (\d[\d,]*)', re.IGNORECASE)


def generated_prefixes(added_paths: list[str]) -> list[str]:
    """Top-most generated directory containing each added path, sorted.

    ``web/target/debug/x.h`` yields ``web/target``. A path outside every
    generated directory contributes nothing.
    """
    found: set[str] = set()
    for path in added_paths:
        parts = path.split("/")
        for index, part in enumerate(parts[:-1]):
            if part in GENERATED_DIRS:
                found.add("/".join(parts[: index + 1]))
                break
    return sorted(found)


def top_level_summary(paths: list[str], limit: int = 5) -> str:
    """The largest two-segment directories among ``paths``, for an error message."""
    counts: dict[str, int] = {}
    for path in paths:
        key = "/".join(path.split("/")[:2])
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return ", ".join(f"{name} ({count} files)" for name, count in ranked)


def _sections(diff: str) -> list[str]:
    """Split a unified diff into one chunk per file."""
    pieces = re.split(r"(?m)^(?=diff --git )", diff)
    return [piece for piece in pieces if piece]


def bound_diff(diff: str, stat: str, max_chars: int = ADVERSARIAL_DIFF_MAX_CHARS) -> str:
    """``diff`` unchanged when it fits, otherwise a bounded stand-in.

    Over budget, the result is the ``--stat`` summary plus whole per-file
    patches, smallest first, until the budget is spent. Files left out are named
    so the tester reads them from the repository instead.
    """
    if len(diff) <= max_chars:
        return diff
    header = (
        f"(The full patch is {len(diff):,} characters, over the {max_chars:,} limit, so it is "
        "summarised. Read any omitted file from the repository.)\n\nChange summary:\n"
        f"{stat.strip()[: max_chars // 4]}\n"
    )
    budget = max_chars - len(header)
    kept: list[str] = []
    omitted: list[str] = []
    for section in sorted(_sections(diff), key=len):
        name = re.match(r"diff --git a/(\S+)", section)
        label = name.group(1) if name else "(unnamed)"
        if len(section) <= budget:
            kept.append(section)
            budget -= len(section)
        else:
            omitted.append(label)
    note = ""
    if omitted:
        shown = omitted[:200]
        note = ("\nPatches omitted for size (" + str(len(omitted)) + " files): "
                + ", ".join(shown) + (" ..." if len(omitted) > len(shown) else "") + "\n")
    return header + "\n" + "".join(kept) + note


def parse_input_limit(text: str) -> tuple[int, int] | None:
    """``(actual, maximum)`` characters when ``text`` is an input-size refusal.

    Either number is 0 when the provider did not state it. ``None`` means the
    text is not such a refusal.
    """
    if not INPUT_LIMIT_RE.search(text):
        return None
    actual = _ACTUAL_RE.search(text)
    limit = _MAX_RE.search(text)
    maximum = 0
    if limit:
        maximum = int((limit.group(1) or limit.group(2)).replace(",", ""))
    return (int(actual.group(1)) if actual else 0, maximum)
