"""Native CLI session continuity. Only identities/digests live in worker state.

No prompts, source snapshots or inference results are cached here. Providers
remain responsible for prompt caching and context compaction.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid

SESSION_MAX_IDLE_SECONDS = 24 * 60 * 60
SESSION_POLICY_VERSION = 1
_RESUME_FAILURE_CODES = frozenset({
    "context_length_exceeded",
    "context_window_exceeded",
    "conversation_not_found",
    "session_not_found",
    "thread_not_found",
})
# Prose that reports a compaction that worked. The bare ``compact_boundary``
# marker is only success evidence on a non-error event: an error event that
# carries that subtype is the CLI reporting a compaction that failed.
_COMPACTION_SUCCESS_TEXT = (
    r"successfully\s+compact(?:ed|ion)|"
    r"compacted\s+(?:the\s+)?conversation|compacted\s+successfully"
)
_SUCCESSFUL_COMPACTION_PROSE = re.compile(_COMPACTION_SUCCESS_TEXT, re.IGNORECASE)
_SUCCESSFUL_COMPACTION = re.compile(r"compact_boundary|" + _COMPACTION_SUCCESS_TEXT, re.IGNORECASE)
_RESUME_FAILURE_TEXT = re.compile(
    r"(?:session|thread|conversation)[^\n]{0,120}(?:not found|does not exist|expired|corrupt|invalid)|"
    r"(?:failed|unable|cannot) to (?:resume|load session|load thread)|"
    r"no (?:conversation|session|thread) found|"
    r"context (?:window|length)[^\n]{0,60}(?:exceed|full)|"
    r"no rollout found for thread id|"
    r"prompt is too long",
    re.IGNORECASE,
)


def valid_session_id(value: object) -> bool:
    # Explicit UUIDs only: never --last, a thread name, or option-like text.
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def _error_flag(value: object) -> bool:
    """A CLI ``is_error`` marker, whether it arrives as a bool, number or string."""
    if isinstance(value, str):
        return value.strip().lower() not in {"", "false", "0", "no", "none", "null"}
    return bool(value)


def resume_failure(raw: str) -> bool:
    """Only resume/context errors warrant a fresh retry, not arbitrary failures.

    The event decides, not its wording. A ``compact_boundary`` event is success
    evidence only on a non-error event: one flagged ``is_error`` or typed
    ``error``/``turn.failed`` is a failed compaction whatever its message says
    or omits (no flag, no exhaustion code, empty or success-sounding text).
    """
    diagnostics = []
    codes = set()

    def collect_codes(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in {"code", "type"} and isinstance(nested, str):
                    codes.add(nested.strip().lower().replace("-", "_"))
                if isinstance(nested, (dict, list)):
                    collect_codes(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_codes(nested)

    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            if not _SUCCESSFUL_COMPACTION.search(line):
                diagnostics.append(line)
            continue
        if not isinstance(event, dict):
            continue
        error_typed = event.get("type") in {"error", "turn.failed"}
        flagged = _error_flag(event.get("is_error"))
        if event.get("subtype") == "compact_boundary":
            if flagged or error_typed:
                return True
            continue
        if error_typed or flagged:
            collect_codes(event)
            blob = json.dumps(event)
            if not _SUCCESSFUL_COMPACTION_PROSE.search(blob):
                diagnostics.append(blob)
    if codes & _RESUME_FAILURE_CODES:
        return True
    return bool(_RESUME_FAILURE_TEXT.search("\n".join(diagnostics)))


class PromptSessionMixin:
    def cache_routing_evidence(self, role: str = "primary") -> list[dict]:
        from usage_report import cache_routing_evidence
        repository = getattr(getattr(self, "history", None), "repository", None)
        if repository is None:
            return []
        try:
            with repository.connect() as database:
                return cache_routing_evidence(database, self.config.github_repository, role)
        except Exception:  # optional telemetry must not disturb routing/fallbacks
            return []

    def session_role(self) -> tuple[str, bool]:
        # A one-off AI pass (architecture documentation review) is its own
        # fresh conversation: never resumed, never remembered.
        if getattr(self, "_independent_ai_pass", False):
            return "documentation", False
        from swarm_issue_worker import ADVERSARIAL_STAGES
        try:
            state = self.read_state()
        except (OSError, ValueError):
            # Routing can compare a saved choice before an issue state exists,
            # and optional cache evidence must never make that path fail.
            state = {}
        if not isinstance(state, dict):
            state = {}
        for stage in ADVERSARIAL_STAGES:
            loop = state.get(stage.key)
            if isinstance(loop, dict) and loop.get("active"):
                phase = loop.get("phase")
                try:
                    epoch = int(loop.get("epoch") or 1)
                    round_number = int(loop.get("round") or 0)
                except (TypeError, ValueError, OverflowError):
                    return "", False
                if (isinstance(loop.get("epoch"), bool) or isinstance(loop.get("round"), bool)
                        or epoch < 1 or round_number < 0 or phase not in {"fix", "test"}):
                    return "", False
                if phase == "fix":
                    return f"{stage.key}:fix:{epoch}", True
                # Fresh assessment each round; only an interrupted same phase
                # may resume. Never share with implementers or another stage.
                # A rejected report starts a new assessment even at the same
                # epoch/round, so it cannot resume the discarded conversation.
                # Later retries keep their own identity and may resume if
                # interrupted after the rejection.
                generation = self._rejection_generation(loop)
                return f"{stage.key}:test:{epoch}:{round_number}{generation}", False
        return "primary", True

    def _rejection_generation(self, loop: dict) -> str:
        # The loop sets this key when it discards a report and pops it when the
        # replacement is accepted, so presence is the signal. A damaged payload
        # (empty, non-dict) must still fail closed to a fresh assessment.
        if "retry_rejection" not in loop:
            return ""
        rejected = loop["retry_rejection"]
        attempts = 1
        if isinstance(rejected, dict):
            raw = rejected.get("attempts")
            try:
                attempts = int(raw or 1)
            except (TypeError, ValueError, OverflowError):
                attempts = 1
            if isinstance(raw, bool) or attempts < 1:
                attempts = 1
        return f":rejected:{attempts}"

    def session_context(self) -> str:
        state = self.read_state()
        root = self.config.repo_dir.resolve()
        digest = hashlib.sha256()
        identity = [SESSION_POLICY_VERSION, str(root), self.config.github_repository,
                    self.issue.number, self.issue.title, self.issue.body, self.issue.labels,
                    state.get("base_sha"), self.git("branch", "--show-current", check=False)]
        digest.update(json.dumps(identity, sort_keys=True).encode())
        # Hash instructions from disk on every execution, including untracked
        # instructions. Never follow symlinks outside the checkout or retain
        # their contents in application state.
        pathspecs = ("AGENTS.md", "CLAUDE.md", ":(glob)**/AGENTS.md",
                     ":(glob)**/CLAUDE.md", ".claude", ".codex")
        paths = self.git("ls-files", "--cached", "--others", "--exclude-standard", "-z",
                         "--", *pathspecs, check=False)
        # Provider CLIs discover repository instructions from the filesystem,
        # including intentionally ignored local guidance. Ask Git for ignored
        # files separately so its normal untracked-file filter cannot hide them.
        paths += self.git("ls-files", "--others", "--ignored", "--exclude-standard", "-z",
                          "--", *pathspecs, check=False)
        for name in sorted(set(paths.split("\0")) - {""}):
            path = root / name
            if root not in path.resolve().parents or not path.is_file():
                continue
            digest.update(name.encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()

    def prepare_cli_session(self) -> bool:
        """Select a compatible session after routing. Return whether a supplied
        continuation lost its context and needs a freshly constructed prompt.
        """
        self._cli_session_role = ""
        if not self.issue:
            return False
        if getattr(self, "_independent_ai_pass", False):
            # Fail closed even if the caller left resume=True and the
            # implementer's UUID in place. Do not persist this identity.
            self._cli_session_role = "documentation"
            self._cli_usage_baseline = None
            self._cli_usage_totals = None
            self.choice.resume = False
            self.choice.session_id = (
                str(uuid.uuid4()) if self.choice.key in {"claude", "grok"} else ""
            )
            return False
        if self.choice.key not in {"claude", "codex"}:
            self.forget_cli_session()
            return False
        state = self.read_state()
        role, reusable = self.session_role()
        try:
            context = self.session_context()
        except OSError:
            # Unreadable/changing instructions cannot prove compatibility.
            context = str(uuid.uuid4())
        sessions = state.get("cli_sessions") or {}
        entry = sessions.get(role) if isinstance(sessions, dict) else None
        was_resume = self.choice.resume
        compatible = False
        if isinstance(entry, dict):
            try:
                age = time.time() - float(entry.get("updated_at") or 0)
            except (TypeError, ValueError, OverflowError):
                age = float("inf")
            compatible = (
                entry.get("context") == context
                and entry.get("provider") == self.choice.key
                and entry.get("model") == self.choice.model
                and entry.get("effort") == self.choice.effort
                and entry.get("started") is True
                and valid_session_id(entry.get("id"))
                and math.isfinite(age) and 0 <= age < SESSION_MAX_IDLE_SECONDS
                and (reusable or (was_resume and self.choice.session_id == entry.get("id")))
                and bool(entry.get("head"))
                and self.git_ok("merge-base", "--is-ancestor", entry["head"], "HEAD")
            )
        self._cli_usage_baseline = entry.get("usage_totals") if compatible else None
        self._cli_usage_totals = None
        if compatible:
            self.choice.session_id = entry["id"]
            self.choice.resume = True
            self.update_state(session_id=entry["id"], session_started=True)
        elif was_resume or entry:
            self.fresh_cli_session()
        self._cli_session_role = role
        self._cli_session_context = context
        return was_resume and not compatible

    def forget_cli_session(self) -> None:
        if not self.issue:
            return
        role, _ = self.session_role()
        sessions = self.read_state().get("cli_sessions")
        if isinstance(sessions, dict) and role in sessions:
            self.update_state(cli_sessions={k: v for k, v in sessions.items() if k != role})

    def fresh_cli_session(self) -> None:
        self._cli_usage_baseline = None
        self._cli_usage_totals = None
        self.choice.resume = False
        self.choice.session_id = self.new_session_id(self.config.require_spec(self.choice.key))
        self.update_state_for_choice(self.choice)

    def remember_cli_session(self, success: bool) -> None:
        role = getattr(self, "_cli_session_role", "")
        if getattr(self, "_independent_ai_pass", False):
            return
        if self.choice.key not in {"claude", "codex"}:
            self.forget_cli_session()
            return
        if not role:
            return
        state = self.read_state()
        sessions = state.get("cli_sessions") or {}
        if not isinstance(sessions, dict):
            sessions = {}
        # Bound metadata: keep one entry per role/stage; old review rounds and
        # failed fix epochs cannot become candidates again.
        family = role.split(":")[:2]
        sessions = {k: v for k, v in sessions.items() if k.split(":")[:2] != family}
        sessions[role] = {
            "id": self.choice.session_id, "provider": self.choice.key,
            "model": self.choice.model, "effort": self.choice.effort,
            "context": self._cli_session_context, "updated_at": time.time(),
            "usage_totals": {k: v for k, v in (getattr(self, "_cli_usage_totals", None) or {}).items()
                             if k.endswith("tokens") and (v is None or isinstance(v, int))},
            "head": self.git("rev-parse", "HEAD", check=False),
            "started": bool(state.get("session_started")) and
                       valid_session_id(self.choice.session_id) and
                       (success or self.ai_failure_is_quota()),
        }
        self.update_state(cli_sessions=sessions)

    def fresh_session_prompt(self, prompt: str) -> str:
        if not self.issue:
            return prompt
        role, _ = self.session_role()
        if role == "documentation":
            return prompt
        if role == "primary":
            # Rebuild current issue/conventions/handoff, never replay a saved
            # source snapshot. A normal first-attempt prompt may already be the
            # complete current issue prompt; do not embed it in another copy
            # during a model fallback. A resume-recovery delta is appended to
            # newly reconstructed current context.
            requirements = (
                f"Issue title:\n{self.issue.title}\nIssue number:\n#{self.issue.number}\n\n"
                f"Issue description:\n{self.issue.body}\n\nIssue tags:\n"
            )
            if prompt.startswith(requirements):
                return prompt
            return self.build_prompt(False, "", bool(self.worktree_status())) + "\nCurrent request:\n" + prompt
        from swarm_issue_worker import ADVERSARIAL_STAGES
        for stage in ADVERSARIAL_STAGES:
            loop = self.read_state().get(stage.key)
            if isinstance(loop, dict) and loop.get("active"):
                return self.adversarial_prompt(stage, loop)
        return prompt
