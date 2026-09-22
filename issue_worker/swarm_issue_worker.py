#!/usr/bin/env python3
"""Process at most one assigned GitHub issue with Claude, Codex, or Grok.

This is the Python implementation of the SWARM unattended issue worker. The
state files and exit codes intentionally remain compatible with the former
shell worker so an upgrade can resume existing active and quota-paused runs.

Providers are an open set (see ``KNOWN_PROVIDERS`` / ``ProviderSpec``): among
whichever providers are enabled for the flow, a new issue goes to the one with
the most usage remaining (so no account is drained before the others). A named
``--preferred-provider`` wins only when remaining usage is tied. ``auto`` means
the user has no favorite, so those ties follow the default provider order.
A follow-up review pass prefers a *different* provider than the previous one,
falling back to the same one only when it is the only one with capacity.

All AI work happens on an integration branch (``--integration-branch``, default
``ai-main``) that is kept in parity with ``--base-branch``. It is never merged
into it automatically unless ``--auto-promote`` is set; otherwise that final
promotion is a human action (a PR opened from the desktop app's Branches
view). Each issue gets one branch,
``<prefix>/<first-ai>/issue-<n>``, reused by every later pass regardless of
which provider runs it. Commit subjects are prefixed ``[<provider>]``.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

SCRIPT_HOME = Path(os.environ.get("SWARM_ISSUE_WORKER_SCRIPT_DIR", Path(__file__).resolve().parent)).resolve()
if str(SCRIPT_HOME) not in sys.path:
    sys.path.insert(0, str(SCRIPT_HOME))

# Mixin/helpers import worker types lazily. Keep their identity when this file
# is launched as a CLI so WorkerError is still caught by main's handler.
if __name__ == "__main__":
    sys.modules["swarm_issue_worker"] = sys.modules[__name__]

from github_app_auth import DEFAULT_CONFIG_PATH, GitHubAppAuth
from ai_execution_history import ExecutionHistoryService, ExecutionStart, PROMPT_TEMPLATE_VERSION
from adversarial_uat import AdversarialUatMixin, CAP_HIT_PR_MARKER, CAP_HIT_PR_NOTICE
from issue_images import (
    MAX_IMAGES,
    ImageDownloadError,
    IssueImage,
    assistant_result_text,
    claude_stream_message,
    codex_image_flags,
    download_issue_image,
    extract_issue_image_refs,
    format_image_note,
    grok_prompt_json,
    inlined_images,
)
from dynamic_router import (
    DEFAULT_ROUTING_OPTIMIZATION,
    InvalidRouterModel,
    RouterCandidate,
    RouterError,
    RoutingTier,
    build_model_correction_prompt,
    build_router_prompt,
    catalog_model_names,
    default_provider_strengths,
    default_router_effort,
    default_router_model,
    fallback_routing_decision,
    format_routing_notice,
    load_routing_tiers,
    normalize_routing_optimization,
    resolve_routing_decision,
    run_provider_router,
)


ISSUE_COMPLETED_EXIT_CODE = 10
QUOTA_PAUSED_EXIT_CODE = 11
PROVIDER_UNAVAILABLE_EXIT_CODE = 12
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
QUOTA_RE = re.compile(
    r"usage limit|rate[ _-]?limit|quota|credits? (?:are )?(?:exhausted|unavailable)|"
    r"limit (?:has been )?reached|hit your .*limit|resets? at|insufficient_quota",
    re.IGNORECASE,
)
# A provider CLI refusing the model name it was given (a routing tier or saved
# setting naming a model this account does not have) — as opposed to failing
# while working. Grok: `Couldn't set model 'x': ... "unknown model id"`.
MODEL_REJECTED_RE = re.compile(
    r"unknown model id|couldn'?t set model|(?:unknown|invalid|unsupported|unrecognized) model|"
    r"issue with the selected model|requires usage credits|"
    r"model[^\n]{0,80}(?:not found|does not exist|is not supported|not available)",
    re.IGNORECASE,
)
COMMIT_MARKER_RE = re.compile(r"swarm-issue-worker:commit:([0-9a-f]{40})")
THROUGH_COMMENT_RE = re.compile(r"through-comment:([0-9]+)")
ENVIRONMENT_ONLY_MARKER_RE = re.compile(
    r"swarm-issue-worker:environment-only:issue:[0-9]+;provider:[a-z0-9_-]+"
)
NEEDS_INPUT_MARKER_RE = re.compile(
    r"swarm-issue-worker:needs-input:issue:[0-9]+;provider:([a-z0-9_-]+)"
)
NEEDS_INPUT_LABEL = "AI Needs Input"
NEEDS_INPUT_MARKER = "SWARM_NEEDS_INPUT"
QUESTION_ANSWER_MARKER_RE = re.compile(
    r"swarm-issue-worker:question-answer:issue:[0-9]+;provider:([a-z0-9_-]+)"
)
QUESTION_LABEL = "Question"
QUESTION_ANSWER_MARKER = "SWARM_QUESTION_ANSWER"

# The terminal outcomes that never produce a commit. An issue branch left
# behind by one of them is empty by construction: no pull request is ever
# opened for it, so merged-PR cleanup never sees it (see
# `reconcile_orphan_issue_branches`).
NO_CODE_FINAL_STATUSES: frozenset[str] = frozenset(
    {"environment_only", "answered", "awaiting_input"}
)
NO_CODE_TERMINAL_OUTCOMES: frozenset[str] = frozenset(
    {"environment-only", "question-answer", "needs-input"}
)

# Issue priority, honored when choosing which assigned issue to work next.
# Lower rank sorts first (Urgent before High before Medium before Low). An issue
# with no recognized priority label is treated as Low.
PRIORITY_RANKS: dict[str, int] = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
DEFAULT_PRIORITY_RANK: int = PRIORITY_RANKS["low"]
# Matches labels like "urgent", "priority: high", "priority/medium", "P2".
PRIORITY_LABEL_RE = re.compile(
    r"^(?:priority\s*[:/_-]?\s*)?(urgent|high|medium|low)$|^p([0-3])$"
)
_PN_RANKS: tuple[str, ...] = ("urgent", "high", "medium", "low")


def priority_rank(labels: Iterable[str]) -> int:
    """Return the strongest priority rank named by an issue's labels."""
    best = DEFAULT_PRIORITY_RANK
    for label in labels:
        match = PRIORITY_LABEL_RE.match(str(label).strip().lower())
        if not match:
            continue
        word = match.group(1) or _PN_RANKS[int(match.group(2))]
        best = min(best, PRIORITY_RANKS[word])
    return best


def issue_labels(remote_issue: dict[str, Any]) -> list[str]:
    return [str(label["name"]) for label in remote_issue.get("labels", [])]

# The full set of providers this worker knows how to drive, in default
# rotation order. `key` is the lowercase id used for GitHub App lookups and CLI
# flags; branch/commit attribution maps Grok's provider id to the vendor name
# `xai`. `name` is the display form persisted as `ai_tool` in saved state and
# embedded in completion comments.
KNOWN_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("claude", "Claude"),
    ("codex", "Codex"),
    ("grok", "Grok"),
)
KNOWN_PROVIDER_KEYS: tuple[str, ...] = tuple(key for key, _ in KNOWN_PROVIDERS)
KNOWN_PROVIDER_NAMES: tuple[str, ...] = tuple(name for _, name in KNOWN_PROVIDERS)
# No named favorite. New issues go to the provider with the most usage
# remaining; exact ties follow KNOWN_PROVIDERS order.
PREFERRED_PROVIDER_AUTO = "auto"
BRANCH_PROVIDER_KEYS: tuple[str, ...] = tuple(
    "xai" if key == "grok" else key for key in KNOWN_PROVIDER_KEYS
)


def app_owned_untracked_line(line: str) -> bool:
    """True when a porcelain line is an untracked path under ``.swarm/``."""
    if not line.startswith("?? "):
        return False
    path = line[3:]
    if path.startswith('"') and path.endswith('"') and len(path) >= 2:
        path = path[1:-1]
    return path == ".swarm" or path.startswith(".swarm/")


def ai_tool_key(provider_key: str) -> str:
    """Stable identifier used in Git branch names and commit subjects."""
    return "xai" if provider_key == "grok" else provider_key

# Parses "Completed by **Claude**." / "Reworked by **Grok**." back out of a
# completion comment. Built from the full known set so an old comment still
# resolves even if that provider is currently excluded from the flow.
PREVIOUS_AI_RE = re.compile(
    r"(?:Completed|Reworked|Input requested|Answered) by \*\*("
    + "|".join(re.escape(n) for n in KNOWN_PROVIDER_NAMES)
    + r")\*\*"
)
AUTOPILOT_INSTRUCTION = (
    "This is an unattended autopilot run. Resolve ambiguity from the issue and repository, make "
    "reasonable safe assumptions, and complete the issue using the approach you recommend. Never ask about "
    "preferences, implementation choices, or anything you can safely decide yourself. If several "
    "valid approaches exist, choose the best maintainable option yourself. Ask for user input only "
    "when continuing is genuinely impossible without credentials, authority, an external action, "
    "or information that is not available to you."
)
NEEDS_INPUT_INSTRUCTION = (
    f"If and only if you are completely blocked on required user input, make no repository changes "
    f"and put {NEEDS_INPUT_MARKER} on its own final line. Before that marker, use exactly these "
    "Markdown headings: '## Action required', '## Summary', '## Recommendations', and "
    "'## Step-by-step guide'. Under Action required, ask one clear question or name the exact user "
    "action needed and state the exact reply that will resume work. Under Summary, explain why AI "
    "cannot continue without it. Under Recommendations, give your preferred course and explicitly "
    "warn the user not to put passwords, tokens, private keys, or other sensitive information in "
    "the issue whenever credentials are involved. Under Step-by-step guide, provide concrete "
    "numbered instructions when setup or an external action is required; otherwise write '- None.'"
)
QUESTION_INSTRUCTION = (
    f"This issue is labelled {QUESTION_LABEL}. Answer the question; do not edit files, create "
    "commits, or propose a code change as completed work. You may inspect the repository and use "
    "read-only commands to ground the answer. Use exactly these Markdown headings: '## Answer', "
    "'## Evidence', and '## Recommendations'. Be direct under Answer, cite the relevant repository "
    "evidence under Evidence, and put practical next steps under Recommendations (or '- None.' if "
    f"there are none). Put {QUESTION_ANSWER_MARKER} on its own final line."
)
SUMMARY_INSTRUCTION = (
    f"Unless you are returning {NEEDS_INPUT_MARKER} or {QUESTION_ANSWER_MARKER}, your final response "
    "is shown in the terminal "
    "and posted to GitHub as rendered Markdown. "
    "Keep it concise and use exactly these headings: '## Summary', '## Changes', "
    "'## Verification', and '## Operational notes'. Under Summary, state the outcome and the "
    "problem resolved in one short paragraph. Under Changes and Verification, use short bullets. "
    "Under Operational notes, state whether the commit was pushed and mention only deployment, "
    "restart, migration, or follow-up requirements that actually apply; otherwise write '- None.' "
    "Do not include code snippets, diffs, file contents, command transcripts, or step-by-step "
    "implementation output."
)
ENVIRONMENT_ONLY_MARKER = "SWARM_ENVIRONMENT_ONLY"

# CI monitoring (``--monitor-actions``). Issues the worker files for a failing
# pipeline carry CI_FAILURE_LABEL and a hidden marker in the body; both are how
# a later run recognizes that this failure was already reported.
CI_FAILURE_LABEL = "ci-failure"
CI_FAILURE_LABELS: tuple[tuple[str, str, str], ...] = (
    ("bug", "D73A4A", "Something isn't working"),
    (CI_FAILURE_LABEL, "B60205", "Filed by SWARM when the repository's Actions pipeline is failing"),
)
CI_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
CI_LOG_EXCERPT_CHARS = 3000
CI_ISSUE_MARKER_RE = re.compile(r"swarm-issue-worker:ci-failure:branch:([^;\s]+);sha:([0-9a-f]{40})")


class WorkerError(RuntimeError):
    pass


def timestamp() -> str:
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


def iso_timestamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def log(message: str) -> None:
    stream = sys.stderr if message.lstrip().upper().startswith("ERROR:") else sys.stdout
    print(f"[{timestamp()}] {message}", file=stream, flush=True)


def env_value(name: str, fallback: str) -> str:
    return os.environ.get(name, fallback)


def env_bool(name: str, fallback: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return fallback
    return value.lower() in {"1", "true", "yes", "on"}


def csv_values(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def command_available(command: str | None) -> bool:
    return bool(command and (shutil.which(command) or Path(command).exists()))


def run_command(
    command: Sequence[str | Path],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(
        [str(item) for item in command],
        cwd=cwd,
        env=merged_env,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise WorkerError(f"Command failed ({' '.join(map(str, command))}): {detail}")
    return result


TRANSIENT_GITHUB_ERROR = re.compile(
    r"something went wrong while executing your query"
    r"|HTTP 5\d\d|\b50[0-4]\b|bad gateway|service unavailable|gateway time-?out"
    r"|timed? ?out|connection (?:reset|refused|closed)|unexpected EOF",
    re.IGNORECASE,
)


def is_transient_github_error(message: str) -> bool:
    """Whether a failed `gh` call looks like a GitHub-side hiccup worth retrying.

    GraphQL reports internal failures as a bare "Something went wrong while
    executing your query" plus a request ID, with no typed error — indistinguishable
    from a 5xx, and cleared by trying again."""
    return bool(TRANSIENT_GITHUB_ERROR.search(message))


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def flatten_pages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise WorkerError("GitHub returned a non-list response")
    if value and all(isinstance(item, list) for item in value):
        return [entry for page in value for entry in page]
    return list(value)


@dataclasses.dataclass(frozen=True)
class ProviderSpec:
    """Everything the worker needs to run and account for one AI provider."""

    key: str            # "claude" | "codex" | "grok"
    name: str           # "Claude" | "Codex" | "Grok"
    model: str
    effort: str
    router_model: str
    router_effort: str
    # Operator-editable description of what this tool is best at. The router
    # weighs it when choosing which enabled tool receives an issue.
    strengths: str
    bin: str | None
    enabled: bool       # in the rotation for new work

    @classmethod
    def from_args(cls, args: argparse.Namespace, key: str, name: str) -> "ProviderSpec":
        return cls(
            key=key,
            name=name,
            model=getattr(args, f"{key}_model"),
            effort=getattr(args, f"{key}_effort"),
            router_model=getattr(args, f"{key}_router_model"),
            router_effort=getattr(args, f"{key}_router_effort"),
            strengths=getattr(args, f"{key}_router_strengths", "") or default_provider_strengths(key),
            bin=getattr(args, f"{key}_bin") or None,
            enabled=key in set(args.enabled_provider or KNOWN_PROVIDER_KEYS),
        )


@dataclasses.dataclass(frozen=True)
class Config:
    script_dir: Path
    repo_dir: Path
    state_dir: Path
    github_repository: str
    github_assignee: str
    trusted_followup_authors: tuple[str, ...]
    completion_authors: tuple[str, ...]
    ready_label: str
    minimum_remaining_percent: float
    providers: tuple[ProviderSpec, ...]
    dynamic_model_routing: bool
    routing_tiers: dict[str, tuple[Any, ...]]
    routing_optimization: str
    allow_usage_credit_models: bool
    preferred_provider: str
    dry_run: bool
    gh_bin: str
    git_bin: str
    python_bin: str
    github_apps_config: Path
    openssl_bin: str
    require_bot_auth: bool
    auto_approve: bool
    auto_merge: bool
    auto_promote: bool
    monitor_actions: bool
    require_issue_tests: bool
    adversarial_uat_enabled: bool
    allow_environment_only_summary: bool
    branch_prefix: str
    base_branch: str
    integration_branch: str
    remote_name: str
    github_host: str
    ai_execution_history_enabled: bool
    prompt_feedback_upload_enabled: bool
    application_version: str
    execution_history_db: Path

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Config":
        script_dir = SCRIPT_HOME
        state_dir = Path(args.state_dir).expanduser().resolve()
        history_db = (
            Path(args.execution_history_db).expanduser().resolve()
            if args.execution_history_db
            else state_dir / "swarm-automation.sqlite3"
        )
        return cls(
            script_dir=script_dir,
            repo_dir=Path(args.repo_dir).expanduser().resolve(),
            state_dir=state_dir,
            github_repository=args.github_repository,
            github_assignee=args.assignee,
            trusted_followup_authors=tuple(args.trusted_followup_author),
            completion_authors=tuple(args.completion_author),
            ready_label=args.ready_label,
            minimum_remaining_percent=args.minimum_remaining_percent,
            providers=tuple(
                ProviderSpec.from_args(args, key, name) for key, name in KNOWN_PROVIDERS
            ),
            dynamic_model_routing=bool(args.dynamic_model_routing),
            routing_tiers=_routing_tiers_from_args(args.routing_tiers),
            routing_optimization=normalize_routing_optimization(args.routing_optimization),
            allow_usage_credit_models=bool(args.allow_usage_credit_models),
            preferred_provider=args.preferred_provider,
            dry_run=args.dry_run,
            gh_bin=args.gh_bin,
            git_bin=args.git_bin,
            python_bin=args.python_bin,
            github_apps_config=Path(args.github_apps_config).expanduser(),
            openssl_bin=args.openssl_bin,
            require_bot_auth=args.require_bot_auth,
            auto_approve=args.auto_approve,
            auto_merge=args.auto_merge,
            auto_promote=args.auto_promote,
            monitor_actions=args.monitor_actions,
            require_issue_tests=args.require_issue_tests,
            adversarial_uat_enabled=args.adversarial_uat_enabled,
            allow_environment_only_summary=args.allow_environment_only_summary,
            branch_prefix=args.branch_prefix.strip("/"),
            base_branch=args.base_branch,
            integration_branch=args.integration_branch,
            remote_name=args.remote_name,
            github_host=args.github_host,
            ai_execution_history_enabled=args.ai_execution_history_enabled,
            prompt_feedback_upload_enabled=args.prompt_feedback_upload_enabled,
            application_version=args.application_version,
            execution_history_db=history_db,
        )

    def spec(self, provider: str) -> ProviderSpec | None:
        provider = str(provider).lower()
        return next((s for s in self.providers if s.key == provider), None)

    def require_spec(self, provider: str) -> ProviderSpec:
        spec = self.spec(provider)
        if spec is None:
            raise WorkerError(f"Unknown AI provider: {provider}")
        return spec

    @property
    def enabled_specs(self) -> tuple[ProviderSpec, ...]:
        return tuple(s for s in self.providers if s.enabled)


class PidLock:
    def __init__(self, path: Path, label: str) -> None:
        self.path = path
        self.label = label
        self.acquired = False

    def __enter__(self) -> "PidLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.mkdir()
        except FileExistsError:
            pid_path = self.path / "pid"
            try:
                owner = int(pid_path.read_text().strip())
                os.kill(owner, 0)
            except (OSError, ValueError):
                pid_path.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    self.path.rmdir()
                try:
                    self.path.mkdir()
                except FileExistsError as error:
                    raise WorkerError(f"Another {self.label} acquired the lock during recovery") from error
            else:
                log(f"Another {self.label} is already running as pid {owner}; skipping this run.")
                raise SystemExit(0)
        (self.path / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        self.acquired = True
        return self

    def __exit__(self, *_: object) -> None:
        if self.acquired:
            (self.path / "pid").unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                self.path.rmdir()


class GitHubClient:
    def __init__(self, config: Config, apps: GitHubAppAuth) -> None:
        self.config = config
        self.apps = apps

    def environment(self, provider: str | None = None, *, require: bool | None = None) -> dict[str, str]:
        if provider and self.apps.configured(provider):
            return self.apps.bot_environment(provider)
        must_use_bot = self.config.require_bot_auth if require is None else require
        if provider and must_use_bot:
            raise WorkerError(f"GitHub App authentication is required but not configured for {provider}")
        return {}

    def gh(self, arguments: Sequence[str], provider: str | None = None, input_text: str | None = None) -> str:
        result = run_command(
            [self.config.gh_bin, *arguments],
            cwd=self.config.repo_dir,
            env=self.environment(provider),
            input_text=input_text,
        )
        return result.stdout

    def api_list(self, endpoint: str, fields: dict[str, str | int] | None = None) -> list[dict[str, Any]]:
        arguments = ["api", "--method", "GET", "--paginate", "--slurp", endpoint]
        for key, value in (fields or {}).items():
            flag = "-F" if isinstance(value, int) else "-f"
            arguments.extend([flag, f"{key}={value}"])
        return flatten_pages(json.loads(self.gh(arguments)))


@dataclasses.dataclass
class IssueContext:
    number: int
    title: str
    body: str
    labels: list[str]
    url: str
    work_type: str = "initial"
    previous_commit_sha: str = ""
    previous_ai: str = ""
    previous_completion_comment: dict[str, Any] | None = None
    followup_comments: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    trigger_comment_id: int | None = None
    # True for the issue filed by the CI monitor and handed straight to this
    # run, so it is never also picked up by ``select_issue`` in the same pass.
    ci_monitor: bool = False


@dataclasses.dataclass
class ProviderChoice:
    name: str
    model: str
    effort: str
    session_id: str = ""
    resume: bool = False

    @property
    def key(self) -> str:
        return self.name.lower()


@dataclasses.dataclass(frozen=True)
class ProviderUsage:
    """Result of probing one provider's remaining usage.

    status: 0 = usable, 1 = below the configured minimum, 2 = unavailable
            (not installed, not signed in, or the probe itself failed).
    remaining_percent: headroom left in the provider's most constrained usage
            window. None when it could
            not be determined.
    detail: a short human-readable breakdown of each usage window (e.g.
            "session 82% / week 95% remaining"). None when unavailable.
    """

    status: int
    remaining_percent: float | None = None
    detail: str | None = None

    @property
    def usable(self) -> bool:
        return self.status == 0


def is_worker_comment(comment: dict[str, Any]) -> bool:
    return "<!-- swarm-issue-worker:" in str(comment.get("body") or "")


def normalize_author(login: str) -> str:
    """Fold a GitHub login for trusted/completion author matching.

    The API reports bot accounts with a ``[bot]`` suffix (``github-actions[bot]``,
    ``swarm-codex-bot[bot]``) while operators routinely list them without it
    (``github-actions``). Comparing case-insensitively and with the suffix
    stripped lets either form match.
    """
    return login.strip().lower().removesuffix("[bot]")


def author_matches(login: str, allowed: Iterable[str]) -> bool:
    return normalize_author(login) in {normalize_author(name) for name in allowed}


# `gh pr merge` refuses with this when branch protection reserves merges to
# specific people (or a bypass list) and the bot is not one of them. Retrying
# cannot help; only a human with merge access can.
MERGE_BLOCKED_BY_POLICY = re.compile(
    r"base branch policy prohibits the merge|protected branch", re.IGNORECASE
)


def is_merge_blocked_by_policy(message: str) -> bool:
    return bool(MERGE_BLOCKED_BY_POLICY.search(message))


# Product versioning (see .claude/rules/versioning.md): a repository opts in by
# tracking a `VERSION` file holding `MAJOR.MINOR.PATCH` as of the commit that
# last changed it. CI adds one patch per later commit; the only thing the
# worker ever writes is a minor bump, and only for an issue a trusted user
# labelled `minor`.
VERSION_FILE = "VERSION"
MINOR_VERSION_LABEL = "minor"
VERSION_LINE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _version_entry_lines(text: str) -> list[int]:
    return [
        index
        for index, line in enumerate(text.splitlines())
        if line.strip() and not line.strip().startswith("#")
    ]


def parse_version_file(text: str) -> tuple[int, int, int] | None:
    """`(major, minor, patch)` from a VERSION file, or None when it is not
    exactly one `MAJOR.MINOR.PATCH` line (blank lines and `#` comments are
    ignored)."""
    entries = _version_entry_lines(text)
    if len(entries) != 1:
        return None
    match = VERSION_LINE_RE.match(text.splitlines()[entries[0]].strip())
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def bump_minor_in_version_text(text: str) -> str:
    """The same file with its version line raised to the next minor
    (`0.1.9` -> `0.2.0`), leaving comments untouched."""
    parsed = parse_version_file(text)
    if parsed is None:
        raise WorkerError(f"{VERSION_FILE} is not a single MAJOR.MINOR.PATCH line")
    lines = text.splitlines()
    lines[_version_entry_lines(text)[0]] = f"{parsed[0]}.{parsed[1] + 1}.0"
    return "\n".join(lines) + "\n"


def extract_completion_metadata(
    comments: Iterable[dict[str, Any]], completion_authors: set[str]
) -> dict[str, Any] | None:
    allowed_completion = {normalize_author(name) for name in completion_authors}
    matches = []
    for comment in sorted(comments, key=lambda item: int(item.get("id", 0))):
        body = str(comment.get("body") or "")
        author = str((comment.get("user") or {}).get("login") or "")
        marker = COMMIT_MARKER_RE.search(body)
        if marker and normalize_author(author) in allowed_completion:
            matches.append({"commit_sha": marker.group(1), "comment_id": int(comment["id"]), "author": author})
    return matches[-1] if matches else None


def extract_needs_input_metadata(
    comments: Iterable[dict[str, Any]], completion_authors: set[str]
) -> dict[str, Any] | None:
    """Return the newest authenticated input request posted by a worker."""
    allowed_completion = {normalize_author(name) for name in completion_authors}
    matches = []
    for comment in sorted(comments, key=lambda item: int(item.get("id", 0))):
        body = str(comment.get("body") or "")
        author = str((comment.get("user") or {}).get("login") or "")
        marker = NEEDS_INPUT_MARKER_RE.search(body)
        if marker and normalize_author(author) in allowed_completion:
            matches.append(
                {
                    "provider": marker.group(1),
                    "comment_id": int(comment["id"]),
                    "author": author,
                    "comment": comment,
                }
            )
    return matches[-1] if matches else None


def extract_question_answer_metadata(
    comments: Iterable[dict[str, Any]], completion_authors: set[str]
) -> dict[str, Any] | None:
    """Return the newest authenticated no-code answer posted by a worker."""
    allowed_completion = {normalize_author(name) for name in completion_authors}
    matches = []
    for comment in sorted(comments, key=lambda item: int(item.get("id", 0))):
        body = str(comment.get("body") or "")
        author = str((comment.get("user") or {}).get("login") or "")
        marker = QUESTION_ANSWER_MARKER_RE.search(body)
        if marker and normalize_author(author) in allowed_completion:
            matches.append(
                {
                    "provider": marker.group(1),
                    "comment_id": int(comment["id"]),
                    "author": author,
                    "comment": comment,
                }
            )
    return matches[-1] if matches else None


def latest_terminal_outcome(
    comments: Iterable[dict[str, Any]], completion_authors: set[str]
) -> str:
    """Which terminal worker comment an issue currently ends with.

    One of ``commit``, ``environment-only``, ``question-answer``,
    ``needs-input``, or ``""`` when the issue carries no authenticated
    terminal worker comment at all. Only comments from a configured
    completion author count, so a quoted marker in someone else's comment can
    never be mistaken for the worker's own record of what happened."""
    allowed_completion = {normalize_author(name) for name in completion_authors}
    outcome = ""
    for comment in sorted(comments, key=lambda item: int(item.get("id", 0))):
        body = str(comment.get("body") or "")
        author = str((comment.get("user") or {}).get("login") or "")
        if normalize_author(author) not in allowed_completion:
            continue
        for name, expression in (
            ("commit", COMMIT_MARKER_RE),
            ("environment-only", ENVIRONMENT_ONLY_MARKER_RE),
            ("question-answer", QUESTION_ANSWER_MARKER_RE),
            ("needs-input", NEEDS_INPUT_MARKER_RE),
        ):
            if expression.search(body):
                outcome = name
                break
    return outcome


def extract_followup_metadata(
    comments: Iterable[dict[str, Any]],
    trusted_followup_authors: set[str],
    completion_authors: set[str],
) -> dict[str, Any] | None:
    allowed_trusted = {normalize_author(name) for name in trusted_followup_authors}
    allowed_completion = {normalize_author(name) for name in completion_authors}
    ordered = sorted(comments, key=lambda item: int(item.get("id", 0)))
    completion_comments = []
    for comment in ordered:
        body = str(comment.get("body") or "")
        author = str((comment.get("user") or {}).get("login") or "")
        if (
            COMMIT_MARKER_RE.search(body)
            or NEEDS_INPUT_MARKER_RE.search(body)
            or QUESTION_ANSWER_MARKER_RE.search(body)
        ) and normalize_author(author) in allowed_completion:
            completion_comments.append(comment)
    if not completion_comments:
        return None
    completion = completion_comments[-1]
    body = str(completion.get("body") or "")
    commit_match = COMMIT_MARKER_RE.search(body)
    if not commit_match:
        # An input request may follow an earlier code completion. Preserve that
        # commit as useful review context, but allow an initial no-code request
        # to resume without inventing a commit requirement.
        prior_commits = [
            COMMIT_MARKER_RE.search(str(item.get("body") or ""))
            for item in ordered
            if int(item.get("id", 0)) < int(completion["id"])
            and normalize_author(str((item.get("user") or {}).get("login") or ""))
            in allowed_completion
        ]
        commit_match = next((match for match in reversed(prior_commits) if match), None)
    ai_match = PREVIOUS_AI_RE.search(body)
    through_match = THROUGH_COMMENT_RE.search(body)
    processed_through = int(through_match.group(1)) if through_match else int(completion["id"])
    # A no-code/environment-only follow-up has no commit marker of its own,
    # but it still consumes every trusted comment through the trigger that
    # caused the review. Treat its authenticated marker as a durable cursor;
    # otherwise the next worker cycle sees the same CI/operator comment after
    # the older commit marker and runs the AI again forever.
    for comment in ordered:
        comment_body = str(comment.get("body") or "")
        author = str((comment.get("user") or {}).get("login") or "")
        if (
            ENVIRONMENT_ONLY_MARKER_RE.search(comment_body)
            and normalize_author(author) in allowed_completion
        ):
            environment_through = THROUGH_COMMENT_RE.search(comment_body)
            if environment_through:
                processed_through = max(processed_through, int(environment_through.group(1)))
    followups = []
    for comment in ordered:
        author = str((comment.get("user") or {}).get("login") or "")
        if (
            int(comment.get("id", 0)) > processed_through
            and not is_worker_comment(comment)
            and normalize_author(author) in allowed_trusted
        ):
            followups.append(
                {
                    "id": int(comment["id"]),
                    "author": author,
                    "created_at": str(comment.get("created_at") or ""),
                    "body": str(comment.get("body") or ""),
                }
            )
    if not followups:
        return None
    return {
        "previous_commit_sha": commit_match.group(1) if commit_match else "",
        "previous_ai": ai_match.group(1) if ai_match else "",
        "previous_completion_comment": {
            "id": int(completion["id"]),
            "author": str((completion.get("user") or {}).get("login") or "unknown"),
            "created_at": str(completion.get("created_at") or ""),
            "body": body,
        },
        "followup_comments": followups,
        "trigger_comment_id": int(followups[-1]["id"]),
        "trigger_created_at": str(followups[0]["created_at"]),
    }


class Worker(AdversarialUatMixin):
    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = config.state_dir
        self.state.mkdir(parents=True, exist_ok=True)
        self.lock_dir = self.state / "worker.lock"
        self.completed_file = self.state / "completed-issues"
        self.pending_file = self.state / "pending-delivery.json"
        self.in_progress_file = self.state / "in-progress-issue.json"
        self.paused_dir = self.state / "quota-paused-issues"
        self.closed_paused_dir = self.state / "closed-paused-issues"
        self.ai_output_file = self.state / "last-ai-output.log"
        self.ai_diagnostic_file = self.state / "last-ai-diagnostic.log"
        self.ai_prompt_file = self.state / "last-ai-prompt.txt"
        self.apps = GitHubAppAuth(
            config.github_apps_config, config.openssl_bin, repository=config.github_repository
        )
        self.github = GitHubClient(config, self.apps)
        self.choice: ProviderChoice | None = None
        self.issue: IssueContext | None = None
        self.issue_images: list[IssueImage] = []
        self._image_by_url: dict[str, IssueImage] = {}
        self._image_failures: set[str] = set()
        self.routing: dict[str, Any] | None = None
        self.quota_resume_ready = False
        # Remaining-usage snapshot for the chosen provider taken while selecting
        # it for a fresh run, reused by post_started_comment so the start notice
        # doesn't probe /usage a second time.
        self.start_usage: ProviderUsage | None = None
        # Usage probe for every enabled provider taken while selecting one for
        # a fresh run, plus the resulting best-first order. The dynamic router
        # picks the AI tool out of that order, so it never hands an issue to a
        # provider that has no capacity this pass.
        self.provider_usages: dict[str, ProviderUsage] = {}
        self.provider_priority: tuple[str, ...] = ()
        self.history = ExecutionHistoryService(
            config.ai_execution_history_enabled,
            config.execution_history_db,
        )
        if self.history.error:
            log(f"WARNING: AI execution history is unavailable: {self.history.error}")

    def git(self, *arguments: str, env: dict[str, str] | None = None, check: bool = True) -> str:
        return run_command(
            [self.config.git_bin, "-C", self.config.repo_dir, *arguments], env=env, check=check
        ).stdout.strip()

    def worktree_status(self) -> str:
        """Porcelain lines that are real checkout work.

        The desktop app writes an untracked test-definition draft under
        ``.swarm/``. That draft must not block a new issue and must not be
        committed onto an issue branch. A change to a file Git already tracks
        under ``.swarm/`` still counts.
        """
        lines = [
            line
            for line in self.git("status", "--porcelain").splitlines()
            if line and not app_owned_untracked_line(line)
        ]
        return "\n".join(lines)

    def app_owned_untracked_paths(self) -> list[str]:
        return [
            path
            for path in self.git(
                "ls-files", "--others", "--exclude-standard", "-z", "--", ".swarm", check=False
            ).split("\0")
            if path
        ]

    def git_ok(self, *arguments: str) -> bool:
        return run_command(
            [self.config.git_bin, "-C", self.config.repo_dir, *arguments], check=False
        ).returncode == 0

    def read_state(self, path: Path | None = None) -> dict[str, Any]:
        return read_json(path or self.in_progress_file)

    def write_state(self, value: dict[str, Any], path: Path | None = None) -> None:
        atomic_write_json(path or self.in_progress_file, value)

    def update_state(self, **changes: Any) -> dict[str, Any]:
        state = self.read_state()
        state.update(changes)
        self.write_state(state)
        return state

    def completed_numbers(self) -> set[int]:
        if not self.completed_file.exists():
            return set()
        return {
            int(line)
            for line in self.completed_file.read_text(encoding="utf-8").splitlines()
            if line.isdigit()
        }

    def record_completed(self, issue_number: int) -> None:
        completed = self.completed_numbers()
        if issue_number not in completed:
            with self.completed_file.open("a", encoding="utf-8") as stream:
                stream.write(f"{issue_number}\n")

    def clear_in_progress(self, issue_number: int) -> None:
        if self.in_progress_file.exists() and int(self.read_state().get("issue_number", -1)) == issue_number:
            self.in_progress_file.unlink()

    @property
    def trusted_followup_authors(self) -> set[str]:
        return set(self.config.trusted_followup_authors)

    @property
    def completion_authors(self) -> set[str]:
        return set(self.config.completion_authors) | self.apps.completion_authors()

    def comments(self, issue_number: int) -> list[dict[str, Any]]:
        return self.github.api_list(
            f"repos/{self.config.github_repository}/issues/{issue_number}/comments", {"per_page": 100}
        )

    def provider_bin(self, key: str) -> str | None:
        spec = self.config.spec(key)
        return spec.bin if spec else None

    def claude_usage(self) -> ProviderUsage:
        claude_bin = self.provider_bin("claude")
        if not command_available(claude_bin):
            log("Claude remaining quota — unavailable (claude was not found in PATH).")
            return ProviderUsage(2)
        result = run_command(
            [
                claude_bin,
                "-p",
                "/usage",
                "--output-format",
                "json",
                "--tools",
                "",
                "--no-session-persistence",
            ],
            check=False,
        )
        if result.returncode != 0:
            log("Claude quota unavailable: Claude Code's /usage command failed. Run 'claude auth login' if this persists.")
            return ProviderUsage(2)
        try:
            usage = str(json.loads(result.stdout).get("result") or "")
        except json.JSONDecodeError:
            usage = ""
        session = re.search(r"^Current session:\s*([0-9.]+)% used", usage, re.MULTILINE)
        week = re.search(r"^Current week(?: \([^)]*\))?:\s*([0-9.]+)% used", usage, re.MULTILINE)
        if not session or not week:
            log("Claude quota unavailable: Claude Code returned an unrecognized /usage format.")
            return ProviderUsage(2)
        session_remaining = 100 - float(session.group(1))
        week_remaining = 100 - float(week.group(1))
        log(f"Claude remaining quota — session: {session_remaining:g}%; week: {week_remaining:g}%.")
        remaining = min(session_remaining, week_remaining)
        detail = f"session {session_remaining:g}% / week {week_remaining:g}% remaining"
        below_minimum = remaining < self.config.minimum_remaining_percent
        return ProviderUsage(1 if below_minimum else 0, remaining, detail)

    def claude_capacity(self) -> int:
        return self.claude_usage().status

    def codex_usage(self) -> ProviderUsage:
        codex_bin = self.provider_bin("codex")
        if not command_available(codex_bin):
            log("Codex quota unavailable: codex was not found in PATH.")
            return ProviderUsage(2)
        result: subprocess.CompletedProcess[str] | None = None
        for attempt in range(2):
            result = run_command(
                [
                    self.config.python_bin,
                    self.config.script_dir / "codex_rate_limits.py",
                    "--codex-bin",
                    codex_bin,
                    "--timeout",
                    "30",
                ],
                check=False,
            )
            if result.returncode == 0:
                break
            if attempt == 0:
                log("Codex capacity check did not respond; retrying once.")
                time.sleep(0.5)
        assert result is not None
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            reason = f" Details: {detail[-1]}" if detail else ""
            log(f"Codex quota unavailable after two attempts.{reason}")
            return ProviderUsage(2)
        try:
            limits = json.loads(result.stdout)
            windows = [limits.get(key) for key in ("primary", "secondary") if limits.get(key) is not None]
            used = [float(window["usedPercent"]) for window in windows]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            log("Codex quota unavailable: the local rate-limit response was invalid.")
            return ProviderUsage(2)
        if not used:
            log("Codex quota unavailable: the local rate-limit response had no active windows.")
            return ProviderUsage(2)
        summary = "; ".join(
            f"{key}: {100 - float(limits[key]['usedPercent']):g}%"
            for key in ("primary", "secondary")
            if limits.get(key) is not None
        )
        log(f"Codex remaining quota — {summary}.")
        remaining = min(100 - amount for amount in used)
        detail = f"{summary} remaining"
        available = (
            limits.get("rateLimitReachedType") is None
            and not bool(limits.get("spendControlReached", False))
            and remaining >= self.config.minimum_remaining_percent
        )
        return ProviderUsage(0 if available else 1, remaining, detail)

    def codex_capacity(self) -> int:
        return self.codex_usage().status

    def grok_usage(self) -> ProviderUsage:
        grok_bin = self.provider_bin("grok")
        if not command_available(grok_bin):
            log("Grok quota unavailable: grok was not found in PATH.")
            return ProviderUsage(2)
        home = Path(os.environ.get("HOME", "~")).expanduser()
        if not (home / ".grok" / "auth.json").is_file():
            if os.environ.get("XAI_API_KEY"):
                # Pay-as-you-go API billing has no account allowance to read.
                log("Grok remaining quota — API-key billing has no account allowance to check.")
                return ProviderUsage(0, 100.0, "API-key billing; no account allowance to check")
            log("Grok quota unavailable: not signed in (run 'grok login').")
            return ProviderUsage(2)
        # A signed-in grok.com account has a weekly/monthly credit allowance
        # (the CLI's /usage "Usage limit" tab), read through the local agent.
        result: subprocess.CompletedProcess[str] | None = None
        for attempt in range(2):
            result = run_command(
                [
                    self.config.python_bin,
                    self.config.script_dir / "grok_rate_limits.py",
                    "--grok-bin",
                    grok_bin,
                    "--timeout",
                    "30",
                ],
                check=False,
            )
            if result.returncode == 0:
                break
            if attempt == 0:
                log("Grok capacity check did not respond; retrying once.")
                time.sleep(0.5)
        assert result is not None
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            reason = f" Details: {detail[-1]}" if detail else ""
            log(f"Grok quota unavailable after two attempts.{reason}")
            return ProviderUsage(2)
        try:
            limits = json.loads(result.stdout)
            used = float(limits["usedPercent"])
            period = str(limits.get("period") or "period")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            log("Grok quota unavailable: the local usage response was invalid.")
            return ProviderUsage(2)
        remaining = max(0.0, min(100.0, 100 - used))
        log(f"Grok remaining quota — {period}: {remaining:g}%.")
        below_minimum = remaining < self.config.minimum_remaining_percent
        return ProviderUsage(1 if below_minimum else 0, remaining, f"{period} {remaining:g}% remaining")

    def grok_capacity(self) -> int:
        return self.grok_usage().status

    def provider_usage(self, provider: str) -> ProviderUsage:
        key = str(provider).lower()
        if key == "claude":
            return self.claude_usage()
        if key == "codex":
            return self.codex_usage()
        if key == "grok":
            return self.grok_usage()
        raise WorkerError(f"Invalid AI provider in saved state: {provider}")

    def provider_capacity(self, provider: str) -> int:
        return self.provider_usage(provider).status

    def usage_snapshot(self, provider: str) -> dict[str, Any] | None:
        """Probe a provider's remaining usage and return a JSON-safe snapshot.

        Used to record how much of a provider's quota was left when a bot
        picked up an issue and again when it finished, so the difference
        approximates what the issue cost. Returns None when the probe failed
        (provider missing, signed out, or an unrecognized response).
        """
        return self.snapshot_from_usage(self.provider_usage(provider))

    @staticmethod
    def snapshot_from_usage(usage: ProviderUsage | None) -> dict[str, Any] | None:
        if usage is None or usage.remaining_percent is None:
            return None
        return {
            "remaining_percent": usage.remaining_percent,
            "detail": usage.detail,
            "captured_at": iso_timestamp(),
        }

    @staticmethod
    def format_usage_snapshot(snapshot: dict[str, Any] | None) -> str:
        if not snapshot or snapshot.get("remaining_percent") is None:
            return "unavailable"
        line = f"{float(snapshot['remaining_percent']):g}% remaining"
        if snapshot.get("detail"):
            line += f" ({snapshot['detail']})"
        return line

    def render_usage_report(self, provider: str, start: dict[str, Any] | None, end: dict[str, Any] | None) -> str:
        """Build the Markdown usage block shown on the completion comment."""
        if not start and not end:
            return ""
        lines = [f"- {provider} usage at start: {self.format_usage_snapshot(start)}"]
        lines.append(f"- {provider} usage at completion: {self.format_usage_snapshot(end)}")
        if (
            start
            and end
            and start.get("remaining_percent") is not None
            and end.get("remaining_percent") is not None
        ):
            spent = float(start["remaining_percent"]) - float(end["remaining_percent"])
            if spent >= 0:
                lines.append(
                    f"- Approx. {provider} usage for this issue: {spent:g} percentage "
                    "points of its most constrained quota window"
                )
            else:
                lines.append(
                    f"- {provider} quota window reset during this run; "
                    "per-issue consumption could not be measured"
                )
        return "\n".join(lines) + "\n"

    def new_session_id(self, spec: ProviderSpec) -> str:
        # Claude and Grok take a caller-supplied UUID up front; Codex mints its
        # own thread id which run_ai captures from the first JSON event.
        return str(uuid.uuid4()) if spec.key in ("claude", "grok") else ""

    def preferred_provider_key(self) -> str | None:
        """Named tie-break provider, or None for ``auto`` / a provider that is off."""
        key = self.config.preferred_provider.lower()
        if key == PREFERRED_PROVIDER_AUTO:
            return None
        enabled = {spec.key for spec in self.config.enabled_specs}
        return key if key in enabled else None

    def choose_provider(
        self, previous_ai: str, remaining: dict[str, float | None]
    ) -> ProviderChoice | None:
        """Pick a provider for this pass.

        `remaining` maps each enabled provider that currently has at least
        ``minimum_remaining_percent`` headroom to its most constrained usage
        window's remaining percentage. Providers below the minimum or whose usage could not be read are absent.

        New issue: the provider with the most usage remaining is chosen, so no
        single account is drained before the others. A named preferred provider
        wins only an exact tie. ``auto`` (no preference) breaks those ties by
        the default provider order instead.

        Follow-up (``previous_ai`` set): the provider that completed the previous
        pass is pushed to the back so the follow-up gets an independent
        reviewer; the rest keep their most-usage-first order. The previous
        provider is only reused when nothing else has capacity.
        """
        specs = {spec.name: spec for spec in self.config.enabled_specs}
        candidates = self.provider_priority_order(previous_ai, remaining)
        previous = (previous_ai or "").capitalize()
        if not candidates:
            return None
        name = candidates[0]
        if previous and name != previous:
            log(
                f"Follow-up review prefers {name} (most usage remaining of the other "
                f"providers) because {previous} completed the previous pass."
            )
        elif previous:
            log(f"No other enabled provider has capacity; falling back to {name} for this follow-up.")
        else:
            log(f"Selected {name}: most usage remaining among enabled providers with capacity.")
        spec = specs[name]
        return ProviderChoice(
            name=spec.name,
            model=spec.model,
            effort=spec.effort,
            session_id=self.new_session_id(spec),
        )

    def provider_priority_order(
        self, previous_ai: str, remaining: dict[str, float | None]
    ) -> list[str]:
        """Enabled provider names that have capacity, best candidate first.

        Most usage remaining wins, a named preferred provider breaks exact
        ties, and the provider that completed the previous pass is pushed to
        the back so a follow-up gets an independent reviewer. Both
        ``choose_provider`` and the dynamic router work from this one order.
        """
        order_index = {spec.name: index for index, spec in enumerate(self.config.enabled_specs)}
        preferred_key = self.preferred_provider_key()
        preferred = next(
            (spec.name for spec in self.config.enabled_specs if spec.key == preferred_key),
            "",
        )
        candidates = [
            spec.name for spec in self.config.enabled_specs if spec.name in remaining
        ]
        candidates.sort(
            key=lambda name: (
                -(remaining[name] if remaining[name] is not None else float("-inf")),
                name != preferred,
                order_index[name],
            )
        )
        previous = (previous_ai or "").capitalize()
        if previous in candidates and len(candidates) > 1:
            candidates = [name for name in candidates if name != previous] + [previous]
        return candidates

    def choose_handoff_provider(self, previous_choice: ProviderChoice, reason: str) -> ProviderChoice | None:
        """Pick a different enabled provider for an already-owned issue branch.

        A saved branch remains the source of truth. The replacement provider
        starts a fresh session on that same branch instead of resuming the
        original provider's session.
        """
        specs = {spec.key: spec for spec in self.config.enabled_specs if spec.name != previous_choice.name}
        preferred = self.preferred_provider_key()
        order = [preferred] if preferred else []
        order += [spec.key for spec in self.config.enabled_specs if spec.key not in order]
        replacement_spec = None
        for key in order:
            spec = specs.get(key)
            if spec and self.provider_capacity(spec.key) == 0:
                replacement_spec = spec
                break
        if replacement_spec is None:
            return None
        assert self.issue
        replacement = ProviderChoice(
            name=replacement_spec.name,
            model=replacement_spec.model,
            effort=replacement_spec.effort,
            session_id=self.new_session_id(replacement_spec),
        )
        log(
            f"{previous_choice.name} cannot continue issue #{self.issue.number} ({reason}); "
            f"{replacement.name} will continue on the existing issue branch."
        )
        return replacement

    def update_state_for_choice(self, choice: ProviderChoice) -> None:
        self.update_state(
            ai_tool=choice.name,
            model=choice.model,
            effort=choice.effort,
            session_id=choice.session_id,
            session_started=False,
        )

    def validate_paused_state(self, state: dict[str, Any]) -> None:
        required_strings = ("issue_title", "issue_url", "base_sha", "ai_tool", "model", "effort", "session_id")
        if not isinstance(state.get("issue_number"), int):
            raise WorkerError("Paused state has no numeric issue_number")
        for key in required_strings:
            if not isinstance(state.get(key), str) or not state[key]:
                raise WorkerError(f"Paused state has no valid {key}")
        if not SHA_RE.fullmatch(state["base_sha"]):
            raise WorkerError("Paused state has an invalid base_sha")
        for key in ("candidate_sha", "attempt_start_sha"):
            value = state.get(key, "")
            if value and (not isinstance(value, str) or not SHA_RE.fullmatch(value)):
                raise WorkerError(f"Paused state has an invalid {key}")
        if state["ai_tool"] not in KNOWN_PROVIDER_NAMES or state.get("status") != "quota_paused":
            raise WorkerError("Paused state has an invalid provider or status")

    def resolve_recovery_candidate(self, base_sha: str, saved_sha: str, tip_sha: str) -> str:
        canonical = self.git("rev-parse", "--verify", f"{saved_sha}^{{commit}}", check=False)
        if (
            canonical
            and self.git_ok("merge-base", "--is-ancestor", base_sha, canonical)
            and self.git_ok("merge-base", "--is-ancestor", canonical, tip_sha)
        ):
            return canonical
        if not SHA_RE.fullmatch(saved_sha):
            raise WorkerError(f"Invalid recovery commit: {saved_sha}")
        prefix = saved_sha[:8]
        matches = [
            sha for sha in self.git("rev-list", tip_sha, f"^{base_sha}").splitlines() if sha.startswith(prefix)
        ]
        if len(matches) != 1:
            raise WorkerError(f"Cannot safely repair recovery commit: {saved_sha}")
        return matches[0]

    def normalize_recovery_commits(self, path: Path, tip_sha: str) -> dict[str, Any]:
        state = read_json(path)
        base = self.git("rev-parse", "--verify", f"{state.get('base_sha', '')}^{{commit}}", check=False)
        if not base or not self.git_ok("merge-base", "--is-ancestor", base, tip_sha):
            raise WorkerError(f"Saved base is invalid or not an ancestor of {tip_sha}")
        candidate = str(state.get("candidate_sha") or "")
        canonical_candidate = self.resolve_recovery_candidate(base, candidate, tip_sha) if candidate else ""
        attempt = str(state.get("attempt_start_sha") or "")
        canonical_attempt = self.git("rev-parse", "--verify", f"{attempt}^{{commit}}", check=False) if attempt else ""
        if attempt and (
            not canonical_attempt
            or not self.git_ok("merge-base", "--is-ancestor", base, canonical_attempt)
            or not self.git_ok("merge-base", "--is-ancestor", canonical_attempt, tip_sha)
        ):
            raise WorkerError("Saved attempt_start_sha is invalid or outside the recovery history")
        state["base_sha"] = base
        if canonical_candidate:
            state["candidate_sha"] = canonical_candidate
        else:
            state.pop("candidate_sha", None)
        if canonical_attempt:
            state["attempt_start_sha"] = canonical_attempt
        else:
            state.pop("attempt_start_sha", None)
        atomic_write_json(path, state)
        if candidate and candidate != canonical_candidate:
            log(f"Repaired saved recovery commit {candidate} as {canonical_candidate}.")
        return state

    def suspend_paused(self) -> None:
        state = self.read_state()
        self.validate_paused_state(state)
        current = self.git("rev-parse", "HEAD")
        state = self.normalize_recovery_commits(self.in_progress_file, current)
        issue_number = int(state["issue_number"])
        base = str(state["base_sha"])
        if not self.git_ok("merge-base", "--is-ancestor", base, current):
            raise WorkerError(f"Paused issue #{issue_number}'s branch no longer descends from its saved base commit")
        self.paused_dir.mkdir(parents=True, exist_ok=True)
        paused_file = self.paused_dir / f"{issue_number}.json"
        if paused_file.exists():
            raise WorkerError(f"A quota-paused state already exists for issue #{issue_number}: {paused_file}")
        stash_oid = ""
        if self.worktree_status():
            # Leave the app's untracked .swarm/ draft in place. Stashing it
            # would hide the test definition for every other issue that runs
            # while this one is paused.
            exclusions = [":(exclude).swarm"]
            if state.get("adversarial"):
                # UAT owns the tracked suite definition. Shelve that along with
                # the tests, while keeping unrelated untracked app drafts local.
                exclusions = [f":(exclude){path}" for path in self.git(
                    "ls-files", "--others", "--exclude-standard", "-z", "--", ".swarm"
                ).split("\0") if path and path != ".swarm/tests.json"]
            self.git(
                "stash",
                "push",
                "--include-untracked",
                "--message",
                f"swarm issue worker paused #{issue_number}",
                "--",
                ".",
                *exclusions,
            )
            stash_oid = self.git("rev-parse", "refs/stash")
            if self.worktree_status():
                raise WorkerError(f"Could not shelve all work for quota-paused issue #{issue_number}")
        attempt = str(state.get("attempt_start_sha") or "")
        candidate = str(state.get("candidate_sha") or "")
        if attempt and current != attempt:
            candidate = current
        elif not attempt and not candidate and state.get("quota_paused_at"):
            candidate = self.git(
                "rev-list", "-1", f"--before={state['quota_paused_at']}", current, check=False
            )
            if candidate == base:
                candidate = ""
        if candidate and not self.git_ok("merge-base", "--is-ancestor", base, candidate):
            raise WorkerError(f"Candidate commit for paused issue #{issue_number} is not after its base")
        if candidate:
            state["candidate_sha"] = candidate
        if stash_oid:
            state["worktree_stash_oid"] = stash_oid
        atomic_write_json(paused_file, state)
        self.in_progress_file.unlink()
        integ = self.config.integration_branch
        if self.git("branch", "--show-current") != integ and not self.worktree_status():
            self.git("switch", integ, check=False)
        log(f"Shelved quota-paused issue #{issue_number}; other ready issues may now run.")

    def restore_paused(self, paused_file: Path) -> None:
        if self.in_progress_file.exists():
            raise WorkerError("Cannot restore a quota-paused issue while another issue is active")
        state = read_json(paused_file)
        self.validate_paused_state(state)
        if self.worktree_status():
            raise WorkerError("Repository must be clean before restoring a quota-paused issue")
        branch = str(
            state.get("branch_name")
            or f"{self.config.branch_prefix}/{ai_tool_key(str(state['ai_tool']).lower())}/issue-{state['issue_number']}"
        )
        if self.git("branch", "--show-current") != branch:
            if self.git_ok("show-ref", "--verify", f"refs/heads/{branch}"):
                self.git("switch", branch)
            else:
                self.git("switch", "-c", branch, str(state["base_sha"]))
        state["branch_name"] = branch
        atomic_write_json(paused_file, state)
        state = self.normalize_recovery_commits(paused_file, self.git("rev-parse", "HEAD"))
        stash_oid = str(state.get("worktree_stash_oid") or "")
        issue_number = int(state["issue_number"])
        if stash_oid:
            if not self.git_ok("cat-file", "-e", f"{stash_oid}^{{commit}}"):
                raise WorkerError(f"Shelved work for issue #{issue_number} is missing: {stash_oid}")
            if not self.git_ok("stash", "apply", "--index", stash_oid):
                raise WorkerError(
                    f"Shelved work for issue #{issue_number} conflicts with newer commits; resolve manually"
                )
        state["status"] = "active"
        state["quota_resumed_at"] = iso_timestamp()
        state.pop("worktree_stash_oid", None)
        self.write_state(state)
        paused_file.unlink()
        if stash_oid:
            stash_list = self.git("stash", "list", "--format=%H %gd")
            reference = next((line.split()[1] for line in stash_list.splitlines() if line.split()[0] == stash_oid), "")
            if reference:
                self.git("stash", "drop", reference, check=False)

    def paused_files(self) -> list[Path]:
        files = sorted(self.paused_dir.glob("*.json")) if self.paused_dir.is_dir() else []
        for path in files:
            self.validate_paused_state(read_json(path))
        return files

    def issue_is_closed(self, issue_number: int) -> bool:
        issue = json.loads(
            self.github.gh(
                ["api", "--method", "GET", f"repos/{self.config.github_repository}/issues/{issue_number}"]
            )
        )
        return str(issue.get("state") or "").lower() == "closed"

    def reconcile_issue_pull_requests(self) -> None:
        """Reconcile automation for existing issue PRs.

        The approval setting is intentionally one operation: approve an issue
        PR and then squash-merge it into the AI integration branch.
        """
        if self.config.dry_run:
            return
        output = self.github.gh(
            [
                "pr",
                "list",
                "--repo",
                self.config.github_repository,
                "--base",
                self.config.integration_branch,
                "--state",
                "all",
                "--limit",
                "1000",
                "--json",
                "url,state,headRefName,headRefOid,isDraft,mergeable,reviewDecision,body",
            ]
        )
        for pull_request in json.loads(output):
            branch = str(pull_request.get("headRefName") or "")
            match = re.fullmatch(
                rf"{re.escape(self.config.branch_prefix)}/([^/]+)/issue-(\d+)", branch
            )
            if not match or bool(pull_request.get("isDraft")):
                continue
            issue_number = int(match.group(2))
            provider = match.group(1).lower()
            if provider == "xai":
                provider = "grok"
            pr_url = str(pull_request.get("url") or "")
            if not pr_url:
                raise WorkerError(f"GitHub returned incomplete pull request data for {branch}")
            pr_state = str(pull_request.get("state") or "").upper()
            if pr_state == "MERGED":
                remote_ref = f"refs/remotes/{self.config.remote_name}/{branch}"
                if self.git_ok("show-ref", "--verify", remote_ref) and self.issue_is_closed(
                    issue_number
                ):
                    self.delete_remote_issue_branch(branch, provider)
                continue
            if pr_state != "OPEN":
                continue
            if CAP_HIT_PR_MARKER in str(pull_request.get("body") or ""):
                log(f"Issue #{issue_number} has an adversarial UAT deadlock; leaving its PR for human adjudication.")
                continue
            if (
                self.config.auto_approve
                and str(pull_request.get("reviewDecision") or "").upper() != "APPROVED"
            ):
                self.approve_pull_request(pr_url, provider)
            if not self.config.auto_approve:
                continue
            if str(pull_request.get("mergeable") or "").upper() == "CONFLICTING":
                log(
                    f"Issue #{issue_number} has merge conflicts on {branch}; "
                    "leaving its pull request open."
                )
                continue
            head_sha = str(pull_request.get("headRefOid") or "")
            if not SHA_RE.fullmatch(head_sha):
                raise WorkerError(f"GitHub returned incomplete pull request data for {branch}")
            merge_sha = self.merge_pull_request(
                pr_url, head_sha, provider, issue_number
            )
            self.delete_remote_issue_branch(branch, provider)
            log(
                f"Approved and squash-merged issue #{issue_number} from {branch} "
                f"into {self.config.integration_branch} as {merge_sha}."
            )
        self.auto_promote_integration_branch()

    def protected_attempt_branches(self) -> set[str]:
        """Branch names an unfinished attempt still needs.

        Covers the active attempt, a completion whose GitHub delivery has not
        finished, and every quota-paused session. Every provider's branch name
        is protected for those issue numbers, not just the saved one: a
        handoff can move an issue to a different provider between two runs, so
        the saved name alone is not the full set of branches that attempt may
        still use."""
        names: set[str] = set()
        paths = [self.in_progress_file, self.pending_file]
        if self.paused_dir.is_dir():
            paths.extend(sorted(self.paused_dir.glob("*.json")))
        for path in paths:
            if not path.exists():
                continue
            state = read_json(path)
            branch = str(state.get("branch_name") or "")
            if branch:
                names.add(branch)
            number = state.get("issue_number")
            if number is None:
                continue
            for key in (*KNOWN_PROVIDER_KEYS, *BRANCH_PROVIDER_KEYS):
                names.add(f"{self.config.branch_prefix}/{key}/issue-{int(number)}")
        return names

    def branches_with_pull_requests(self) -> set[str]:
        """Head branch of every pull request GitHub has ever recorded here."""
        output = self.github.gh(
            [
                "pr", "list", "--repo", self.config.github_repository,
                "--state", "all", "--limit", "1000", "--json", "headRefName",
            ]
        )
        try:
            records = json.loads(output or "null")
        except json.JSONDecodeError as error:
            raise WorkerError(
                "GitHub returned an unreadable pull request list for "
                f"{self.config.github_repository}"
            ) from error
        if not isinstance(records, list):
            raise WorkerError(
                "GitHub returned an unreadable pull request list for "
                f"{self.config.github_repository}"
            )
        return {str(record.get("headRefName") or "") for record in records}

    def terminal_no_code_outcome(self, issue_number: int) -> str:
        """The terminal no-code result recorded for `issue_number`, or `""`.

        Execution history is authoritative whenever it knows this issue;
        otherwise the issue's own authenticated worker comments are read, so
        branches older than the history database can still be reconciled.
        A code completion, an attempt still in flight, and no evidence at all
        all return `""` — the caller then keeps the branch."""
        statuses = self.history.final_statuses(self.config.github_repository, issue_number)
        if statuses:
            newest = statuses[0]
            return newest if newest in NO_CODE_FINAL_STATUSES else ""
        try:
            comments = self.comments(issue_number)
        except (WorkerError, json.JSONDecodeError):
            return ""
        outcome = latest_terminal_outcome(comments, self.completion_authors)
        return outcome if outcome in NO_CODE_TERMINAL_OUTCOMES else ""

    def orphan_branch_blocker(self, branch: str, remote_sha: str, issue_number: int) -> str:
        """Why a pull-request-less issue branch must be kept, or `""`."""
        if branch == self.git("branch", "--show-current"):
            return "it is the branch currently checked out"
        if not self.git_ok("cat-file", "-e", f"{remote_sha}^{{commit}}"):
            return f"its tip {remote_sha[:12]} could not be fetched for inspection"
        merged = any(
            self.git_ok("show-ref", "--verify", ref)
            and self.git_ok("merge-base", "--is-ancestor", remote_sha, ref)
            for ref in (
                f"refs/remotes/{self.config.remote_name}/{self.config.integration_branch}",
                f"refs/remotes/{self.config.remote_name}/{self.config.base_branch}",
            )
        )
        if not merged:
            return (
                f"it carries commits that are not in {self.config.integration_branch} "
                f"or {self.config.base_branch}"
            )
        if not self.terminal_no_code_outcome(issue_number):
            return f"issue #{issue_number} has no confirmed terminal no-code result"
        return ""

    def reconcile_orphan_issue_branches(self) -> None:
        """Remove historical issue branches a terminal no-code result stranded.

        No pull request is ever opened for an environment-only summary, a
        question answer or an input request, so `reconcile_issue_pull_requests`
        never sees those branches and they keep showing as active work forever.
        Absence of a pull request is deliberately *not* sufficient on its own —
        a live attempt is legitimately between branch creation and PR
        publication — so a branch is only removed when it is unprotected by
        saved worker state, has never had a pull request, carries nothing
        beyond the integration/base history, and its issue reached a terminal
        no-code result. Anything unavailable or ambiguous keeps the branch."""
        if self.config.dry_run:
            return
        pattern = self.issue_branch_pattern()
        listing = self.git("ls-remote", "--heads", self.config.remote_name, check=False)
        candidates: list[tuple[str, str, str, int]] = []
        for line in listing.splitlines():
            sha, _, ref = line.partition("\t")
            name = ref.strip().removeprefix("refs/heads/")
            match = pattern.fullmatch(name)
            if not match or not SHA_RE.fullmatch(sha.strip()):
                continue
            provider = "grok" if match.group(1) == "xai" else match.group(1)
            candidates.append((name, sha.strip(), provider, int(match.group(2))))
        if not candidates:
            return
        try:
            protected = self.protected_attempt_branches()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            log(
                "WARNING: skipping orphan issue-branch cleanup; saved worker state "
                f"could not be read: {error}"
            )
            return
        candidates = [item for item in candidates if item[0] not in protected]
        if not candidates:
            return
        try:
            delivered = self.branches_with_pull_requests()
        except WorkerError as error:
            log(f"WARNING: skipping orphan issue-branch cleanup; GitHub was unavailable: {error}")
            return
        candidates = [item for item in candidates if item[0] not in delivered]
        if not candidates:
            return
        self.git("fetch", "--prune", self.config.remote_name, check=False)
        for branch, remote_sha, provider, issue_number in candidates:
            try:
                blocker = self.orphan_branch_blocker(branch, remote_sha, issue_number)
            except WorkerError as error:
                blocker = f"it could not be inspected: {error}"
            if blocker:
                log(f"Kept orphan issue branch {branch} because {blocker}.")
                continue
            try:
                self.delete_remote_issue_branch(branch, provider, label="no-code orphan")
            except WorkerError as error:
                log(f"WARNING: could not remove orphan issue branch {branch}: {error}")
                continue
            if self.git_ok("show-ref", "--verify", f"refs/heads/{branch}"):
                self.git("branch", "-D", branch, check=False)
            log(
                f"Reconciled orphan issue branch {branch}: issue #{issue_number} ended with a "
                "no-code result and the branch held no unique work."
            )

    def archive_closed_paused(self, paused_file: Path) -> Path:
        state = read_json(paused_file)
        self.validate_paused_state(state)
        issue_number = int(state["issue_number"])
        self.closed_paused_dir.mkdir(parents=True, exist_ok=True)
        archive_file = self.closed_paused_dir / f"{issue_number}.json"
        if archive_file.exists():
            suffix = dt.datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
            archive_file = self.closed_paused_dir / f"{issue_number}-{suffix}.json"
        state.update(
            {
                "status": "closed_while_paused",
                "closed_detected_at": iso_timestamp(),
                "archived_from": paused_file.name,
            }
        )
        atomic_write_json(archive_file, state)
        paused_file.unlink()
        log(
            f"Issue #{issue_number} was closed while quota-paused; archived its saved attempt "
            "without running AI and continuing to the next eligible issue."
        )
        return archive_file

    def skip_closed_in_progress_pause(self) -> None:
        state = self.read_state()
        issue_number = int(state["issue_number"])
        self.suspend_paused()
        self.archive_closed_paused(self.paused_dir / f"{issue_number}.json")

    def mark_quota_paused(self) -> None:
        assert self.choice and self.issue
        state = self.read_state()
        if state.get("status") == "quota_paused":
            return
        state.update(
            {
                "status": "quota_paused",
                "quota_paused_at": iso_timestamp(),
                "quota_pause_count": int(state.get("quota_pause_count", 0)) + 1,
                "quota_comment_posted": False,
            }
        )
        self.write_state(state)
        log(
            f"Paused issue #{self.issue.number} because {self.choice.name} usage is unavailable; "
            f"session {self.choice.session_id} was preserved."
        )

    def post_quota_comment(self) -> None:
        assert self.choice and self.issue
        state = self.read_state()
        if state.get("quota_comment_posted"):
            return
        marker = (
            f"<!-- swarm-issue-worker:quota-paused:issue:{self.issue.number};"
            f"session:{self.choice.session_id} -->"
        )
        # One notice per preserved AI session, even if an external supervisor
        # repeatedly invokes the worker while capacity is unavailable. Also
        # recognize the older marker shape containing ``pause:<n>`` so an
        # upgrade does not add another notice to an already-paused issue.
        legacy_or_current_marker = re.compile(
            rf"swarm-issue-worker:quota-paused:issue:{self.issue.number};"
            rf"(?:pause:[0-9]+;)?session:{re.escape(self.choice.session_id)}(?:\s|-->)"
        )
        existing = any(
            legacy_or_current_marker.search(str(comment.get("body") or ""))
            for comment in self.comments(self.issue.number)
        )
        if not existing:
            body = (
                f"{marker}\nWork paused because **{self.choice.name}** no longer has sufficient usage available.\n\n"
                f"- Model: `{self.choice.model}`\n- Session: `{self.choice.session_id}`\n"
                "- The current work and AI session were saved.\n"
                f"- The worker will wait for {self.choice.name} specifically, include new trusted comments, "
                "and resume this same session automatically.\n"
            )
            log(f"Posting the one-time quota pause notice to GitHub issue #{self.issue.number}.")
            self.github.gh(
                ["issue", "comment", str(self.issue.number), "--repo", self.config.github_repository, "--body-file", "-"],
                self.choice.key,
                body,
            )
        self.update_state(quota_comment_posted=True)

    def started_comment_marker(self) -> str:
        assert self.issue and self.choice
        return (
            f"<!-- swarm-issue-worker:started:issue:{self.issue.number};"
            f"provider:{self.choice.key};branch:{self.expected_branch()} -->"
        )

    def post_started_comment(self) -> None:
        assert self.issue and self.choice
        state = self.read_state()
        if state.get("started_comment_posted"):
            return
        marker = self.started_comment_marker()
        already_posted = any(
            marker in str(comment.get("body") or "")
            for comment in self.comments(self.issue.number)
        )
        usage_at_start: dict[str, Any] | None = None
        if not already_posted:
            action = "started follow-up work on" if self.issue.work_type == "followup" else "started working on"
            if self.start_usage is not None:
                usage_at_start = self.snapshot_from_usage(self.start_usage)
            else:
                usage_at_start = self.usage_snapshot(self.choice.key)
            body = (
                f"{marker}\n🤖 **{self.choice.name} Bot** {action} this issue.\n\n"
                f"- Model: `{self.choice.model}`\n"
                f"- Branch: `{self.expected_branch()}`\n"
                f"- {self.choice.name} usage remaining: {self.format_usage_snapshot(usage_at_start)}\n"
            )
            if self.routing:
                body += "\n" + format_routing_notice(self.routing) + "\n"
            self.github.gh(
                [
                    "issue",
                    "comment",
                    str(self.issue.number),
                    "--repo",
                    self.config.github_repository,
                    "--body-file",
                    "-",
                ],
                self.choice.key,
                body,
            )
            log(f"Posted {self.choice.name} Bot start notice to issue #{self.issue.number}.")
        else:
            log(f"Issue #{self.issue.number} already has this work-round start notice.")
        self.update_state(started_comment_posted=True, usage_at_start=usage_at_start)

    def resumed_comment_marker(self, resume_token: str) -> str:
        assert self.issue and self.choice
        return (
            f"<!-- swarm-issue-worker:resumed:issue:{self.issue.number};"
            f"provider:{self.choice.key};session:{self.choice.session_id};at:{resume_token} -->"
        )

    def post_resumed_comment(self) -> None:
        """Announce that a preserved session is being picked back up.

        The start notice (post_started_comment) only fires on the first round
        of work; when the worker later reclaims a quota-paused attempt —
        either with the same provider or a hand-off provider continuing on the
        existing branch — nothing on the issue shows that work has restarted.
        This posts that notice once per resume, keyed on ``quota_resumed_at``
        so repeated scheduler ticks within the same resumed run do not repeat
        it, and calls out any trusted comments left while the work was paused.
        """
        assert self.issue and self.choice
        if not self.quota_resume_ready:
            return
        state = self.read_state()
        resume_token = str(state.get("quota_resumed_at") or "")
        if not resume_token or state.get("resumed_comment_token") == resume_token:
            return
        marker = self.resumed_comment_marker(resume_token)
        comments = self.comments(self.issue.number)
        already_posted = any(
            marker in str(comment.get("body") or "") for comment in comments
        )
        if not already_posted:
            new_comments = self.load_resume_comments(
                self.issue.number, int(state.get("session_comment_id", 0)), comments
            )
            usage = self.usage_snapshot(self.choice.key)
            lines = [
                marker,
                f"🤖 **{self.choice.name} Bot** is resuming work on this issue.",
                "",
                f"- Model: `{self.choice.model}`",
                f"- Branch: `{self.expected_branch()}`",
                f"- Session: `{self.choice.session_id}`",
                f"- {self.choice.name} usage remaining: {self.format_usage_snapshot(usage)}",
            ]
            if new_comments:
                count = len(new_comments)
                noun = "comment" if count == 1 else "comments"
                lines.append(
                    f"- Picking up {count} new trusted {noun} left while the work was paused."
                )
            body = "\n".join(lines) + "\n"
            self.github.gh(
                [
                    "issue",
                    "comment",
                    str(self.issue.number),
                    "--repo",
                    self.config.github_repository,
                    "--body-file",
                    "-",
                ],
                self.choice.key,
                body,
            )
            log(f"Posted {self.choice.name} Bot resume notice to issue #{self.issue.number}.")
        else:
            log(f"Issue #{self.issue.number} already has this resume notice.")
        self.update_state(resumed_comment_token=resume_token)

    def ai_failure_is_quota(self) -> bool:
        combined = ""
        for path in (self.ai_output_file, self.ai_diagnostic_file):
            if path.exists():
                combined += path.read_text(encoding="utf-8", errors="replace")
        if QUOTA_RE.search(combined):
            return True
        assert self.choice
        return self.provider_capacity(self.choice.name) == 1

    def load_resume_comments(
        self,
        issue_number: int,
        after_id: int,
        comments: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        source = comments if comments is not None else self.comments(issue_number)
        return [
            {
                "id": int(comment["id"]),
                "author": str((comment.get("user") or {}).get("login") or "unknown"),
                "created_at": str(comment.get("created_at") or ""),
                "body": str(comment.get("body") or ""),
            }
            for comment in sorted(source, key=lambda item: int(item["id"]))
            if int(comment["id"]) > after_id
            and not is_worker_comment(comment)
            and author_matches(
                str((comment.get("user") or {}).get("login") or ""), self.trusted_followup_authors
            )
        ]

    def save_new_state(self, issue: IssueContext, choice: ProviderChoice, base_sha: str) -> None:
        state = {
            "issue_number": issue.number,
            "issue_title": issue.title,
            "issue_url": issue.url,
            "base_sha": base_sha,
            "branch_name": self.expected_branch(),
            "work_type": issue.work_type,
            "previous_commit_sha": issue.previous_commit_sha,
            "previous_ai": issue.previous_ai,
            "previous_completion_comment": issue.previous_completion_comment,
            "followup_comments": issue.followup_comments,
            "trigger_comment_id": issue.trigger_comment_id,
            "ai_tool": choice.name,
            "model": choice.model,
            "effort": choice.effort,
            "session_id": choice.session_id,
            "session_comment_id": issue.trigger_comment_id or 0,
            "status": "active",
            "quota_pause_count": 0,
            "started_at": iso_timestamp(),
            "execution_id": self.history.execution_id,
        }
        if self.routing:
            state["routing_decision"] = self.routing
        self.write_state(state)

    def maybe_apply_dynamic_routing(self) -> None:
        """Grade a new attempt and apply the configured complexity tier.

        Resumed sessions keep the model they already started with. A retry of
        an attempt that already recorded a decision reuses it, but only when
        that decision's model is still one the current configuration would
        actually produce (see `_stored_decision_still_valid`) — otherwise the
        attempt never really got underway (a fallback pick, or a tier the
        model catalog has since dropped) and re-routing is safe. The original
        issue text is not modified.
        """
        assert self.choice and self.issue
        if not self.config.dynamic_model_routing or self.choice.resume:
            self.routing = None
            return
        if self.in_progress_file.exists():
            state = self.read_state()
            stored = state.get("routing_decision")
            if (
                isinstance(stored, dict)
                and stored.get("provider") == self.choice.key
                and stored.get("selected_model")
            ):
                if self._stored_decision_still_valid(stored):
                    self.choice.model = str(state.get("model") or stored["selected_model"])
                    self.choice.effort = str(
                        state.get("effort") or stored.get("reasoning_effort") or self.choice.effort
                    )
                    self.routing = stored
                    log("Reusing the routing decision already recorded for this attempt.")
                    return
                log(
                    f"Stored routing decision selected {stored['selected_model']!r}, which the "
                    "current configuration no longer offers (it was likely repaired away, e.g. a "
                    "retired model or one needing usage credits the toggle now excludes); "
                    "re-routing instead of reusing it."
                )
        self.apply_dynamic_routing()

    def _stored_decision_still_valid(self, stored: dict[str, Any]) -> bool:
        """Whether a saved routing decision's model is still reachable from
        the current configuration, so it is safe to keep resuming this
        attempt with it rather than re-routing.

        Config can change between a worker's cycles — a model retired, or a
        saved selection self-healed away from one that turned out to need
        usage credits the account doesn't have (see `allow_usage_credit_models`
        in `src/config.rs`) — and an in-progress attempt must not keep
        replaying a now-stale pick just because it is still on disk.
        """
        assert self.choice
        model = str(stored.get("selected_model") or "").strip()
        if not model:
            return False
        host = self.config.spec(self.choice.key)
        if host is not None and model == host.model:
            return True
        if any(tier.model == model for tier in self.config.routing_tiers.get(self.choice.key, ())):
            return True
        # The router picks from the model catalog, not only from the tiers, so
        # a catalog model this configuration still offers is just as valid.
        return model in catalog_model_names(
            (self.choice.key,),
            allow_usage_credit_models=self.config.allow_usage_credit_models,
        )

    def ensure_bot_auth(self) -> None:
        """Fail early when the chosen provider cannot act as its GitHub App.

        Called again after dynamic routing hands the issue to a different AI
        tool, so the replacement is verified the same way the original was.
        """
        assert self.choice
        if not self.config.require_bot_auth:
            return
        if not self.apps.configured(self.choice.key):
            raise WorkerError(
                f"Bot auth is required, but {self.choice.name} has no entry in {self.config.github_apps_config}"
            )
        # Resolve the installation for this repository's owner now, so a
        # missing install fails here with a clear message instead of an
        # opaque GraphQL permissions error partway through branch setup.
        try:
            self.apps.verify_installation(self.choice.key)
        except RuntimeError as error:
            log(
                f"ERROR: GitHub App for {self.choice.name} cannot act on "
                f"{self.config.github_repository}: {error}"
            )
            raise WorkerError(str(error)) from error

    def routing_candidates(
        self, excluded_models: set[tuple[str, str]] | None = None
    ) -> list[RouterCandidate]:
        """Enabled AI tools the router may hand this issue to, best first.

        Only tools with capacity this pass are offered, so a routing decision
        can always be honoured. Once a branch is already owned (an in-progress
        attempt) the tool is fixed and routing may still choose the model and
        effort, but not a different tool.
        """
        assert self.choice
        if self.in_progress_file.exists():
            keys = [self.choice.key]
        else:
            keys = [name.lower() for name in self.provider_priority]
            if not keys:
                keys = [spec.key for spec in self.config.enabled_specs]
            if self.choice.key not in keys:
                keys.insert(0, self.choice.key)
        excluded = excluded_models or set()
        candidates: list[RouterCandidate] = []
        for key in keys:
            spec = self.config.spec(key)
            tiers: list[RoutingTier] = []
            for tier in self.config.routing_tiers.get(key, ()):
                if (key, tier.model) not in excluded:
                    tiers.append(tier)
                elif spec is not None and (key, spec.model) not in excluded:
                    # Keep every complexity band covered, but substitute the
                    # validated configured model for the one just rejected.
                    tiers.append(
                        RoutingTier(
                            tier.min_complexity,
                            tier.max_complexity,
                            spec.model,
                            spec.effort,
                        )
                    )
            if spec is None or not tiers:
                continue
            usage = self.provider_usages.get(spec.name)
            candidates.append(
                RouterCandidate(
                    key=spec.key,
                    name=spec.name,
                    tiers=tuple(tiers),
                    strengths=spec.strengths,
                    usage_remaining=usage.remaining_percent if usage else None,
                    excluded_models=tuple(
                        model for provider, model in sorted(excluded) if provider == key
                    ),
                )
            )
        return candidates

    def apply_dynamic_routing(
        self, excluded_models: set[tuple[str, str]] | None = None
    ) -> None:
        assert self.choice and self.issue
        host = self.config.require_spec(self.choice.key)
        fallback_model = self.choice.model
        fallback_effort = self.choice.effort
        original_body = self.issue.body
        candidates = self.routing_candidates(excluded_models)
        previous_provider = (self.issue.previous_ai or "").lower()
        rework = self.issue.work_type == "followup"
        try:
            images = self.ensure_issue_images()
            prompt = build_router_prompt(
                title=self.issue.title,
                body=original_body,
                labels=list(self.issue.labels),
                candidates=candidates,
                previous_provider=previous_provider,
                rework=rework,
                image_count=len(images),
                comment_image_count=sum(
                    1 for image in images if not image.source.startswith("issue description")
                ),
                routing_optimization=self.config.routing_optimization,
                allow_usage_credit_models=self.config.allow_usage_credit_models,
            )
            raw = self.run_router(host, prompt, images)
            decision = self.resolve_router_response(
                raw,
                prompt=prompt,
                candidates=candidates,
                host=host,
                images=images,
                previous_provider=previous_provider,
                rework=rework,
            )
        except RouterError as error:
            log(
                f"Dynamic routing failed ({error}); using configured "
                f"{fallback_model} / {fallback_effort}."
            )
            self.routing = fallback_routing_decision(
                provider=host.key,
                provider_name=host.name,
                model=fallback_model,
                effort=fallback_effort,
                reason=str(error),
                router_model=host.router_model,
                router_effort=host.router_effort,
                router_provider=host.key,
                candidates=[candidate.key for candidate in candidates],
                routing_optimization=self.config.routing_optimization,
            )
        else:
            self.adopt_routing_decision(decision)
        self.issue.body = original_body
        if self.in_progress_file.exists():
            self.update_state(
                model=self.choice.model,
                effort=self.choice.effort,
                routing_decision=self.routing,
            )

    def run_router(self, host: "ProviderSpec", prompt: str, images: Sequence[Any]) -> str:
        return run_provider_router(
            provider=host.key,
            bin_path=host.bin or "",
            model=host.router_model,
            effort=host.router_effort,
            prompt=prompt,
            cwd=self.config.repo_dir,
            images=tuple(image.path for image in images),
        )

    def resolve_router_response(
        self,
        raw: str,
        *,
        prompt: str,
        candidates: Sequence[RouterCandidate],
        host: "ProviderSpec",
        images: Sequence[Any],
        previous_provider: str,
        rework: bool,
    ) -> dict[str, Any]:
        """Validate the router response, correcting one invalid model choice.

        The router names the worker model itself now, so it can name one that
        does not exist. That earns exactly one corrective follow-up call with
        the full catalog restated — never a silent substitution. A second
        invalid answer (or a failed retry) resolves the first response through
        the configured complexity tiers instead, so a persistently wrong router
        degrades to the old deterministic behavior rather than stalling the
        issue.
        """

        def resolve(payload: Any, *, allow_tier_fallback: bool) -> dict[str, Any]:
            return resolve_routing_decision(
                payload,
                candidates,
                default_provider=host.key,
                router_provider=host.key,
                router_model=host.router_model,
                router_effort=host.router_effort,
                previous_provider=previous_provider,
                rework=rework,
                routing_optimization=self.config.routing_optimization,
                allow_usage_credit_models=self.config.allow_usage_credit_models,
                allow_tier_fallback=allow_tier_fallback,
            )

        try:
            return resolve(raw, allow_tier_fallback=False)
        except InvalidRouterModel as invalid:
            log(
                f"Dynamic routing: the router named {invalid.model or '(no model)'}, which is not a "
                "model this app can run; asking it once more with the full catalog."
            )
            correction = build_model_correction_prompt(
                prompt,
                named_model=invalid.model,
                candidates=candidates,
                allow_usage_credit_models=self.config.allow_usage_credit_models,
            )
            try:
                retry = self.run_router(host, correction, images)
                return resolve(retry, allow_tier_fallback=True)
            except RouterError as error:
                log(
                    "Dynamic routing: the corrective router call did not produce a usable model "
                    f"({error}); using the configured complexity tier instead."
                )
            return resolve(invalid.payload, allow_tier_fallback=True)

    def adopt_routing_decision(self, decision: dict[str, Any]) -> None:
        """Apply a validated decision: the AI tool first, then model and effort."""
        assert self.choice
        selected = str(decision.get("provider") or self.choice.key)
        if selected != self.choice.key:
            spec = self.config.require_spec(selected)
            reason = str(decision.get("provider_reason") or "").strip()
            log(
                f"Dynamic routing hands this issue to {spec.name} instead of {self.choice.name}"
                + (f": {reason}" if reason else ".")
            )
            self.choice = ProviderChoice(
                name=spec.name,
                model=spec.model,
                effort=spec.effort,
                session_id=self.new_session_id(spec),
            )
            self.start_usage = self.provider_usages.get(spec.name)
            self.ensure_bot_auth()
        override = str(decision.get("provider_override_reason") or "").strip()
        if override:
            log(override)
        self.choice.model = str(decision["selected_model"])
        self.choice.effort = str(decision["reasoning_effort"])
        self.routing = decision
        log(
            f"Dynamic routing selected {self.choice.name} {self.choice.model} with effort "
            f"{self.choice.effort} (grade {decision['prompt_grade']}, complexity "
            f"{decision['complexity']}/10, confidence "
            f"{int(round(float(decision.get('confidence') or 0) * 100))}%)."
        )

    def start_execution_history(self) -> None:
        assert self.issue and self.choice
        persisted_branch = ""
        if self.in_progress_file.exists():
            persisted_branch = str(self.read_state().get("branch_name") or "")
        branch = persisted_branch or (
            f"{self.config.branch_prefix}/{ai_tool_key(self.choice.key)}/issue-{self.issue.number}"
        )
        execution_id = self.history.start(
            ExecutionStart(
                repository=self.config.github_repository,
                issue_number=self.issue.number,
                issue_url=self.issue.url,
                issue_title=self.issue.title,
                issue_body=self.issue.body,
                provider=self.choice.name,
                model=self.choice.model,
                effort=self.choice.effort,
                branch_name=branch,
                application_version=self.config.application_version,
                prompt_template_version=PROMPT_TEMPLATE_VERSION,
                routing_decision=self.routing,
            ),
            iso_timestamp(),
            existing_id=(str(self.read_state().get("execution_id") or "")
                         if self.in_progress_file.exists() and self.read_state().get("adversarial") else ""),
        )
        if execution_id and self.in_progress_file.exists():
            self.update_state(execution_id=execution_id)

    @staticmethod
    def summary_section(output: str, heading: str) -> str:
        match = re.search(
            rf"(?ims)^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)", output
        )
        return match.group(1).strip() if match else ""

    def finish_execution_history(
        self,
        status: str,
        output: str = "",
        *,
        commit_shas: Sequence[str] = (),
        files_changed: Sequence[str] = (),
        pull_request_url: str = "",
    ) -> None:
        if not self.history.execution_id:
            return
        completed = iso_timestamp()
        started_text = (
            str(self.read_state().get("started_at") or completed)
            if self.in_progress_file.exists()
            else completed
        )
        try:
            started = dt.datetime.fromisoformat(started_text)
            ended = dt.datetime.fromisoformat(completed)
            duration = max(0.0, (ended - started).total_seconds())
        except ValueError:
            duration = None
        pr_match = re.search(r"/pull/(\d+)(?:$|[/?#])", pull_request_url)
        fields: dict[str, Any] = {
            "completed_at": completed,
            "duration_seconds": duration,
            "files_changed": files_changed,
            "commit_shas": commit_shas,
            "pull_request_number": int(pr_match.group(1)) if pr_match else None,
            "pull_request_url": pull_request_url,
            "final_status": status,
        }
        if output:
            fields.update(
                requested_work_summary=self.summary_section(output, "Summary"),
                changes_summary=self.summary_section(output, "Changes"),
            )
        self.history.update(completed, **fields)

    def issue_from_state(self, state: dict[str, Any], remote_issue: dict[str, Any]) -> IssueContext:
        return IssueContext(
            number=int(state["issue_number"]),
            title=str(remote_issue["title"]),
            body=str(remote_issue.get("body") or ""),
            labels=issue_labels(remote_issue),
            url=str(remote_issue["html_url"]),
            work_type=str(state.get("work_type") or "initial"),
            previous_commit_sha=str(state.get("previous_commit_sha") or ""),
            previous_ai=str(state.get("previous_ai") or ""),
            previous_completion_comment=state.get("previous_completion_comment"),
            followup_comments=list(state.get("followup_comments") or []),
            trigger_comment_id=state.get("trigger_comment_id"),
        )

    def assigned_issues(self) -> list[dict[str, Any]]:
        issues = self.github.api_list(
            f"repos/{self.config.github_repository}/issues",
            {
                "state": "open",
                "assignee": self.config.github_assignee,
                "sort": "created",
                "direction": "asc",
                "per_page": 100,
            },
        )
        return sorted(
            [
                issue
                for issue in issues
                if "pull_request" not in issue
                and any(
                    assignee.get("login") == self.config.github_assignee
                    for assignee in issue.get("assignees", [])
                )
            ],
            key=lambda issue: int(issue["number"]),
        )

    def record_completed_from_comments(self, issue_number: int, comments: list[dict[str, Any]]) -> bool:
        waiting = extract_needs_input_metadata(comments, self.completion_authors)
        answered = extract_question_answer_metadata(comments, self.completion_authors)
        metadata = extract_completion_metadata(comments, self.completion_authors)
        newest_code_id = int(metadata["comment_id"]) if metadata else -1
        newest_no_code = max(
            (item for item in (waiting, answered) if item),
            key=lambda item: int(item["comment_id"]),
            default=None,
        )
        if newest_no_code and int(newest_no_code["comment_id"]) > newest_code_id:
            self.record_completed(issue_number)
            self.clear_in_progress(issue_number)
            if newest_no_code is waiting:
                log(f"Issue #{issue_number} is waiting for trusted user input.")
            else:
                log(f"Question issue #{issue_number} has an authenticated AI answer.")
            return True
        if not metadata:
            return False
        commit_sha = str(metadata["commit_sha"])
        # A completion marker alone means an AI opened/updated the issue PR
        # against the integration branch. The issue must be closed before that
        # PR can be merged. Only treat the issue as fully landed once the commit
        # is reachable from the integration branch.
        landed = ""
        for ref in (
            f"refs/remotes/{self.config.remote_name}/{self.config.integration_branch}",
            f"refs/heads/{self.config.integration_branch}",
        ):
            resolved = self.git("rev-parse", "--verify", ref, check=False)
            if resolved:
                landed = resolved
                break
        if (
            not landed
            or not self.git_ok("cat-file", "-e", f"{commit_sha}^{{commit}}")
            or not self.git_ok("merge-base", "--is-ancestor", commit_sha, landed)
        ):
            return False
        self.record_completed(issue_number)
        self.clear_in_progress(issue_number)
        log(
            f"Issue #{issue_number} is done — {commit_sha} has landed on "
            f"{self.config.integration_branch}."
        )
        return True

    def select_issue(self) -> IssueContext | None:
        # A resumable in-progress issue always wins; otherwise the next issue is
        # the highest-priority ready one (see ``priority_rank``), breaking ties by
        # lowest issue number so equal-priority work keeps first-opened order.
        issues = self.assigned_issues()
        completed = self.completed_numbers()
        paused = {int(path.stem) for path in self.paused_files()}
        in_progress_number: int | None = None
        saved_state: dict[str, Any] | None = None
        if self.in_progress_file.exists():
            saved_state = self.read_state()
            in_progress_number = int(saved_state["issue_number"])
            if str(saved_state.get("work_type") or "initial") == "initial" and in_progress_number in completed:
                self.clear_in_progress(in_progress_number)
                saved_state = None
                in_progress_number = None

        for candidate in issues:
            number = int(candidate["number"])
            if number in completed or number in paused:
                continue
            comments = self.comments(number)
            if self.record_completed_from_comments(number, comments):
                completed.add(number)
                if in_progress_number == number:
                    in_progress_number = None
                    saved_state = None

        if in_progress_number is not None:
            remote = next((item for item in issues if int(item["number"]) == in_progress_number), None)
            if remote is None:
                raise WorkerError(
                    f"Saved in-progress issue #{in_progress_number} is no longer open and assigned to "
                    f"{self.config.github_assignee}; review {self.in_progress_file}."
                )
            assert saved_state is not None
            return self.issue_from_state(saved_state, remote)

        ready: list[tuple[int, str, dict[str, Any], dict[str, Any] | None]] = []
        for candidate in issues:
            number = int(candidate["number"])
            if number in paused:
                continue
            if number not in completed:
                ready.append((number, "initial", candidate, None))
                continue
            metadata = extract_followup_metadata(
                self.comments(number), self.trusted_followup_authors, self.completion_authors
            )
            if not metadata:
                continue
            previous_commit = str(metadata["previous_commit_sha"])
            if previous_commit and not SHA_RE.fullmatch(previous_commit):
                raise WorkerError(
                    f"Latest completion on issue #{number} has no valid completion commit"
                )
            ready.append((number, "followup", candidate, metadata))

        if ready:
            # Honor issue priority first (Urgent > High > Medium > Low; no
            # priority label counts as Low), then fall back to lowest issue
            # number so ties stay in the historical first-opened order.
            _, work_type, remote, metadata = min(
                ready,
                key=lambda item: (priority_rank(issue_labels(item[2])), item[0]),
            )
            if work_type == "initial":
                return IssueContext(
                    number=int(remote["number"]),
                    title=str(remote["title"]),
                    body=str(remote.get("body") or ""),
                    labels=issue_labels(remote),
                    url=str(remote["html_url"]),
                )
            assert metadata is not None
            return IssueContext(
                number=int(remote["number"]),
                title=str(remote["title"]),
                body=str(remote.get("body") or ""),
                labels=issue_labels(remote),
                url=str(remote["html_url"]),
                work_type="followup",
                previous_commit_sha=str(metadata["previous_commit_sha"]),
                previous_ai=str(metadata["previous_ai"]),
                previous_completion_comment=metadata["previous_completion_comment"],
                followup_comments=metadata["followup_comments"],
                trigger_comment_id=int(metadata["trigger_comment_id"]),
            )
        if paused:
            log("No other issue can be worked now; quota-paused issues remain safely shelved.")
        else:
            log(f"No new issue or follow-up comment assigned to {self.config.github_assignee} was found.")
        return None

    def choice_from_state(self, state: dict[str, Any]) -> ProviderChoice:
        # `session_id` for Claude is generated and persisted (in
        # save_new_state, via prepare_repository) before the `claude`
        # process is ever actually invoked in run_ai — so its mere presence
        # doesn't mean a resumable session exists yet. `session_started` is
        # only set (in run_ai) once that first invocation genuinely happens;
        # gating resume on both prevents a crash between those two points
        # (e.g. post_started_comment failing) from producing a retry that
        # tries to --resume a session ID that was never actually started.
        return ProviderChoice(
            name=str(state["ai_tool"]),
            model=str(state["model"]),
            effort=str(state["effort"]),
            session_id=str(state.get("session_id") or ""),
            resume=bool(state.get("session_id")) and bool(state.get("session_started")),
        )

    def prepare_paused_resume(self) -> bool:
        if self.in_progress_file.exists():
            state = self.read_state()
            if state.get("status") == "quota_paused":
                self.validate_paused_state(state)
                issue_number = int(state["issue_number"])
                if self.issue_is_closed(issue_number):
                    if self.config.dry_run:
                        log(
                            f"Dry run: issue #{issue_number} is closed; would archive its quota-paused "
                            "attempt without running AI."
                        )
                        return True
                    self.skip_closed_in_progress_pause()
                else:
                    self.issue = IssueContext(
                        int(state["issue_number"]), str(state["issue_title"]), "", [], str(state["issue_url"])
                    )
                    self.choice = self.choice_from_state(state)
                    capacity = self.provider_capacity(self.choice.name)
                    if capacity != 0:
                        handoff = self.choose_handoff_provider(self.choice, "usage is unavailable")
                        if handoff:
                            if self.config.dry_run:
                                log(
                                    f"Dry run: would let {handoff.name} continue quota-paused "
                                    f"issue #{self.issue.number} on the existing branch."
                                )
                                return True
                            self.choice = handoff
                            self.update_state(status="active", quota_resumed_at=iso_timestamp())
                            self.update_state_for_choice(handoff)
                            self.quota_resume_ready = True
                            return False
                        if self.config.dry_run:
                            log(f"Dry run: issue #{self.issue.number} remains quota-paused on {self.choice.name}.")
                            return True
                        self.post_quota_comment()
                        self.suspend_paused()
                        raise SystemExit(QUOTA_PAUSED_EXIT_CODE)
                    if self.config.dry_run:
                        log(
                            f"Dry run: would resume {self.choice.name} session {self.choice.session_id} "
                            f"for issue #{self.issue.number}."
                        )
                        return True
                    self.update_state(status="active", quota_resumed_at=iso_timestamp())
                    self.choice.resume = True
                    self.quota_resume_ready = True
                    log(
                        f"{self.choice.name} usage is available again; preparing to resume session "
                        f"{self.choice.session_id} for issue #{self.issue.number}."
                    )
                    return False

        if not self.in_progress_file.exists():
            for paused_file in self.paused_files():
                state = read_json(paused_file)
                issue_number = int(state["issue_number"])
                if self.issue_is_closed(issue_number):
                    if self.config.dry_run:
                        log(
                            f"Dry run: issue #{issue_number} is closed; would archive its quota-paused "
                            "attempt without running AI."
                        )
                        continue
                    self.archive_closed_paused(paused_file)
                    continue
                provider = str(state["ai_tool"])
                if self.provider_capacity(provider) != 0:
                    self.issue = IssueContext(
                        int(state["issue_number"]), str(state["issue_title"]), "", [], str(state["issue_url"])
                    )
                    pinned_choice = self.choice_from_state(state)
                    handoff = self.choose_handoff_provider(pinned_choice, "usage is unavailable")
                    if not handoff:
                        continue
                    if self.config.dry_run:
                        log(
                            f"Dry run: would restore quota-paused issue #{state['issue_number']} "
                            f"and let {handoff.name} continue on the existing branch."
                        )
                        return True
                    self.restore_paused(paused_file)
                    self.update_state_for_choice(handoff)
                    restored = self.read_state()
                    self.choice = self.choice_from_state(restored)
                    self.quota_resume_ready = True
                    log(
                        f"{handoff.name} restored quota-paused issue #{restored['issue_number']} "
                        "on the existing branch."
                    )
                    break
                if self.config.dry_run:
                    log(
                        f"Dry run: would restore quota-paused issue #{state['issue_number']} "
                        f"with its pinned {provider} session."
                    )
                    return True
                self.restore_paused(paused_file)
                restored = self.read_state()
                self.choice = self.choice_from_state(restored)
                self.choice.resume = True
                self.quota_resume_ready = True
                log(
                    f"{provider} usage is available again; restored session {self.choice.session_id} "
                    f"for issue #{restored['issue_number']}."
                )
                break
        return False

    def render_pending_comment(self, pending: dict[str, Any]) -> str:
        commit_sha = str(pending["commit_sha"])
        marker = f"<!-- swarm-issue-worker:commit:{commit_sha}"
        trigger = pending.get("trigger_comment_id")
        if isinstance(trigger, int) and trigger > 0:
            marker += f";through-comment:{trigger}"
        marker += " -->"
        verb = "Reworked" if pending.get("work_type") == "followup" else "Completed"
        provider = str(pending.get("ai_tool") or pending.get("ai") or "the AI")
        branch_line = ""
        if pending.get("branch_name"):
            pr = f" → {pending['pull_request_url']}" if pending.get("pull_request_url") else ""
            branch_line = f"- Branch: `{pending['branch_name']}`{pr}\n"
        usage_lines = self.render_usage_report(
            provider, pending.get("usage_at_start"), pending.get("usage_at_completion")
        )
        return (
            f"{marker}\n{verb} by **{pending.get('ai_tool') or pending.get('ai')}**.\n\n"
            f"- Model: `{pending.get('model', 'unknown')}`\n"
            f"- Effort: `{pending.get('effort', 'unknown')}`\n"
            f"{branch_line}"
            f"- Commit: `{commit_sha}` — {pending['commit_message']}\n"
            f"{usage_lines}\n"
            f"{pending.get('adversarial_summary', '')}"
            "<details><summary>AI completion summary</summary>\n\n"
            f"{pending.get('ai_output') or '(No captured AI output was available.)'}\n"
            "</details>\n"
        )

    def post_pending_comment(self, pending: dict[str, Any]) -> dict[str, Any]:
        if pending.get("github_comment_posted"):
            return pending
        issue_number = int(pending["issue_number"])
        body = self.render_pending_comment(pending)
        marker = body.splitlines()[0]
        already_posted = any(marker in str(comment.get("body") or "") for comment in self.comments(issue_number))
        provider = str(pending.get("ai_tool") or pending.get("ai") or "").lower()
        if not already_posted:
            log(f"Posting the AI completion summary to GitHub issue #{issue_number}.")
            self.github.gh(
                ["issue", "comment", str(issue_number), "--repo", self.config.github_repository, "--body-file", "-"],
                provider,
                body,
            )
        else:
            log(f"GitHub issue #{issue_number} already has the completion response for {pending['commit_sha']}.")
        pending["github_comment_posted"] = True
        atomic_write_json(self.pending_file, pending)
        return pending

    def add_pending_label(self, pending: dict[str, Any]) -> dict[str, Any]:
        if pending.get("ready_for_testing_label_added"):
            return pending
        provider = str(pending.get("ai_tool") or pending.get("ai") or "").lower()
        issue_number = int(pending["issue_number"])
        log(f"Adding the '{self.config.ready_label}' label to GitHub issue #{issue_number}.")
        edit_arguments = [
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            self.config.github_repository,
            "--add-label",
            self.config.ready_label,
        ]
        try:
            self.github.gh(edit_arguments, provider)
        except WorkerError as error:
            if "not found" not in str(error).lower():
                raise
            log(
                f"The '{self.config.ready_label}' label does not exist; creating it before retrying."
            )
            self.github.gh(
                [
                    "label",
                    "create",
                    self.config.ready_label,
                    "--repo",
                    self.config.github_repository,
                    "--color",
                    "0E8A16",
                    "--description",
                    "AI work is ready for human testing",
                ],
                provider,
            )
            self.github.gh(edit_arguments, provider)
        pending["ready_for_testing_label_added"] = True
        atomic_write_json(self.pending_file, pending)
        return pending

    def deliver_pending(self) -> None:
        if not self.pending_file.exists():
            return
        if self.config.dry_run:
            log("Dry run: pending GitHub delivery exists; no delivery or AI work was performed.")
            raise SystemExit(0)
        pending = read_json(self.pending_file)
        pending = self.post_pending_comment(pending)
        pending = self.add_pending_label(pending)
        issue_number = int(pending["issue_number"])
        self.record_completed(issue_number)
        execution_id = str(pending.get("execution_id") or "")
        if execution_id and self.history.repository:
            self.history.execution_id = execution_id
            self.finish_execution_history(
                "completed",
                str(pending.get("ai_output") or ""),
                commit_shas=list(pending.get("commit_shas") or [pending["commit_sha"]]),
                files_changed=list(pending.get("files_changed") or []),
                pull_request_url=str(pending.get("pull_request_url") or ""),
            )
        self.pending_file.unlink()
        self.clear_in_progress(issue_number)
        log(f"GitHub delivery finished; issue #{issue_number} is marked completed locally.")

    def build_prompt(
        self,
        recovery_mode: bool,
        recovery_candidate: str,
        recovery_dirty: bool,
    ) -> str:
        assert self.issue and self.choice
        issue = self.issue
        question_issue = QUESTION_LABEL.lower() in {label.lower() for label in issue.labels}
        state = self.read_state()
        lines: list[str] = []
        extra_image_sources: list[tuple[str, str]] = []
        if self.choice.resume:
            comments = self.load_resume_comments(issue.number, int(state.get("session_comment_id", 0)))
            continuation = (
                f"Continue answering the question with read-only inspection on {self.expected_branch()}. "
                "Do not modify the repository, commit, or push."
                if question_issue else
                "Inspect and preserve work already present in the repository, finish the implementation "
                f"and foreground verification on {self.expected_branch()}. Do not switch branches, push, or "
                "ask for interactive input. The worker will commit any completed changes you leave uncommitted."
            )
            lines.extend(
                [
                    f"Continue the existing unattended session for GitHub issue #{issue.number} "
                    f"({issue.title}) from exactly where the previous turn stopped when usage became unavailable.",
                    continuation,
                ]
            )
            if comments:
                lines.append("\nTrusted GitHub comments added since this session last received issue context:")
                for comment in comments:
                    lines.append(
                        f"Comment #{comment['id']} by @{comment['author']} at {comment['created_at']}:\n"
                        f"{comment['body']}\n"
                    )
                lines.append("Treat these comments as additional requirements or corrections for the work you are continuing.")
                log(f"Adding trusted issue comments through comment {comments[-1]['id']} to the resumed session.")
                self.update_state(session_comment_id=int(comments[-1]["id"]))
                extra_image_sources.extend(
                    (f"comment #{comment['id']}", str(comment.get("body") or "")) for comment in comments
                )
        else:
            task_instruction = (
                f"Answer this issue's question using evidence from {self.config.repo_dir}. Follow repository "
                f"instructions and remain on {self.expected_branch()}. Use read-only inspection as needed, but "
                "do not modify the repository, commit, or push."
                if question_issue
                else
                f"Implement this issue in {self.config.repo_dir}. Follow repository instructions, run relevant "
                f"tests, and remain on {self.expected_branch()} (branched from "
                f"{self.config.integration_branch}; the pull request targets "
                f"{self.config.integration_branch}, never {self.config.base_branch}). You may commit, but do "
                f"not push. Prefix every commit subject with `[{ai_tool_key(self.choice.key)}]` and include #{issue.number} "
                f"(e.g. `[claude] Fix the parser (#42)`). The worker will commit anything you leave "
                "uncommitted. Run verification commands in the foreground; do not return while tests or "
                "builds are still running."
            )
            lines.extend(
                [
                    "Issue title:",
                    issue.title,
                    "Issue number:",
                    f"#{issue.number}",
                    "",
                    "Issue description:",
                    issue.body,
                    "",
                    "Issue tags:",
                    ", ".join(issue.labels) or "none",
                    "",
                    AUTOPILOT_INSTRUCTION,
                    task_instruction,
                ]
            )
            if issue.work_type == "followup":
                lines.extend(
                    [
                        "\nFollow-up rework context:",
                        (
                            "This question was previously answered, but new GitHub comments request another pass. "
                            "Treat them as clarification or additional questions and answer them without changing code."
                            if question_issue else
                            "This issue was previously worked, but new GitHub comments indicate that it needs another "
                            "pass. Treat them as refinement or defect feedback. Reinspect the implementation, make the "
                            "additional fix, and verify it. The worker will create a commit if needed."
                        ),
                    ]
                )
                if issue.previous_ai and self.choice.name != issue.previous_ai:
                    lines.append(
                        f"The previous pass was completed by {issue.previous_ai}. You are intentionally providing an "
                        "independent second-provider review; challenge prior assumptions and use issue comments and "
                        "repository evidence as the source of truth."
                    )
                if issue.previous_commit_sha:
                    previous_show = self.git(
                        "show", "--no-ext-diff", "--format=fuller", "--stat", "--summary", issue.previous_commit_sha
                    )
                    lines.extend(
                        [
                            "\nPrevious completion commit and change summary:",
                            previous_show,
                            f"\nInspect the complete previous patch with: git show --no-ext-diff {issue.previous_commit_sha}",
                        ]
                    )
                else:
                    previous_body = str((issue.previous_completion_comment or {}).get("body") or "")
                    lines.append(
                        "\nThe previous pass answered this question without changing code."
                        if QUESTION_ANSWER_MARKER_RE.search(previous_body)
                        else "\nThe previous pass made no code change because it was waiting for required user input."
                    )
                lines.extend(
                    [
                        "\nPrevious worker completion comment:",
                        self.format_comment(issue.previous_completion_comment or {}),
                        "\nNew GitHub follow-up comments to address, in order:",
                    ]
                )
                lines.extend(self.format_comment(comment) for comment in issue.followup_comments)
        if self.config.require_issue_tests and not self.config.adversarial_uat_enabled and not question_issue:
            lines.append(
                "Also add or update UAT and integration tests that cover this issue. If the repository "
                "does not have a relevant test layer, say why under Verification."
            )
        if self.config.adversarial_uat_enabled and not question_issue:
            lines.append(
                "An independent adversarial tester will verify this patch before delivery. "
                "Do not edit, disable, or retire tests under tests/adversarial/ or suites with "
                "origin=adversarial in .swarm/tests.json. Dispute incorrect expectations with "
                "issue/spec evidence; only a fresh tester may adjudicate them."
            )
        if self.config.allow_environment_only_summary:
            lines.append(
                "If repository evidence shows this is caused only by local environment, credentials, "
                "external services, or infrastructure state, do not write code. Provide the requested "
                f"summary and put {ENVIRONMENT_ONLY_MARKER} on its own final line."
            )
        if question_issue:
            lines.append(QUESTION_INSTRUCTION)
        lines.append(NEEDS_INPUT_INSTRUCTION)
        if self.uses_version_file():
            lines.append(
                f"Do not edit the `{VERSION_FILE}` file: the worker manages the product version, and any "
                "edit you make to it is discarded."
            )
        lines.append(SUMMARY_INSTRUCTION)
        if recovery_dirty:
            lines.append(
                "The worktree also contains uncommitted changes. Inspect them, but do not assume they belong to "
                "this issue: preserve unrelated changes exactly. If any are unfinished work for this issue, finish "
                "and commit only that issue work."
            )
        if recovery_mode and recovery_candidate:
            lines.append(
                f"This is a recovery verification run. Commit {recovery_candidate} was created after the original "
                "attempt began and may already implement this issue. Verify implementation and tests. If complete, "
                "do not duplicate code or rewrite history; put SWARM_RECOVERY_COMPLETE on its own final line after "
                "your summary. If incomplete, finish it; the worker will commit any remaining completed changes."
            )
        note = format_image_note(self.ensure_issue_images(extra_image_sources))
        if note:
            lines.extend(["", note])
        return "\n".join(lines) + "\n"

    def ensure_issue_images(
        self, extra_sources: Sequence[tuple[str, str]] | None = None
    ) -> list[IssueImage]:
        """Download GitHub-hosted images from the issue, follow-ups, and any extra text.

        Already downloaded URLs are reused. A failed download is logged and
        skipped so a missing picture does not block the rest of the issue.
        """
        assert self.issue
        pieces: list[tuple[str, str]] = [("issue description", self.issue.body or "")]
        for comment in self.issue.followup_comments:
            pieces.append((f"comment #{comment.get('id')}", str(comment.get("body") or "")))
        pieces.extend(extra_sources or ())
        refs: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for source, text in pieces:
            for url, alt in extract_issue_image_refs(text, github_host=self.config.github_host):
                if url in seen:
                    continue
                seen.add(url)
                refs.append((url, alt, source))
        if len(refs) > MAX_IMAGES:
            log(
                f"WARNING: Issue #{self.issue.number} has {len(refs)} images; "
                f"attaching the first {MAX_IMAGES}."
            )
            refs = refs[:MAX_IMAGES]
        needs_download = any(
            url not in self._image_by_url and url not in self._image_failures for url, _, _ in refs
        )
        token = self._github_image_token() if needs_download else ""
        ordered: list[IssueImage] = []
        dest = self.state / "issue-images" / str(self.issue.number)
        for url, alt, source in refs:
            cached = self._image_by_url.get(url)
            if cached is not None:
                ordered.append(cached)
                continue
            if url in self._image_failures:
                continue
            try:
                image = download_issue_image(url, alt, source, dest, token=token)
            except ImageDownloadError as error:
                self._image_failures.add(url)
                log(f"WARNING: Could not download an image from {source} on issue #{self.issue.number}: {error}")
                continue
            self._image_by_url[url] = image
            ordered.append(image)
            log(f"Downloaded an issue image for #{self.issue.number} from {source}.")
        self.issue_images = ordered
        return ordered

    def _github_image_token(self) -> str:
        """Token that can read this repository's uploaded images.

        A configured GitHub App token wins. Otherwise `gh auth token` uses the
        same login the worker already uses for issues. The value is never logged.
        """
        environment = os.environ.copy()
        if self.choice is not None:
            try:
                environment.update(self.github.environment(self.choice.key))
            except WorkerError:
                pass
        token = environment.get("GH_TOKEN") or environment.get("GITHUB_TOKEN") or ""
        if token:
            return token
        if not self.config.gh_bin:
            return ""
        result = subprocess.run(
            [self.config.gh_bin, "auth", "token"],
            cwd=self.config.repo_dir,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def ai_reported_environment_only(self, output: str) -> tuple[bool, str]:
        if not self.config.allow_environment_only_summary:
            return False, output
        pattern = rf"^\s*{re.escape(ENVIRONMENT_ONLY_MARKER)}\s*$\n?"
        if not re.search(pattern, output, re.MULTILINE):
            return False, output
        cleaned = re.sub(pattern, "", output, flags=re.MULTILINE).rstrip() + "\n"
        return True, cleaned

    @staticmethod
    def ai_reported_needs_input(output: str) -> tuple[bool, str]:
        pattern = rf"^\s*{re.escape(NEEDS_INPUT_MARKER)}\s*$\n?"
        if not re.search(pattern, output, re.MULTILINE):
            return False, output
        cleaned = re.sub(pattern, "", output, flags=re.MULTILINE).rstrip() + "\n"
        return True, cleaned

    @staticmethod
    def ai_reported_question_answer(output: str) -> tuple[bool, str]:
        pattern = rf"^\s*{re.escape(QUESTION_ANSWER_MARKER)}\s*$\n?"
        if not re.search(pattern, output, re.MULTILINE):
            return False, output
        cleaned = re.sub(pattern, "", output, flags=re.MULTILINE).rstrip() + "\n"
        return True, cleaned

    @staticmethod
    def format_comment(comment: dict[str, Any]) -> str:
        return (
            f"Comment #{comment.get('id')} by @{comment.get('author', 'unknown')} at "
            f"{comment.get('created_at', '')}:\n{comment.get('body', '')}\n"
        )

    def first_ai_key(self) -> str:
        """The AI that created this issue's branch. Every later pass — even by
        a different provider — works out of that one branch. Resolved from the
        persisted branch name, else a remote branch lookup, else the current
        provider (a genuinely fresh issue)."""
        assert self.issue and self.choice
        cached = getattr(self, "_first_ai_key_cache", None)
        if cached and cached[0] == self.issue.number:
            return cached[1]
        persisted = ""
        if self.in_progress_file.exists():
            persisted = str(self.read_state().get("branch_name") or "")
        parts = persisted.split("/")
        if len(parts) == 3 and parts[0] == self.config.branch_prefix:
            key = parts[1]
        else:
            remote_branch = self.find_remote_issue_branch(self.issue.number)
            remote_parts = remote_branch.split("/") if remote_branch else []
            key = remote_parts[1] if len(remote_parts) == 3 else ai_tool_key(self.choice.key)
        self._first_ai_key_cache = (self.issue.number, key)
        return key

    def find_remote_issue_branch(self, issue_number: int) -> str:
        """`<prefix>/<ai>/issue-<n>` on the remote, if it exists."""
        pattern = re.compile(
            rf"^{re.escape(self.config.branch_prefix)}/([^/]+)/issue-{issue_number}$"
        )
        listing = self.git(
            "ls-remote", "--heads", self.config.remote_name, check=False
        )
        for line in listing.splitlines():
            _, _, ref = line.partition("\t")
            name = ref.strip().removeprefix("refs/heads/")
            if pattern.match(name):
                return name
        return ""

    def remote_is_github_host(self) -> bool:
        """Whether the configured Git remote is hosted by this GitHub server."""
        remote_url = self.git("remote", "get-url", self.config.remote_name, check=False).strip()
        host = re.escape(self.config.github_host.strip().lower())
        normalized = remote_url.lower()
        return bool(
            host
            and (
                re.search(rf"^[^@/]+@{host}:", normalized)
                or re.search(rf"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?{host}(?::[0-9]+)?/", normalized)
            )
        )

    def remote_branch_exists_at(self, branch: str, sha: str) -> bool:
        """Whether `branch` is already on the remote, pointing exactly at `sha`."""
        listing = self.git(
            "ls-remote", "--heads", self.config.remote_name, f"refs/heads/{branch}", check=False
        )
        return any(
            line.split("\t")[:2] == [sha, f"refs/heads/{branch}"] for line in listing.splitlines()
        )

    def gh_retrying_transient(
        self,
        arguments: Sequence[str],
        *,
        branch: str,
        base_sha: str,
        attempts: int = 3,
    ) -> str | None:
        """`gh` call that retries GitHub-side hiccups with a short backoff.

        Returns None when a failed attempt nonetheless left `branch` on the
        remote at `base_sha` — GitHub can create the linked branch and still
        report an internal error, and retrying then would only fail with
        "already exists". Anything that is not transient is raised at once."""
        assert self.choice
        for attempt in range(1, attempts + 1):
            try:
                return self.github.gh(arguments, self.choice.key)
            except WorkerError as error:
                if self.remote_branch_exists_at(branch, base_sha):
                    log(f"GitHub reported an error but branch {branch} exists at {base_sha[:12]}; continuing.")
                    return None
                if attempt == attempts or not is_transient_github_error(str(error)):
                    raise
                delay = 2 * attempt
                log(f"GitHub API hiccup linking {branch} (attempt {attempt}/{attempts}); retrying in {delay}s.")
                time.sleep(delay)
        raise AssertionError("unreachable")

    def create_linked_issue_branch(self, branch: str, base_sha: str) -> None:
        """Create a remote branch through its issue so GitHub tracks the link."""
        assert self.issue and self.choice
        issue_text = self.gh_retrying_transient(
            [
                "api",
                "--method",
                "GET",
                f"repos/{self.config.github_repository}/issues/{self.issue.number}",
            ],
            branch=branch,
            base_sha=base_sha,
        )
        if issue_text is None:
            return
        try:
            issue_id = str(json.loads(issue_text)["node_id"])
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise WorkerError(
                f"GitHub did not return a node ID for issue #{self.issue.number}"
            ) from error
        if not issue_id:
            raise WorkerError(f"GitHub returned an empty node ID for issue #{self.issue.number}")
        mutation = (
            "mutation($issueId:ID!,$oid:GitObjectID!,$name:String!){"
            "createLinkedBranch(input:{issueId:$issueId,oid:$oid,name:$name}){issue{id}}}"
        )
        response_text = self.gh_retrying_transient(
            [
                "api",
                "graphql",
                "-f",
                f"query={mutation}",
                "-f",
                f"issueId={issue_id}",
                "-f",
                f"oid={base_sha}",
                "-f",
                f"name={branch}",
            ],
            branch=branch,
            base_sha=base_sha,
        )
        if response_text is None:
            return
        try:
            linked_issue_id = str(
                json.loads(response_text)["data"]["createLinkedBranch"]["issue"]["id"]
            )
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise WorkerError(
                f"GitHub did not confirm the linked branch for issue #{self.issue.number}"
            ) from error
        if linked_issue_id != issue_id:
            raise WorkerError(
                f"GitHub linked branch {branch} to an unexpected issue"
            )
        log(f"Created GitHub-linked branch {branch} for issue #{self.issue.number}.")

    def expected_branch(self) -> str:
        assert self.issue and self.choice
        return f"{self.config.branch_prefix}/{self.first_ai_key()}/issue-{self.issue.number}"

    def review_provider(self, implementing_provider: str | None = None) -> str:
        """Key of a provider to attribute the PR approval to — any enabled
        provider other than the one that implemented the change, preferring the
        configured `preferred_provider`. Falls back to the implementer only when
        it is the single enabled provider."""
        if implementing_provider is None:
            assert self.choice
            implementing_provider = self.choice.key
        candidates = [s.key for s in self.config.enabled_specs if s.key != implementing_provider]
        if not candidates:
            return implementing_provider
        preferred = self.preferred_provider_key()
        return preferred if preferred in candidates else candidates[0]

    def provider_environment(self) -> dict[str, str]:
        assert self.choice
        environment = self.github.environment(self.choice.key)
        return environment

    def run_ai(self, prompt: str) -> int:
        assert self.choice
        self.ai_output_file.write_text("", encoding="utf-8")
        self.ai_diagnostic_file.write_text("", encoding="utf-8")
        env = os.environ.copy()
        env.update(self.provider_environment())
        runner = {
            "claude": self._run_claude,
            "codex": self._run_codex,
            "grok": self._run_grok,
        }.get(self.choice.key)
        if runner is None:
            raise WorkerError(f"No runner for provider {self.choice.name}")
        status = runner(prompt, env)
        if status != 0 and self.recover_from_rejected_model():
            runner = {
                "claude": self._run_claude,
                "codex": self._run_codex,
                "grok": self._run_grok,
            }.get(self.choice.key)
            if runner is None:
                raise WorkerError(f"No runner for provider {self.choice.name}")
            env = os.environ.copy()
            env.update(self.provider_environment())
            status = runner(prompt, env)
        return status

    def recover_from_rejected_model(self) -> bool:
        """When the AI CLI rejects a model, re-evaluate the live routing choice.

        A routing tier or an old saved setting can name a model this account
        does not offer (e.g. Grok's `unknown model id`). Failing then only
        repeats every cycle because the model is pinned in the issue's state.
        Dynamic routing is therefore run again immediately with the rejected
        model excluded. Without dynamic routing, the configured pair remains
        the bounded one-time fallback. Returns whether one retry is useful."""
        assert self.choice
        spec = self.config.spec(self.choice.key)
        if spec is None:
            return False
        combined = ""
        for path in (self.ai_output_file, self.ai_diagnostic_file):
            if path.exists():
                combined += path.read_text(encoding="utf-8", errors="replace")
        if not MODEL_REJECTED_RE.search(combined):
            return False
        rejected = {
            "provider": self.choice.key,
            "provider_name": self.choice.name,
            "model": self.choice.model,
            "effort": self.choice.effort,
        }
        requires_usage_credits = bool(
            re.search(r"requires usage credits", combined, re.IGNORECASE)
        )
        provider_message = next(
            (
                line.strip()[:500]
                for line in combined.splitlines()
                if MODEL_REJECTED_RE.search(line)
            ),
            "The provider rejected the selected model.",
        )
        if self.config.dynamic_model_routing and self.issue is not None:
            # Give a router failure a known-good fallback; otherwise the
            # fallback would reuse the rejected model still held in choice.
            self.choice.model = spec.model
            self.choice.effort = spec.effort
            self.choice.resume = False
            self.choice.session_id = self.new_session_id(spec)
            previous_routing = dict(self.routing or {})
            rejection_reason = (
                " because it requires usage credits" if requires_usage_credits else ""
            )
            log(
                f"{rejected['provider_name']} rejected model '{rejected['model']}'"
                f"{rejection_reason}; re-running pre-flight routing immediately with that model "
                "excluded."
            )
            self.routing = None
            self.apply_dynamic_routing(
                {(str(rejected["provider"]), str(rejected["model"]))}
            )
            if (self.choice.key, self.choice.model) == (
                rejected["provider"],
                rejected["model"],
            ):
                return False
            re_evaluation = {
                "timestamp": iso_timestamp(),
                "trigger": "model_requires_usage_credits"
                if requires_usage_credits
                else "model_rejected",
                "original_provider": rejected["provider"],
                "original_model": rejected["model"],
                "original_effort": rejected["effort"],
                "provider_message": provider_message,
                "configuration": (
                    "Models requiring separate usage credits are unavailable in the current "
                    "account/configuration."
                    if requires_usage_credits
                    else "The selected model was rejected by the provider."
                ),
                "replacement_provider": self.choice.key,
                "replacement_model": self.choice.model,
                "replacement_effort": self.choice.effort,
                "previous_routing_decision": previous_routing,
            }
            assert self.routing is not None
            self.routing["re_evaluation"] = re_evaluation
            if self.in_progress_file.exists():
                self.update_state_for_choice(self.choice)
                self.update_state(
                    routing_decision=self.routing,
                    routing_re_evaluation=re_evaluation,
                )
            reason = (
                "the provider required separate usage credits that current configuration does "
                "not allow"
                if requires_usage_credits
                else "the provider rejected that model"
            )
            message = (
                f"Pre-flight routing originally selected {rejected['provider_name']} "
                f"{rejected['model']} at {rejected['effort']} effort, but {reason}; real-time "
                f"re-evaluation selected {self.choice.name} {self.choice.model} at "
                f"{self.choice.effort} effort."
            )
            log(f"WARNING: {message}")
            self.history.warning(message, str(re_evaluation["timestamp"]))
            return True
        if (spec.model, spec.effort) == (self.choice.model, self.choice.effort):
            return False
        message = (
            f"{self.choice.name} does not offer model '{self.choice.model}'; retrying with the "
            f"configured '{spec.model}' at effort '{spec.effort}'. Fix the routing tier or "
            "setting that names it."
        )
        log(f"WARNING: {message}")
        self.history.warning(message, iso_timestamp())
        self.choice.model = spec.model
        self.choice.effort = spec.effort
        # The failed attempt may already have claimed its session id. A model
        # change must start clean rather than resuming that rejected session.
        self.choice.resume = False
        self.choice.session_id = self.new_session_id(spec)
        self.update_state_for_choice(self.choice)
        return True

    def _run_claude(self, prompt: str, env: dict[str, str]) -> int:
        assert self.choice
        claude_bin = self.provider_bin("claude")
        if not claude_bin:
            raise WorkerError("Claude executable is unavailable")
        log("Claude is working. Detailed implementation output is hidden; its final summary will appear when finished.")
        command = [
            claude_bin,
            "--model",
            self.choice.model,
            "--effort",
            self.choice.effort,
            "--permission-mode",
            "bypassPermissions",
        ]
        command.extend(
            ["--resume", self.choice.session_id]
            if self.choice.resume
            else ["--session-id", self.choice.session_id]
        )
        image_paths = [image.path for image in self.issue_images]
        inlined = inlined_images(prompt, image_paths, kind="claude") if image_paths else []
        if image_paths and not inlined:
            log(
                "WARNING: Issue images could not be inlined into the Claude prompt; "
                "the prompt lists their local files instead."
            )
        if inlined:
            command.extend(
                ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
            )
            stdin_text: str | None = claude_stream_message(prompt, inlined)
        else:
            command.extend(["-p", "-"])
            stdin_text = None
        process = subprocess.Popen(
            command,
            cwd=self.config.repo_dir,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # The process now genuinely owns self.choice.session_id (created
        # via --session-id, or attached via --resume) — from here on a
        # retry may legitimately --resume it. See choice_from_state.
        self.update_state(session_started=True)
        assert process.stdin and process.stdout
        process.stdin.write(stdin_text if stdin_text is not None else prompt)
        process.stdin.close()
        if inlined:
            raw = "".join(process.stdout)
            text = assistant_result_text(raw)
            if text and not text.endswith("\n"):
                text += "\n"
            self.ai_output_file.write_text(text, encoding="utf-8")
        else:
            with self.ai_output_file.open("w", encoding="utf-8") as output:
                for line in process.stdout:
                    output.write(line)
        return process.wait()

    def _run_grok(self, prompt: str, env: dict[str, str]) -> int:
        assert self.choice
        grok_bin = self.provider_bin("grok")
        if not grok_bin:
            raise WorkerError("Grok executable is unavailable")
        log(
            "Grok is working. Detailed implementation output is hidden; its final "
            "summary will appear when finished."
        )
        self.ai_prompt_file.write_text(prompt, encoding="utf-8")
        image_paths = [image.path for image in self.issue_images]
        inlined = inlined_images(prompt, image_paths, kind="grok") if image_paths else []
        if image_paths and not inlined:
            log(
                "WARNING: Issue images could not be inlined into the Grok prompt; "
                "the prompt lists their local files instead."
            )
        command = [grok_bin]
        if inlined:
            command.extend(["--prompt-json", grok_prompt_json(prompt, inlined)])
        else:
            command.extend(["--prompt-file", str(self.ai_prompt_file)])
        command.extend(
            [
                "--model",
                self.choice.model,
                "--reasoning-effort",
                self.choice.effort,
                "--permission-mode",
                "bypassPermissions",
                "--output-format",
                "json",
                "--cwd",
                str(self.config.repo_dir),
            ]
        )
        command.extend(
            ["--resume", self.choice.session_id]
            if self.choice.resume
            else ["--session-id", self.choice.session_id]
        )
        self.update_state(session_started=True)
        with self.ai_diagnostic_file.open("w", encoding="utf-8") as diagnostic:
            result = subprocess.run(
                command,
                cwd=self.config.repo_dir,
                env=env,
                text=True,
                stdout=diagnostic,
                stderr=subprocess.STDOUT,
                check=False,
            )
        # `--output-format json` prints one object: {"text": ..., "sessionId": ...}.
        raw = self.ai_diagnostic_file.read_text(encoding="utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            text = str(payload.get("text") or "")
            if text:
                self.ai_output_file.write_text(
                    text if text.endswith("\n") else text + "\n", encoding="utf-8"
                )
            session_id = str(payload.get("sessionId") or "")
            if session_id and session_id != self.choice.session_id:
                self.choice.session_id = session_id
                self.update_state(session_id=session_id, session_started=True)
        return result.returncode

    def _run_codex(self, prompt: str, env: dict[str, str]) -> int:
        assert self.choice
        codex_bin = self.provider_bin("codex")
        if not codex_bin:
            raise WorkerError("Codex executable is unavailable")
        log("Codex is working. Detailed implementation output is hidden; its final summary will appear when finished.")
        command = [codex_bin, "exec"]
        if self.choice.resume:
            command.append("resume")
        # Before `-m`, so a variadic `--image` cannot consume the stdin prompt (`-`).
        command.extend(codex_image_flags(image.path for image in self.issue_images))
        command.extend(
            [
                "-m",
                self.choice.model,
                "--dangerously-bypass-approvals-and-sandbox",
                "--dangerously-bypass-hook-trust",
                "-c",
                f'model_reasoning_effort="{self.choice.effort}"',
            ]
        )
        if not self.choice.resume:
            command.extend(["-C", str(self.config.repo_dir)])
        command.extend(["--json", "--output-last-message", str(self.ai_output_file)])
        if self.choice.resume:
            command.append(self.choice.session_id)
        command.append("-")
        with self.ai_diagnostic_file.open("w", encoding="utf-8") as diagnostic:
            result = subprocess.run(
                command,
                cwd=self.config.repo_dir,
                env=env,
                input=prompt,
                text=True,
                stdout=diagnostic,
                stderr=subprocess.STDOUT,
                check=False,
            )
        for line in self.ai_diagnostic_file.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started" and event.get("thread_id"):
                self.choice.session_id = str(event["thread_id"])
                self.update_state(session_id=self.choice.session_id, session_started=True)
                break
        return result.returncode

    def remote_has_no_branches(self) -> bool:
        """True only when the remote answers and has no branches at all — a
        freshly created, empty GitHub repository."""
        result = run_command(
            [self.config.git_bin, "-C", self.config.repo_dir, "ls-remote", "--heads",
             self.config.remote_name],
            check=False,
        )
        return result.returncode == 0 and not result.stdout.strip()

    def initial_readme(self) -> str:
        name = self.config.github_repository.rsplit("/", 1)[-1] or self.config.github_repository
        return (
            f"# {name}\n\n"
            "This repository was set up by SWARM Automation, which added this README as the "
            f"initial commit so `{self.config.base_branch}` exists for issue work to branch from.\n\n"
            "Assign an issue to the automation to start building.\n"
        )

    def bootstrap_empty_repository(self) -> bool:
        """Give a brand-new, empty repository its first commit.

        Issue work branches from `base_branch`, which does not exist until
        something is pushed, so an empty repo would fail every cycle. This is the
        one time the worker pushes to `base_branch`: a README-only initial commit,
        made only when the remote has no branches at all and the local checkout
        is empty and clean. The commit is built with plumbing so nothing local
        changes until the push has succeeded, which makes a failed attempt
        harmless to retry. Returns whether it created the branch."""
        if self.git_ok("rev-parse", "--verify", "--quiet", "HEAD"):
            return False
        if not self.remote_has_no_branches():
            return False
        remote = self.config.remote_name
        base = self.config.base_branch
        if self.worktree_status():
            log(
                f"{self.config.github_repository} has no branches yet, but its checkout has "
                f"files of its own; not creating {base} over them."
            )
            return False
        if self.config.dry_run:
            log(f"Dry run: {self.config.github_repository} is empty; would create {base} with a README.")
            return False
        log(f"{self.config.github_repository} is empty; creating {base} with an initial README commit.")
        git = [self.config.git_bin, "-C", self.config.repo_dir]
        identity = self.integration_push_environment()
        blob = run_command(
            [*git, "hash-object", "-w", "--stdin"], input_text=self.initial_readme()
        ).stdout.strip()
        tree = run_command(
            [*git, "mktree"], input_text=f"100644 blob {blob}\tREADME.md\n"
        ).stdout.strip()
        commit = run_command(
            [*git, "commit-tree", tree, "-m", "Initial commit"], env=identity
        ).stdout.strip()
        result = self.push_ref(f"{commit}:refs/heads/{base}")
        if result.returncode != 0:
            raise WorkerError(
                f"Could not create {base} in the empty repository "
                f"{self.config.github_repository}: "
                f"{(result.stderr or result.stdout or '').strip() or 'git push failed'}"
            )
        self.git("fetch", remote)
        self.git("symbolic-ref", "HEAD", f"refs/heads/{base}")
        self.git("reset", "--hard", f"{remote}/{base}")
        log(f"Created {base} at {commit} in {self.config.github_repository}.")
        return True

    def synchronize_base_branch(self) -> str:
        """Fast-forward the local read-only mirror of `base_branch` from the
        remote. The only push to `base_branch` is the initial README commit that
        `bootstrap_empty_repository` makes in a brand-new, empty repository."""
        remote = self.config.remote_name
        base = self.config.base_branch
        if not self.git_ok("show-ref", "--verify", f"refs/heads/{base}"):
            self.git("branch", base, f"{remote}/{base}")
        current = self.git("branch", "--show-current")
        if current != base:
            if self.worktree_status():
                raise WorkerError(f"Cannot synchronize {base} while the checkout is dirty on {current}")
            self.git("switch", base)
        if self.worktree_status():
            raise WorkerError(f"Cannot synchronize dirty {base}")
        remote_base = self.git("rev-parse", f"{remote}/{base}")
        self.git("merge", "--ff-only", f"{remote}/{base}", check=False)
        if self.git("rev-parse", "HEAD") != remote_base:
            raise WorkerError(
                f"Local {base} has diverged from {remote}/{base}; refusing to create AI work until it is reconciled"
            )
        synchronized = self.git("rev-parse", "HEAD")
        log(f"Local {base} mirrors {remote}/{base} at {synchronized}.")
        return synchronized

    def synchronize_integration_branch(self) -> str:
        """Ensure `integration_branch` exists, merge `base_branch` into it to
        keep parity (ff-only, else a merge commit, else abort the run), push it,
        and leave the checkout resting on it. Returns its HEAD sha."""
        remote = self.config.remote_name
        base = self.config.base_branch
        integ = self.config.integration_branch
        self.git("fetch", remote, check=False)
        base_head = self.synchronize_base_branch()

        if not self.git_ok("show-ref", "--verify", f"refs/heads/{integ}"):
            if self.git_ok("show-ref", "--verify", f"refs/remotes/{remote}/{integ}"):
                self.git("branch", integ, f"{remote}/{integ}")
            else:
                self.git("branch", integ, base)
                log(f"Created integration branch {integ} from {base}.")
        if self.git("branch", "--show-current") != integ:
            if self.worktree_status():
                raise WorkerError(f"Cannot switch to {integ}: the checkout is dirty")
            self.git("switch", integ)
        # Catch the integration branch up with any pushed changes to itself.
        if self.git_ok("show-ref", "--verify", f"refs/remotes/{remote}/{integ}"):
            remote_integ = self.git("rev-parse", f"{remote}/{integ}")
            self.git("merge", "--ff-only", f"{remote}/{integ}", check=False)
            if not self.git_ok("merge-base", "--is-ancestor", remote_integ, "HEAD"):
                raise WorkerError(
                    f"Local {integ} has diverged from {remote}/{integ}; refusing to create AI work until it is reconciled"
                )

        merged = self.git("merge", "--ff-only", base, check=False)
        if self.git("rev-parse", "HEAD") == base_head:
            pass
        elif self.git_ok("merge-base", "--is-ancestor", base, "HEAD"):
            log(f"{integ} already contains {base}.")
        else:
            result = run_command(
                [self.config.git_bin, "-C", self.config.repo_dir, "merge", "--no-edit",
                 "-m", f"[{integ}] sync {base}", base],
                check=False,
            )
            if result.returncode != 0:
                self.git("merge", "--abort", check=False)
                raise WorkerError(
                    f"{integ} conflicts with {base}; refusing to create an issue branch until a human "
                    "reconciles the integration branch"
                )
            else:
                log(f"Merged {base} into {integ}.")
        _ = merged
        self.push_integration_branch()
        head = self.git("rev-parse", "HEAD")
        return head

    def push_integration_branch(self) -> None:
        integ = self.config.integration_branch
        result = self.push_ref(f"HEAD:refs/heads/{integ}")
        if result.returncode != 0:
            raise WorkerError(
                f"Could not push synchronized {integ}: "
                f"{result.stderr.strip() or result.stdout.strip() or 'git push failed'}"
            )

    def push_ref(self, refspec: str, provider: str | None = None):
        """Push `refspec` to the remote. Uses a bot's installation token over
        HTTPS when a GitHub App is configured, otherwise a plain push to the
        configured remote (which the local checkout already authenticates)."""
        environment = self.integration_push_environment(provider)
        token = environment.get("GH_TOKEN", "")
        if token:
            with tempfile.TemporaryDirectory(prefix="swarm-git-askpass.") as temporary:
                askpass = Path(temporary) / "askpass.sh"
                askpass.write_text(
                    "#!/bin/sh\n"
                    'case "$1" in\n'
                    "  *Username*) printf '%s\\n' x-access-token ;;\n"
                    "  *) printf '%s\\n' \"$SWARM_GITHUB_APP_PUSH_TOKEN\" ;;\n"
                    "esac\n",
                    encoding="utf-8",
                )
                askpass.chmod(0o700)
                push_env = dict(environment)
                push_env.update(
                    {
                        "GIT_ASKPASS": str(askpass),
                        "GIT_TERMINAL_PROMPT": "0",
                        "SWARM_GITHUB_APP_PUSH_TOKEN": token,
                    }
                )
                return run_command(
                    [self.config.git_bin, "-C", self.config.repo_dir, "push",
                     f"https://{self.config.github_host}/{self.config.github_repository}.git", refspec],
                    env=push_env,
                    check=False,
                )
        return run_command(
            [self.config.git_bin, "-C", self.config.repo_dir, "push", self.config.remote_name, refspec],
            check=False,
        )

    def integration_push_environment(self, provider: str | None = None) -> dict[str, str]:
        """Bot env for pushing — the preferred provider's bot when configured,
        else the current provider's, else empty. ``auto`` is not a bot identity."""
        for key in (provider, self.preferred_provider_key(), getattr(self.choice, "key", "")):
            if key and self.apps.configured(key):
                return self.apps.bot_environment(key)
        return {}

    def issue_branch_pattern(self) -> re.Pattern[str]:
        """Matches `<prefix>/<provider>/issue-<n>` for every known provider.

        Group 1 is the branch's provider segment (`xai` for Grok), group 2 the
        issue number. Every branch-deleting path shares this one pattern so a
        new provider can never be recognized by one of them and not another."""
        provider_keys = "|".join(
            re.escape(key) for key in (*KNOWN_PROVIDER_KEYS, *BRANCH_PROVIDER_KEYS)
        )
        return re.compile(
            rf"^{re.escape(self.config.branch_prefix)}/({provider_keys})/issue-([0-9]+)$"
        )

    def delete_remote_issue_branch(
        self, branch: str, provider: str | None = None, *, label: str = "merged"
    ) -> None:
        if not self.issue_branch_pattern().fullmatch(branch):
            raise WorkerError(f"Refusing to delete unexpected branch name: {branch}")
        result = self.push_ref(f":refs/heads/{branch}", provider)
        detail = (result.stderr or result.stdout or "").strip()
        if result.returncode != 0 and "remote ref does not exist" not in detail.lower():
            raise WorkerError(
                f"Could not remove {label} issue branch {branch}: {detail or 'git push failed'}"
            )
        self.git("fetch", "--prune", self.config.remote_name, check=False)
        log(f"Removed {label} remote issue branch {branch}.")

    def prune_merged_worker_branches(self) -> None:
        pattern = self.issue_branch_pattern()
        branches = self.git("for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
        for branch in branches:
            if not pattern.fullmatch(branch):
                continue
            if self.git_ok("merge-base", "--is-ancestor", branch, self.config.integration_branch):
                self.git("branch", "-D", branch, check=False)
                log(f"Removed merged local worker branch {branch}.")

    def prepare_repository(self) -> tuple[str, bool, str, bool]:
        assert self.issue and self.choice
        if not self.git_ok("rev-parse", "--is-inside-work-tree"):
            raise WorkerError(f"--repo-dir is not a Git repository: {self.config.repo_dir}")
        # A brand-new repository has no `base_branch` to branch from; create it first.
        self.bootstrap_empty_repository()
        current_branch = self.git("branch", "--show-current")
        expected = self.expected_branch()
        state_exists = self.in_progress_file.exists()
        integ = self.config.integration_branch
        if not state_exists:
            if current_branch not in (integ, self.config.base_branch):
                if self.worktree_status():
                    raise WorkerError("Repository has changes on a work branch with no recovery state")
                if not self.git_ok("switch", integ):
                    self.git("switch", self.config.base_branch)
            if self.worktree_status():
                log("Repository has uncommitted changes unrelated to a saved attempt; deferring new issue work.")
                raise SystemExit(0)
            if self.issue.work_type == "followup":
                # Reuse the one branch this issue has always used.
                remote_branch = self.find_remote_issue_branch(self.issue.number)
                recreated_linked_branch = False
                self.synchronize_integration_branch()
                if remote_branch:
                    self.git("fetch", self.config.remote_name, remote_branch, check=False)
                    if self.git_ok("show-ref", "--verify", f"refs/heads/{expected}"):
                        self.git("switch", expected)
                        self.git("merge", "--ff-only", "FETCH_HEAD", check=False)
                    else:
                        self.git("switch", "-c", expected, "FETCH_HEAD")
                    # Rework on top of current integration-branch code, not the
                    # stale tip from when this issue was last merged. When the
                    # branch is fully merged (the common case) this simply
                    # fast-forwards it to the integration branch.
                    if not self.git_ok("merge", "--ff-only", integ):
                        if self.git_ok("merge", "--no-edit", integ):
                            log(f"Merged {integ} into {expected} before reworking.")
                        else:
                            self.git("merge", "--abort", check=False)
                            self.history.warning(
                                f"{expected} conflicts with {integ}; continuing from the branch tip",
                                iso_timestamp(),
                            )
                            log(
                                f"WARNING: {expected} conflicts with {integ}; "
                                f"reworking from the branch tip without the latest integration changes."
                            )
                    log(f"Continuing issue #{self.issue.number} on its existing branch {expected}.")
                else:
                    base = self.git("rev-parse", integ)
                    self.save_new_state(self.issue, self.choice, base)
                    if self.remote_is_github_host():
                        self.create_linked_issue_branch(expected, base)
                        recreated_linked_branch = True
                    self.git("switch", "-c", expected, integ)
                    log(f"Follow-up: no remote branch found; recreated {expected} from {integ}.")
                base = self.git("rev-parse", "HEAD")
                self.save_new_state(self.issue, self.choice, base)
                if recreated_linked_branch:
                    self.update_state(branch_linked=True)
            else:
                base = self.synchronize_integration_branch()
                self.prune_merged_worker_branches()
                # Persist ownership before creating the branch.
                self.save_new_state(self.issue, self.choice, base)
                if self.git_ok("show-ref", "--verify", f"refs/heads/{expected}"):
                    if self.git_ok("merge-base", "--is-ancestor", expected, integ):
                        self.git("branch", "-D", expected)
                    else:
                        raise WorkerError(
                            f"Existing branch {expected} contains unmerged work; recovery state was preserved"
                        )
                if self.remote_is_github_host():
                    self.create_linked_issue_branch(expected, base)
                    self.update_state(branch_linked=True)
                self.git("switch", "-c", expected, base)
                log(f"Created issue branch {expected} from {integ} at {base}.")
        else:
            state = self.read_state()
            expected = str(state.get("branch_name") or expected)
            if self.remote_is_github_host() and not state.get("branch_linked"):
                remote_branch = self.find_remote_issue_branch(self.issue.number)
                if remote_branch == expected:
                    self.update_state(branch_linked=True)
                else:
                    self.create_linked_issue_branch(expected, str(state["base_sha"]))
                    self.update_state(branch_linked=True)
            if current_branch != expected:
                if self.worktree_status():
                    raise WorkerError(f"Repository must be on saved issue branch {expected} before recovery")
                if self.git_ok("show-ref", "--verify", f"refs/heads/{expected}"):
                    self.git("switch", expected)
                else:
                    self.git("switch", "-c", expected, str(state["base_sha"]))
                    log(f"Recreated interrupted issue branch {expected} from its saved base.")

        run_start = self.git("rev-parse", "HEAD")
        base = run_start
        recovery_mode = False
        candidate = ""
        recovery_dirty = False
        # A follow-up to an environment-only summary, a question answer or an
        # input request has no previous commit to protect — only a follow-up to
        # a real completion does.
        if self.issue.work_type == "followup" and self.issue.previous_commit_sha:
            prev = self.issue.previous_commit_sha
            if not self.git_ok("cat-file", "-e", f"{prev}^{{commit}}"):
                raise WorkerError(
                    f"Previous completion commit is unavailable locally: {prev}"
                )
            # The prior completion is safe as long as it is reachable from
            # either the resumed issue branch (still un-merged) or the
            # integration branch (already merged). `previous_commit_sha` is
            # sometimes the PR *merge* commit rather than the branch work tip
            # — that merge commit is a descendant of the branch, so it is only
            # ever reachable via the integration branch, which is exactly the
            # case this second check covers.
            on_branch = self.git_ok("merge-base", "--is-ancestor", prev, run_start)
            merged = self.git_ok("merge-base", "--is-ancestor", prev, integ)
            if not on_branch and not merged:
                raise WorkerError(
                    f"Previous issue completion {prev} is not reachable from the issue "
                    f"branch or {integ}; refusing to rework on top of lost work"
                )
            if merged and not on_branch:
                log(
                    f"Previous completion {prev} is already merged into {integ}; "
                    f"reworking issue #{self.issue.number} from the current branch tip."
                )
        if state_exists:
            state = self.normalize_recovery_commits(self.in_progress_file, run_start)
            if int(state["issue_number"]) != self.issue.number:
                raise WorkerError(
                    f"Issue #{state['issue_number']} is already in progress; refusing issue #{self.issue.number}"
                )
            base = str(state["base_sha"])
            candidate = str(state.get("candidate_sha") or "")
            recovery_mode = True
            if run_start != base:
                if not candidate:
                    candidate = run_start
                    self.update_state(candidate_sha=candidate)
                log(f"Verifying commit {candidate} as recovered implementation for issue #{self.issue.number}.")
            if self.worktree_status():
                recovery_dirty = True
                log(f"Preserving uncommitted work while recovering issue #{self.issue.number}.")
            elif not candidate:
                log(f"Retrying issue #{self.issue.number} from its original clean base commit.")
        else:
            if self.worktree_status():
                log("Repository has uncommitted changes unrelated to a saved attempt; deferring new issue work.")
                raise SystemExit(0)
            if not self.in_progress_file.exists():
                self.save_new_state(self.issue, self.choice, base)
        self.update_state(attempt_start_sha=run_start, branch_name=expected)
        return run_start, recovery_mode, candidate, recovery_dirty

    def uses_version_file(self, revision: str = "HEAD") -> bool:
        """Whether this repository has opted into the VERSION scheme."""
        return self.git_ok("cat-file", "-e", f"{revision}:{VERSION_FILE}")

    def version_at(self, revision: str) -> tuple[int, int, int] | None:
        result = run_command(
            [self.config.git_bin, "-C", self.config.repo_dir, "show", f"{revision}:{VERSION_FILE}"],
            check=False,
        )
        return parse_version_file(result.stdout) if result.returncode == 0 else None

    def minor_bump_requested_by_trusted_user(self) -> bool:
        """The issue carries the `minor` label AND a trusted user applied it.
        Anyone with triage access can label an issue, so who applied the most
        recent `minor` label is checked against the trusted authors."""
        assert self.issue
        if MINOR_VERSION_LABEL not in {label.lower() for label in self.issue.labels}:
            return False
        events = self.github.api_list(
            f"repos/{self.config.github_repository}/issues/{self.issue.number}/events"
        )
        applied = [
            event
            for event in events
            if event.get("event") == "labeled"
            and str((event.get("label") or {}).get("name") or "").lower() == MINOR_VERSION_LABEL
        ]
        if not applied:
            return False
        actor = str((applied[-1].get("actor") or {}).get("login") or "")
        if author_matches(actor, self.trusted_followup_authors):
            return True
        log(
            f"Ignoring the '{MINOR_VERSION_LABEL}' label on issue #{self.issue.number}: it was "
            f"applied by @{actor or 'an unknown user'}, who is not a trusted author."
        )
        return False

    def changes_besides_version(self, run_start: str) -> bool:
        """Whether the run changed anything other than VERSION, committed or not."""
        committed = not self.git_ok(
            "diff", "--quiet", run_start, "HEAD", "--", ".", f":(exclude){VERSION_FILE}"
        )
        uncommitted = any(
            line[3:].strip() != VERSION_FILE for line in self.worktree_status().splitlines()
        )
        return committed or uncommitted

    def enforce_version_policy(self, run_start: str) -> None:
        """Keep VERSION changes to labelled issues (see versioning.md).

        Only the worker writes VERSION: an edit the AI made on its own —
        committed or not — is put back to what the run started with, and an
        issue a trusted user labelled `minor` gets the next minor, once per
        release (skipped when the branch already carries a bump that
        `base_branch` does not have yet). Nothing happens in a repository
        without a VERSION file, or when the AI changed nothing else.
        """
        assert self.issue
        if not self.uses_version_file(run_start):
            return
        changed_elsewhere = self.changes_besides_version(run_start)
        if not self.git_ok("diff", "--quiet", run_start, "--", VERSION_FILE):
            self.git("checkout", run_start, "--", VERSION_FILE)
            log(f"Discarded an AI edit to {VERSION_FILE}; only the worker changes the version.")
        if not changed_elsewhere or not self.minor_bump_requested_by_trusted_user():
            return
        current = self.version_at(run_start)
        if current is None:
            log(f"WARNING: {VERSION_FILE} is not a single MAJOR.MINOR.PATCH line; not bumping the minor.")
            return
        released = self.version_at(f"{self.config.remote_name}/{self.config.base_branch}")
        if released is None:
            log(
                f"{VERSION_FILE} is not on {self.config.base_branch} yet, so there is no release to "
                "measure against; not bumping the minor."
            )
            return
        if current[:2] > released[:2]:
            log(
                f"The minor was already bumped to {current[0]}.{current[1]} since the last release on "
                f"{self.config.base_branch}; one bump per release, so issue #{self.issue.number} adds none."
            )
            return
        path = Path(self.config.repo_dir) / VERSION_FILE
        bumped = bump_minor_in_version_text(path.read_text(encoding="utf-8"))
        path.write_text(bumped, encoding="utf-8")
        new = parse_version_file(bumped)
        assert new is not None
        log(
            f"Issue #{self.issue.number} is labelled '{MINOR_VERSION_LABEL}': "
            f"{VERSION_FILE} {current[0]}.{current[1]}.{current[2]} -> {new[0]}.{new[1]}.{new[2]}."
        )

    def commit_completed_work(self, run_start: str) -> str:
        assert self.issue and self.choice
        self.enforce_version_policy(run_start)
        if not self.worktree_status():
            return self.git("rev-parse", "HEAD")
        app_owned = self.app_owned_untracked_paths()
        self.git("add", "--all")
        if app_owned:
            self.git("reset", "-q", "--", *app_owned)
        if self.git_ok("diff", "--cached", "--quiet"):
            raise WorkerError(
                f"Issue #{self.issue.number} left worktree changes that Git could not stage"
            )
        current = self.git("rev-parse", "HEAD")
        tag = f"[{ai_tool_key(self.choice.key)}] "
        if current == run_start:
            title = re.sub(r"\s+", " ", self.issue.title).strip()
            message = f"{tag}{title} (#{self.issue.number})"
        else:
            message = f"{tag}Commit remaining completed work (#{self.issue.number})"
        run_command(
            [
                self.config.git_bin,
                "-C",
                self.config.repo_dir,
                "commit",
                "--no-verify",
                "-m",
                message,
            ],
            env=self.provider_environment(),
        )
        committed = self.git("rev-parse", "HEAD")
        if self.worktree_status():
            raise WorkerError(
                f"Issue #{self.issue.number} still has uncommitted changes after worker commit"
            )
        log(f"Committed completed issue #{self.issue.number} work as {committed}.")
        return committed

    def ensure_issue_reference(self, commit_sha: str, recovered: bool) -> str:
        assert self.issue and self.choice
        body = self.git("log", "-1", "--format=%B", commit_sha)
        tag = f"[{ai_tool_key(self.choice.key)}]"
        has_ref = bool(re.search(rf"(^|[^0-9])#{self.issue.number}([^0-9]|$)", body))
        lines = body.splitlines() or [""]
        subject = lines[0]
        needs_tag = not subject.lstrip().startswith(tag)
        if has_ref and not needs_tag:
            return commit_sha
        if recovered:
            log(
                f"Recovered commit {commit_sha} is established; leaving its message unchanged."
            )
            return commit_sha
        if needs_tag:
            subject = f"{tag} {subject}".strip()
        if not has_ref:
            subject = f"{subject} (#{self.issue.number})"
        lines[0] = subject
        run_command(
            [self.config.git_bin, "-C", self.config.repo_dir, "commit", "--amend", "--no-verify", "-F", "-"],
            env=self.provider_environment(),
            input_text="\n".join(lines) + "\n",
        )
        amended = self.git("rev-parse", "HEAD")
        log(f"Normalized the commit subject for issue #{self.issue.number} ({subject!r}).")
        return amended

    def validate_new_commit_messages(self, run_start: str, completion: str) -> None:
        """Keep incorrectly attributed AI commits off the remote branch."""
        assert self.issue and self.choice
        expected = f"[{ai_tool_key(self.choice.key)}]"
        commits = self.git("rev-list", "--reverse", f"{run_start}..{completion}").splitlines()
        untagged = []
        for sha in commits:
            subject = self.git("log", "-1", "--format=%s", sha)
            if not subject.lstrip().startswith(expected):
                untagged.append(f"{sha[:8]} {subject}")
        if untagged:
            raise WorkerError(
                f"{self.choice.name} created commit(s) without the required {expected} prefix; "
                "nothing was pushed: " + "; ".join(untagged)
            )

    def approve_pull_request(
        self,
        pr_url: str,
        implementing_provider: str | None = None,
        body: str = "Automated approval after the implementing provider completed verification.",
    ) -> str:
        reviewer = self.review_provider(implementing_provider)
        self.github.gh(
            [
                "pr",
                "review",
                pr_url,
                "--repo",
                self.config.github_repository,
                "--approve",
                "--body",
                body,
            ],
            reviewer,
        )
        log(f"{reviewer.capitalize()} Bot approved {pr_url}.")
        return reviewer

    def return_to_integration_branch(self, branch: str) -> str:
        integ = self.config.integration_branch
        if self.worktree_status():
            raise WorkerError("Cannot finish PR delivery while the issue branch is dirty")
        self.git("fetch", self.config.remote_name, check=False)
        if self.git("branch", "--show-current") != integ:
            if not self.git_ok("switch", integ):
                self.git("switch", "-c", integ, f"{self.config.remote_name}/{integ}")
        if self.git_ok("show-ref", "--verify", f"refs/remotes/{self.config.remote_name}/{integ}"):
            self.git("merge", "--ff-only", f"{self.config.remote_name}/{integ}", check=False)
        synchronized = self.git("rev-parse", "HEAD")
        if branch and branch != integ:
            self.git("branch", "-D", branch, check=False)
        if self.git("branch", "--show-current") != integ:
            raise WorkerError(f"PR delivery did not return the checkout to {integ}")
        log(f"Returned the clean local checkout to {integ} at {synchronized}.")
        return synchronized

    def open_pull_request_blocker(self, branch: str) -> str:
        """`""` only when GitHub positively confirms `branch` has no open PR.

        An unreachable or unreadable answer is a blocker, not a pass: the
        caller is about to delete a branch, so "GitHub did not say" must never
        be treated as "GitHub said no"."""
        try:
            listing = self.github.gh(
                [
                    "pr", "list", "--repo", self.config.github_repository,
                    "--head", branch, "--state", "open", "--limit", "1", "--json", "url",
                ]
            )
        except WorkerError as error:
            return f"the open pull request check for {branch} failed: {error}"
        try:
            records = json.loads(listing or "null")
        except json.JSONDecodeError:
            records = None
        if not isinstance(records, list):
            return f"GitHub returned an unreadable pull request list for {branch}"
        if records:
            url = str((records[0] or {}).get("url") or "an open pull request")
            return f"{branch} still has an open pull request ({url})"
        return ""

    def no_code_cleanup_blocker(self, branch: str) -> str:
        """Why a terminal no-code issue branch must be kept, or `""`.

        Every check fails closed. A branch is only safe to remove when it is
        the branch this attempt is standing on, the worktree is clean, the
        branch carries nothing beyond the commit the attempt started from
        locally *and* on the remote, and GitHub confirms no open pull request
        uses it."""
        if not branch:
            return "the worker does not know which branch this attempt used"
        if not self.issue_branch_pattern().fullmatch(branch):
            return f"{branch} is not a worker issue branch"
        current = self.git("branch", "--show-current")
        if current != branch:
            return f"the checkout is on {current or 'a detached HEAD'} rather than {branch}"
        if self.worktree_status():
            return f"{branch} has uncommitted changes"
        tip = self.git("rev-parse", "HEAD")
        state = self.read_state() if self.in_progress_file.exists() else {}
        anchors = [
            str(state.get(key) or "") for key in ("base_sha", "attempt_start_sha")
        ]
        anchors = [value for value in anchors if value]
        if not anchors:
            return f"the starting commit saved for {branch} is unknown"
        for value in anchors:
            if not SHA_RE.fullmatch(value) or not self.git_ok(
                "cat-file", "-e", f"{value}^{{commit}}"
            ):
                return f"the starting commit {value} saved for {branch} is unavailable"
            if self.git("rev-list", "--count", f"{value}..{tip}") != "0":
                return f"{branch} carries commits beyond {value[:12]}"
        # "Nothing new since the attempt started" is not on its own proof the
        # branch is empty: a follow-up work-round starts from the branch tip,
        # so an earlier round's still-unmerged commit sits at the anchor too.
        # Require the tip to already be part of the integration history.
        integ = self.config.integration_branch
        if not any(
            self.git_ok("show-ref", "--verify", ref)
            and self.git_ok("merge-base", "--is-ancestor", tip, ref)
            for ref in (
                f"refs/heads/{integ}",
                f"refs/remotes/{self.config.remote_name}/{integ}",
            )
        ):
            return f"{branch} carries work that is not yet in {integ}"
        listing = self.git(
            "ls-remote", "--heads", self.config.remote_name, f"refs/heads/{branch}", check=False
        )
        for line in listing.splitlines():
            remote_sha = line.split("\t")[0].strip()
            if remote_sha != tip:
                return f"the remote {branch} is at {remote_sha[:12]} rather than {tip[:12]}"
        return self.open_pull_request_blocker(branch)

    def cleanup_no_code_branch(self, outcome: str) -> str:
        """Return the checkout to the integration branch and drop the empty
        issue branch a terminal no-code result left behind.

        Returns ``cleaned``, ``kept`` or ``failed``. This never raises. The
        no-code result is already published on the issue by the time cleanup
        runs, so a cleanup problem is recorded as a warning and the branch is
        retained for review — it must never turn a delivered outcome into a
        failed run, and it must never suppress a branch it could not actually
        remove (see .claude/rules/issue-branch-delivery.md)."""
        assert self.issue and self.choice
        state = self.read_state() if self.in_progress_file.exists() else {}
        branch = str(state.get("branch_name") or self.expected_branch())
        blocker = self.no_code_cleanup_blocker(branch)
        if blocker:
            message = f"Kept issue branch {branch} after the {outcome} result because {blocker}."
            self.history.note(message, iso_timestamp())
            log(message)
            return "kept"
        try:
            self.return_to_integration_branch(branch)
            self.delete_remote_issue_branch(branch, self.choice.key, label="no-code")
        except WorkerError as error:
            message = (
                f"Could not clean up issue branch {branch} after the {outcome} result: {error}. "
                "The branch was left in place."
            )
            self.history.warning(message, iso_timestamp())
            log(f"WARNING: {message}")
            return "failed"
        message = (
            f"Removed the empty issue branch {branch} after the {outcome} result and returned "
            f"the checkout to {self.config.integration_branch}."
        )
        self.history.note(message, iso_timestamp())
        log(message)
        return "cleaned"

    def deliver_pull_request(self, commit_sha: str, *, allow_automation: bool = True) -> tuple[str, str, str]:
        assert self.issue and self.choice
        branch = self.expected_branch()
        environment = self.provider_environment()
        existing_text = self.github.gh(
            [
                "pr",
                "list",
                "--repo",
                self.config.github_repository,
                "--head",
                branch,
                "--state",
                "all",
                "--limit",
                "1",
                "--json",
                "url,state,mergeCommit,baseRefName,body",
            ],
            self.choice.key,
        )
        existing = json.loads(existing_text)
        if (
            existing
            and existing[0].get("state") == "MERGED"
            and existing[0].get("baseRefName") == self.config.integration_branch
        ):
            pr_url = str(existing[0]["url"])
            delivered_sha = str((existing[0].get("mergeCommit") or {}).get("oid") or "")
            if not SHA_RE.fullmatch(delivered_sha):
                raise WorkerError(f"Merged PR did not report a valid merge commit: {pr_url}")
            # A branch name is reused across every work-round on the same
            # issue, so "there is a merged PR for this branch name" is not
            # proof `commit_sha` — the commit *this* round just produced —
            # is part of it: it can just as easily be a previous round's
            # already-merged PR sitting under the same name, with the new
            # commit still only local. Confirm the new commit is actually an
            # ancestor of (or equal to) what was merged before trusting this
            # as "nothing to deliver" — otherwise the new commit is silently
            # never pushed while the work-round still reports success (see
            # issue-branch-delivery.md).
            # Fetch the integration branch itself rather than `delivered_sha`
            # directly: GitHub generally refuses to fetch an arbitrary commit
            # that isn't a ref tip, but `delivered_sha` is by definition on
            # `integration_branch` (that's what "merged into it" means), so
            # fetching the branch always brings it in.
            self.git("fetch", self.config.remote_name, self.config.integration_branch, check=False)
            if self.git_ok("merge-base", "--is-ancestor", commit_sha, delivered_sha):
                if self.issue_is_closed(self.issue.number):
                    self.delete_remote_issue_branch(branch, self.choice.key)
                self.return_to_integration_branch(branch)
                log(f"Recovered already-merged pull request {pr_url} for issue #{self.issue.number}.")
                return pr_url, branch, delivered_sha
            log(
                f"PR {pr_url} for {branch} is merged, but commit {commit_sha} is not part of it "
                "— this branch was reused for a new work-round; delivering it as new work instead "
                "of treating it as already merged."
            )

        # A separate scheduler reconciliation also approves open PRs. Put the
        # durable hold on a reused PR before pushing any failing UAT commit.
        if existing and existing[0].get("state") == "OPEN" and not allow_automation:
            existing_body = str(existing[0].get("body") or "")
            if CAP_HIT_PR_MARKER not in existing_body:
                self.github.gh(
                    ["pr", "edit", str(existing[0]["url"]), "--repo", self.config.github_repository, "--body-file", "-"],
                    self.choice.key, CAP_HIT_PR_NOTICE + existing_body,
                )
        push_result = self.push_ref(f"HEAD:refs/heads/{branch}")
        if push_result.returncode != 0:
            raise WorkerError(
                f"Could not push {branch}: "
                f"{push_result.stderr.strip() or push_result.stdout.strip() or 'git push failed'}"
            )
        _ = environment
        if existing and existing[0].get("state") == "OPEN":
            pr_url = str(existing[0]["url"])
            log(f"Reusing existing pull request {pr_url} for issue #{self.issue.number}.")
            existing_body = str(existing[0].get("body") or "")
            if allow_automation and CAP_HIT_PR_MARKER in existing_body:
                # Only a successful UAT follow-up may release a failed head.
                loop = self.read_state().get("adversarial", {})
                if loop.get("outcome") not in {"clean_first_pass", "resolved_after_n"}:
                    raise WorkerError("An adversarial cap-hit PR requires a passing UAT follow-up before automatic delivery")
                self.github.gh(
                    ["pr", "edit", pr_url, "--repo", self.config.github_repository, "--body-file", "-"],
                    self.choice.key, existing_body.replace(CAP_HIT_PR_NOTICE, "").replace(CAP_HIT_PR_MARKER, "").strip(),
                )
        else:
            title = self.git("log", "-1", "--format=%s", commit_sha)
            body = (
                f"Automated {self.choice.name} implementation for #{self.issue.number}.\n\n"
                f"Commit: `{commit_sha}`\n"
            )
            if not allow_automation:
                body = CAP_HIT_PR_NOTICE + body
            output = self.github.gh(
                [
                    "pr",
                    "create",
                    "--repo",
                    self.config.github_repository,
                    "--head",
                    branch,
                    "--base",
                    self.config.integration_branch,
                    "--title",
                    title,
                    "--body-file",
                    "-",
                ],
                self.choice.key,
                body,
            ).strip()
            pr_url = output.splitlines()[-1]
        delivered_sha = commit_sha
        if allow_automation and self.config.auto_approve:
            self.approve_pull_request(pr_url)
            delivered_sha = self.merge_pull_request(
                pr_url, commit_sha, self.choice.key, self.issue.number
            )
            self.delete_remote_issue_branch(branch, self.choice.key)
            self.return_to_integration_branch(branch)
            self.auto_promote_integration_branch(self.choice.key)
        return pr_url, branch, delivered_sha

    def default_provider(self) -> str | None:
        """The preferred enabled provider (else the first enabled one), used for
        GitHub actions that happen outside any provider's own work-round."""
        enabled = [spec.key for spec in self.config.enabled_specs]
        if not enabled:
            return None
        return self.preferred_provider_key() or enabled[0]

    def auto_promote_integration_branch(self, provider: str | None = None) -> str | None:
        """Best-effort roll-up of `integration_branch` into `base_branch`.

        Only runs when `--auto-promote` (and therefore issue-PR auto-merge) is
        on. It works purely against GitHub (open or reuse the integration PR,
        approve it as another provider's bot, merge-commit it), so the shared
        checkout is never touched. It runs *after* an issue's own delivery has
        already succeeded, so a failure here is logged and retried on the next
        run instead of failing — or un-delivering — the issue work. Returns
        the merged PR URL, or None when nothing was promoted. Without a
        `provider` (the start-of-run sweep), the preferred enabled provider
        opens the PR.
        """
        if not (self.config.auto_promote and self.config.auto_approve) or self.config.dry_run:
            return None
        provider = provider or self.default_provider()
        if provider is None:
            return None
        try:
            return self.promote_integration_branch(provider)
        except WorkerError as error:
            log(
                f"Could not promote {self.config.integration_branch} into "
                f"{self.config.base_branch}; leaving it for the next run: {error}"
            )
            return None

    def promote_integration_branch(self, provider: str) -> str | None:
        remote = self.config.remote_name
        base = self.config.base_branch
        integ = self.config.integration_branch
        self.git("fetch", remote, check=False)
        if not self.git_ok("show-ref", "--verify", f"refs/remotes/{remote}/{integ}"):
            return None
        if not self.git_ok("show-ref", "--verify", f"refs/remotes/{remote}/{base}"):
            return None
        if int(self.git("rev-list", "--count", f"{remote}/{base}..{remote}/{integ}")) == 0:
            return None
        listing = json.loads(
            self.github.gh(
                [
                    "pr",
                    "list",
                    "--repo",
                    self.config.github_repository,
                    "--base",
                    base,
                    "--head",
                    integ,
                    "--state",
                    "open",
                    "--limit",
                    "1",
                    "--json",
                    "url,headRefOid,mergeable,reviewDecision",
                ],
                provider,
            )
        )
        if listing:
            pull_request = listing[0]
            pr_url = str(pull_request["url"])
            if self.promotion_is_blocked(pr_url, str(pull_request.get("headRefOid") or "")):
                # A human already has this one; do not re-approve or retry a
                # merge branch protection will keep refusing.
                return None
        else:
            output = self.github.gh(
                [
                    "pr",
                    "create",
                    "--repo",
                    self.config.github_repository,
                    "--head",
                    integ,
                    "--base",
                    base,
                    "--title",
                    f"Merge {integ} into {base}",
                    "--body-file",
                    "-",
                ],
                provider,
                f"Automatic promotion of AI-integration work from `{integ}` to `{base}`.\n",
            ).strip()
            pr_url = output.splitlines()[-1]
            pull_request = {}
        if str(pull_request.get("mergeable") or "").upper() == "CONFLICTING":
            raise WorkerError(f"{pr_url} has merge conflicts; a human needs to resolve them")
        if str(pull_request.get("reviewDecision") or "").upper() != "APPROVED":
            self.approve_pull_request(
                pr_url,
                provider,
                f"Automated approval to promote `{integ}` into `{base}`.",
            )
        head_sha = self.github.gh(
            [
                "pr",
                "view",
                pr_url,
                "--repo",
                self.config.github_repository,
                "--json",
                "headRefOid",
                "--jq",
                ".headRefOid",
            ],
            provider,
        ).strip()
        if not SHA_RE.fullmatch(head_sha):
            raise WorkerError(f"GitHub returned no head commit for {pr_url}")
        try:
            self.github.gh(
                [
                    "pr",
                    "merge",
                    pr_url,
                    "--repo",
                    self.config.github_repository,
                    "--merge",
                    "--match-head-commit",
                    head_sha,
                ],
                provider,
            )
        except WorkerError as error:
            if not is_merge_blocked_by_policy(str(error)):
                raise
            self.record_promotion_blocked(pr_url, head_sha, provider)
            return None
        log(f"Promoted {integ} into {base} via {pr_url}.")
        return pr_url

    def promotion_blocked_file(self) -> Path:
        return self.state / "promotion-blocked.json"

    def promotion_is_blocked(self, pr_url: str, head_sha: str) -> bool:
        """Whether this exact promotion PR (at this head commit) was already found
        to need a manual merge. A new commit on `integration_branch` changes the
        head, so the merge is tried again."""
        if not head_sha:
            return False
        try:
            record = read_json(self.promotion_blocked_file())
        except (OSError, ValueError):
            return False
        return (
            isinstance(record, dict)
            and record.get("pr_url") == pr_url
            and record.get("head_sha") == head_sha
        )

    def record_promotion_blocked(self, pr_url: str, head_sha: str, provider: str) -> None:
        """Remember that branch protection stops the bot merging `pr_url`, say so
        once on the PR, and log it once — instead of failing the same way every
        cycle."""
        base = self.config.base_branch
        integ = self.config.integration_branch
        atomic_write_json(
            self.promotion_blocked_file(),
            {"pr_url": pr_url, "head_sha": head_sha, "recorded_at": iso_timestamp()},
        )
        log(
            f"Promotion PR {pr_url} needs a manual merge: branch protection on {base} does not "
            "let the automation merge it. Left open; it will not be retried until "
            f"{integ} changes."
        )
        marker = f"<!-- swarm-issue-worker:promotion-blocked:pr:{pr_url} -->"
        try:
            existing = self.github.gh(
                [
                    "pr",
                    "view",
                    pr_url,
                    "--repo",
                    self.config.github_repository,
                    "--json",
                    "comments",
                    "--jq",
                    ".comments[].body",
                ],
                provider,
            )
            if marker in existing:
                return
            self.github.gh(
                [
                    "pr",
                    "comment",
                    pr_url,
                    "--repo",
                    self.config.github_repository,
                    "--body-file",
                    "-",
                ],
                provider,
                f"{marker}\n"
                f"🤖 **{provider.capitalize()} Bot** approved this pull request but cannot merge it: "
                f"the branch protection rules on `{base}` do not allow the automation to.\n\n"
                f"A maintainer with merge access needs to merge it. The worker leaves it open and "
                f"will not retry until `{integ}` gets new commits.\n",
            )
        except WorkerError as error:
            log(f"WARNING: could not comment on {pr_url} about the blocked promotion: {error}")

    def merge_pull_request(
        self,
        pr_url: str,
        head_sha: str,
        provider: str,
        issue_number: int,
    ) -> str:
        """Squash an approved issue PR and record the result on its issue."""
        self.github.gh(
            [
                "pr",
                "merge",
                pr_url,
                "--repo",
                self.config.github_repository,
                "--squash",
                "--delete-branch",
                "--match-head-commit",
                head_sha,
            ],
            provider,
        )
        merge_sha = self.github.gh(
            [
                "pr",
                "view",
                pr_url,
                "--repo",
                self.config.github_repository,
                "--json",
                "mergeCommit",
                "--jq",
                ".mergeCommit.oid",
            ],
            provider,
        ).strip()
        if not SHA_RE.fullmatch(merge_sha):
            raise WorkerError(f"Merged PR did not report a valid merge commit: {pr_url}")
        body = (
            f"Squash-merged into `{self.config.integration_branch}` "
            f"(commit `{merge_sha}`) via {pr_url}.\n\n"
            + (
                f"Automatic promotion is on, so `{self.config.integration_branch}` is then merged "
                f"into `{self.config.base_branch}` automatically."
                if self.config.auto_promote
                else f"`{self.config.integration_branch}` reaches `{self.config.base_branch}` only "
                "when a human merges the integration pull request."
            )
        )
        self.github.gh(
            [
                "issue",
                "comment",
                str(issue_number),
                "--repo",
                self.config.github_repository,
                "--body",
                body,
            ],
            provider,
        )
        return merge_sha

    def finalize_issue(self, commit_sha: str, ai_output: str) -> None:
        assert self.issue and self.choice
        base_sha = str(self.read_state().get("base_sha") or "")
        commits = list(reversed(self.git("rev-list", f"{base_sha}..{commit_sha}").splitlines()))
        files = self.git("diff", "--name-only", base_sha, commit_sha).splitlines()
        pr_url, branch, commit_sha = self.deliver_pull_request(commit_sha)
        if commit_sha not in commits:
            commits.append(commit_sha)
        self.history.note("Commit and pull request delivery completed", iso_timestamp())
        usage_at_start = self.read_state().get("usage_at_start")
        pending = {
            "issue_number": self.issue.number,
            "issue_title": self.issue.title,
            "issue_url": self.issue.url,
            "ai": self.choice.name,
            "ai_tool": self.choice.name,
            "model": self.choice.model,
            "effort": self.choice.effort,
            "work_type": self.issue.work_type,
            "trigger_comment_id": self.issue.trigger_comment_id,
            "ai_output": ai_output,
            "commit_sha": commit_sha,
            "commit_message": self.git("log", "-1", "--format=%s", commit_sha),
            "github_comment_posted": False,
            "ready_for_testing_label_added": False,
            "pull_request_url": pr_url,
            "branch_name": branch,
            "adversarial_summary": self.adversarial_summary_line(),
            "usage_at_start": usage_at_start,
            "usage_at_completion": self.usage_snapshot(self.choice.key),
            "execution_id": self.history.execution_id,
            "commit_shas": commits,
            "files_changed": files,
        }
        atomic_write_json(self.pending_file, pending)
        pending = self.post_pending_comment(pending)
        pending = self.add_pending_label(pending)
        self.record_completed(self.issue.number)
        self.finish_execution_history(
            "completed",
            ai_output,
            commit_shas=commits,
            files_changed=files,
            pull_request_url=pr_url,
        )
        self.pending_file.unlink()
        self.clear_in_progress(self.issue.number)
        log(
            f"Finished issue #{self.issue.number} with {self.choice.name}: "
            f"{pending['commit_message']} ({commit_sha})."
        )

    def finalize_environment_only(self, ai_output: str) -> None:
        assert self.issue and self.choice
        marker_fields = (
            f"swarm-issue-worker:environment-only:issue:{self.issue.number};"
            f"provider:{self.choice.key}"
        )
        if self.issue.trigger_comment_id:
            marker_fields += f";through-comment:{self.issue.trigger_comment_id}"
        marker = f"<!-- {marker_fields} -->"
        usage_lines = self.render_usage_report(
            self.choice.name,
            self.read_state().get("usage_at_start"),
            self.usage_snapshot(self.choice.key),
        )
        body = (
            f"{marker}\nReviewed by **{self.choice.name}** with no code changes.\n\n"
            "- Result: this appears to be environmental rather than a code change.\n"
            f"{usage_lines}\n"
            "<details><summary>AI summary</summary>\n\n"
            f"{ai_output or '(No captured AI output was available.)'}\n"
            "</details>\n"
        )
        existing = any(marker in str(comment.get("body") or "") for comment in self.comments(self.issue.number))
        if not existing:
            log(f"Posting the environment-only summary to GitHub issue #{self.issue.number}.")
            self.github.gh(
                [
                    "issue",
                    "comment",
                    str(self.issue.number),
                    "--repo",
                    self.config.github_repository,
                    "--body-file",
                    "-",
                ],
                self.choice.key,
                body,
            )
        self.record_completed(self.issue.number)
        self.history.note("Execution completed without repository changes", iso_timestamp())
        self.cleanup_no_code_branch("environment-only")
        self.finish_execution_history("environment_only", ai_output)
        self.clear_in_progress(self.issue.number)
        log(f"Finished issue #{self.issue.number} with {self.choice.name}: environment-only summary posted.")

    def finalize_needs_input(self, ai_output: str, *, delivery: tuple[str, str, str] | None = None) -> None:
        """Pause an impossible-to-continue issue until a trusted user replies."""
        assert self.issue and self.choice
        marker_fields = (
            f"swarm-issue-worker:needs-input:issue:{self.issue.number};"
            f"provider:{self.choice.key}"
        )
        if self.issue.trigger_comment_id:
            marker_fields += f";through-comment:{self.issue.trigger_comment_id}"
        marker = f"<!-- {marker_fields} -->"
        body = (
            f"{marker}\n# 🤖 AI needs your input\n\n"
            "Work is paused because the AI cannot continue safely without the requested user "
            "answer or external action. This status is reserved for genuine blockers, not normal "
            "implementation choices.\n\n"
            f"{ai_output or '(No captured AI explanation was available.)'}\n"
            "## How to resume\n\n"
            "Complete the action above or answer the question in **one new comment**. Only replies "
            "from a configured trusted follow-up author resume automation. Do not put passwords, "
            "tokens, private keys, or other sensitive information in this issue.\n"
        )
        existing = any(marker in str(comment.get("body") or "") for comment in self.comments(self.issue.number))
        if not existing:
            log(f"Posting the required-input question to GitHub issue #{self.issue.number}.")
            self.github.gh(
                [
                    "issue", "comment", str(self.issue.number), "--repo",
                    self.config.github_repository, "--body-file", "-",
                ],
                self.choice.key,
                body,
            )
        self.ensure_label(
            NEEDS_INPUT_LABEL,
            "D93F0B",
            "AI work is blocked and requires a trusted user's answer or action",
            self.choice.key,
        )
        label_arguments = [
            "issue", "edit", str(self.issue.number), "--repo", self.config.github_repository,
            "--add-label", NEEDS_INPUT_LABEL,
        ]
        if self.config.ready_label.lower() in {label.lower() for label in self.issue.labels}:
            label_arguments.extend(["--remove-label", self.config.ready_label])
        self.github.gh(label_arguments, self.choice.key)
        self.record_completed(self.issue.number)
        self.history.note("Execution paused for required trusted-user input", iso_timestamp())
        # Ordinary input requests are code-free. A UAT deadlock instead retains
        # its delivered PR, branch and tests for trusted-author adjudication.
        if delivery:
            pr_url, _branch, commit_sha = delivery
            base = str(self.read_state()["base_sha"])
            self.finish_execution_history(
                "awaiting_input", ai_output, pull_request_url=pr_url,
                commit_shas=self.git("rev-list", f"{base}..{commit_sha}").splitlines(),
                files_changed=self.git("diff", "--name-only", base, commit_sha).splitlines(),
            )
        else:
            self.cleanup_no_code_branch("needs-input")
            self.finish_execution_history("awaiting_input", ai_output)
        self.clear_in_progress(self.issue.number)
        log(f"Issue #{self.issue.number} is labelled '{NEEDS_INPUT_LABEL}' and waiting for user input.")

    def finalize_question_answer(self, ai_output: str) -> None:
        """Post a no-code answer for an issue explicitly labelled Question."""
        assert self.issue and self.choice
        marker_fields = (
            f"swarm-issue-worker:question-answer:issue:{self.issue.number};"
            f"provider:{self.choice.key}"
        )
        if self.issue.trigger_comment_id:
            marker_fields += f";through-comment:{self.issue.trigger_comment_id}"
        marker = f"<!-- {marker_fields} -->"
        body = (
            f"{marker}\n# 🤖 AI answer\n\n"
            f"Answered by **{self.choice.name}** after the normal pre-flight grading and routing flow. "
            "No repository changes were made.\n\n"
            f"{ai_output or '(No captured AI answer was available.)'}"
        )
        existing = any(marker in str(comment.get("body") or "") for comment in self.comments(self.issue.number))
        if not existing:
            log(f"Posting the AI answer to GitHub question issue #{self.issue.number}.")
            self.github.gh(
                [
                    "issue", "comment", str(self.issue.number), "--repo",
                    self.config.github_repository, "--body-file", "-",
                ],
                self.choice.key,
                body,
            )
        if self.config.ready_label.lower() in {label.lower() for label in self.issue.labels}:
            self.github.gh(
                [
                    "issue", "edit", str(self.issue.number), "--repo", self.config.github_repository,
                    "--remove-label", self.config.ready_label,
                ],
                self.choice.key,
            )
        self.record_completed(self.issue.number)
        self.history.note("Question answered without repository changes", iso_timestamp())
        self.cleanup_no_code_branch("question-answer")
        self.finish_execution_history("answered", ai_output)
        self.clear_in_progress(self.issue.number)
        log(f"Finished question issue #{self.issue.number} with a no-code answer from {self.choice.name}.")

    def clear_needs_input_label(self) -> None:
        """Remove the waiting label when a trusted response starts a follow-up."""
        assert self.issue and self.choice
        if NEEDS_INPUT_LABEL.lower() not in {label.lower() for label in self.issue.labels}:
            return
        self.github.gh(
            [
                "issue", "edit", str(self.issue.number), "--repo", self.config.github_repository,
                "--remove-label", NEEDS_INPUT_LABEL,
            ],
            self.choice.key,
        )
        log(f"Trusted input received; removed '{NEEDS_INPUT_LABEL}' from issue #{self.issue.number}.")

    def run_selected_issue(self) -> int:
        assert self.issue
        if self.issue.work_type == "followup":
            log(
                f"Selected issue #{self.issue.number} for rework after GitHub follow-up comment "
                f"{self.issue.trigger_comment_id}: {self.issue.title}"
            )
        elif self.issue.ci_monitor:
            log(f"Working CI failure issue #{self.issue.number} filed by the Actions monitor: {self.issue.title}")
        else:
            log(f"Selected oldest unprocessed assigned issue: #{self.issue.number} {self.issue.title}")

        if self.in_progress_file.exists():
            state = self.read_state()
            self.choice = self.choice_from_state(state)
            if not self.quota_resume_ready:
                capacity = self.provider_capacity(self.choice.name)
                if capacity == 2:
                    handoff = self.choose_handoff_provider(self.choice, "usage could not be verified")
                    if handoff:
                        self.choice = handoff
                        self.update_state_for_choice(handoff)
                    else:
                        log(
                            f"Could not verify {self.choice.name} usage for pinned issue #{self.issue.number}; "
                            "leaving state active and retrying later."
                        )
                        return PROVIDER_UNAVAILABLE_EXIT_CODE
                if capacity == 1:
                    handoff = self.choose_handoff_provider(self.choice, "usage is unavailable")
                    if handoff:
                        self.choice = handoff
                        self.update_state_for_choice(handoff)
                    else:
                        if self.config.dry_run:
                            log(
                                f"Dry run: pinned {self.choice.name} session {self.choice.session_id} is waiting for usage."
                            )
                            return 0
                        if not self.choice.session_id:
                            raise WorkerError(f"Pinned {self.choice.name} attempt has no resumable session ID")
                        self.mark_quota_paused()
                        self.post_quota_comment()
                        self.suspend_paused()
                        return QUOTA_PAUSED_EXIT_CODE
        else:
            usages = {
                spec.name: self.provider_usage(spec.key)
                for spec in self.config.enabled_specs
            }
            remaining = {
                name: usage.remaining_percent
                for name, usage in usages.items()
                if usage.usable
            }
            self.provider_usages = usages
            self.provider_priority = tuple(
                self.provider_priority_order(self.issue.previous_ai, remaining)
            )
            self.choice = self.choose_provider(self.issue.previous_ai, remaining)
            if not self.choice:
                enabled = ", ".join(spec.name for spec in self.config.enabled_specs) or "no provider"
                log(
                    f"No enabled provider ({enabled}) has at least "
                    f"{self.config.minimum_remaining_percent:g}% remaining in every active quota "
                    "window; stopping."
                )
                return PROVIDER_UNAVAILABLE_EXIT_CODE
            self.start_usage = usages.get(self.choice.name)

        assert self.choice
        self.ensure_bot_auth()
        if self.config.dry_run:
            if self.config.dynamic_model_routing and not self.choice.resume:
                log(
                    "Dry run: dynamic model routing is enabled and would grade this issue, then "
                    "choose the AI tool, model, and reasoning effort before execution."
                )
            log(f"Dry run complete: would run {self.choice.name} for {self.issue.url}.")
            return 0

        if not (self.in_progress_file.exists() and self.read_state().get("adversarial")):
            self.maybe_apply_dynamic_routing()
        if self.issue.work_type == "followup":
            self.clear_needs_input_label()
        if self.choice.resume:
            log(
                f"Pinned {self.choice.name} model {self.choice.model} session {self.choice.session_id} "
                f"with effort {self.choice.effort} for this continuation."
            )
        else:
            log(f"Selected {self.choice.name} model {self.choice.model} with effort {self.choice.effort} for this run.")

        self.start_execution_history()
        if self.routing and self.routing.get("fallback"):
            self.history.warning(
                "Dynamic routing fell back to the configured worker model: "
                f"{self.routing.get('grade_reason') or 'router unavailable'}",
                iso_timestamp(),
            )
        elif self.routing:
            note = (
                f"Dynamic routing selected {self.choice.name} {self.choice.model} with effort "
                f"{self.choice.effort}; prompt grade {self.routing.get('prompt_grade')}."
            )
            provider_reason = str(self.routing.get("provider_reason") or "").strip()
            if provider_reason:
                note += f" Why {self.choice.name}: {provider_reason}"
            complexity_reason = str(self.routing.get("complexity_reason") or "").strip()
            if complexity_reason:
                note += f" Complexity {self.routing.get('complexity')}/10: {complexity_reason}"
            override = str(self.routing.get("provider_override_reason") or "").strip()
            if override:
                note += f" {override}"
            self.history.note(note, iso_timestamp())
        self.history.update(iso_timestamp(), final_status="preparing_repository")
        run_start, recovery_mode, candidate, recovery_dirty = self.prepare_repository()
        # save_new_state may have created/replaced state after history started.
        if self.history.execution_id and self.read_state().get("execution_id") != self.history.execution_id:
            self.update_state(execution_id=self.history.execution_id)
        self.history.note("Repository prepared", iso_timestamp())
        # The loop is part of this work-round, with one Started comment even
        # when a different tester or fixer owns the active quota checkpoint.
        if not self.read_state().get("adversarial"):
            self.post_started_comment()
        self.post_resumed_comment()
        if self.read_state().get("adversarial"):
            self.refresh_adversarial_requirements()
            return self.run_adversarial_delivery()
        prompt = self.build_prompt(recovery_mode, candidate, recovery_dirty)
        self.history.update(
            iso_timestamp(), effective_prompt=prompt, final_status="prompt_generated"
        )
        self.history.note("AI execution began", iso_timestamp())
        self.history.update(iso_timestamp(), final_status="running")
        ai_status = self.run_ai(prompt)
        if ai_status != 0 or not self.ai_output_file.exists() or self.ai_output_file.stat().st_size == 0:
            if self.ai_failure_is_quota():
                if not self.choice.session_id:
                    raise WorkerError(
                        f"{self.choice.name} exhausted usage before returning a resumable session ID; "
                        "worktree preserved but automatic resume is unavailable"
                    )
                self.mark_quota_paused()
                self.history.warning("AI usage quota became unavailable", iso_timestamp())
                self.finish_execution_history("quota_paused")
                self.post_quota_comment()
                self.suspend_paused()
                return QUOTA_PAUSED_EXIT_CODE
            if ai_status != 0:
                raise WorkerError(
                    f"{self.choice.name} exited unsuccessfully. Session and repository state were preserved; "
                    f"see {self.ai_output_file} and {self.ai_diagnostic_file}."
                )
            raise WorkerError(
                f"{self.choice.name} finished without a final summary. Session and repository state were preserved."
            )

        output = self.ai_output_file.read_text(encoding="utf-8", errors="replace")
        self.history.note("AI response received", iso_timestamp())
        ai_operational_notes = self.summary_section(output, "Operational notes")
        if ai_operational_notes and ai_operational_notes != "- None.":
            self.history.note(f"AI operational notes: {ai_operational_notes}", iso_timestamp())
        self.history.update(
            iso_timestamp(),
            requested_work_summary=self.summary_section(output, "Summary"),
            changes_summary=self.summary_section(output, "Changes"),
            final_status="ai_response_received",
        )
        # None of the three providers streams its raw output to the operator
        # log any more (Claude's per-line echo was removed alongside it) —
        # the full completion summary is posted to the GitHub issue instead
        # (see render_pending_comment). Only note that it arrived and where
        # the raw text lives on disk.
        log(
            f"{self.choice.name} returned a completion summary "
            f"({len(output)} chars); see {self.ai_output_file}."
        )
        if self.git("branch", "--show-current") != self.expected_branch():
            raise WorkerError(
                f"{self.choice.name} changed branches; refusing to commit outside {self.expected_branch()}"
            )
        needs_input, output = self.ai_reported_needs_input(output)
        question_answer, output = self.ai_reported_question_answer(output)
        question_issue = QUESTION_LABEL.lower() in {label.lower() for label in self.issue.labels}
        if question_answer and not question_issue:
            raise WorkerError(
                f"{self.choice.name} returned a question answer for an issue without the '{QUESTION_LABEL}' label"
            )
        if question_issue:
            if self.git("rev-parse", "HEAD") != run_start or self.worktree_status():
                raise WorkerError(
                    f"{self.choice.name} changed the repository while answering a '{QUESTION_LABEL}' issue; "
                    "question issues must remain code-free"
                )
            if not question_answer and not needs_input:
                raise WorkerError(
                    f"{self.choice.name} did not return {QUESTION_ANSWER_MARKER} or a genuine input request "
                    f"for this '{QUESTION_LABEL}' issue"
                )
        if self.config.adversarial_uat_enabled and not question_issue:
            protection = {"phase": "fix", "stage_base": run_start, "dispute": ""}
            self.validate_adversarial_edits(protection, {})
            if protection["dispute"]:
                self.update_state(adversarial_initial_dispute=protection["dispute"])
        after = self.commit_completed_work(run_start)
        completion = after
        recovered = False
        environment_only, output = self.ai_reported_environment_only(output)
        if needs_input:
            if after != run_start or self.worktree_status():
                raise WorkerError(
                    f"{self.choice.name} requested user input but also left repository changes; "
                    "refusing to publish an ambiguous partial result"
                )
            self.ai_output_file.write_text(output, encoding="utf-8")
            self.finalize_needs_input(output)
            return ISSUE_COMPLETED_EXIT_CODE
        if question_answer:
            self.ai_output_file.write_text(output, encoding="utf-8")
            self.finalize_question_answer(output)
            return ISSUE_COMPLETED_EXIT_CODE
        if after != run_start:
            pass
        elif recovery_mode and candidate and re.search(r"^\s*SWARM_RECOVERY_COMPLETE\s*$", output, re.MULTILINE):
            completion = candidate
            recovered = True
            output = re.sub(r"^\s*SWARM_RECOVERY_COMPLETE\s*$\n?", "", output, flags=re.MULTILINE)
            self.ai_output_file.write_text(output, encoding="utf-8")
            log(f"Accepted commit {completion} as recovered implementation for issue #{self.issue.number}.")
        elif environment_only:
            if self.worktree_status():
                raise WorkerError(
                    f"{self.choice.name} reported an environmental issue but left uncommitted changes"
                )
            self.ai_output_file.write_text(output, encoding="utf-8")
            self.finalize_environment_only(output)
            return ISSUE_COMPLETED_EXIT_CODE
        else:
            raise WorkerError(
                f"{self.choice.name} finished without producing changes or a new commit. "
                "Recovery state was preserved."
            )
        if self.git("branch", "--show-current") != self.expected_branch():
            raise WorkerError(f"{self.choice.name} changed branches; commit was not left on {self.expected_branch()}")
        base = str(self.read_state()["base_sha"])
        if not self.git_ok("merge-base", "--is-ancestor", base, after):
            raise WorkerError(f"{self.choice.name} rewrote history instead of adding a descendant commit")
        completion = self.ensure_issue_reference(completion, recovered)
        self.validate_new_commit_messages(run_start, completion)
        self.history.note("Repository changes and commit validation completed", iso_timestamp())
        self.history.update(iso_timestamp(), final_status="validated")
        if self.worktree_status():
            raise WorkerError(
                f"Issue #{self.issue.number} cannot be delivered with uncommitted changes"
            )
        if self.config.adversarial_uat_enabled:
            self.initialize_adversarial(completion, output)
            return self.run_adversarial_delivery()
        self.history.update(iso_timestamp(), adversarial_outcome="disabled")
        self.finalize_issue(completion, output)
        return ISSUE_COMPLETED_EXIT_CODE

    def latest_pipeline_runs(self) -> list[dict[str, Any]]:
        """The newest Actions run of each workflow on the integration branch,
        newest first. Read with the operator's own `gh` sign-in, like the issue
        queue, so the bot apps need no extra Actions permission."""
        listing = json.loads(
            self.github.gh(
                [
                    "run",
                    "list",
                    "--repo",
                    self.config.github_repository,
                    "--branch",
                    self.config.integration_branch,
                    "--limit",
                    "50",
                    "--json",
                    "databaseId,workflowName,status,conclusion,headSha,url,event,createdAt",
                ]
            )
            or "[]"
        )
        latest: dict[str, dict[str, Any]] = {}
        for run in sorted(listing, key=lambda item: str(item.get("createdAt") or ""), reverse=True):
            latest.setdefault(str(run.get("workflowName") or ""), run)
        return sorted(latest.values(), key=lambda item: str(item.get("createdAt") or ""), reverse=True)

    def failing_pipeline_runs(self) -> list[dict[str, Any]]:
        # A workflow whose newest run is still queued or running has no verdict
        # yet, so it is neither failing nor healthy this tick.
        return [
            run
            for run in self.latest_pipeline_runs()
            if str(run.get("status") or "").lower() == "completed"
            and str(run.get("conclusion") or "").lower() in CI_FAILED_CONCLUSIONS
            and SHA_RE.fullmatch(str(run.get("headSha") or ""))
        ]

    def ci_failure_reported(self, head_sha: str) -> bool:
        """True when this failure needs no new issue: an issue for the branch is
        still open (it is being, or is about to be, worked), or one was already
        filed for this exact head commit (a person closed it; do not refile)."""
        listing = json.loads(
            self.github.gh(
                [
                    "issue",
                    "list",
                    "--repo",
                    self.config.github_repository,
                    "--label",
                    CI_FAILURE_LABEL,
                    "--state",
                    "all",
                    "--limit",
                    "100",
                    "--json",
                    "state,body",
                ]
            )
            or "[]"
        )
        for issue in listing:
            match = CI_ISSUE_MARKER_RE.search(str(issue.get("body") or ""))
            if not match or match.group(1) != self.config.integration_branch:
                continue
            if str(issue.get("state") or "").upper() == "OPEN" or match.group(2) == head_sha:
                return True
        return False

    def ci_failure_run_excerpt(self, run: dict[str, Any]) -> str:
        try:
            log_text = self.github.gh(
                [
                    "run",
                    "view",
                    str(run["databaseId"]),
                    "--repo",
                    self.config.github_repository,
                    "--log-failed",
                ]
            )
        except (WorkerError, KeyError):
            return ""
        return log_text.strip()[-CI_LOG_EXCERPT_CHARS:].replace("```", "'''")

    def render_ci_failure_issue(self, failing: list[dict[str, Any]]) -> tuple[str, str]:
        integ = self.config.integration_branch
        sha = str(failing[0]["headSha"])
        names = [str(run.get("workflowName") or "workflow") for run in failing]
        title = f"Fix failing CI on {integ}: {', '.join(names)}"
        if len(title) > 120:
            title = title[:117].rstrip() + "..."
        lines = [
            f"<!-- swarm-issue-worker:ci-failure:branch:{integ};sha:{sha} -->",
            f"The most recent GitHub Actions run of {len(failing)} workflow(s) on `{integ}` "
            "failed. This issue was filed automatically because CI monitoring is enabled for "
            "this repository.",
            "",
            "## Failing pipelines",
        ]
        for index, run in enumerate(failing):
            lines.append(
                f"- **{run.get('workflowName') or 'workflow'}** — {run.get('conclusion')} "
                f"({run.get('event')}) on `{str(run['headSha'])[:7]}`: {run.get('url')}"
            )
            excerpt = self.ci_failure_run_excerpt(run) if index < 3 else ""
            if excerpt:
                lines += [
                    "",
                    "<details><summary>Failed step output (tail)</summary>",
                    "",
                    "```",
                    excerpt,
                    "```",
                    "</details>",
                    "",
                ]
        lines += [
            "",
            "## Task",
            "Find the root cause of the failure and fix it so the pipeline passes again. Fix the "
            "underlying problem: do not disable, skip, or delete the failing workflow or tests to "
            "make it green. If the failure is caused only by the environment (missing secrets, "
            "runner or external-service outage), do not change code and say so.",
        ]
        return title, "\n".join(lines) + "\n"

    def ensure_label(self, name: str, color: str, description: str, provider: str) -> None:
        try:
            self.github.gh(
                [
                    "label",
                    "create",
                    name,
                    "--repo",
                    self.config.github_repository,
                    "--color",
                    color,
                    "--description",
                    description,
                ],
                provider,
            )
        except WorkerError as error:
            if "already exists" not in str(error).lower():
                raise

    def file_labelled_issue(
        self, title: str, body: str, labels: Sequence[tuple[str, str, str]], provider: str
    ) -> str:
        """Shared auto-filing mechanism for CI and out-of-scope UAT findings."""
        for label, color, description in labels:
            self.ensure_label(label, color, description, provider)
        return self.github.gh(
            ["issue", "create", "--repo", self.config.github_repository,
             "--title", title, "--body-file", "-", "--assignee", self.config.github_assignee,
             *[part for label, _, _ in labels for part in ("--label", label)]],
            provider, body,
        )

    def monitor_repository_actions(self) -> IssueContext | None:
        """File — and hand straight to this run — an issue for a failing pipeline.

        Runs only when `--monitor-actions` is on. When the newest Actions run of
        any workflow on the integration branch failed and no CI-failure issue is
        already open (or was already filed for that commit), it creates one,
        labelled and assigned like any other worker issue, and returns it so
        this same run works it. The caller then skips `select_issue`, and a later
        run treats the open issue as tracked, so the failure is never worked
        twice. Best-effort: a problem here is logged and never blocks the
        regular queue.
        """
        if not self.config.monitor_actions:
            return None
        if self.in_progress_file.exists():
            return None  # an interrupted issue resumes first
        try:
            failing = self.failing_pipeline_runs()
            if not failing:
                log(f"GitHub Actions on {self.config.integration_branch} are passing.")
                return None
            names = ", ".join(str(run.get("workflowName")) for run in failing)
            if self.ci_failure_reported(str(failing[0]["headSha"])):
                log(
                    f"Failing pipeline(s) on {self.config.integration_branch} ({names}) "
                    "already have an issue."
                )
                return None
            if self.config.dry_run:
                log(f"Dry run: would file a CI failure issue for {names}.")
                return None
            provider = self.default_provider()
            if provider is None:
                log("No enabled provider is available to file a CI failure issue.")
                return None
            title, body = self.render_ci_failure_issue(failing)
            output = self.file_labelled_issue(title, body, CI_FAILURE_LABELS, provider)
        except (WorkerError, ValueError) as error:
            log(f"Could not check GitHub Actions; continuing with the issue queue: {error}")
            return None
        url = output.strip().splitlines()[-1] if output.strip() else ""
        match = re.search(r"/issues/([0-9]+)$", url)
        if not match:
            log(f"GitHub did not return an issue URL for the CI failure issue: {output.strip()!r}")
            return None
        log(f"Filed CI failure issue {url}.")
        return IssueContext(
            number=int(match.group(1)),
            title=title,
            body=body,
            labels=[label for label, _, _ in CI_FAILURE_LABELS],
            url=url,
            ci_monitor=True,
        )

    def run(self) -> int:
        with PidLock(self.lock_dir, "worker"):
            for executable, label in (
                (self.config.gh_bin, "gh"),
                (self.config.git_bin, "git"),
                (self.config.python_bin, "python3"),
            ):
                if not command_available(executable):
                    raise WorkerError(f"{label} is required but was not found in PATH")
            self.deliver_pending()
            self.reconcile_issue_pull_requests()
            self.reconcile_orphan_issue_branches()
            if self.prepare_paused_resume():
                return 0
            # A CI failure issue the monitor just filed is worked directly by
            # this run, so the regular queue must not also select it.
            self.issue = self.monitor_repository_actions() or self.select_issue()
            if not self.issue:
                return 0
            try:
                return self.run_selected_issue()
            except Exception as error:
                self.history.warning(str(error), iso_timestamp())
                self.finish_execution_history("failed")
                raise


def executable_default(name: str) -> str:
    return shutil.which(name) or ""


def _routing_tiers_from_args(raw: str) -> dict[str, tuple[Any, ...]]:
    try:
        return load_routing_tiers(raw)
    except ValueError as error:
        raise WorkerError(str(error)) from error


def build_parser() -> argparse.ArgumentParser:
    script_dir = SCRIPT_HOME
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", default=env_value("SWARM_REPO_DIR", str(script_dir.parent.parent)))
    parser.add_argument(
        "--state-dir",
        default=env_value("SWARM_ISSUE_WORKER_STATE_DIR", str(home / ".local/state/swarm-issue-worker")),
    )
    parser.add_argument(
        "--github-repository", default=env_value("SWARM_GITHUB_REPOSITORY", "DotNetRockStar/swarm")
    )
    parser.add_argument("--assignee", default=env_value("SWARM_GITHUB_ASSIGNEE", "DotNetRockStar"))
    parser.add_argument(
        "--trusted-followup-author",
        action="append",
        default=list(csv_values(env_value("SWARM_TRUSTED_FOLLOWUP_AUTHORS", env_value("SWARM_TRUSTED_FOLLOWUP_AUTHOR", "DotNetRockStar")))),
    )
    parser.add_argument(
        "--completion-author",
        action="append",
        default=list(csv_values(env_value("SWARM_COMPLETION_AUTHORS", "DotNetRockStar"))),
    )
    parser.add_argument(
        "--ready-label", default=env_value("SWARM_READY_FOR_TESTING_LABEL", "Ready For Testing")
    )
    parser.add_argument(
        "--minimum-remaining-percent",
        type=float,
        default=float(env_value("SWARM_MIN_REMAINING_PERCENT", "10")),
    )
    _provider_model_defaults = {
        "claude": "claude-sonnet-5",
        "codex": "gpt-5.6-luna",
        "grok": "grok-4.6",
    }
    _provider_effort_defaults = {
        "claude": "low",
        "codex": "medium",
        "grok": "low",
    }
    for _key in KNOWN_PROVIDER_KEYS:
        parser.add_argument(
            f"--{_key}-model",
            default=env_value(f"SWARM_{_key.upper()}_MODEL", _provider_model_defaults[_key]),
        )
        parser.add_argument(
            f"--{_key}-effort",
            default=env_value(f"SWARM_{_key.upper()}_EFFORT", _provider_effort_defaults[_key]),
        )
        parser.add_argument(
            f"--{_key}-router-model",
            default=env_value(f"SWARM_{_key.upper()}_ROUTER_MODEL", default_router_model(_key)),
        )
        parser.add_argument(
            f"--{_key}-router-effort",
            default=env_value(f"SWARM_{_key.upper()}_ROUTER_EFFORT", default_router_effort(_key)),
        )
        parser.add_argument(
            f"--{_key}-router-strengths",
            default=env_value(
                f"SWARM_{_key.upper()}_ROUTER_STRENGTHS", default_provider_strengths(_key)
            ),
            help=f"What {_key} is best at, weighed when the router picks an AI tool.",
        )
        parser.add_argument(
            f"--{_key}-bin",
            default=env_value(f"{_key.upper()}_BIN", executable_default(_key)),
        )
    parser.add_argument(
        "--enabled-provider",
        action="append",
        choices=KNOWN_PROVIDER_KEYS,
        default=list(csv_values(env_value("SWARM_ENABLED_PROVIDERS", ""))) or None,
        help="Provider id to include in the rotation (repeatable). Defaults to all known providers.",
    )
    parser.add_argument(
        "--preferred-provider",
        choices=(*KNOWN_PROVIDER_KEYS, PREFERRED_PROVIDER_AUTO),
        default=env_value("SWARM_PREFERRED_PROVIDER", "claude").lower(),
        help="Named tie-break provider, or 'auto' to always prefer the most usage remaining.",
    )
    parser.add_argument(
        "--dynamic-model-routing",
        action=argparse.BooleanOptionalAction,
        default=env_bool("SWARM_DYNAMIC_MODEL_ROUTING", False),
        help="Grade each new issue and choose the worker model from the routing tiers.",
    )
    parser.add_argument(
        "--routing-tiers",
        default=env_value("SWARM_ROUTING_TIERS", ""),
        help="JSON object of per-provider complexity tiers. Empty uses the built-in table.",
    )
    parser.add_argument(
        "--routing-optimization",
        choices=("cost", "best"),
        default=env_value("SWARM_ROUTING_OPTIMIZATION", DEFAULT_ROUTING_OPTIMIZATION).lower(),
        help="Whether dynamic routing favors the cheapest capable model or the best fit for the work.",
    )
    parser.add_argument(
        "--allow-usage-credit-models",
        action=argparse.BooleanOptionalAction,
        default=env_bool("SWARM_ALLOW_USAGE_CREDIT_MODELS", False),
        help="Offer models that bill against a separate usage-credit balance to the router.",
    )
    parser.add_argument("--dry-run", action="store_true", default=env_bool("SWARM_ISSUE_WORKER_DRY_RUN"))
    parser.add_argument("--gh-bin", default=env_value("GH_BIN", executable_default("gh")))
    parser.add_argument("--git-bin", default=env_value("GIT_BIN", executable_default("git")))
    parser.add_argument("--python-bin", default=env_value("PYTHON_BIN", executable_default("python3")))
    parser.add_argument(
        "--github-apps-config",
        default=env_value("SWARM_GITHUB_APPS_CONFIG", str(DEFAULT_CONFIG_PATH)),
    )
    parser.add_argument("--openssl-bin", default=env_value("OPENSSL_BIN", executable_default("openssl") or "openssl"))
    parser.add_argument(
        "--require-bot-auth",
        action=argparse.BooleanOptionalAction,
        default=env_bool("SWARM_REQUIRE_BOT_AUTH", True),
    )
    parser.add_argument(
        "--auto-approve",
        action=argparse.BooleanOptionalAction,
        default=env_bool("SWARM_AUTO_APPROVE", False),
    )
    parser.add_argument(
        "--auto-merge",
        action=argparse.BooleanOptionalAction,
        default=env_bool("SWARM_AUTO_MERGE", False),
    )
    parser.add_argument(
        "--auto-promote",
        action=argparse.BooleanOptionalAction,
        default=env_bool("SWARM_AUTO_PROMOTE", False),
    )
    parser.add_argument(
        "--monitor-actions",
        action=argparse.BooleanOptionalAction,
        default=env_bool("SWARM_MONITOR_ACTIONS", False),
    )
    parser.add_argument(
        "--require-issue-tests",
        action=argparse.BooleanOptionalAction,
        default=env_bool("SWARM_REQUIRE_ISSUE_TESTS", False),
    )
    parser.add_argument(
        "--adversarial-uat-enabled",
        action=argparse.BooleanOptionalAction,
        default=env_bool("SWARM_ADVERSARIAL_UAT_ENABLED", False),
    )
    parser.add_argument(
        "--allow-environment-only-summary",
        action=argparse.BooleanOptionalAction,
        default=env_bool("SWARM_ALLOW_ENVIRONMENT_ONLY_SUMMARY", False),
    )
    parser.add_argument("--branch-prefix", default=env_value("SWARM_BRANCH_PREFIX", "ai"))
    parser.add_argument("--base-branch", default=env_value("SWARM_BASE_BRANCH", "main"))
    parser.add_argument(
        "--integration-branch",
        default=env_value("SWARM_INTEGRATION_BRANCH", "ai-main"),
    )
    parser.add_argument("--remote-name", default=env_value("SWARM_GIT_REMOTE", "origin"))
    parser.add_argument("--github-host", default=env_value("SWARM_GITHUB_HOST", "github.com"))
    parser.add_argument(
        "--ai-execution-history-enabled",
        action=argparse.BooleanOptionalAction,
        default=env_bool("SWARM_AI_EXECUTION_HISTORY_ENABLED", False),
    )
    parser.add_argument(
        "--prompt-feedback-upload-enabled",
        action=argparse.BooleanOptionalAction,
        default=env_bool("SWARM_PROMPT_FEEDBACK_UPLOAD_ENABLED", False),
    )
    parser.add_argument(
        "--application-version",
        default=env_value("SWARM_APPLICATION_VERSION", "development"),
    )
    parser.add_argument(
        "--execution-history-db",
        default=env_value("SWARM_AI_EXECUTION_HISTORY_DB", ""),
        help="Shared application SQLite path; defaults beneath --state-dir.",
    )
    return parser


def resolve_preferred_provider(preferred: str, enabled: Iterable[str]) -> str:
    """Keep ``auto``. A named provider that is not enabled falls back to the
    first known provider that is."""
    preferred = preferred.lower()
    enabled_set = set(enabled)
    if preferred == PREFERRED_PROVIDER_AUTO or preferred in enabled_set:
        return preferred
    return next(key for key in KNOWN_PROVIDER_KEYS if key in enabled_set)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 <= args.minimum_remaining_percent <= 100:
        raise WorkerError("--minimum-remaining-percent must be between 0 and 100")
    if args.base_branch == args.integration_branch:
        raise WorkerError("--integration-branch must differ from --base-branch")
    enabled = set(args.enabled_provider or KNOWN_PROVIDER_KEYS)
    if not enabled:
        raise WorkerError("At least one --enabled-provider is required")
    resolved_preferred = resolve_preferred_provider(args.preferred_provider, enabled)
    if resolved_preferred != args.preferred_provider:
        log(
            f"Preferred provider '{args.preferred_provider}' is not enabled; "
            f"using '{resolved_preferred}' as the tie-breaker."
        )
        args.preferred_provider = resolved_preferred
    config = Config.from_args(args)
    return Worker(config).run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (WorkerError, OSError, ValueError, json.JSONDecodeError) as error:
        log(f"ERROR: {error}")
        raise SystemExit(1)
