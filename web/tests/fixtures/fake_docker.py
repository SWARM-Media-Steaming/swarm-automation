#!/usr/bin/env python3
"""Stand-in for the docker CLI used by the job-runner contract tests.

State directory: ``SWARM_FAKE_DOCKER_STATE``. Secret values from ``--env-file``
are not written into that state. ``exit_queue`` (a list of integers in the
state file) makes each new container already exited with that code; an empty
queue leaves it running.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    root = Path(os.environ["SWARM_FAKE_DOCKER_STATE"])
    root.mkdir(parents=True, exist_ok=True)
    path = root / "state.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {
        "n": 0,
        "containers": {},
        "argv": [],
        "exit_queue": [],
    }
    args = sys.argv[1:]
    state.setdefault("argv", []).append(args)
    state.setdefault("containers", {})
    state.setdefault("exit_queue", [])
    state.setdefault("n", 0)

    def save() -> None:
        path.write_text(json.dumps(state), encoding="utf-8")

    if not args:
        save()
        print("no command", file=sys.stderr)
        return 1
    command, rest = args[0], args[1:]

    def flag(name: str) -> str | None:
        if name not in rest:
            return None
        index = rest.index(name)
        if index + 1 >= len(rest):
            return None
        return rest[index + 1]

    if command == "run":
        name = flag("--name")
        env_file = flag("--env-file")
        if not name or not env_file:
            save()
            print("run needs --name and --env-file", file=sys.stderr)
            return 1
        names: list[str] = []
        gh_token = False
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            names.append(key)
            if key == "GH_TOKEN" and value:
                gh_token = True
        state["n"] = int(state["n"]) + 1
        cid = f"cid{state['n']}"
        queue = state["exit_queue"]
        exit_code = queue.pop(0) if queue else None
        record = {
            "name": name,
            "status": "exited" if exit_code is not None else "running",
            "exit": 0 if exit_code is None else int(exit_code),
            "env_names": names,
            "gh_token": gh_token,
        }
        state["containers"][cid] = record
        state["containers"][name] = record
        save()
        print(cid)
        return 0

    if command == "inspect":
        cid = rest[-1] if rest else ""
        record = state["containers"].get(cid)
        save()
        if not record:
            print("no such container", file=sys.stderr)
            return 1
        print(json.dumps([{"State": {"Status": record["status"], "ExitCode": record["exit"]}}]))
        return 0

    if command in {"pause", "unpause", "stop", "rm"}:
        cid = rest[-1]
        record = state["containers"].get(cid)
        if record is None:
            save()
            print("no such container", file=sys.stderr)
            return 1
        if command == "pause":
            record["status"] = "paused"
        elif command == "unpause":
            record["status"] = "running"
        elif command == "stop":
            record["status"] = "exited"
            record["exit"] = 143
        else:
            state["containers"].pop(cid, None)
            state["containers"] = {
                key: value for key, value in state["containers"].items() if value is not record
            }
        save()
        print(cid)
        return 0

    if command == "logs":
        save()
        print("worker: started")
        if "-f" in rest:
            time.sleep(30)
        return 0

    save()
    print(f"unknown {command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as error:
        print(f"fake docker: {error}", file=sys.stderr)
        raise SystemExit(1)
