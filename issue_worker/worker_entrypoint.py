#!/usr/bin/env python3
"""Container/CLI entrypoint for the issue worker.

The worker image copies ``issue_worker/`` as-is and runs this file. It needs
only the standard library and its sibling modules (a test enforces that), so
nothing from the desktop app (``src/``, ``ui/``, Tauri) is ever imported. It is
equivalent to running ``swarm_issue_worker.py`` and keeps its exit codes
(including 13, the strict-epoch yield, and 14, the automation hold; 1 is an
error, 130 an interrupt).
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = str(Path(__file__).resolve().parent)
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def run(argv: list[str] | None = None) -> int:
    import json

    import swarm_issue_worker as worker

    try:
        return int(worker.main(argv) or 0)
    except KeyboardInterrupt:
        return 130
    except (worker.WorkerError, OSError, ValueError, json.JSONDecodeError) as error:
        worker.log(f"ERROR: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
