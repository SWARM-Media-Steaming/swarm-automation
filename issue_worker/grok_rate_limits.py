#!/usr/bin/env python3
"""Read the Grok account's credit allowance through the local Grok agent.

Grok Build has real usage limits: a signed-in grok.com account gets a weekly
(or monthly) credit allowance, shown in the CLI's ``/usage`` "Usage limit" tab.
That data comes from the agent's ``_x.ai/billing`` extension method, which
``grok -p /usage`` does not reach (it would spend a model turn), so this talks
to ``grok agent stdio`` directly. No model turn is made.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import tempfile
import time
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grok-bin", required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser.parse_args()


class LineReader:
    """Reads newline-delimited messages from a child's stdout with a deadline.

    `select()` only reports what is still in the OS pipe, so it must not be
    mixed with a buffered `readline()`: when the agent writes several lines at
    once (a notification followed by the reply), the buffered reader swallows
    them all, `select()` then sees nothing, and the wait times out. This keeps
    its own buffer over `os.read` instead."""

    def __init__(self, stream: Any) -> None:
        self.fd = stream.fileno()
        self.buffer = b""

    def readline(self, deadline: float) -> bytes:
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for the Grok agent")
            readable, _, _ = select.select([self.fd], [], [], remaining)
            if not readable:
                raise TimeoutError("timed out waiting for the Grok agent")
            chunk = os.read(self.fd, 65536)
            if not chunk:
                raise RuntimeError("the Grok agent exited before replying")
            self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b"\n")
        return line


def send(process: subprocess.Popen[bytes], message: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
    process.stdin.flush()


def receive_response(reader: LineReader, request_id: int, deadline: float) -> dict[str, Any]:
    while True:
        try:
            message = json.loads(reader.readline(deadline))
        except ValueError:
            continue  # stray non-protocol output
        # Notifications (no id) such as `_x.ai/mcp/servers_updated` can arrive
        # in between; only the reply to our request matters.
        if (
            isinstance(message, dict)
            and message.get("id") == request_id
            and ("result" in message or "error" in message)
        ):
            return message


def period_label(period_type: object) -> str:
    text = str(period_type or "").upper()
    for name in ("WEEKLY", "MONTHLY", "DAILY"):
        if name in text:
            return {"WEEKLY": "week", "MONTHLY": "month", "DAILY": "day"}[name]
    return "period"


def normalize(result: dict[str, Any]) -> dict[str, Any]:
    """Reduce the billing response to what the worker needs."""
    config = result.get("config")
    if not isinstance(config, dict):
        raise RuntimeError("Grok returned no billing config")
    used = config.get("creditUsagePercent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        raise RuntimeError("Grok's billing response had no credit usage percentage")
    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    return {
        "usedPercent": max(0.0, min(100.0, float(used))),
        "period": period_label(period.get("type")),
        "resetsAt": period.get("end"),
        "tier": result.get("subscription_tier"),
    }


def main() -> int:
    args = parse_args()
    process: subprocess.Popen[bytes] | None = None
    deadline = time.monotonic() + args.timeout

    try:
        process = subprocess.Popen(
            [args.grok_bin, "agent", "--no-leader", "stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            # A neutral directory, so no project's own Grok config or hooks load.
            cwd=tempfile.gettempdir(),
        )
        assert process.stdout is not None
        reader = LineReader(process.stdout)
        send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "swarm_issue_worker", "version": "1.0.0"},
                },
            },
        )
        initialized = receive_response(reader, 1, deadline)
        if "error" in initialized:
            raise RuntimeError(f"Grok initialization failed: {initialized['error']}")

        send(process, {"jsonrpc": "2.0", "id": 2, "method": "_x.ai/billing", "params": {}})
        response = receive_response(reader, 2, deadline)
        if "error" in response:
            raise RuntimeError(f"Grok billing request failed: {response['error']}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Grok returned no billing result")

        print(json.dumps(normalize(result), separators=(",", ":"), sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(f"Could not read Grok usage: {error}", file=sys.stderr)
        return 1
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
