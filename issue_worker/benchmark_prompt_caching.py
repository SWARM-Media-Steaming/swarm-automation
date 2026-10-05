#!/usr/bin/env python3
"""Bounded native-CLI benchmark, in disposable repos, with correctness checks.

Opt-in developer command, not an application setting. Uses existing CLI login.
Only numeric telemetry and outcomes are emitted; no transcripts/auth are saved.
Run --provider claude|codex --model <installed-model>. Each mode makes three
calls: implementation, a break/fix requirement, and an independent review.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from token_usage import normalize_usage

SPEC = """Implement clamp(value, low, high) in clamp.py. Return low when value is below
low, high when value is above high, otherwise value. Preserve the tests. Run
python3 -m unittest discover in the foreground. Do not commit, access network,
inspect other directories or ask questions. Limit work to this disposable repo.
"""
BASE_TEST = '''import unittest
from clamp import clamp
class ClampTests(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(clamp(-2, 0, 5), 0)
        self.assertEqual(clamp(7, 0, 5), 5)
        self.assertEqual(clamp(3, 0, 5), 3)
'''
FIX_TEST = '''    def test_invalid_bounds(self):
        with self.assertRaises(ValueError):
            clamp(3, 5, 0)
'''


def invoke(provider, model, root, prompt, session=""):
    binary = shutil.which(provider)
    if not binary:
        raise RuntimeError(f"{provider} CLI unavailable")
    if provider == "claude":
        command = [binary, "--model", model, "--permission-mode", "bypassPermissions",
                   "--resume" if session else "--session-id", session or str(uuid.uuid4()),
                   "-p", "-", "--output-format", "stream-json", "--verbose"]
    else:
        command = [binary, "exec", *(["resume"] if session else []), "-m", model,
                   "--dangerously-bypass-approvals-and-sandbox", "--json"]
        command.extend([session, "-"] if session else ["-"])
    start = time.monotonic()
    result = subprocess.run(command, cwd=root, input=prompt, capture_output=True, text=True,
                            timeout=180, env=os.environ.copy())
    latency = round((time.monotonic() - start) * 1000)
    usage = normalize_usage(provider, result.stdout)
    found_session = ""
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            found_session = event.get("session_id") or event.get("thread_id") or found_session
    error_type = ""
    if result.returncode:
        diagnostic = result.stdout + result.stderr
        error_type = next((name for name, pattern in (
            ("authentication_unavailable", r"(?i)not logged in|authentication|unauthorized|login|credential"),
            ("model_unavailable", r"(?i)unknown model|model.*not (?:found|exist|available|supported)|model_not_found"),
            ("capacity_unavailable", r"(?i)rate limit|quota|usage limit|credit"),
        ) if re.search(pattern, diagnostic)), "provider_failed")
    metrics = {key: getattr(usage, key, None) for key in (
        "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reported_cost")}
    return found_session, {**metrics, "error_type": error_type, "duration_ms": latency, "exit_code": result.returncode,
                           "session_reused": bool(session), "prompt_bytes": len(prompt.encode())}


def benchmark(provider, model):
    observations = []
    for reuse in (False, True):
        with tempfile.TemporaryDirectory(prefix="chomp-cache-benchmark-") as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "clamp.py").write_text("def clamp(value, low, high):\n    return min(value, high)\n")
            (root / "test_clamp.py").write_text(BASE_TEST)
            # Stable repository guidance is substantial enough to exercise native
            # prefixes, while remaining identical in the two comparison arms.
            guidance = "\n".join(f"Component {n}: pure functions; preserve inputs; test boundary values."
                                 for n in range(100))
            (root / "AGENTS.md").write_text(guidance)
            (root / "CLAUDE.md").write_text(guidance)
            session = ""
            for stage in ("implementation", "fix", "review"):
                if stage == "implementation":
                    prompt = SPEC
                elif stage == "fix":
                    (root / "test_clamp.py").write_text(BASE_TEST + FIX_TEST)
                    prompt = ("Continue this issue. " if reuse else SPEC) + (
                        "New requirement: when low > high raise ValueError. The new test fails. "
                        "Read the current files, fix clamp.py and run the tests. Preserve test_clamp.py.")
                else:
                    prompt = ("Independently review clamp.py against this spec: " + SPEC +
                              "Also low > high must raise ValueError. Read-only: do not edit files. "
                              "Do not inspect any prior agent transcripts or summaries. Run the tests.")
                before = (root / "clamp.py").read_bytes()
                use_session = session if reuse and stage == "fix" else ""
                session, metrics = invoke(provider, model, root, prompt, use_session)
                checked = subprocess.run(["python3", "-m", "unittest", "discover"], cwd=root,
                                         capture_output=True, timeout=30)
                correct = checked.returncode == 0 and (root / "test_clamp.py").read_text() == (
                    BASE_TEST if stage == "implementation" else BASE_TEST + FIX_TEST)
                if stage == "review":
                    correct = correct and (root / "clamp.py").read_bytes() == before and not use_session
                row = {"provider": provider, "model": model, "mode": "reuse" if reuse else "fresh",
                       "stage": stage, "correct": correct, **metrics}
                observations.append(row)
                print(json.dumps(row), flush=True)
                if metrics["exit_code"] or not correct:
                    return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=("claude", "codex"))
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    try:
        return 0 if benchmark(args.provider, args.model) else 1
    except (RuntimeError, subprocess.TimeoutExpired):
        print(json.dumps({"provider": args.provider, "status": "unavailable_or_timeout"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
