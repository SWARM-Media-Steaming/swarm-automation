"""Optional dynamic model routing for one GitHub issue.

The router grades the original issue and scores its complexity. The worker
model and reasoning effort then come from the configured tier table for that
score. The original issue text is never rewritten. Keep the default tier
tables in sync with ``default_routing_tiers`` in ``src/config.rs``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


PROMPT_GRADES = (
    "A+",
    "A",
    "A-",
    "B+",
    "B",
    "B-",
    "C+",
    "C",
    "C-",
    "D+",
    "D",
    "D-",
    "F",
)
RISK_LEVELS = ("low", "medium", "high")
CONTEXT_REQUIREMENTS = ("small", "medium", "large")
EFFORT_LABELS = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "XHigh",
    "max": "Max",
}

# Complexity bands from the feature request. Models stay in this table so the
# rest of the app does not hardcode which slug serves which score.
_DEFAULT_TIER_ROWS: dict[str, tuple[tuple[int, int, str, str], ...]] = {
    "claude": (
        (1, 3, "claude-haiku-4-5", "low"),
        (4, 6, "claude-sonnet-5", "medium"),
        (7, 8, "claude-opus-5", "high"),
        (9, 10, "claude-opus-5", "max"),
    ),
    "codex": (
        (1, 3, "gpt-5.6-luna", "low"),
        (4, 6, "gpt-5.6-terra", "medium"),
        (7, 8, "gpt-5.6-sol", "high"),
        (9, 10, "gpt-6-astra", "xhigh"),
    ),
    "grok": (
        (1, 3, "grok-4.3", "low"),
        (4, 6, "grok-4.6", "medium"),
        (7, 8, "grok-4.6", "high"),
        (9, 10, "grok-4.6", "xhigh"),
    ),
}
_DEFAULT_ROUTER = {
    "claude": ("claude-haiku-4-5", "low"),
    "codex": ("gpt-5.6-luna", "low"),
    "grok": ("grok-4.3", "low"),
}

ROUTER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string"},
        "complexity": {"type": "integer", "minimum": 1, "maximum": 10},
        "risk": {"type": "string", "enum": list(RISK_LEVELS)},
        "context_requirement": {"type": "string", "enum": list(CONTEXT_REQUIREMENTS)},
        "selected_model": {"type": "string"},
        "reasoning_effort": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "prompt_grade": {"type": "string", "enum": list(PROMPT_GRADES)},
        "grade_reason": {"type": "string"},
    },
    "required": [
        "task_type",
        "complexity",
        "risk",
        "context_requirement",
        "selected_model",
        "reasoning_effort",
        "confidence",
        "prompt_grade",
        "grade_reason",
    ],
}


class RouterError(ValueError):
    """The router response cannot be applied. The caller falls back."""


@dataclasses.dataclass(frozen=True)
class RoutingTier:
    min_complexity: int
    max_complexity: int
    model: str
    effort: str

    def matches(self, complexity: int) -> bool:
        return self.min_complexity <= complexity <= self.max_complexity

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_complexity": self.min_complexity,
            "max_complexity": self.max_complexity,
            "model": self.model,
            "effort": self.effort,
        }


def default_router_model(provider: str) -> str:
    return _DEFAULT_ROUTER.get(provider, ("", "low"))[0]


def default_router_effort(provider: str) -> str:
    return _DEFAULT_ROUTER.get(provider, ("", "low"))[1]


def default_routing_tiers() -> dict[str, tuple[RoutingTier, ...]]:
    return {
        provider: tuple(
            RoutingTier(min_complexity, max_complexity, model, effort)
            for min_complexity, max_complexity, model, effort in rows
        )
        for provider, rows in _DEFAULT_TIER_ROWS.items()
    }


def load_routing_tiers(raw: str) -> dict[str, tuple[RoutingTier, ...]]:
    """Built-in tiers, with any JSON object replacing the listed providers.

    Missing providers keep the built-in table. An empty string means the
    built-in table alone.
    """
    tiers = default_routing_tiers()
    text = str(raw or "").strip()
    if not text:
        return tiers
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"routing tiers are not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("routing tiers must be a JSON object keyed by provider")
    for key, rows in parsed.items():
        provider = str(key).strip().lower()
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"{provider} routing tiers must be a non-empty list")
        tiers[provider] = tuple(_parse_tier(provider, item) for item in rows)
    return tiers


def _parse_tier(provider: str, item: Any) -> RoutingTier:
    if not isinstance(item, dict):
        raise ValueError(f"{provider} routing tier must be an object")
    try:
        tier = RoutingTier(
            min_complexity=int(item["min_complexity"]),
            max_complexity=int(item["max_complexity"]),
            model=str(item["model"]).strip(),
            effort=str(item["effort"]).strip(),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{provider} routing tier is incomplete") from error
    if tier.min_complexity > tier.max_complexity or not tier.model or not tier.effort:
        raise ValueError(f"{provider} routing tier has an invalid range or model")
    if tier.min_complexity < 1 or tier.max_complexity > 10:
        raise ValueError(f"{provider} routing tier must stay within complexity 1–10")
    return tier


def tier_for_complexity(tiers: tuple[RoutingTier, ...] | list[RoutingTier], complexity: int) -> RoutingTier:
    matches = [tier for tier in tiers if tier.matches(complexity)]
    if not matches:
        raise RouterError(f"no routing tier covers complexity {complexity}")
    chosen = matches[0]
    if not chosen.model or not chosen.effort:
        raise RouterError(f"routing tier for complexity {complexity} has no model")
    return chosen


def build_router_prompt(
    *,
    title: str,
    body: str,
    labels: list[str],
    provider: str,
    tiers: tuple[RoutingTier, ...] | list[RoutingTier],
) -> str:
    """Ask for a grade of the original issue. The issue text is quoted only."""
    tier_lines = [
        f"- complexity {tier.min_complexity}-{tier.max_complexity}: model {tier.model}, reasoning {tier.effort}"
        for tier in tiers
    ]
    quoted_body = body if body.strip() else "(empty)"
    return "\n".join(
        [
            "You are the SWARM dynamic model router.",
            "Grade the original GitHub issue below for an AI coding agent.",
            "Do not rewrite, expand, or replace the issue. Return one JSON object and nothing else.",
            "",
            "Score these and only these:",
            "1. task_type — a short label such as debugging, feature, refactor, test, docs, or review.",
            "2. complexity — integer 1 through 10.",
            "3. context_requirement — small, medium, or large.",
            "4. risk — low, medium, or high.",
            "5. selected_model and reasoning_effort — copy the tier below whose range contains your complexity.",
            "6. confidence — a number from 0 to 1.",
            "7. prompt_grade — exactly one of: " + ", ".join(PROMPT_GRADES) + ".",
            "8. grade_reason — one or two sentences on how well the issue communicates the work.",
            "",
            "Grade clarity, specificity, requirements, acceptance criteria, useful context, ambiguity,",
            "and whether an AI coding agent could execute the work without guessing.",
            "",
            f"Provider: {provider}",
            "Configured tiers:",
            *tier_lines,
            "",
            "Original issue title:",
            title,
            "",
            "Original issue description:",
            quoted_body,
            "",
            "Original issue tags:",
            ", ".join(labels) if labels else "none",
        ]
    ) + "\n"


def parse_router_payload(raw: str) -> dict[str, Any]:
    """Pull the JSON object out of a model response, including fenced output."""
    text = str(raw or "").strip()
    if not text:
        raise RouterError("router returned an empty response")
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fence.group(1) if fence else text
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise RouterError("router response did not contain a JSON object")
        try:
            payload = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as error:
            raise RouterError("router response was not valid JSON") from error
    if not isinstance(payload, dict):
        raise RouterError("router response JSON was not an object")
    return payload


def _confidence(value: Any) -> float:
    if isinstance(value, str):
        stripped = value.strip().rstrip("%")
        try:
            number = float(stripped)
        except ValueError as error:
            raise RouterError("router confidence was not a number") from error
        if value.strip().endswith("%"):
            number /= 100
    else:
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise RouterError("router confidence was not a number") from error
    if number > 1 and number <= 100:
        number /= 100
    if number < 0 or number > 1:
        raise RouterError("router confidence was outside 0–1")
    return number


def resolve_routing_decision(
    provider: str,
    payload: dict[str, Any] | str,
    tiers_by_provider: dict[str, tuple[RoutingTier, ...] | list[RoutingTier]],
    *,
    router_model: str,
    router_effort: str,
) -> dict[str, Any]:
    """Validate the router object and apply the configured complexity tier.

    The router's own model suggestion is recorded, but the worker model and
    effort are taken from the tier table so mappings stay configurable.
    """
    if isinstance(payload, str):
        parsed = parse_router_payload(payload)
    elif isinstance(payload, dict):
        parsed = payload
    else:
        raise RouterError("router payload was not a JSON object")
    try:
        complexity = int(parsed["complexity"])
    except (KeyError, TypeError, ValueError) as error:
        raise RouterError("router complexity was missing") from error
    if complexity < 1 or complexity > 10:
        raise RouterError("router complexity was outside 1–10")
    grade = str(parsed.get("prompt_grade") or "").strip()
    if grade not in PROMPT_GRADES:
        raise RouterError(f"router prompt grade {grade or '(empty)'} is not an allowed grade")
    risk = str(parsed.get("risk") or "").strip().lower()
    if risk not in RISK_LEVELS:
        raise RouterError("router risk was not low, medium, or high")
    context = str(parsed.get("context_requirement") or "").strip().lower()
    if context not in CONTEXT_REQUIREMENTS:
        raise RouterError("router context requirement was not small, medium, or large")
    task_type = str(parsed.get("task_type") or "").strip().lower()
    if not task_type or len(task_type) > 40:
        raise RouterError("router task type was missing")
    reason = str(parsed.get("grade_reason") or "").strip()
    if not reason:
        raise RouterError("router grade explanation was missing")
    tiers = tiers_by_provider.get(provider)
    if not tiers:
        raise RouterError(f"no routing tiers are configured for {provider}")
    tier = tier_for_complexity(tiers, complexity)
    return {
        "provider": provider,
        "task_type": task_type,
        "complexity": complexity,
        "risk": risk,
        "context_requirement": context,
        "selected_model": tier.model,
        "reasoning_effort": tier.effort,
        "router_suggested_model": str(parsed.get("selected_model") or "").strip(),
        "router_suggested_effort": str(parsed.get("reasoning_effort") or "").strip(),
        "confidence": _confidence(parsed.get("confidence")),
        "prompt_grade": grade,
        "grade_reason": reason[:500],
        "router_model": router_model,
        "router_effort": router_effort,
        "fallback": False,
    }


def fallback_routing_decision(
    *,
    provider: str,
    model: str,
    effort: str,
    reason: str,
    router_model: str,
    router_effort: str,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "task_type": "",
        "complexity": None,
        "risk": "",
        "context_requirement": "",
        "selected_model": model,
        "reasoning_effort": effort,
        "confidence": 0,
        "prompt_grade": "",
        "grade_reason": reason.strip()[:500],
        "router_model": router_model,
        "router_effort": router_effort,
        "fallback": True,
    }


def display_model_name(value: str) -> str:
    """Human label for a model slug. ``gpt-5.6-sol`` becomes ``GPT-5.6 Sol``."""
    parts = [part for part in re.split(r"[-_]", str(value or "").strip()) if part]
    if not parts:
        return str(value or "")
    rendered: list[str] = []
    for part in parts:
        lower = part.lower()
        if lower == "gpt":
            rendered.append("GPT")
        elif lower in {"claude", "grok"}:
            rendered.append(lower.capitalize())
        elif re.fullmatch(r"[0-9.]+", part):
            rendered.append(part)
        else:
            rendered.append(part[:1].upper() + part[1:])
    collapsed: list[str] = []
    for part in rendered:
        if collapsed and re.fullmatch(r"[0-9]+", collapsed[-1]) and re.fullmatch(r"[0-9]+", part):
            collapsed[-1] = f"{collapsed[-1]}.{part}"
        else:
            collapsed.append(part)
    rendered = collapsed
    if rendered[0] == "GPT" and len(rendered) > 1 and re.fullmatch(r"[0-9.]+", rendered[1]):
        tail = " ".join(rendered[2:])
        return f"{rendered[0]}-{rendered[1]}" + (f" {tail}" if tail else "")
    return " ".join(rendered)


def display_effort(value: str) -> str:
    key = str(value or "").strip().lower()
    if key in EFFORT_LABELS:
        return EFFORT_LABELS[key]
    return key[:1].upper() + key[1:] if key else str(value or "")


def format_routing_notice(decision: dict[str, Any]) -> str:
    """Issue-comment block shown when SWARM takes ownership."""
    model = display_model_name(str(decision.get("selected_model") or ""))
    effort = display_effort(str(decision.get("reasoning_effort") or ""))
    if decision.get("fallback"):
        lines = [
            "SWARM AI Routing",
            "Routing fell back to the configured worker model and reasoning effort.",
            f"Selected Model: {model}",
            f"Reasoning: {effort}",
        ]
        reason = str(decision.get("grade_reason") or "").strip()
        if reason:
            lines.extend(["", reason])
        return "\n".join(lines)
    confidence = float(decision.get("confidence") or 0)
    percent = int(round(confidence * 100))
    lines = [
        "SWARM AI Routing",
        f"Prompt Grade: {decision.get('prompt_grade')}",
        f"Complexity: {decision.get('complexity')}/10",
        f"Selected Model: {model}",
        f"Reasoning: {effort}",
        f"Routing Confidence: {percent}%",
        "",
        str(decision.get("grade_reason") or "").strip(),
    ]
    return "\n".join(lines)


def _command_available(path: str) -> bool:
    if not path:
        return False
    candidate = Path(path)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return True
    return shutil.which(path) is not None


def extract_router_text(provider: str, stdout: str, last_message: str = "") -> str:
    """Normalize Claude, Codex, and Grok one-shot output to the model's text."""
    if provider == "codex" and last_message.strip():
        return last_message.strip()
    raw = stdout.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(payload, dict):
        if "prompt_grade" in payload or "task_type" in payload:
            return json.dumps(payload)
        for key in ("result", "text", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                return json.dumps(value)
    return raw


def run_provider_router(
    *,
    provider: str,
    bin_path: str,
    model: str,
    effort: str,
    prompt: str,
    cwd: Path,
    timeout: float = 180,
) -> str:
    """One-shot router call that does not start or resume the worker session.

    Claude runs with no tools and no session persistence. Codex is ephemeral
    and sandboxed read-only. Grok is limited to one turn and cannot ask to
    edit the repository. A failure raises ``RouterError`` so the worker can
    keep the manually configured model.
    """
    if not _command_available(bin_path):
        raise RouterError(f"{provider} executable is unavailable")
    if not model.strip():
        raise RouterError(f"{provider} router model is empty")
    schema = json.dumps(ROUTER_RESPONSE_SCHEMA, separators=(",", ":"))
    with tempfile.TemporaryDirectory(prefix="swarm-router-") as temporary:
        temp = Path(temporary)
        schema_path = temp / "router-schema.json"
        schema_path.write_text(schema, encoding="utf-8")
        last_message = temp / "router-last.txt"
        prompt_path = temp / "router-prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        if provider == "claude":
            command = [
                bin_path,
                "--model",
                model,
                "--effort",
                effort or "low",
                "-p",
                "--output-format",
                "json",
                "--json-schema",
                schema,
                "--tools",
                "",
                "--no-session-persistence",
            ]
            completed = _run(command, cwd=cwd, timeout=timeout, stdin=prompt)
            return extract_router_text(provider, completed)
        if provider == "codex":
            command = [
                bin_path,
                "exec",
                "-m",
                model,
                "-c",
                f'model_reasoning_effort="{effort or "low"}"',
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--json",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(last_message),
                "-C",
                str(cwd),
                "-",
            ]
            completed = _run(command, cwd=cwd, timeout=timeout, stdin=prompt)
            message = last_message.read_text(encoding="utf-8") if last_message.exists() else ""
            return extract_router_text(provider, completed, message)
        if provider == "grok":
            command = [
                bin_path,
                "--prompt-file",
                str(prompt_path),
                "--model",
                model,
                "--reasoning-effort",
                effort or "low",
                "--output-format",
                "json",
                "--json-schema",
                schema,
                "--max-turns",
                "1",
                "--permission-mode",
                "dontAsk",
                "--disable-web-search",
                "--no-subagents",
                "--cwd",
                str(cwd),
            ]
            completed = _run(command, cwd=cwd, timeout=timeout, stdin=None)
            return extract_router_text(provider, completed)
    raise RouterError(f"no router runner for provider {provider}")


def _run(command: list[str], *, cwd: Path, timeout: float, stdin: str | None) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RouterError("router timed out") from error
    except OSError as error:
        raise RouterError(f"router could not be started: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        suffix = f": {detail[-1][:240]}" if detail else ""
        raise RouterError(f"router exited with status {completed.returncode}{suffix}")
    return completed.stdout or ""
