#!/usr/bin/env python3
"""What's wrong? — a read-only diagnostic explainer for the SWARM issue worker.

Triggered by the "What's wrong?" button on the desktop app's Info & Debug
page. Never commits, pushes, branches, or otherwise changes a managed
repository; the one exception is ``--file-issue``, which posts a single,
already-generated diagnosis as a new GitHub issue in swarm-automation's own
repository, unassigned, only when the caller explicitly asks for that
specific, previously-computed problem by id.

Flow (see the module functions for detail):
  1. Gather deterministic evidence for every repo in ``--repos-file`` (log
     tails, state files, and — only when the symptom looks
     environment/network-shaped — a small fixed set of read-only checks: a
     retried `git ls-remote` and the GitHub status API), plus any scheduler
     log lines not attributable to a specific repo, attributed to the app
     itself.
  2. Resolve what can be resolved for free: known literal patterns, or a
     fresh cached explanation of the same problem from a past run.
  3. Whatever is left goes into exactly one capacity probe + one dynamic
     routing decision + one schema-constrained one-shot model call —
     reusing the worker's real routing algorithm and provider quota pool,
     never a separate budget or a hardcoded cheap model.
  4. Persist every problem (canned, cached, or freshly explained) to
     ``diagnostic_explanations`` and print one JSON object to stdout.

Only the final JSON object may reach real stdout: everything the reused
Worker/router machinery itself prints via ``log()`` is captured and
discarded (kept only in the persisted evidence on error) so a caller
parsing stdout as JSON is never broken by an incidental log line.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from ai_execution_history import sanitize_text
from diagnostic_store import DiagnosticProblem, DiagnosticRepository
from dynamic_router import RouterCandidate, RouterError, build_router_prompt, run_provider_oneshot
from swarm_issue_worker import Config, Worker, build_parser, iso_timestamp, run_command

APP_REPOSITORY = "swarm-automation-app"
LOG_TAIL_LINES = 4000
EVIDENCE_LOG_LINES = 40
DEFAULT_CACHE_SECONDS = 900
NETWORK_PROBE_ATTEMPTS = 3
GITHUB_STATUS_URL = "https://www.githubstatus.com/api/v2/status.json"

DIAGNOSTIC_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "repository": {"type": "string"},
                    "explanation": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "actionable_items": {"type": "array", "items": {"type": "string"}},
                    "is_bug": {"type": "boolean"},
                    "suggested_issue_title": {"type": "string"},
                    "suggested_issue_body": {"type": "string"},
                },
                "required": [
                    "repository", "explanation", "confidence", "actionable_items",
                    "is_bug", "suggested_issue_title", "suggested_issue_body",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["problems"],
    "additionalProperties": False,
}

# (pattern, explanation, actionable_items, confidence). Checked in order;
# the first match resolves the problem without spending a model call.
# These are conditions the worker's own log lines already name explicitly,
# so pattern-matching them is not a guess.
CANNED_PATTERNS: tuple[tuple[re.Pattern[str], str, tuple[str, ...], str], ...] = (
    (
        re.compile(r"quota_paused|no longer has sufficient usage", re.I),
        "This repository is paused because its AI provider ran out of usage for now. "
        "The worker will resume automatically once usage is available again.",
        ("Wait for the provider's usage window to reset.",
         "Or enable a different provider with remaining capacity."),
        "high",
    ),
    (
        re.compile(r"Permission denied \(publickey\)", re.I),
        "A git-over-SSH operation failed to authenticate. In this app this has so far always "
        "turned out to be a transient GitHub-side hiccup rather than a real key or permission "
        "problem — retrying the same operation moments later succeeds.",
        ("Retry the action — if it succeeds on a retry, it was transient and needs no fix.",
         "If it keeps failing, check https://www.githubstatus.com and confirm "
         "`ssh -T git@github.com` succeeds from a terminal."),
        "medium",
    ),
)

_NETWORK_HEURISTIC = re.compile(
    r"Permission denied \(publickey\)|Could not resolve host|Could not read from remote|"
    r"timed out|Connection refused|SSL certificate|fatal: unable to access|"
    r"5\d\d\b.*github|github.*5\d\d\b",
    re.I,
)
_SUCCESS_MARKERS = ("Committed completed issue", "no issue to work right now", "Finished issue")


def tail_lines(path: Path, limit: int) -> list[str]:
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-limit:]


def repo_log_lines(lines: list[str], label: str) -> list[str]:
    """Lines in a combined scheduler log that belong to one repo's bracket label."""
    marker = f"[{label}]"
    return [line for line in lines if marker in line]


def recent_errors(lines: list[str]) -> list[str]:
    """``ERROR:`` lines since the most recent success/idle marker, oldest first.

    A later success clears earlier noise: a repo that failed once and then
    recovered is not an active problem.
    """
    errors: list[str] = []
    for line in lines:
        if "ERROR:" in line:
            errors.append(line)
        elif any(marker in line for marker in _SUCCESS_MARKERS):
            errors = []
    return errors


def looks_network_shaped(text: str) -> bool:
    return bool(_NETWORK_HEURISTIC.search(text))


def problem_signature(repository: str, errors: list[str]) -> str:
    """Stable id for "this same problem", independent of timestamps.

    Only the error text (with obvious timestamps/hex ids stripped) feeds the
    hash, so the identical failure recurring across retries produces the
    same signature and can be cached/deduped.
    """
    normalized = "\n".join(
        re.sub(r"[0-9a-f]{7,40}|\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\S*", "", line)
        for line in errors
    )
    digest = hashlib.sha256(f"{repository}:{normalized}".encode("utf-8")).hexdigest()
    return digest[:20]


def fetch_github_status(timeout: float = 5.0) -> str:
    with urllib.request.urlopen(GITHUB_STATUS_URL, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return str(payload.get("status", {}).get("description", "unknown"))


def run_network_probe(worker: Worker, attempts: int = NETWORK_PROBE_ATTEMPTS) -> str:
    """Fixed, read-only checks: does a retried git operation actually keep
    failing, and is GitHub itself reporting a problem? This is the same
    legwork a human would do by hand before concluding "the key is broken"
    versus "GitHub had a blip" — never a live, model-directed shell.
    """
    lines: list[str] = []
    successes = 0
    for attempt in range(1, attempts + 1):
        result = run_command(
            [worker.config.git_bin, "-C", worker.config.repo_dir,
             "ls-remote", "--exit-code", worker.config.remote_name],
            check=False,
        )
        ok = result.returncode == 0
        successes += int(ok)
        detail = "" if ok else f" ({(result.stderr or result.stdout or '').strip().splitlines()[-1:]})"
        lines.append(f"attempt {attempt}/{attempts}: git ls-remote {'succeeded' if ok else 'failed'}{detail}")
    verdict = f"{successes}/{attempts} attempts succeeded"
    if 0 < successes < attempts:
        verdict += " — intermittent, not a persistent failure."
    elif successes == 0:
        verdict += " — every attempt failed."
    lines.append(verdict)
    try:
        lines.append(f"githubstatus.com reports: {fetch_github_status()}")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        lines.append(f"githubstatus.com check could not be completed: {error}")
    return "\n".join(lines)


def canned_resolution(errors: list[str]) -> tuple[str, tuple[str, ...], str] | None:
    text = "\n".join(errors)
    for pattern, explanation, actionable_items, confidence in CANNED_PATTERNS:
        if pattern.search(text):
            return explanation, actionable_items, confidence
    return None


def unbracketed_lines(lines: list[str], labels: list[str]) -> list[str]:
    """Lines that don't belong to any configured repo's bracket label —
    scheduler-level output attributable to the app itself, not one repo."""
    markers = [f"[{label}]" for label in labels]
    return [line for line in lines if not any(marker in line for marker in markers)]


def gather_app_candidate(log_lines: list[str], labels: list[str]) -> dict[str, Any] | None:
    """Deterministic evidence for scheduler-level problems not attributable
    to any one configured repo. ``None`` when nothing is wrong. Unlike
    ``gather_candidate`` there is no specific repo Worker here, so evidence
    is log-only — no state files, no network probe.
    """
    lines = unbracketed_lines(log_lines, labels)
    errors = recent_errors(lines)
    if not errors:
        return None
    evidence: list[dict[str, str]] = [{"source": "log", "excerpt": "\n".join(lines[-EVIDENCE_LOG_LINES:])}]
    return {
        "repository": APP_REPOSITORY,
        "signature": problem_signature(APP_REPOSITORY, errors),
        "errors": errors,
        "evidence": evidence,
    }


def gather_candidate(label: str, repository: str, worker: Worker, log_lines: list[str]) -> dict[str, Any] | None:
    """Deterministic evidence for one repo/app. ``None`` when nothing is wrong."""
    lines = repo_log_lines(log_lines, label)
    errors = recent_errors(lines)
    if not errors:
        return None
    evidence: list[dict[str, str]] = [{"source": "log", "excerpt": "\n".join(lines[-EVIDENCE_LOG_LINES:])}]
    if worker.in_progress_file.exists():
        try:
            state = worker.read_state()
        except (OSError, ValueError):
            state = {}
        if state:
            evidence.append({"source": "in-progress-issue.json", "excerpt": json.dumps(state, indent=2)[:4000]})
        for path, name in ((worker.ai_output_file, "last-ai-output.log"),
                            (worker.ai_diagnostic_file, "last-ai-diagnostic.log")):
            tail = tail_lines(path, limit=60)
            if tail:
                evidence.append({"source": name, "excerpt": "\n".join(tail)})
    if looks_network_shaped("\n".join(errors)):
        evidence.append({"source": "network probe", "excerpt": run_network_probe(worker)})
    return {
        "repository": repository,
        "signature": problem_signature(repository, errors),
        "errors": errors,
        "evidence": evidence,
    }


def resolve_canned_or_cached(
    candidate: dict[str, Any], store: DiagnosticRepository, cache_seconds: int
) -> DiagnosticProblem | None:
    canned = canned_resolution(candidate["errors"])
    if canned:
        explanation, actionable_items, confidence = canned
        return DiagnosticProblem(
            repository=candidate["repository"], signature=candidate["signature"], source="canned",
            explanation=explanation, confidence=confidence, evidence=tuple(candidate["evidence"]),
            actionable_items=actionable_items, is_bug=False,
        )
    cached = store.find_by_signature(candidate["signature"], max_age_seconds=cache_seconds)
    if cached:
        return DiagnosticProblem(
            repository=cached["repository"], signature=cached["signature"], source="cache",
            provider=cached.get("provider", ""), model=cached.get("model", ""),
            explanation=cached["explanation"], confidence=cached["confidence"],
            evidence=tuple(candidate["evidence"]), actionable_items=tuple(cached["actionable_items"]),
            is_bug=cached["is_bug"], suggested_issue_title=cached.get("suggested_issue_title", ""),
            suggested_issue_body=cached.get("suggested_issue_body", ""),
        )
    return None


def select_provider(app_worker: Worker) -> tuple[Any, dict[str, float | None]] | tuple[None, dict[str, float | None]]:
    """Same capacity probe every other one-shot caller uses: usable providers,
    most usage remaining first. Returns ``(None, remaining)`` — the existing
    "nothing usable right now" convention — when no provider has capacity.
    """
    usages = {spec.name: app_worker.provider_usage(spec.key) for spec in app_worker.config.enabled_specs}
    remaining = {name: usage.remaining_percent for name, usage in usages.items() if usage.usable}
    choice = app_worker.choose_provider("", remaining)
    return choice, remaining


def synthesize(
    app_worker: Worker, choice: Any, remaining: dict[str, float | None], candidates_needing_ai: list[dict[str, Any]]
) -> dict[str, Any]:
    """One routing decision plus one schema-constrained one-shot call
    explaining every still-unresolved problem at once.
    """
    host = app_worker.config.require_spec(choice.key)
    router_candidates = [
        RouterCandidate(key=spec.key, name=spec.name, tiers=app_worker.config.routing_tiers.get(spec.key, ()),
                         strengths=spec.strengths, usage_remaining=remaining.get(spec.name))
        for spec in app_worker.config.enabled_specs
    ]
    evidence_text = "\n\n".join(
        f"### {item['repository']} — error(s):\n" + "\n".join(item["errors"]) + "\n\nEvidence:\n"
        + "\n\n".join(f"[{e['source']}]\n{e['excerpt']}" for e in item["evidence"])
        for item in candidates_needing_ai
    )
    title = "Diagnose the current SWARM issue-worker problem(s)"
    body = (
        "You are explaining, in plain language, what is currently wrong with the SWARM issue "
        "worker to someone who did not write this codebase. You are given every piece of "
        "evidence that was gathered; do not ask for more, do not suggest running additional "
        "commands or investigation — respond only from what is below.\n\n"
        "For each repository below, decide: is this a genuine defect in the swarm-automation "
        "worker itself (is_bug: true) or something else (a transient/external condition, "
        "expected behavior, user-actionable configuration) (is_bug: false)? "
        "suggested_issue_title/suggested_issue_body only need to be meaningful when is_bug is "
        "true, and the body must stand alone for someone unfamiliar with this session to "
        "triage.\n\n" + evidence_text
    )
    prompt = build_router_prompt(
        title=title, body=body, labels=[], candidates=router_candidates,
        routing_optimization=app_worker.config.routing_optimization,
        allow_usage_credit_models=app_worker.config.allow_usage_credit_models,
    )
    try:
        raw = app_worker.run_router(host, prompt, [])
        decision = app_worker.resolve_router_response(
            raw, prompt=prompt, candidates=router_candidates, host=host, images=[],
            previous_provider="", rework=False,
        )
    except RouterError as error:
        raise DiagnosisUnavailable(f"routing failed: {error}") from error
    spec = app_worker.config.spec(decision["provider"])
    if spec is None:
        raise DiagnosisUnavailable(f"router selected an unknown provider: {decision['provider']}")
    synthesis_prompt = (
        f"{body}\n\nReturn only the JSON object described by the schema: one entry per repository "
        "listed above, in the same order."
    )
    try:
        raw_response = run_provider_oneshot(
            provider=spec.key, bin_path=spec.bin or "", model=decision["selected_model"],
            effort=decision["reasoning_effort"], prompt=synthesis_prompt, cwd=app_worker.config.repo_dir,
            schema=DIAGNOSTIC_RESPONSE_SCHEMA,
        )
    except RouterError as error:
        raise DiagnosisUnavailable(f"synthesis call failed: {error}") from error
    try:
        payload = json.loads(raw_response)
        problems = payload["problems"]
        if not isinstance(problems, list):
            raise ValueError("problems must be a list")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise DiagnosisUnavailable(f"synthesis response was not valid: {error}") from error
    return {"provider": spec.name, "model": decision["selected_model"], "problems": problems}


class DiagnosisUnavailable(RuntimeError):
    """The AI synthesis step could not run or did not return a usable result."""


def build_worker(entry: dict[str, Any], extra_argv: list[str]) -> Worker:
    args = build_parser().parse_args([*[str(v) for v in entry.get("worker_args", [])], *extra_argv])
    return Worker(Config.from_args(args))


def diagnose(
    repos_file: Path, app_log: Path, cron_log: Path,
    extra_argv: list[str], cache_seconds: int = DEFAULT_CACHE_SECONDS,
) -> dict[str, Any]:
    entries = json.loads(repos_file.read_text(encoding="utf-8")) if repos_file.is_file() else []
    log_lines = tail_lines(app_log, LOG_TAIL_LINES) or tail_lines(cron_log, LOG_TAIL_LINES)

    with contextlib.redirect_stdout(io.StringIO()):
        workers: list[tuple[str, str, Worker]] = []
        for entry in entries:
            worker = build_worker(entry, extra_argv)
            workers.append((str(entry.get("label") or worker.config.repo_dir), worker.config.github_repository, worker))
        probe_worker = workers[0][2] if workers else None

        db_path = probe_worker.config.execution_history_db if probe_worker else None
        store = DiagnosticRepository(db_path) if db_path else None

        candidates: list[dict[str, Any]] = []
        for label, repository, worker in workers:
            candidate = gather_candidate(label, repository, worker, log_lines)
            if candidate:
                candidates.append(candidate)
        app_candidate = gather_app_candidate(log_lines, [label for label, _, _ in workers])
        if app_candidate:
            candidates.append(app_candidate)

        run_id = str(uuid.uuid4())
        now = iso_timestamp()
        resolved: list[DiagnosticProblem] = []
        needing_ai: list[dict[str, Any]] = []
        for candidate in candidates:
            found = resolve_canned_or_cached(candidate, store, cache_seconds) if store else None
            if found:
                resolved.append(found)
            else:
                needing_ai.append(candidate)

        unavailable_reason: str | None = None
        if needing_ai:
            if probe_worker is None:
                unavailable_reason = "No repository is configured."
            else:
                choice, remaining = select_provider(probe_worker)
                if choice is None:
                    unavailable_reason = "No AI provider currently has usage remaining."
                    for candidate in needing_ai:
                        resolved.append(DiagnosticProblem(
                            repository=candidate["repository"], signature=candidate["signature"],
                            source="unavailable", evidence=tuple(candidate["evidence"]),
                        ))
                else:
                    try:
                        synthesis = synthesize(probe_worker, choice, remaining, needing_ai)
                    except DiagnosisUnavailable as error:
                        unavailable_reason = str(error)
                        for candidate in needing_ai:
                            resolved.append(DiagnosticProblem(
                                repository=candidate["repository"], signature=candidate["signature"],
                                source="unavailable", evidence=tuple(candidate["evidence"]),
                            ))
                    else:
                        by_repo = {candidate["repository"]: candidate for candidate in needing_ai}
                        for item in synthesis["problems"]:
                            candidate = by_repo.get(item.get("repository", ""))
                            signature = candidate["signature"] if candidate else problem_signature(
                                str(item.get("repository", "")), []
                            )
                            evidence = tuple(candidate["evidence"]) if candidate else ()
                            resolved.append(DiagnosticProblem(
                                repository=str(item.get("repository", "")), signature=signature, source="ai",
                                provider=synthesis["provider"], model=synthesis["model"],
                                explanation=str(item.get("explanation", "")),
                                confidence=str(item.get("confidence", "")), evidence=evidence,
                                actionable_items=tuple(item.get("actionable_items", [])),
                                is_bug=bool(item.get("is_bug", False)),
                                suggested_issue_title=str(item.get("suggested_issue_title", "")),
                                suggested_issue_body=str(item.get("suggested_issue_body", "")),
                            ))

        problems_out: list[dict[str, Any]] = []
        if store:
            for problem in resolved:
                problem_id = store.insert(run_id, now, problem)
                problems_out.append({
                    "problem_id": problem_id, "repository": problem.repository, "source": problem.source,
                    "provider": problem.provider or None, "model": problem.model or None,
                    "explanation": problem.explanation, "confidence": problem.confidence,
                    "evidence": list(problem.evidence), "actionable_items": list(problem.actionable_items),
                    "is_bug": problem.is_bug,
                    "suggested_issue_title": problem.suggested_issue_title or None,
                    "suggested_issue_body": problem.suggested_issue_body or None,
                })

    return {
        "ai_available": unavailable_reason is None,
        "run_id": run_id,
        "generated_at": now,
        "problems": problems_out,
        "unavailable_reason": unavailable_reason,
    }


def file_diagnostic_issue(store: DiagnosticRepository, problem_id: str, worker: Worker) -> dict[str, Any]:
    record = store.get(problem_id)
    if record is None:
        raise ValueError(f"No diagnostic problem found with id {problem_id}")
    if not record["is_bug"]:
        raise ValueError("This problem was not identified as a swarm-automation bug.")
    if record.get("filed_issue_url"):
        return {"filed_issue_url": record["filed_issue_url"], "already_filed": True}
    marker = f"<!-- swarm-diagnose:signature:{record['signature']} -->"
    existing = json.loads(worker.github.gh(
        ["issue", "list", "--repo", "SWARM-Media-Steaming/swarm-automation", "--state", "all",
         "--search", f'"{record["signature"]}" in:body', "--json", "url,body", "--limit", "20"],
        None,
    ))
    for item in existing:
        if marker in item.get("body", ""):
            store.mark_filed(problem_id, item["url"], iso_timestamp())
            return {"filed_issue_url": item["url"], "already_filed": True}
    body = f"{marker}\n{record['suggested_issue_body']}\n\nRepository affected: {record['repository']}"
    url = worker.file_labelled_issue(
        title=record["suggested_issue_title"] or "Diagnosed swarm-automation problem",
        body=body,
        labels=(("bug", "d73a4a", "Something is not working"),
                ("self-diagnosed", "0e8a16", "Filed by the What's wrong? diagnostic")),
        provider="claude",
        assignee="",
        repo="SWARM-Media-Steaming/swarm-automation",
    )
    store.mark_filed(problem_id, url, iso_timestamp())
    return {"filed_issue_url": url, "already_filed": False}


def build_diagnose_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repos-file", required=True, type=Path)
    parser.add_argument("--app-log", type=Path, default=None)
    parser.add_argument("--cron-log", type=Path, default=None)
    parser.add_argument("--cache-seconds", type=int, default=DEFAULT_CACHE_SECONDS)
    parser.add_argument("--file-issue", action="store_true")
    parser.add_argument("--problem-id", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_diagnose_parser()
    args, extra_argv = parser.parse_known_args(argv)

    if args.file_issue:
        if not args.problem_id:
            print(json.dumps({"error": "--file-issue requires --problem-id"}))
            return 1
        entries = json.loads(args.repos_file.read_text(encoding="utf-8")) if args.repos_file.is_file() else []
        if not entries:
            print(json.dumps({"error": "no repositories configured"}))
            return 1
        with contextlib.redirect_stdout(io.StringIO()):
            worker = build_worker(entries[0], extra_argv)
            store = DiagnosticRepository(worker.config.execution_history_db)
            try:
                result = file_diagnostic_issue(store, args.problem_id, worker)
            except ValueError as error:
                print(json.dumps({"error": sanitize_text(str(error))}))
                return 1
        print(json.dumps(result))
        return 0

    result = diagnose(
        repos_file=args.repos_file,
        app_log=args.app_log or Path(),
        cron_log=args.cron_log or Path(),
        extra_argv=extra_argv,
        cache_seconds=args.cache_seconds,
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
