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


def valid_session_id(value: object) -> bool:
    # Explicit UUIDs only: never --last, a thread name, or option-like text.
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def resume_failure(raw: str) -> bool:
    """Only resume/context errors warrant a fresh retry, not arbitrary failures."""
    diagnostics = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            diagnostics.append(line)
            continue
        if isinstance(event, dict) and (event.get("type") in {"error", "turn.failed"} or event.get("is_error")):
            diagnostics.append(json.dumps(event))
    return bool(re.search(
        r"(?:session|thread|conversation)[^\n]{0,120}(?:not found|does not exist|expired|corrupt|invalid)|"
        r"(?:failed|unable|cannot) to (?:resume|load session|load thread)|"
        r"no (?:conversation|session|thread) found|context (?:window|length)[^\n]{0,60}(?:exceed|full)|"
        r"prompt is too long",
        "\n".join(diagnostics), re.IGNORECASE,
    ))


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
        from swarm_issue_worker import ADVERSARIAL_STAGES
        state = self.read_state()
        for stage in ADVERSARIAL_STAGES:
            loop = state.get(stage.key)
            if isinstance(loop, dict) and loop.get("active"):
                epoch = int(loop.get("epoch") or 1)
                if loop.get("phase") == "fix":
                    return f"{stage.key}:fix:{epoch}", True
                # Fresh assessment each round; only an interrupted same phase
                # may resume. Never share with implementers or another stage.
                return f"{stage.key}:test:{epoch}:{loop.get('round', 0)}", False
        return "primary", True

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
        paths = self.git("ls-files", "--cached", "--others", "--exclude-standard", "-z",
                         "--", "AGENTS.md", "CLAUDE.md", ":(glob)**/AGENTS.md",
                         ":(glob)**/CLAUDE.md", ".claude", ".codex", check=False)
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
        self.choice.resume = False
        self.choice.session_id = self.new_session_id(self.config.require_spec(self.choice.key))
        self.update_state_for_choice(self.choice)

    def remember_cli_session(self, success: bool) -> None:
        role = getattr(self, "_cli_session_role", "")
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
        if role == "primary":
            # Rebuild current issue/conventions/handoff, never replay a saved
            # source snapshot. Preserve the current continuation/amendments.
            return self.build_prompt(False, "", bool(self.worktree_status())) + "\nCurrent request:\n" + prompt
        from swarm_issue_worker import ADVERSARIAL_STAGES
        for stage in ADVERSARIAL_STAGES:
            loop = self.read_state().get(stage.key)
            if isinstance(loop, dict) and loop.get("active"):
                return self.adversarial_prompt(stage, loop)
        return prompt
