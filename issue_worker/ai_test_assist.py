#!/usr/bin/env python3
"""Best-effort AI helpers for the two gaps the deterministic test scheduler
cannot fill on its own: finding test entry points a conventional-manifest scan
cannot see, and generating placeholder data for a suite that says it needs
some. Every other part of the test scheduler (discovery via manifests, running
suites, recording results) stays deterministic and never reaches this file.

Invoked by the Rust test runner (``testing.rs``) as a subprocess, one call per
need, printing a single JSON object to stdout. It is never given write access
to the repository and never executes a suggested command — only the human
reviewing a definition draft, or the repository's own suite commands, do that.

The capacity-probing logic below intentionally mirrors
``swarm_issue_worker.Worker.claude_usage`` / ``codex_usage`` / ``grok_usage``.
It is a separate, parallel implementation (this script has no GitHub/issue
context to construct a ``Worker`` from) — keep the two in sync if either
provider's usage output format changes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_HOME = Path(os.environ.get("SWARM_ISSUE_WORKER_SCRIPT_DIR", Path(__file__).resolve().parent)).resolve()
if str(SCRIPT_HOME) not in sys.path:
    sys.path.insert(0, str(SCRIPT_HOME))

from swarm_issue_worker import command_available  # noqa: E402  (reuse the tested primitive)

# Directories a shallow repository listing never needs to descend into when
# looking for hints about how tests are run.
IGNORED_DIRECTORY_NAMES = {
    ".git", "node_modules", "target", "dist", "build", ".venv", "venv",
    "__pycache__", ".next", ".cache", "vendor",
}


def _run(command: list[str], *, input_text: str | None = None, timeout: float = 30.0) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        return 1, str(error)
    return result.returncode, (result.stdout or result.stderr or "")


def claude_capacity(bin_path: str, minimum_remaining_percent: float) -> dict[str, Any]:
    if not command_available(bin_path):
        return {"available": False, "detail": "claude was not found on PATH"}
    returncode, stdout = _run(
        [bin_path, "-p", "/usage", "--output-format", "json", "--tools", "", "--no-session-persistence"],
        timeout=30,
    )
    if returncode != 0:
        return {"available": False, "detail": "Claude Code's /usage command failed"}
    try:
        usage = str(json.loads(stdout).get("result") or "")
    except json.JSONDecodeError:
        usage = ""
    session = re.search(r"^Current session:\s*([0-9.]+)% used", usage, re.MULTILINE)
    week = re.search(r"^Current week(?: \([^)]*\))?:\s*([0-9.]+)% used", usage, re.MULTILINE)
    if not session or not week:
        return {"available": False, "detail": "Claude Code returned an unrecognized /usage format"}
    remaining = min(100 - float(session.group(1)), 100 - float(week.group(1)))
    detail = f"session {100 - float(session.group(1)):g}% / week {100 - float(week.group(1)):g}% remaining"
    return {"available": remaining >= minimum_remaining_percent, "detail": detail}


def codex_capacity(bin_path: str, minimum_remaining_percent: float, python_bin: str, script_dir: str) -> dict[str, Any]:
    if not command_available(bin_path):
        return {"available": False, "detail": "codex was not found on PATH"}
    rate_limits_script = Path(script_dir) / "codex_rate_limits.py"
    if not rate_limits_script.is_file():
        return {"available": False, "detail": "the Codex rate-limit helper is missing from this build"}
    returncode, stdout = _run(
        [python_bin, str(rate_limits_script), "--codex-bin", bin_path, "--timeout", "30"],
        timeout=35,
    )
    if returncode != 0:
        return {"available": False, "detail": "Codex's local rate-limit check failed"}
    try:
        limits = json.loads(stdout)
        windows = [limits.get(key) for key in ("primary", "secondary") if limits.get(key) is not None]
        used = [float(window["usedPercent"]) for window in windows]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {"available": False, "detail": "Codex's rate-limit response was invalid"}
    if not used:
        return {"available": False, "detail": "Codex's rate-limit response had no active windows"}
    remaining = min(100 - amount for amount in used)
    available = (
        limits.get("rateLimitReachedType") is None
        and not bool(limits.get("spendControlReached", False))
        and remaining >= minimum_remaining_percent
    )
    return {"available": available, "detail": f"{remaining:g}% remaining"}


def grok_capacity(
    bin_path: str, minimum_remaining_percent: float, python_bin: str, script_dir: str
) -> dict[str, Any]:
    if not command_available(bin_path):
        return {"available": False, "detail": "grok was not found on PATH"}
    home = Path(os.environ.get("HOME", "~")).expanduser()
    if not (home / ".grok" / "auth.json").is_file():
        if os.environ.get("XAI_API_KEY"):
            return {"available": True, "detail": "API-key billing; no account allowance to check"}
        return {"available": False, "detail": "not signed in to Grok"}
    rate_limits_script = Path(script_dir) / "grok_rate_limits.py"
    if not rate_limits_script.is_file():
        return {"available": False, "detail": "the Grok usage helper is missing from this build"}
    returncode, stdout = _run(
        [python_bin, str(rate_limits_script), "--grok-bin", bin_path, "--timeout", "30"],
        timeout=35,
    )
    if returncode != 0:
        return {"available": False, "detail": "Grok's local usage check failed"}
    try:
        limits = json.loads(stdout)
        used = float(limits["usedPercent"])
        period = str(limits.get("period") or "period")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {"available": False, "detail": "Grok's usage response was invalid"}
    remaining = max(0.0, min(100.0, 100 - used))
    return {
        "available": remaining >= minimum_remaining_percent,
        "detail": f"{period} {remaining:g}% remaining",
    }


def pick_provider(
    providers: list[dict[str, Any]],
    minimum_remaining_percent: float,
    python_bin: str,
    script_dir: str,
) -> dict[str, Any]:
    """First enabled provider (in caller-supplied order) with capacity."""
    reasons = []
    for provider in providers:
        if not provider.get("enabled") or not provider.get("bin"):
            continue
        key = provider.get("id")
        if key == "claude":
            usage = claude_capacity(provider["bin"], minimum_remaining_percent)
        elif key == "codex":
            usage = codex_capacity(provider["bin"], minimum_remaining_percent, python_bin, script_dir)
        elif key == "grok":
            usage = grok_capacity(provider["bin"], minimum_remaining_percent, python_bin, script_dir)
        else:
            continue
        if usage["available"]:
            return {"available": True, "provider": key, "detail": usage["detail"]}
        reasons.append(f"{key}: {usage['detail']}")
    detail = "; ".join(reasons) if reasons else "no enabled AI provider is configured"
    return {"available": False, "provider": None, "detail": detail}


def generate(provider_id: str, bin_path: str, model: str, prompt: str, timeout: float) -> dict[str, Any]:
    """One-shot, tool-free text completion — the same stateless call shape
    ``claude_capacity`` uses for ``/usage``. Only Claude Code is wired up
    today; Codex and Grok's one-shot invocation shapes have not been
    reviewed for this read-only, no-tools use case, so they report
    unavailable rather than guessing at an unreviewed command line."""
    if provider_id != "claude":
        return {"ok": False, "error": f"AI gap-filling is not implemented for '{provider_id}' yet"}
    if not command_available(bin_path):
        return {"ok": False, "error": "claude was not found on PATH"}
    command = [bin_path, "-p", prompt, "--output-format", "json", "--tools", "", "--no-session-persistence"]
    if model:
        command[1:1] = ["--model", model]
    returncode, stdout = _run(command, timeout=timeout)
    if returncode != 0:
        return {"ok": False, "error": "Claude Code did not respond"}
    try:
        text = str(json.loads(stdout).get("result") or "").strip()
    except json.JSONDecodeError:
        text = ""
    if not text:
        return {"ok": False, "error": "Claude Code returned no output"}
    return {"ok": True, "text": text}


def shallow_listing(workspace: Path, max_entries: int = 200) -> str:
    """A depth-limited directory listing used to give the discovery prompt
    just enough shape to spot custom test entry points, without reading file
    contents or descending into dependency/build directories."""
    lines: list[str] = []

    def walk(directory: Path, depth: int) -> None:
        if len(lines) >= max_entries or depth > 2:
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            if len(lines) >= max_entries:
                return
            if entry.name.startswith(".") and entry.name not in {".swarm"}:
                continue
            relative = entry.relative_to(workspace)
            if entry.is_dir():
                if entry.name in IGNORED_DIRECTORY_NAMES:
                    continue
                lines.append(f"{relative}/")
                walk(entry, depth + 1)
            else:
                lines.append(str(relative))

    walk(workspace, 0)
    return "\n".join(lines)


DISCOVERY_PROMPT_TEMPLATE = """A desktop app is looking for how automated tests are run in this repository. \
Conventional checks (Cargo.toml, package.json test script, pytest/tox config, go.mod, Gradle wrappers, a \
scripts/tests/ directory) found nothing. Here is a shallow listing of the repository (depth 2, common \
dependency/build directories excluded):

{listing}

From file and directory names alone (you cannot read file contents or run anything), suggest any commands \
that plausibly run this repository's tests — for example a Makefile "test" target, a documented CI command, \
or a test runner implied by the project layout. If nothing plausible stands out, suggest nothing.

Respond with ONLY a single JSON object, no other text, matching this shape:
{{"suites": [{{"id": "short-kebab-id", "name": "Human name", "command": ["argv0", "arg1"], \
"timeoutSeconds": 1800, "disruptive": false}}], "notes": ["short caveat strings, if any"]}}
Use an empty "suites" array if you found nothing plausible.
"""


def discover(workspace: str, provider_id: str, bin_path: str, model: str, timeout: float) -> dict[str, Any]:
    listing = shallow_listing(Path(workspace))
    if not listing.strip():
        return {"ok": True, "suites": [], "notes": []}
    prompt = DISCOVERY_PROMPT_TEMPLATE.format(listing=listing)
    result = generate(provider_id, bin_path, model, prompt, timeout)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "AI-assisted discovery failed")}
    text = result["text"].strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1 :] if "\n" in text else text
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "error": "AI-assisted discovery returned invalid JSON"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "AI-assisted discovery returned an unexpected shape"}
    suites = payload.get("suites")
    notes = payload.get("notes")
    valid_suites = []
    if isinstance(suites, list):
        for suite in suites:
            if not isinstance(suite, dict):
                continue
            command = suite.get("command")
            if not isinstance(command, list) or not command or not all(isinstance(part, str) and part.strip() for part in command):
                continue
            valid_suites.append(
                {
                    "id": str(suite.get("id") or suite.get("name") or "ai-suggested"),
                    "name": str(suite.get("name") or suite.get("id") or "AI-suggested suite"),
                    "command": command,
                    "timeoutSeconds": suite.get("timeoutSeconds") if isinstance(suite.get("timeoutSeconds"), int) else 1800,
                    "disruptive": bool(suite.get("disruptive", False)),
                }
            )
    valid_notes = [str(note) for note in notes if isinstance(note, str)] if isinstance(notes, list) else []
    return {"ok": True, "suites": valid_suites, "notes": valid_notes}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    capacity = subparsers.add_parser("capacity")
    capacity.add_argument("--providers-json", required=True)
    capacity.add_argument("--minimum-percent", type=float, required=True)
    capacity.add_argument("--python-bin", required=True)
    capacity.add_argument("--script-dir", required=True)

    generate_parser = subparsers.add_parser("generate")
    generate_parser.add_argument("--provider", required=True)
    generate_parser.add_argument("--bin", required=True)
    generate_parser.add_argument("--model", default="")
    generate_parser.add_argument("--prompt", required=True)
    generate_parser.add_argument("--timeout", type=float, default=120.0)

    discover_parser = subparsers.add_parser("discover")
    discover_parser.add_argument("--workspace", required=True)
    discover_parser.add_argument("--provider", required=True)
    discover_parser.add_argument("--bin", required=True)
    discover_parser.add_argument("--model", default="")
    discover_parser.add_argument("--timeout", type=float, default=90.0)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "capacity":
        providers = json.loads(args.providers_json)
        payload = pick_provider(providers, args.minimum_percent, args.python_bin, args.script_dir)
    elif args.command == "generate":
        payload = generate(args.provider, args.bin, args.model, args.prompt, args.timeout)
    elif args.command == "discover":
        payload = discover(args.workspace, args.provider, args.bin, args.model, args.timeout)
    else:  # pragma: no cover - argparse enforces valid choices
        return 2
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
