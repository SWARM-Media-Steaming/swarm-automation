"""CLI adapter for TypeSafe's Jev decision model.

Jev is a typed-judgment CLI, not an implementation agent. Swarm discovers the
``jev`` executable the same way it discovers Claude, Codex, and Grok: PATH
lookup, an optional configured path, timeout, retry, health check, structured
JSON in/out, and sanitized errors. There is no direct HTTP client.

Invocation (first implementation):

    <bin> ask --json --model <model> --timeout <ms> <request.json>

The request file is JSON with a structured ``state`` object (never raw
credentials) and typed ``questions``. Stdout must be JSON. Several response
shapes are accepted and normalized. Secrets in stderr/stdout are redacted
before they are logged or persisted.

When the executable is missing, times out, returns malformed JSON, or fails
authentication, this adapter raises :class:`JevError` so the decision engine
can fall back. Callers must not treat a raised error as a Jev recommendation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ai_execution_history import sanitize_text


DEFAULT_JEV_MODEL = "jev-latest"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_MAX_RETRIES = 2
# TypeSafe's published input price: $0.042 per million input tokens, output free.
JEV_INPUT_COST_PER_MILLION = 0.042
JEV_OUTPUT_COST_PER_MILLION = 0.0

# Environment variables that may hold a TypeSafe/Jev key. Never logged.
_JEV_KEY_ENV = ("JEV_API_KEY", "TYPESAFE_API_KEY", "TYPESAFE_KEY")


class JevError(RuntimeError):
    """The Jev CLI could not produce a usable typed decision."""

    def __init__(self, message: str, *, error_type: str = "jev_error") -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class JevUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost: float | None = None
    currency: str = "USD"
    model: str = ""
    version: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost": self.estimated_cost,
            "currency": self.currency,
            "model": self.model,
            "version": self.version,
        }


@dataclass
class JevResponse:
    """Normalized CLI output. ``answers`` is question-id → payload."""

    answers: dict[str, Any]
    usage: JevUsage = field(default_factory=JevUsage)
    raw_shape: str = "unknown"
    latency_ms: float = 0.0
    model: str = DEFAULT_JEV_MODEL
    version: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "answers": self.answers,
            "usage": self.usage.as_dict(),
            "raw_shape": self.raw_shape,
            "latency_ms": round(self.latency_ms, 3),
            "model": self.model,
            "version": self.version,
        }


@dataclass(frozen=True)
class JevSettings:
    """Operator-facing Jev configuration. Defaults keep Jev off."""

    enabled: bool = False
    bin: str = ""
    model: str = DEFAULT_JEV_MODEL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    confidence_automation: float = 0.90
    confidence_fallback: float = 0.70
    confidence_security: float = 0.95
    fallback: str = "rules"
    use_preflight: bool = True
    use_workflow: bool = True
    use_uat: bool = True
    use_cyber: bool = True
    use_rag: bool = True
    use_triage: bool = True
    use_completion: bool = True

    def use_category(self, category: str) -> bool:
        mapping = {
            "preflight": self.use_preflight,
            "pre_flight": self.use_preflight,
            "task_classification": self.use_preflight,
            "workflow": self.use_workflow,
            "uat": self.use_uat,
            "uat_finding": self.use_uat,
            "cyber": self.use_cyber,
            "cyber_finding": self.use_cyber,
            "rag": self.use_rag,
            "rag_scope": self.use_rag,
            "context_relevance": self.use_rag,
            "triage": self.use_triage,
            "issue_triage": self.use_triage,
            "completion": self.use_completion,
        }
        return bool(mapping.get(str(category or "").strip().lower(), True))


def clamp_confidence_threshold(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number > 1 and number <= 100:
        number /= 100
    if number < 0 or number > 1:
        return default
    return number


def settings_from_mapping(raw: Mapping[str, Any] | None) -> JevSettings:
    data = dict(raw or {})
    timeout = data.get("timeout_seconds", data.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS))
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        timeout_value = DEFAULT_TIMEOUT_SECONDS
    timeout_value = min(60.0, max(1.0, timeout_value))
    try:
        retries = int(data.get("max_retries", data.get("maxRetries", DEFAULT_MAX_RETRIES)))
    except (TypeError, ValueError):
        retries = DEFAULT_MAX_RETRIES
    retries = min(5, max(0, retries))
    fallback = str(data.get("fallback") or "rules").strip().lower()
    if fallback not in {"rules", "llm", "rules_then_llm"}:
        fallback = "rules"
    model = str(data.get("model") or DEFAULT_JEV_MODEL).strip() or DEFAULT_JEV_MODEL
    return JevSettings(
        enabled=bool(data.get("enabled")),
        bin=str(data.get("bin") or "").strip(),
        model=model,
        timeout_seconds=timeout_value,
        max_retries=retries,
        confidence_automation=clamp_confidence_threshold(
            data.get("confidence_automation", data.get("confidenceAutomation")), 0.90
        ),
        confidence_fallback=clamp_confidence_threshold(
            data.get("confidence_fallback", data.get("confidenceFallback")), 0.70
        ),
        confidence_security=clamp_confidence_threshold(
            data.get("confidence_security", data.get("confidenceSecurity")), 0.95
        ),
        fallback=fallback,
        use_preflight=_flag(data, "use_preflight", "usePreflight", True),
        use_workflow=_flag(data, "use_workflow", "useWorkflow", True),
        use_uat=_flag(data, "use_uat", "useUat", True),
        use_cyber=_flag(data, "use_cyber", "useCyber", True),
        use_rag=_flag(data, "use_rag", "useRag", True),
        use_triage=_flag(data, "use_triage", "useTriage", True),
        use_completion=_flag(data, "use_completion", "useCompletion", True),
    )


def _flag(data: Mapping[str, Any], snake: str, camel: str, default: bool) -> bool:
    if snake in data:
        return bool(data[snake])
    if camel in data:
        return bool(data[camel])
    return default


def discover_jev_bin(configured: str = "") -> str:
    """Resolve the Jev executable: configured path, else PATH lookup."""
    configured = str(configured or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        which = shutil.which(configured)
        if which:
            return which
    found = shutil.which("jev")
    return found or ""


def jev_auth_present(bin_path: str = "") -> bool:
    """Whether a Jev/TypeSafe credential is available without reading its value.

    ``jev auth status`` is authoritative when the executable is known: the CLI
    keeps credentials in profile files whose location Swarm must not guess.
    The environment/file checks are only the fallback when it cannot answer.
    """
    for name in _JEV_KEY_ENV:
        if str(os.environ.get(name) or "").strip():
            return True
    if bin_path:
        try:
            completed = subprocess.run(
                [bin_path, "auth", "status", "--output", "json", "--no-input"],
                capture_output=True, text=True, timeout=4, check=False,
            )
            if completed.returncode == 0:
                status = json.loads(completed.stdout or "{}")
                if isinstance(status, dict) and "authenticated" in status:
                    return bool(status["authenticated"])
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    home = Path.home()
    for path in (
        home / ".config" / "jev" / "config.json",
        home / ".jev" / "auth.json",
        home / ".typesafe" / "credentials",
    ):
        if path.is_file():
            return True
    return False


def context_fingerprint(payload: Any) -> str:
    """Stable hash of structured context. Never includes raw prompts or secrets."""
    text = json.dumps(_fingerprint_value(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _fingerprint_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _fingerprint_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key).lower() not in {"prompt", "body", "raw", "stdout", "stderr", "api_key", "token"}
        }
    if isinstance(value, (list, tuple)):
        return [_fingerprint_value(item) for item in value[:32]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = sanitize_text(value)
    if len(text) > 120:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return text


def estimate_jev_cost(input_tokens: int | None, output_tokens: int | None = None) -> float | None:
    """Dollar estimate from TypeSafe's published Jev pricing. Label as an estimate."""
    if input_tokens is None:
        return None
    output = int(output_tokens or 0)
    return round(
        (max(0, int(input_tokens)) / 1_000_000) * JEV_INPUT_COST_PER_MILLION
        + (max(0, output) / 1_000_000) * JEV_OUTPUT_COST_PER_MILLION,
        8,
    )


def redact_cli_text(text: str) -> str:
    """Strip credentials and collapse CLI chatter for logs and persistence."""
    return sanitize_text(text or "").strip()[:500]


Runner = Callable[[list, float, Optional[str]], subprocess.CompletedProcess]


def _default_runner(
    command: list[str], timeout: float, stdin: str | None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=_sanitized_env(),
    )


def _sanitized_env() -> dict[str, str]:
    """Pass the process environment through; callers must not log it."""
    return dict(os.environ)


class JevCli:
    """Discover, health-check, and invoke the Jev CLI."""

    def __init__(
        self,
        settings: JevSettings | None = None,
        *,
        runner: Runner | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.settings = settings or JevSettings()
        self.runner = runner or _default_runner
        self.sleeper = sleeper or time.sleep
        self.bin_path = discover_jev_bin(self.settings.bin)

    def health(self) -> dict[str, Any]:
        """Connectivity/health without exposing secrets."""
        installed = bool(self.bin_path)
        authenticated = jev_auth_present(self.bin_path)
        version = ""
        reachable = False
        error = ""
        if installed:
            try:
                completed = self.runner([self.bin_path, "--version"], min(4.0, self.settings.timeout_seconds), None)
                version = redact_cli_text((completed.stdout or completed.stderr or "").splitlines()[0] if (completed.stdout or completed.stderr) else "")
                reachable = completed.returncode == 0
                if reachable:
                    # A CLI without `eval` (older/newer incompatible builds)
                    # answers --version but fails every decision, which used
                    # to show as "Connected".
                    probe = self.runner([self.bin_path, "eval", "--help"], min(4.0, self.settings.timeout_seconds), None)
                    if probe.returncode != 0:
                        reachable = False
                        error = "unsupported_cli"
            except subprocess.TimeoutExpired:
                error = "timeout"
            except OSError as exc:
                error = "start_failed"
                version = redact_cli_text(str(exc))
        status = "disabled"
        if self.settings.enabled:
            if not installed:
                status = "not_installed"
            elif error == "timeout":
                status = "timeout"
            elif not reachable and error:
                status = "unavailable"
            elif authenticated:
                status = "ready"
            else:
                status = "sign_in_required"
        return {
            "enabled": self.settings.enabled,
            "installed": installed,
            "authenticated": authenticated,
            "reachable": reachable,
            "status": status,
            "bin": self.bin_path,
            "model": self.settings.model,
            "version": version,
            "error_type": error,
        }

    def ask(
        self,
        *,
        state: Mapping[str, Any],
        questions: Mapping[str, Any],
        model: str = "",
    ) -> JevResponse:
        """Run one typed-question request. Raises :class:`JevError` on failure."""
        if not self.settings.enabled:
            raise JevError("Jev is disabled", error_type="disabled")
        if not self.bin_path:
            raise JevError("Jev executable is unavailable", error_type="not_installed")
        request = {
            "model": str(model or self.settings.model or DEFAULT_JEV_MODEL),
            "state": dict(state),
            "questions": dict(questions),
        }
        attempts = self.settings.max_retries + 1
        last_error: JevError | None = None
        started = time.perf_counter()
        for attempt in range(attempts):
            try:
                response = self._invoke_once(request)
                response.latency_ms = (time.perf_counter() - started) * 1000
                return response
            except JevError as error:
                last_error = error
                retryable = error.error_type in {"timeout", "jev_exit", "start_failed"}
                if not retryable or attempt + 1 >= attempts:
                    raise
                self.sleeper(min(1.5, 0.2 * (attempt + 1)))
        assert last_error is not None
        raise last_error

    def _invoke_once(self, request: dict[str, Any]) -> JevResponse:
        timeout = float(self.settings.timeout_seconds)
        timeout_ms = max(1, int(timeout * 1000))
        with tempfile.TemporaryDirectory(prefix="swarm-jev-") as temporary:
            request_path = Path(temporary) / "request.json"
            request_path.write_text(
                json.dumps(request, ensure_ascii=True, separators=(",", ":")),
                encoding="utf-8",
            )
            command = [
                self.bin_path,
                "eval",
                "--file",
                str(request_path),
                "--model",
                str(request.get("model") or self.settings.model),
                "--timeout",
                f"{timeout_ms}ms",
                # Swarm owns retry/backoff (see ask()); the CLI must not
                # silently multiply attempts on top of it.
                "--max-retries",
                "0",
                "--output",
                "json",
                "--no-input",
            ]
            try:
                completed = self.runner(command, timeout, None)
            except subprocess.TimeoutExpired as error:
                raise JevError("Jev timed out", error_type="timeout") from error
            except OSError as error:
                raise JevError(
                    f"Jev could not be started: {redact_cli_text(str(error))}",
                    error_type="start_failed",
                ) from error
            if completed.returncode != 0:
                detail = redact_cli_text(completed.stderr or completed.stdout or "")
                error_type = "authentication" if _looks_like_auth_failure(detail, completed.returncode) else "jev_exit"
                suffix = f": {detail}" if detail else ""
                raise JevError(
                    f"Jev exited with status {completed.returncode}{suffix}",
                    error_type=error_type,
                )
            return parse_jev_stdout(completed.stdout or "", model=str(request.get("model") or ""))


def _looks_like_auth_failure(detail: str, returncode: int) -> bool:
    text = detail.lower()
    if returncode in {401, 403}:
        return True
    # "API-KEY" / "API_KEY" / "apikey" are the same failure as "api key".
    spaced = re.sub(r"[-_]+", " ", text)
    return any(
        token in spaced
        for token in ("unauthorized", "api key", "apikey", "authentication", "not logged in", "sign in")
    )


def parse_jev_stdout(stdout: str, *, model: str = "") -> JevResponse:
    """Accept several CLI JSON shapes and normalize them.

    Never returns a result for non-JSON or an empty object: those are
    malformed and must fall back.
    """
    raw = (stdout or "").strip()
    if not raw:
        raise JevError("Jev returned no output", error_type="malformed")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # Some CLIs wrap JSON in a result envelope or print a trailer.
        payload = _extract_json_object(raw)
        if payload is None:
            raise JevError("Jev response was not valid JSON", error_type="malformed") from None
    if not isinstance(payload, dict) or not payload:
        raise JevError("Jev response JSON was not an object", error_type="malformed")
    usage = _parse_usage(payload, default_model=model)
    if "answers" in payload and isinstance(payload["answers"], dict):
        answers = payload["answers"]
        if not answers:
            raise JevError("Jev returned no answers", error_type="malformed")
        return JevResponse(
            answers=answers,
            usage=usage,
            raw_shape="answers",
            model=usage.model or model or DEFAULT_JEV_MODEL,
            version=usage.version,
        )
    if any(key in payload for key in ("decision", "decisionType", "decision_type")):
        return JevResponse(
            answers={"decision": payload},
            usage=usage,
            raw_shape="decision",
            model=usage.model or model or DEFAULT_JEV_MODEL,
            version=usage.version,
        )
    # A flat map of question id → value/object.
    if all(isinstance(key, str) for key in payload) and "error" not in payload:
        skip = {"usage", "model", "version", "warnings", "provider", "latency_ms", "id"}
        answers = {key: value for key, value in payload.items() if key not in skip}
        if answers:
            return JevResponse(
                answers=answers,
                usage=usage,
                raw_shape="flat",
                model=usage.model or model or DEFAULT_JEV_MODEL,
                version=usage.version,
            )
    raise JevError("Jev response was missing typed answers", error_type="malformed")


def _extract_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _parse_usage(payload: Mapping[str, Any], *, default_model: str) -> JevUsage:
    blob = payload.get("usage") if isinstance(payload.get("usage"), dict) else payload
    model = str(payload.get("model") or blob.get("model") or default_model or "")
    version = str(payload.get("version") or blob.get("version") or payload.get("model_version") or "")
    input_tokens = _optional_int(blob.get("input_tokens", blob.get("inputTokens", blob.get("prompt_tokens"))))
    output_tokens = _optional_int(blob.get("output_tokens", blob.get("outputTokens", blob.get("completion_tokens"))))
    cost = blob.get("estimated_cost", blob.get("estimatedCost", blob.get("cost")))
    try:
        estimated = float(cost) if cost is not None else estimate_jev_cost(input_tokens, output_tokens)
    except (TypeError, ValueError):
        estimated = estimate_jev_cost(input_tokens, output_tokens)
    return JevUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost=estimated,
        model=model,
        version=version,
    )


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def answer_choice(answer: Any) -> str:
    if isinstance(answer, Mapping):
        for key in ("value", "choice", "decision", "label", "id"):
            if answer.get(key) not in (None, ""):
                return str(answer[key]).strip()
    if answer is None:
        return ""
    return str(answer).strip()


def answer_confidence(answer: Any, default: float = 0.0) -> float:
    if isinstance(answer, Mapping):
        for key in ("confidence", "p", "probability", "score"):
            if key in answer:
                return clamp_confidence_threshold(answer[key], default)
        distribution = answer.get("distribution")
        if isinstance(distribution, Mapping) and distribution:
            try:
                return clamp_confidence_threshold(max(float(v) for v in distribution.values()), default)
            except (TypeError, ValueError):
                return default
    if isinstance(answer, (int, float)):
        return clamp_confidence_threshold(answer, default)
    return default


def answer_score(answer: Any) -> float | None:
    if isinstance(answer, Mapping):
        for key in ("value", "score", "position", "p_yes", "yes"):
            if key in answer and answer[key] is not None:
                return clamp_confidence_threshold(answer[key], 0.0)
        return None
    if isinstance(answer, (int, float)):
        return clamp_confidence_threshold(answer, 0.0)
    return None


def answer_distribution(answer: Any) -> dict[str, float]:
    if not isinstance(answer, Mapping):
        return {}
    blob = answer.get("distribution") or answer.get("scores") or {}
    if not isinstance(blob, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, value in blob.items():
        try:
            out[str(key)] = clamp_confidence_threshold(value, 0.0)
        except (TypeError, ValueError):
            continue
    return out
