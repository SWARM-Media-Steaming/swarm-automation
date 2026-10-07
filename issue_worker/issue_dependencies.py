"""Issue dependencies: parse ``Depends on #N``, resolve them, detect cycles.

The parser is one bounded, linear pass: the input, every line and the number of
references are capped, fenced code and inline code are skipped, and no pattern
nests quantifiers. Resolution talks to GitHub through two injected callables so
tests need no network; any GitHub failure becomes an ``unknown`` state (retry
next tick), never an exception that aborts selection. Standard library plus
sibling modules only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from issue_context import _FENCE_OPEN, _closes_fence

MAX_DEPENDENCIES = 20
MAX_TEXT_CHARS = 200_000
MAX_LINE_CHARS = 1000
MAX_PR_CANDIDATES = 8

SATISFIED = "satisfied"
OPEN = "open"
UNMERGED = "unmerged"
UNKNOWN = "unknown"
DEADLOCK = "deadlock"

_KEYWORD = re.compile(r"\b(?:depends[ \t]+on|blocked[ \t]+by|requires)\b[ \t]*:?[ \t]*", re.I)
_REF = re.compile(
    r"(?:(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9_.-]{1,100}))?#(?P<number>[0-9]{1,9})(?![0-9A-Za-z_])"
)
_SEPARATOR = re.compile(r"[ \t]*(?:,[ \t]*(?:and[ \t]+)?|&[ \t]*|and[ \t]+)?", re.I)


@dataclass(frozen=True, order=True)
class DependencyRef:
    repository: str
    number: int

    def label(self, home: str) -> str:
        return f"#{self.number}" if self.repository.lower() == home.lower() else f"{self.repository}#{self.number}"


@dataclass
class DependencyState:
    ref: DependencyRef
    status: str
    title: str = ""
    detail: str = ""
    pull_number: int = 0
    pull_title: str = ""
    files: list[str] = field(default_factory=list)

    @property
    def satisfied(self) -> bool:
        return self.status == SATISFIED


def _strip_code(text: str) -> list[str]:
    """Lines of ``text`` with fenced blocks removed and inline code spans blanked."""
    lines: list[str] = []
    fence = ""
    for raw in text[:MAX_TEXT_CHARS].splitlines():
        line = raw[:MAX_LINE_CHARS]
        if fence:
            if _closes_fence(line, fence):
                fence = ""
            continue
        opening = _FENCE_OPEN.match(line)
        if opening:
            fence = opening.group("f")
            continue
        # Odd-indexed pieces between backticks are code spans.
        lines.append(" ".join(line.split("`")[::2]))
    return lines


def parse_dependencies(
    text: Any, repository: str, limit: int = MAX_DEPENDENCIES
) -> tuple[list[DependencyRef], bool]:
    """Return ``(references, truncated)`` found in ``text``.

    ``owner/repo#N`` counts only for the same owner as ``repository``; bare
    ``#N`` means ``repository`` itself. Order of first appearance is kept.
    """
    home_owner = repository.split("/", 1)[0].lower()
    found: list[DependencyRef] = []
    seen: set[DependencyRef] = set()
    truncated = False
    for line in _strip_code("" if text is None else str(text)):
        position = 0
        while True:
            keyword = _KEYWORD.search(line, position)
            if keyword is None:
                break
            position = keyword.end()
            while True:
                match = _REF.match(line, position)
                if match is None:
                    break
                position = match.end()
                if match.group("owner"):
                    if match.group("owner").lower() != home_owner:
                        target = None
                    else:
                        target = f"{match.group('owner')}/{match.group('repo')}"
                else:
                    target = repository
                number = int(match.group("number"))
                if target is not None and number > 0:
                    ref = DependencyRef(target, number)
                    if ref not in seen:
                        if len(found) >= limit:
                            truncated = True
                        else:
                            seen.add(ref)
                            found.append(ref)
                position = _SEPARATOR.match(line, position).end()  # always matches (may be empty)
    return found, truncated


def trusted_comment_bodies(comments: Iterable[Mapping[str, Any]], trusted: Iterable[str]) -> list[str]:
    allowed = {name.lower() for name in trusted}
    return [
        str(comment.get("body") or "")
        for comment in comments
        if str((comment.get("user") or {}).get("login") or "").lower() in allowed
    ]


def issue_dependencies(
    body: Any,
    comments: Iterable[Mapping[str, Any]],
    trusted: Iterable[str],
    repository: str,
    own_number: int,
    limit: int = MAX_DEPENDENCIES,
) -> tuple[list[DependencyRef], bool]:
    """Dependencies from the issue body and trusted-author comments, never the issue itself."""
    texts = [str(body or ""), *trusted_comment_bodies(comments, trusted)]
    refs, truncated = parse_dependencies("\n".join(texts), repository, limit + 1)
    refs = [ref for ref in refs if not (ref.repository == repository and ref.number == own_number)]
    if len(refs) > limit:
        refs, truncated = refs[:limit], True
    return refs, truncated


def find_cycle_members(graph: Mapping[Any, Iterable[Any]]) -> set[Any]:
    """Nodes that sit on a dependency cycle (iterative Tarjan; linear time)."""
    index: dict[Any, int] = {}
    low: dict[Any, int] = {}
    on_stack: set[Any] = set()
    stack: list[Any] = []
    members: set[Any] = set()
    counter = 0
    for root in graph:
        if root in index:
            continue
        work: list[tuple[Any, Any]] = [(root, iter(graph.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(graph.get(child, ()))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1 or node in graph.get(node, ()):
                    members.update(component)
    return members


# ---- resolution -------------------------------------------------------------

ApiGet = Callable[[str], Mapping[str, Any]]
ApiList = Callable[[str, "dict[str, str | int] | None"], Sequence[Mapping[str, Any]]]


def _closing_pull(pull: Mapping[str, Any], number: int) -> bool:
    """Does this PR deliver issue ``number`` (branch, title mention or closing keyword)?"""
    head = str((pull.get("head") or {}).get("ref") or "")
    if head.endswith(f"issue-{number}"):
        return True
    title = str(pull.get("title") or "")
    if re.search(rf"#{number}(?![0-9])", title):
        return True
    body = str(pull.get("body") or "")[:MAX_TEXT_CHARS]
    return re.search(rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)[ \t]*:?[ \t]+#{number}(?![0-9])", body, re.I) is not None


class DependencyResolver:
    """Classifies dependencies; caches per instance (one selection pass)."""

    def __init__(self, api_get: ApiGet, api_list: ApiList, branches: Iterable[str]) -> None:
        self._get = api_get
        self._list = api_list
        self._branches = {name for name in branches if name}
        self._cache: dict[DependencyRef, DependencyState] = {}

    def resolve(self, ref: DependencyRef) -> DependencyState:
        if ref not in self._cache:
            try:
                self._cache[ref] = self._resolve(ref)
            except Exception as error:  # GitHub/parse failures are retried next tick
                self._cache[ref] = DependencyState(ref, UNKNOWN, detail=_short_error(error))
        return self._cache[ref]

    def _resolve(self, ref: DependencyRef) -> DependencyState:
        issue = self._get(f"repos/{ref.repository}/issues/{ref.number}")
        title = str(issue.get("title") or "")
        if str(issue.get("state") or "").lower() != "closed":
            return DependencyState(ref, OPEN, title)
        if issue.get("pull_request"):
            pull = self._get(f"repos/{ref.repository}/pulls/{ref.number}")
            return self._from_pull(ref, title, pull) or DependencyState(ref, UNMERGED, title, "closed pull request was not merged")
        if str(issue.get("state_reason") or "").lower() == "not_planned":
            return DependencyState(ref, UNMERGED, title, "closed as not planned")
        numbers: list[int] = []
        for event in self._list(f"repos/{ref.repository}/issues/{ref.number}/timeline", {"per_page": 100}):
            source = (event.get("source") or {}).get("issue") or {}
            pull_ref = source.get("pull_request") or {}
            repo_name = str((source.get("repository") or {}).get("full_name") or ref.repository)
            if event.get("event") == "cross-referenced" and pull_ref and repo_name.lower() == ref.repository.lower():
                number = source.get("number")
                if isinstance(number, int) and number not in numbers:
                    numbers.append(number)
        for number in numbers[-MAX_PR_CANDIDATES:][::-1]:
            pull = self._get(f"repos/{ref.repository}/pulls/{number}")
            state = self._from_pull(ref, title, pull, require_link=True)
            if state:
                return state
        return DependencyState(ref, UNMERGED, title, "no merged pull request in the integration branch")

    def _from_pull(
        self, ref: DependencyRef, title: str, pull: Mapping[str, Any], *, require_link: bool = False
    ) -> DependencyState | None:
        base = str((pull.get("base") or {}).get("ref") or "")
        if not pull.get("merged") or base not in self._branches:
            return None
        number = int(pull.get("number") or 0)
        if require_link and not _closing_pull(pull, ref.number):
            return None
        return DependencyState(
            ref, SATISFIED, title, pull_number=number, pull_title=str(pull.get("title") or "")
        )

    def changed_files(self, state: DependencyState, limit: int) -> list[str]:
        """Best-effort changed file names of the merged PR; failures return an empty list."""
        if not state.pull_number:
            return []
        try:
            files = self._list(
                f"repos/{state.ref.repository}/pulls/{state.pull_number}/files", {"per_page": min(100, max(1, limit * 4))}
            )
        except Exception:
            return []
        return [str(item.get("filename") or "") for item in list(files)[:limit * 4] if item.get("filename")]


def _short_error(error: BaseException) -> str:
    return " ".join(str(error).split())[:160] or type(error).__name__


# ---- markers and comments ---------------------------------------------------

def blockers_key(refs: Iterable[DependencyRef]) -> str:
    return ",".join(f"{ref.repository}#{ref.number}" for ref in sorted(refs))


def waiting_marker(issue_number: int, refs: Iterable[DependencyRef]) -> str:
    return f"<!-- swarm-issue-worker:waiting:issue:{issue_number};on:{blockers_key(refs)} -->"


def released_marker(issue_number: int, refs: Iterable[DependencyRef]) -> str:
    return f"<!-- swarm-issue-worker:dependency-released:issue:{issue_number};on:{blockers_key(refs)} -->"


def cycle_marker(issue_number: int, members: Iterable[Any]) -> str:
    return f"<!-- swarm-issue-worker:dependency-cycle:issue:{issue_number};members:{','.join(str(m) for m in sorted(members))} -->"


def _last_index(bodies: Sequence[str], prefix: str) -> int:
    for index in range(len(bodies) - 1, -1, -1):
        if prefix in bodies[index]:
            return index
    return -1


def needs_waiting_comment(bodies: Sequence[str], issue_number: int, refs: Iterable[DependencyRef]) -> bool:
    """True unless this exact blocker set is already announced since the last release."""
    return _last_index(bodies, waiting_marker(issue_number, refs)) <= _last_index(
        bodies, f"<!-- swarm-issue-worker:dependency-released:issue:{issue_number};"
    )


def needs_released_comment(bodies: Sequence[str], issue_number: int) -> bool:
    """True when a waiting notice exists with no release notice after it."""
    waiting = _last_index(bodies, f"<!-- swarm-issue-worker:waiting:issue:{issue_number};")
    released = _last_index(bodies, f"<!-- swarm-issue-worker:dependency-released:issue:{issue_number};")
    return waiting > released


def _line(state: DependencyState, home: str) -> str:
    reason = {
        OPEN: "still open",
        UNMERGED: state.detail or "closed without merged work",
        UNKNOWN: "could not be checked (will retry)",
        DEADLOCK: "is part of a dependency cycle",
    }.get(state.status, state.status)
    title = f" {state.title.strip()[:100]!r}" if state.title else ""
    return f"- {state.ref.label(home)}{title}: {reason}"


def render_waiting_comment(issue_number: int, states: Sequence[DependencyState], home: str) -> str:
    refs = [state.ref for state in states]
    lines = [
        waiting_marker(issue_number, refs),
        "# ⏳ Waiting on dependencies",
        "",
        "No work has started. This issue will be picked up automatically once every prerequisite "
        "is closed with its work merged into the integration branch.",
        "",
        *(_line(state, home) for state in states),
    ]
    return "\n".join(lines) + "\n"


def render_released_comment(issue_number: int, refs: Sequence[DependencyRef], home: str) -> str:
    names = ", ".join(ref.label(home) for ref in refs) or "its prerequisites"
    return (
        f"{released_marker(issue_number, refs)}\n"
        f"# ▶️ Dependencies satisfied\n\n{names} merged into the integration branch; "
        "this issue is now eligible and will be started in priority order.\n"
    )


def render_cycle_comment(issue_number: int, members: Sequence[Any]) -> str:
    chain = ", ".join(str(m) for m in sorted(members))
    return (
        f"{cycle_marker(issue_number, members)}\n"
        "# ⚠️ Dependency cycle\n\n"
        f"These issues depend on each other and can never start: {chain}. "
        "No work has started. Edit one `Depends on` / `Blocked by` line to break the cycle; "
        "the issue is re-evaluated on every run.\n"
    )
